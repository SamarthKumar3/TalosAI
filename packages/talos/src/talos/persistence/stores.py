from __future__ import annotations

import builtins
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from talos.domain import (
    ConfigurationSnapshot,
    Conversation,
    Message,
    MessageAttachment,
    MessageRole,
)
from talos.errors import ConversationNotFoundError
from talos.events import EventType, RuntimeEvent
from talos.persistence.database import Database
from talos.persistence.models import (
    AgentRunRow,
    ConfigurationVersionRow,
    ConversationRow,
    MessageRow,
    RuntimeEventRow,
    UsageRecordRow,
)


class SqlConversationStore:
    """PostgreSQL-compatible durable implementation of the conversation port."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, configuration: ConfigurationSnapshot) -> Conversation:
        conversation = Conversation(configuration=configuration)
        async with self._database.sessions.begin() as session:
            config_row = await session.scalar(
                select(ConfigurationVersionRow).where(
                    ConfigurationVersionRow.content_hash == configuration.content_hash
                )
            )
            if config_row is None:
                config_row = ConfigurationVersionRow(
                    content_hash=configuration.content_hash,
                    schema_version=configuration.version,
                    resolved=_json_mapping(configuration.resolved),
                    created_at=conversation.created_at,
                )
                session.add(config_row)
                await session.flush()
            session.add(
                ConversationRow(
                    id=conversation.id,
                    configuration_version_id=config_row.id,
                    created_at=conversation.created_at,
                )
            )
        return conversation

    async def get(self, conversation_id: str) -> Conversation:
        async with self._database.sessions() as session:
            result = await session.execute(
                select(ConversationRow, ConfigurationVersionRow)
                .join(
                    ConfigurationVersionRow,
                    ConfigurationVersionRow.id == ConversationRow.configuration_version_id,
                )
                .where(ConversationRow.id == conversation_id)
            )
            item = result.one_or_none()
            if item is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            conversation_row, configuration_row = item
            message_rows = list(
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conversation_id)
                    .order_by(MessageRow.sequence)
                )
            )
        resolved = configuration_row.resolved
        configuration = _snapshot_from_row(configuration_row, resolved)
        return Conversation(
            id=conversation_row.id,
            configuration=configuration,
            messages=[
                Message(
                    id=row.id,
                    run_id=row.run_id,
                    role=MessageRole(row.role),
                    content=row.content,
                    attachments=tuple(
                        MessageAttachment(
                            source_id=item["source_id"],
                            name=item["name"],
                        )
                        for item in row.attachments
                    ),
                    created_at=_aware(row.created_at),
                )
                for row in message_rows
            ],
            created_at=_aware(conversation_row.created_at),
        )

    async def append(self, conversation_id: str, message: Message) -> None:
        async with self._database.sessions.begin() as session:
            conversation = await session.scalar(
                select(ConversationRow)
                .where(ConversationRow.id == conversation_id)
                .with_for_update()
            )
            if conversation is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            highest = await session.scalar(
                select(func.max(MessageRow.sequence)).where(
                    MessageRow.conversation_id == conversation_id
                )
            )
            session.add(
                MessageRow(
                    id=message.id,
                    run_id=message.run_id,
                    conversation_id=conversation_id,
                    sequence=(highest or 0) + 1,
                    role=message.role.value,
                    content=message.content,
                    attachments=[
                        {"source_id": item.source_id, "name": item.name}
                        for item in message.attachments
                    ],
                    created_at=message.created_at,
                )
            )

    async def list(self, *, limit: int = 50) -> list[Conversation]:
        if limit < 1 or limit > 100:
            raise ValueError("Conversation list limit must be between 1 and 100")
        async with self._database.sessions() as session:
            identifiers = list(
                await session.scalars(
                    select(ConversationRow.id)
                    .order_by(ConversationRow.created_at.desc())
                    .limit(limit)
                )
            )
        return [await self.get(identifier) for identifier in identifiers]

    async def response_history(
        self, conversation_id: str, *, start: int = 0, limit: int = 20
    ) -> tuple[ConfigurationSnapshot, int, int, builtins.list[Message]]:
        if start < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid response history page")
        async with self._database.sessions() as session:
            config = await session.scalar(
                select(ConfigurationVersionRow)
                .join(
                    ConversationRow,
                    ConversationRow.configuration_version_id == ConfigurationVersionRow.id,
                )
                .where(ConversationRow.id == conversation_id)
            )
            if config is None:
                raise ConversationNotFoundError("Conversation was not found")
            scope = MessageRow.conversation_id == conversation_id
            eligible = MessageRow.role == MessageRole.ASSISTANT.value
            total = int(
                await session.scalar(select(func.count()).select_from(MessageRow).where(scope)) or 0
            )
            count = int(
                await session.scalar(
                    select(func.count()).select_from(MessageRow).where(scope, eligible)
                )
                or 0
            )
            rows = await session.scalars(
                select(MessageRow)
                .where(scope, eligible)
                .order_by(MessageRow.sequence.desc())
                .offset(min(start, count))
                .limit(limit)
            )
            messages = [
                Message(
                    id=row.id,
                    role=MessageRole(row.role),
                    content="",
                    run_id=row.run_id,
                    created_at=_aware(row.created_at),
                )
                for row in rows
            ]
            return _snapshot_from_row(config, config.resolved), total, count, messages


class SqlRuntimeEventStore:
    """Persists run lifecycle and replayable events before they reach consumers."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_tool_events(self, conversation_id: str) -> list[RuntimeEvent]:
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(RuntimeEventRow)
                    .join(AgentRunRow, AgentRunRow.id == RuntimeEventRow.run_id)
                    .where(
                        AgentRunRow.conversation_id == conversation_id,
                        RuntimeEventRow.conversation_id == conversation_id,
                        RuntimeEventRow.event_type.in_(
                            [EventType.TOOL_COMPLETED.value, EventType.TOOL_FAILED.value]
                        ),
                    )
                    .order_by(RuntimeEventRow.occurred_at, RuntimeEventRow.sequence)
                )
            )
        return [
            RuntimeEvent(
                event_id=row.id,
                type=EventType(row.event_type),
                run_id=row.run_id,
                conversation_id=row.conversation_id,
                sequence=row.sequence,
                payload=row.payload,
                schema_version=row.schema_version,
                occurred_at=_aware(row.occurred_at),
            )
            for row in rows
        ]

    async def append(self, event: RuntimeEvent) -> None:
        async with self._database.sessions.begin() as session:
            run = await session.get(AgentRunRow, event.run_id)
            if run is None:
                if event.type is not EventType.RUN_STARTED:
                    raise RuntimeError("First persisted event must be run.started")
                run = AgentRunRow(
                    id=event.run_id,
                    conversation_id=event.conversation_id,
                    state="running",
                    started_at=event.occurred_at,
                )
                session.add(run)
            payload = _json_mapping(event.payload)
            session.add(
                RuntimeEventRow(
                    id=event.event_id,
                    run_id=event.run_id,
                    conversation_id=event.conversation_id,
                    sequence=event.sequence,
                    event_type=event.type.value,
                    schema_version=event.schema_version,
                    payload=payload,
                    occurred_at=event.occurred_at,
                )
            )
            if event.type is EventType.RESPONSE_COMPLETED:
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    session.add(
                        UsageRecordRow(
                            run_id=event.run_id,
                            input_tokens=int(usage.get("input_tokens", 0)),
                            output_tokens=int(usage.get("output_tokens", 0)),
                        )
                    )
            if event.type in {
                EventType.RUN_COMPLETED,
                EventType.RUN_CANCELLED,
                EventType.RUN_FAILED,
            }:
                run.state = {
                    EventType.RUN_COMPLETED: "completed",
                    EventType.RUN_CANCELLED: "cancelled",
                    EventType.RUN_FAILED: "failed",
                }[event.type]
                run.finished_at = event.occurred_at
                code = payload.get("code")
                run.error_code = str(code) if code is not None else None

    async def list_for_run(self, run_id: str, *, after_sequence: int = 0) -> list[RuntimeEvent]:
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(RuntimeEventRow)
                    .where(
                        RuntimeEventRow.run_id == run_id,
                        RuntimeEventRow.sequence > after_sequence,
                    )
                    .order_by(RuntimeEventRow.sequence)
                )
            )
        return [
            RuntimeEvent(
                event_id=row.id,
                type=EventType(row.event_type),
                run_id=row.run_id,
                conversation_id=row.conversation_id,
                sequence=row.sequence,
                schema_version=row.schema_version,
                payload=row.payload,
                occurred_at=_aware(row.occurred_at),
            )
            for row in rows
        ]

    async def list_recent_run_ids(self, *, limit: int) -> list[str]:
        if limit < 1:
            raise ValueError("Trace list limit must be positive")
        async with self._database.sessions() as session:
            return list(
                await session.scalars(
                    select(AgentRunRow.id).order_by(AgentRunRow.started_at.desc()).limit(limit)
                )
            )

    async def reconcile_interrupted(self) -> int:
        """Fail runs left active by a previous process without replaying side effects."""
        async with self._database.sessions() as session:
            runs = list(
                await session.scalars(select(AgentRunRow).where(AgentRunRow.state == "running"))
            )
            pending = []
            for run in runs:
                highest = await session.scalar(
                    select(func.max(RuntimeEventRow.sequence)).where(
                        RuntimeEventRow.run_id == run.id
                    )
                )
                pending.append((run.id, run.conversation_id, int(highest or 0)))
        for run_id, conversation_id, highest in pending:
            await self.append(
                RuntimeEvent(
                    type=EventType.RUN_RECONCILED,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    sequence=highest + 1,
                    payload={"reason": "service_restarted"},
                )
            )
            await self.append(
                RuntimeEvent(
                    type=EventType.RUN_FAILED,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    sequence=highest + 2,
                    payload={
                        "code": "service_restarted",
                        "message": "The run was interrupted by a service restart",
                        "retryable": True,
                    },
                )
            )
        return len(pending)


def _snapshot_from_row(
    row: ConfigurationVersionRow,
    resolved: Mapping[str, Any],
) -> ConfigurationSnapshot:
    provider = _mapping(resolved, "provider")
    model = _mapping(resolved, "model")
    pipeline = _mapping(resolved, "pipeline")
    return ConfigurationSnapshot(
        version=row.schema_version,
        content_hash=row.content_hash,
        provider_alias=str(provider["alias"]),
        model_alias=str(model["alias"]),
        model_id=str(model["model_id"]),
        pipeline_id=str(pipeline["id"]),
        prompt_id=str(pipeline["prompt_alias"]),
        resolved=resolved,
    )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RuntimeError(f"Persisted configuration field {key!r} is invalid")
    return item


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
