from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select

from talos.persistence.database import Database
from talos.persistence.models import JobRow, RepositoryChunkRow, RepositoryRow, RepositoryVersionRow
from talos.repository import RepositoryError, RepositorySnapshot
from talos.repository_index import RepositoryChunk


class SqlRepositoryStore:
    def __init__(self, database: Database, *, max_attempts: int, retry_base_seconds: int) -> None:
        self._database = database
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds

    async def enqueue(self, snapshot: RepositorySnapshot, model: str, key: str) -> str:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            existing = await session.scalar(select(JobRow).where(JobRow.idempotency_key == key))
            if existing is not None:
                prior = await session.get(RepositoryVersionRow, existing.input_reference)
                if (
                    existing.job_type != "repository_index"
                    or prior is None
                    or prior.repository_id != snapshot.identity
                    or prior.checksum != snapshot.checksum
                ):
                    raise RepositoryError("Idempotency key was used for different input")
                return existing.id
            repository = await session.get(RepositoryRow, snapshot.identity)
            if repository is None:
                repository = RepositoryRow(
                    id=snapshot.identity,
                    name=snapshot.name,
                    active_version_id=None,
                    created_at=now,
                )
                session.add(repository)
            job_id, version_id = str(uuid4()), str(uuid4())
            session.add(
                JobRow(
                    id=job_id,
                    job_type="repository_index",
                    idempotency_key=key,
                    state="pending",
                    attempt_count=0,
                    cancellation_requested=False,
                    input_reference=version_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RepositoryVersionRow(
                    id=version_id,
                    repository_id=snapshot.identity,
                    job_id=job_id,
                    branch=snapshot.branch,
                    commit=snapshot.commit,
                    dirty=snapshot.dirty,
                    checksum=snapshot.checksum,
                    embedding_model=model,
                    files=[
                        {
                            "path": item.path,
                            "content": item.content,
                            "checksum": item.checksum,
                            "language": item.language,
                        }
                        for item in snapshot.files
                    ],
                    file_count=len(snapshot.files),
                    chunk_count=0,
                    skipped=snapshot.skipped,
                    state="pending",
                    created_at=now,
                )
            )
        return job_id

    async def claim_next(
        self, *, lease_seconds: int = 60
    ) -> tuple[str, str, list[dict[str, str]], int] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.job_type == "repository_index",
                    JobRow.cancellation_requested.is_(False),
                    or_(
                        and_(
                            JobRow.state == "pending",
                            or_(JobRow.retry_at.is_(None), JobRow.retry_at <= now),
                        ),
                        and_(JobRow.state == "processing", JobRow.lease_expires_at < now),
                    ),
                )
                .order_by(JobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            version = await session.get(RepositoryVersionRow, job.input_reference)
            if version is None or version.files is None:
                job.state, job.error_code, job.updated_at = "failed", "missing_input", now
                return None
            if job.attempt_count >= self._max_attempts:
                job.state, job.error_code, job.updated_at = "failed", "attempts_exhausted", now
                version.state, version.files = "failed", None
                return None
            job.state = version.state = "processing"
            job.attempt_count += 1
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.retry_at = None
            job.updated_at = now
            return version.repository_id, version.id, version.files, job.attempt_count

    async def renew(self, version_id: str, attempt: int, *, lease_seconds: int = 60) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            version = await session.get(RepositoryVersionRow, version_id)
            if version is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == version.job_id).with_for_update()
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

    async def reusable(
        self, repository_id: str, model: str
    ) -> dict[tuple[str, str], list[RepositoryChunk]]:
        async with self._database.sessions() as session:
            repository = await session.get(RepositoryRow, repository_id)
            if repository is None or repository.active_version_id is None:
                return {}
            version = await session.get(RepositoryVersionRow, repository.active_version_id)
            if version is None or version.embedding_model != model:
                return {}
            rows = (
                await session.scalars(
                    select(RepositoryChunkRow)
                    .where(RepositoryChunkRow.version_id == version.id)
                    .order_by(RepositoryChunkRow.path, RepositoryChunkRow.ordinal)
                )
            ).all()
            result: dict[tuple[str, str], list[RepositoryChunk]] = {}
            for row in rows:
                result.setdefault((row.path, row.file_checksum), []).append(_chunk_from_row(row))
            return result

    async def publish(
        self, version_id: str, chunks: Sequence[RepositoryChunk], *, attempt: int
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            version = await session.get(RepositoryVersionRow, version_id)
            if version is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == version.job_id).with_for_update()
            )
            lease = job.lease_expires_at if job is not None else None
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
                or lease is None
                or lease <= now
            ):
                return False
            repository = await session.scalar(
                select(RepositoryRow)
                .where(RepositoryRow.id == version.repository_id)
                .with_for_update()
            )
            if repository is None:
                return False
            await session.execute(
                delete(RepositoryChunkRow).where(RepositoryChunkRow.version_id == version_id)
            )
            session.add_all(
                RepositoryChunkRow(
                    id=chunk.id,
                    version_id=version_id,
                    path=chunk.path,
                    language=chunk.language,
                    file_checksum=chunk.file_checksum,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    vector=list(chunk.vector),
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    checksum=chunk.checksum,
                    symbol=chunk.symbol,
                )
                for chunk in chunks
            )
            repository.active_version_id = version_id
            version.state, version.files, version.chunk_count = "ready", None, len(chunks)
            job.state, job.output_reference = "completed", version_id
            job.lease_expires_at = job.retry_at = None
            job.updated_at = now
            return True

    async def fail(
        self, version_id: str, code: str, *, attempt: int, transient: bool = False
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            version = await session.get(RepositoryVersionRow, version_id)
            if version is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == version.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
            ):
                return False
            job.error_code, job.lease_expires_at, job.updated_at = code, None, now
            if transient and attempt < self._max_attempts:
                delay = min(self._retry_base_seconds * (2 ** (attempt - 1)), 300)
                job.state = version.state = "pending"
                job.retry_at = now + timedelta(seconds=delay)
                return True
            job.state = version.state = "failed"
            version.files = None
            return False

    async def status(self, job_id: str) -> dict[str, object] | None:
        async with self._database.sessions() as session:
            job = await session.get(JobRow, job_id)
            if job is None or job.job_type != "repository_index":
                return None
            version = await session.get(RepositoryVersionRow, job.input_reference)
            if version is None:
                return None
            return {
                "job_id": job.id,
                "state": job.state,
                "attempt_count": job.attempt_count,
                "retry_at": job.retry_at,
                "error_code": job.error_code,
                "repository_id": version.repository_id,
                "version_id": version.id,
                "branch": version.branch,
                "commit": version.commit,
                "dirty": version.dirty,
                "snapshot": version.checksum,
                "file_count": version.file_count,
                "chunk_count": version.chunk_count,
                "skipped": version.skipped,
            }

    async def cancel(self, job_id: str) -> dict[str, object] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(JobRow.id == job_id, JobRow.job_type == "repository_index")
                .with_for_update()
            )
            if job is None:
                return None
            version = await session.get(RepositoryVersionRow, job.input_reference)
            if job.state in {"pending", "processing"}:
                job.cancellation_requested = True
                job.state = "cancelled"
                job.lease_expires_at = job.retry_at = None
                job.updated_at = now
                if version is not None:
                    version.files, version.state = None, "cancelled"
        return await self.status(job_id)

    async def summary(self, repository_id: str) -> dict[str, object] | None:
        async with self._database.sessions() as session:
            repository = await session.get(RepositoryRow, repository_id)
            if repository is None:
                return None
            version = (
                await session.get(RepositoryVersionRow, repository.active_version_id)
                if repository.active_version_id
                else None
            )
            return {
                "id": repository.id,
                "name": repository.name,
                "active_version": version.id if version else None,
                "branch": version.branch if version else None,
                "commit": version.commit if version else None,
                "dirty": version.dirty if version else None,
                "snapshot": version.checksum if version else None,
                "file_count": version.file_count if version else 0,
                "chunk_count": version.chunk_count if version else 0,
            }

    async def remove(self, repository_id: str) -> bool:
        async with self._database.sessions.begin() as session:
            repository = await session.scalar(
                select(RepositoryRow).where(RepositoryRow.id == repository_id).with_for_update()
            )
            if repository is None:
                return False
            pending = await session.scalar(
                select(JobRow.id)
                .join(RepositoryVersionRow, RepositoryVersionRow.job_id == JobRow.id)
                .where(
                    RepositoryVersionRow.repository_id == repository_id,
                    JobRow.state.in_(("pending", "processing")),
                )
                .limit(1)
            )
            if pending is not None:
                raise RepositoryError("Cancel active repository jobs before removing the index")
            await session.execute(
                delete(RepositoryChunkRow).where(
                    RepositoryChunkRow.version_id.in_(
                        select(RepositoryVersionRow.id).where(
                            RepositoryVersionRow.repository_id == repository_id
                        )
                    )
                )
            )
            await session.execute(
                delete(RepositoryVersionRow).where(
                    RepositoryVersionRow.repository_id == repository_id
                )
            )
            await session.delete(repository)
            return True

    async def search_chunks(
        self, *, repository_id: str | None, model: str, limit: int
    ) -> list[tuple[RepositoryChunk, str, bool, str]]:
        async with self._database.sessions() as session:
            query = select(RepositoryRow).where(RepositoryRow.active_version_id.is_not(None))
            if repository_id is not None:
                query = query.where(RepositoryRow.id == repository_id)
            else:
                query = query.order_by(RepositoryRow.created_at.desc()).limit(1)
            repository = await session.scalar(query)
            if repository is None or repository.active_version_id is None:
                return []
            version = await session.get(RepositoryVersionRow, repository.active_version_id)
            if version is None or version.embedding_model != model:
                return []
            rows = (
                await session.scalars(
                    select(RepositoryChunkRow)
                    .where(RepositoryChunkRow.version_id == version.id)
                    .order_by(RepositoryChunkRow.path, RepositoryChunkRow.ordinal)
                    .limit(limit)
                )
            ).all()
            return [
                (_chunk_from_row(row), version.commit, version.dirty, version.checksum)
                for row in rows
            ]


def _chunk_from_row(row: RepositoryChunkRow) -> RepositoryChunk:
    return RepositoryChunk(
        id=row.id,
        version_id=row.version_id,
        path=row.path,
        language=row.language,
        file_checksum=row.file_checksum,
        ordinal=row.ordinal,
        text=row.text,
        vector=tuple(row.vector),
        start_line=row.start_line,
        end_line=row.end_line,
        checksum=row.checksum,
        symbol=row.symbol,
    )
