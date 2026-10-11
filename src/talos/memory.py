from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from talos.domain import Conversation, Message, MessageRole
from talos.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class MemorySettings:
    short_term_enabled: bool
    summarization_enabled: bool
    summary_after_messages: int
    keep_recent_messages: int
    max_summary_characters: int
    summary_strategy: str
    long_term_enabled: bool
    max_long_term_items: int
    max_memory_characters: int

    def __post_init__(self) -> None:
        if self.summary_after_messages < 2:
            raise ConfigurationError("Memory summary threshold must be at least 2")
        if not 1 <= self.keep_recent_messages < self.summary_after_messages:
            raise ConfigurationError(
                "Memory recent-message count must be below the summary threshold"
            )
        if self.max_summary_characters < 256:
            raise ConfigurationError("Memory summaries must allow at least 256 characters")
        if self.summary_strategy != "extractive_v1":
            raise ConfigurationError("Configured memory summary strategy is not allowlisted")
        if self.max_long_term_items < 1 or self.max_memory_characters < 1:
            raise ConfigurationError("Long-term memory limits must be positive")


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    conversation_id: str
    start_sequence: int
    end_sequence: int
    content: str
    configuration_hash: str
    source_checksum: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    content: str
    source_conversation_id: str
    source_message_id: str | None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryStore(Protocol):
    async def latest_summary(self, conversation_id: str) -> ConversationSummary | None: ...

    async def save_summary(self, summary: ConversationSummary) -> None: ...

    async def list_memories(self, *, limit: int = 100) -> list[MemoryRecord]: ...

    async def save_memory(self, memory: MemoryRecord, *, maximum: int) -> None: ...

    async def delete_memory(self, memory_id: str) -> bool: ...


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._summaries: dict[str, list[ConversationSummary]] = {}
        self._memories: dict[str, MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def latest_summary(self, conversation_id: str) -> ConversationSummary | None:
        async with self._lock:
            values = self._summaries.get(conversation_id, [])
            return values[-1] if values else None

    async def save_summary(self, summary: ConversationSummary) -> None:
        async with self._lock:
            values = self._summaries.setdefault(summary.conversation_id, [])
            if values and summary.end_sequence <= values[-1].end_sequence:
                return
            values.append(summary)

    async def list_memories(self, *, limit: int = 100) -> list[MemoryRecord]:
        async with self._lock:
            return sorted(
                (item for item in self._memories.values() if item.status == "active"),
                key=lambda item: item.created_at,
                reverse=True,
            )[:limit]

    async def save_memory(self, memory: MemoryRecord, *, maximum: int) -> None:
        async with self._lock:
            active = sum(item.status == "active" for item in self._memories.values())
            if active >= maximum:
                raise ConfigurationError("Long-term memory item limit reached")
            self._memories[memory.id] = memory

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            item = self._memories.get(memory_id)
            if item is None or item.status != "active":
                return False
            del self._memories[memory_id]
            return True


class MemoryService:
    def __init__(self, store: MemoryStore, settings: MemorySettings) -> None:
        self.store = store
        self.settings = settings

    async def context_for(
        self, conversation: Conversation
    ) -> tuple[tuple[Message, ...], ConversationSummary | None, bool]:
        messages = (
            tuple(item for item in conversation.messages if item.role is not MessageRole.NOTICE)
            if self.settings.short_term_enabled
            else ()
        )
        created = False
        summary = (
            await self.store.latest_summary(conversation.id)
            if self.settings.short_term_enabled
            else None
        )
        if (
            self.settings.summarization_enabled
            and len(messages) >= self.settings.summary_after_messages
        ):
            cutoff = len(messages) - self.settings.keep_recent_messages
            if summary is None or cutoff > summary.end_sequence:
                summary = self._summarize(conversation, messages[:cutoff], cutoff)
                await self.store.save_summary(summary)
                created = True
        covered = summary.end_sequence if summary is not None else 0
        context: list[Message] = []
        if summary is not None:
            context.append(
                Message(
                    MessageRole.SYSTEM,
                    (
                        f"Conversation summary through message {summary.end_sequence}:\n"
                        f"{summary.content}"
                    ),
                )
            )
        if self.settings.long_term_enabled:
            memories = await self.store.list_memories(limit=self.settings.max_long_term_items)
            if memories:
                context.append(
                    Message(
                        MessageRole.SYSTEM,
                        "Explicitly retained workspace memory:\n"
                        + "\n".join(f"- {item.content}" for item in reversed(memories)),
                    )
                )
        context.extend(messages[covered:])
        return tuple(context), summary, created

    async def compact_removed(
        self, conversation: Conversation, removed: Sequence[Message], max_characters: int
    ) -> ConversationSummary | None:
        source = [item for item in conversation.messages if item.role is not MessageRole.NOTICE]
        removed_ids = {item.id for item in removed}
        cutoff = max(
            (index for index, item in enumerate(source, 1) if item.id in removed_ids), default=0
        )
        previous = await self.store.latest_summary(conversation.id)
        if previous is not None and cutoff <= previous.end_sequence:
            return previous
        if not cutoff:
            return previous
        summary = self._summarize(conversation, source[:cutoff], cutoff)
        summary = replace(summary, content=summary.content[-max_characters:])
        await self.store.save_summary(summary)
        return summary

    async def remember(
        self,
        content: str,
        *,
        source_conversation_id: str,
        source_message_id: str | None = None,
    ) -> MemoryRecord:
        if not self.settings.long_term_enabled:
            raise ConfigurationError("Long-term memory is disabled by configuration")
        normalized = " ".join(content.split())
        if not normalized or len(normalized) > self.settings.max_memory_characters:
            raise ConfigurationError("Memory content is empty or exceeds its configured limit")
        record = MemoryRecord(
            content=normalized,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
        )
        await self.store.save_memory(record, maximum=self.settings.max_long_term_items)
        return record

    async def list(self, *, limit: int = 100) -> list[MemoryRecord]:
        return await self.store.list_memories(limit=limit)

    async def delete(self, memory_id: str) -> bool:
        return await self.store.delete_memory(memory_id)

    def _summarize(
        self,
        conversation: Conversation,
        messages: Sequence[Message],
        end_sequence: int,
    ) -> ConversationSummary:
        source = "\n".join(
            f"{message.role.value}: {' '.join(message.content.split())}" for message in messages
        )
        checksum = hashlib.sha256(source.encode()).hexdigest()
        content = source[-self.settings.max_summary_characters :]
        return ConversationSummary(
            conversation_id=conversation.id,
            start_sequence=1,
            end_sequence=end_sequence,
            content=content,
            configuration_hash=conversation.configuration.content_hash,
            source_checksum=checksum,
        )
