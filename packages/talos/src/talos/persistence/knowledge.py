from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from talos.knowledge import KnowledgeChunk, KnowledgeError, KnowledgeSource
from talos.persistence.database import Database
from talos.persistence.models import JobRow, KnowledgeChunkRow, KnowledgeSourceRow


class SqlKnowledgeStore:
    """PostgreSQL-backed active index and durable ingestion job store."""

    def __init__(
        self, database: Database, *, max_attempts: int, retry_base_seconds: int = 2
    ) -> None:
        self._database = database
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds

    async def enqueue(
        self, name: str, content: str, key: str, *, conversation_id: str | None = None
    ) -> str:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            existing = await session.scalar(select(JobRow).where(JobRow.idempotency_key == key))
            if existing is not None:
                prior = await session.get(KnowledgeSourceRow, existing.input_reference)
                checksum = hashlib.sha256(content.encode()).hexdigest()
                if (
                    existing.job_type != "knowledge_ingest"
                    or prior is None
                    or prior.name != name
                    or prior.checksum != checksum
                    or prior.conversation_id != conversation_id
                ):
                    raise KnowledgeError("Idempotency key was used for different input")
                return existing.id
            job_id = str(uuid4())
            source_id = str(uuid4())
            session.add(
                JobRow(
                    id=job_id,
                    job_type="knowledge_ingest",
                    idempotency_key=key,
                    state="pending",
                    attempt_count=0,
                    cancellation_requested=False,
                    input_reference=source_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                KnowledgeSourceRow(
                    id=source_id,
                    name=name,
                    scope="conversation" if conversation_id is not None else "workspace",
                    conversation_id=conversation_id,
                    checksum=hashlib.sha256(content.encode()).hexdigest(),
                    embedding_model=None,
                    content=content,
                    status="pending",
                    active=False,
                    job_id=job_id,
                    created_at=now,
                )
            )
        return job_id

    async def job_status(self, job_id: str) -> dict[str, object] | None:
        async with self._database.sessions() as session:
            row = await session.get(JobRow, job_id)
            if row is None or row.job_type != "knowledge_ingest":
                return None
            return {
                "id": row.id,
                "source_id": row.input_reference,
                "state": row.state,
                "error_code": row.error_code,
                "cancellation_requested": row.cancellation_requested,
                "attempt_count": row.attempt_count,
                "retry_at": row.retry_at.isoformat() if row.retry_at else None,
            }

    async def attachment(self, source_id: str, conversation_id: str) -> KnowledgeSource | None:
        async with self._database.sessions() as session:
            row = await session.get(KnowledgeSourceRow, source_id)
            if (
                row is None
                or not row.active
                or row.status != "ready"
                or row.scope != "conversation"
                or row.conversation_id != conversation_id
            ):
                return None
            chunk_count = len(
                list(
                    await session.scalars(
                        select(KnowledgeChunkRow.id).where(KnowledgeChunkRow.source_id == source_id)
                    )
                )
            )
            return KnowledgeSource(
                row.id,
                row.name,
                row.checksum,
                chunk_count,
                row.embedding_model or "",
                row.conversation_id,
            )

    async def attachment_preview(
        self, source_id: str, conversation_id: str, *, max_characters: int = 200_000
    ) -> dict[str, object] | None:
        source = await self.attachment(source_id, conversation_id)
        if source is None:
            return None
        async with self._database.sessions() as session:
            chunks = list(
                await session.scalars(
                    select(KnowledgeChunkRow)
                    .where(KnowledgeChunkRow.source_id == source_id)
                    .order_by(KnowledgeChunkRow.ordinal)
                )
            )
        sections: list[str] = []
        last_page: int | None = None
        for chunk in chunks:
            if chunk.page_number is not None and chunk.page_number != last_page:
                sections.append(f"--- Page {chunk.page_number} ---")
                last_page = chunk.page_number
            sections.append(chunk.text)
        content = "\n\n".join(sections)
        return {
            "source_id": source.id,
            "name": source.name,
            "content": content[:max_characters],
            "truncated": len(content) > max_characters,
        }

    async def cancel(self, job_id: str) -> dict[str, object] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(JobRow)
                .where(JobRow.id == job_id, JobRow.job_type == "knowledge_ingest")
                .with_for_update()
            )
            if row is None:
                return None
            if row.state in {"pending", "processing"}:
                row.cancellation_requested = True
                row.state = "cancelled"
                row.lease_expires_at = None
                row.retry_at = None
                row.updated_at = now
                source = await session.get(KnowledgeSourceRow, row.input_reference)
                if source is not None:
                    source.content = None
                    source.status = "cancelled"
            return {
                "id": row.id,
                "state": row.state,
                "cancellation_requested": row.cancellation_requested,
            }

    async def claim_next(self, lease_seconds: int = 60) -> tuple[str, str, str, int] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.job_type == "knowledge_ingest",
                    JobRow.cancellation_requested.is_(False),
                    or_(
                        and_(
                            JobRow.state == "pending",
                            or_(JobRow.retry_at.is_(None), JobRow.retry_at <= now),
                        ),
                        ((JobRow.state == "processing") & (JobRow.lease_expires_at < now)),
                    ),
                )
                .order_by(JobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            source = await session.get(KnowledgeSourceRow, job.input_reference)
            if job.attempt_count >= self._max_attempts:
                job.state = "failed"
                job.error_code = "attempts_exhausted"
                job.lease_expires_at = None
                job.updated_at = now
                if source is not None:
                    source.content = None
                    source.status = "failed"
                return None
            if source is None or source.content is None:
                job.state = "failed"
                job.error_code = "missing_source"
                job.updated_at = now
                return None
            job.state = "processing"
            job.attempt_count += 1
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.retry_at = None
            job.updated_at = now
            return source.id, source.name, source.content, job.attempt_count

    async def retry(self, source_id: str, code: str, *, lease_attempt: int) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            source = await session.get(KnowledgeSourceRow, source_id)
            if source is None or source.job_id is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == source.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != lease_attempt
            ):
                return False
            job.lease_expires_at = None
            job.error_code = code
            job.updated_at = now
            if job.attempt_count >= self._max_attempts:
                job.state = "failed"
                source.status = "failed"
                source.content = None
                return False
            delay = min(self._retry_base_seconds * (2 ** (job.attempt_count - 1)), 300)
            job.state = "pending"
            job.retry_at = now + timedelta(seconds=delay)
            return True

    async def renew(self, source_id: str, attempt: int, lease_seconds: int = 60) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            source = await session.get(KnowledgeSourceRow, source_id)
            if source is None or source.job_id is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == source.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
            ):
                return False
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

    async def replace(
        self,
        source: KnowledgeSource,
        chunks: Sequence[KnowledgeChunk],
        *,
        lease_attempt: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.get(KnowledgeSourceRow, source.id)
            scoped_conversation_id = (
                row.conversation_id if row is not None else source.conversation_id
            )
            await _require_active_lease(session, row, lease_attempt, now)
            await session.execute(
                update(KnowledgeSourceRow)
                .where(
                    KnowledgeSourceRow.name == source.name,
                    KnowledgeSourceRow.scope
                    == ("conversation" if scoped_conversation_id is not None else "workspace"),
                    KnowledgeSourceRow.conversation_id == scoped_conversation_id,
                )
                .values(active=False)
            )
            row = _upsert_source_row(session, row, source, scoped_conversation_id, now)
            session.add_all(
                KnowledgeChunkRow(
                    id=chunk.id,
                    source_id=source.id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    vector=list(chunk.vector),
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    page_number=chunk.page_number,
                    checksum=chunk.checksum,
                    embedding_model=chunk.embedding_model,
                )
                for chunk in chunks
            )
            await _complete_job(session, row, source.id, now)

    async def fail(self, source_id: str, code: str, *, lease_attempt: int) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.get(KnowledgeSourceRow, source_id)
            if row is None:
                return
            if row.job_id:
                job = await session.get(JobRow, row.job_id)
                if (
                    job is not None
                    and job.state == "processing"
                    and job.attempt_count == lease_attempt
                ):
                    row.status = "failed"
                    row.content = None
                    job.state = "cancelled" if job.cancellation_requested else "failed"
                    job.error_code = None if job.cancellation_requested else code
                    job.lease_expires_at = None
                    job.updated_at = now

    async def list_sources(self) -> list[KnowledgeSource]:
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeSourceRow)
                    .where(KnowledgeSourceRow.active.is_(True))
                    .order_by(KnowledgeSourceRow.name)
                )
            )
            counts = [
                len(
                    list(
                        await session.scalars(
                            select(KnowledgeChunkRow.id).where(
                                KnowledgeChunkRow.source_id == row.id
                            )
                        )
                    )
                )
                for row in rows
            ]
        return [
            KnowledgeSource(
                row.id,
                row.name,
                row.checksum,
                count,
                row.embedding_model or "",
                row.conversation_id,
            )
            for row, count in zip(rows, counts, strict=True)
        ]

    async def list_chunks(
        self, *, limit: int, conversation_id: str | None = None
    ) -> list[KnowledgeChunk]:
        async with self._database.sessions() as session:
            rows = list(
                await session.execute(
                    select(KnowledgeChunkRow, KnowledgeSourceRow.name)
                    .join(KnowledgeSourceRow)
                    .where(
                        KnowledgeSourceRow.active.is_(True),
                        or_(
                            KnowledgeSourceRow.scope == "workspace",
                            and_(
                                KnowledgeSourceRow.scope == "conversation",
                                KnowledgeSourceRow.conversation_id == conversation_id,
                            ),
                        ),
                    )
                    .order_by(KnowledgeSourceRow.name, KnowledgeChunkRow.ordinal)
                    .limit(limit)
                )
            )
        return [
            KnowledgeChunk(
                id=chunk.id,
                source_id=chunk.source_id,
                source_name=name,
                ordinal=chunk.ordinal,
                text=chunk.text,
                vector=tuple(chunk.vector),
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                page_number=chunk.page_number,
                checksum=chunk.checksum,
                embedding_model=chunk.embedding_model,
            )
            for chunk, name in rows
        ]


async def _require_active_lease(
    session: AsyncSession,
    row: KnowledgeSourceRow | None,
    lease_attempt: int | None,
    now: datetime,
) -> None:
    if row is None or not row.job_id:
        return
    job = await session.scalar(select(JobRow).where(JobRow.id == row.job_id).with_for_update())
    lease = job.lease_expires_at if job is not None else None
    if lease is not None and lease.tzinfo is None:
        lease = lease.replace(tzinfo=UTC)
    valid = (
        job is not None
        and not job.cancellation_requested
        and job.state == "processing"
        and job.attempt_count == lease_attempt
        and lease is not None
        and lease > now
    )
    if not valid:
        raise KnowledgeError("Knowledge ingestion was cancelled or lost its lease")


def _upsert_source_row(
    session: AsyncSession,
    row: KnowledgeSourceRow | None,
    source: KnowledgeSource,
    conversation_id: str | None,
    now: datetime,
) -> KnowledgeSourceRow:
    if row is None:
        row = KnowledgeSourceRow(
            id=source.id,
            name=source.name,
            scope="conversation" if conversation_id is not None else "workspace",
            conversation_id=conversation_id,
            checksum=source.checksum,
            embedding_model=source.embedding_model,
            content=None,
            status="ready",
            active=True,
            created_at=now,
        )
        session.add(row)
        return row
    row.content = None
    row.status = "ready"
    row.active = True
    row.checksum = source.checksum
    row.embedding_model = source.embedding_model
    return row


async def _complete_job(
    session: AsyncSession, row: KnowledgeSourceRow, source_id: str, now: datetime
) -> None:
    if not row.job_id:
        return
    job = await session.get(JobRow, row.job_id)
    if job is None:
        return
    job.state = "completed"
    job.output_reference = source_id
    job.lease_expires_at = None
    job.retry_at = None
    job.updated_at = now
