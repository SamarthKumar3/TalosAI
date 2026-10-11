from __future__ import annotations

from sqlalchemy import delete, func, select

from talos.errors import ConfigurationError
from talos.memory import ConversationSummary, MemoryRecord
from talos.persistence.database import Database
from talos.persistence.models import ConversationSummaryRow, MemoryRow


class SqlMemoryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def latest_summary(self, conversation_id: str) -> ConversationSummary | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ConversationSummaryRow)
                .where(ConversationSummaryRow.conversation_id == conversation_id)
                .order_by(ConversationSummaryRow.end_sequence.desc())
                .limit(1)
            )
        return _summary(row) if row is not None else None

    async def save_summary(self, summary: ConversationSummary) -> None:
        async with self._database.sessions.begin() as session:
            latest = await session.scalar(
                select(ConversationSummaryRow)
                .where(ConversationSummaryRow.conversation_id == summary.conversation_id)
                .order_by(ConversationSummaryRow.end_sequence.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is not None and latest.end_sequence >= summary.end_sequence:
                return
            session.add(
                ConversationSummaryRow(
                    id=summary.id,
                    conversation_id=summary.conversation_id,
                    start_sequence=summary.start_sequence,
                    end_sequence=summary.end_sequence,
                    content=summary.content,
                    configuration_hash=summary.configuration_hash,
                    source_checksum=summary.source_checksum,
                    created_at=summary.created_at,
                )
            )

    async def list_memories(self, *, limit: int = 100) -> list[MemoryRecord]:
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(MemoryRow)
                    .where(MemoryRow.status == "active")
                    .order_by(MemoryRow.created_at.desc())
                    .limit(limit)
                )
            )
        return [_memory(row) for row in rows]

    async def save_memory(self, memory: MemoryRecord, *, maximum: int) -> None:
        async with self._database.sessions.begin() as session:
            active = await session.scalar(
                select(func.count()).select_from(MemoryRow).where(MemoryRow.status == "active")
            )
            if int(active or 0) >= maximum:
                raise ConfigurationError("Long-term memory item limit reached")
            session.add(
                MemoryRow(
                    id=memory.id,
                    content=memory.content,
                    source_conversation_id=memory.source_conversation_id,
                    source_message_id=memory.source_message_id,
                    status=memory.status,
                    created_at=memory.created_at,
                )
            )

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._database.sessions.begin() as session:
            result = await session.execute(
                delete(MemoryRow).where(MemoryRow.id == memory_id, MemoryRow.status == "active")
            )
            return bool(getattr(result, "rowcount", 0))


def _summary(row: ConversationSummaryRow) -> ConversationSummary:
    return ConversationSummary(
        id=row.id,
        conversation_id=row.conversation_id,
        start_sequence=row.start_sequence,
        end_sequence=row.end_sequence,
        content=row.content,
        configuration_hash=row.configuration_hash,
        source_checksum=row.source_checksum,
        created_at=row.created_at,
    )


def _memory(row: MemoryRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        content=row.content,
        source_conversation_id=row.source_conversation_id,
        source_message_id=row.source_message_id,
        status=row.status,
        created_at=row.created_at,
    )
