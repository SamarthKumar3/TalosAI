from __future__ import annotations

import asyncio
import builtins
from typing import Protocol

from talos.domain import ConfigurationSnapshot, Conversation, Message, MessageRole
from talos.errors import ConversationNotFoundError


class ConversationStore(Protocol):
    async def create(self, configuration: ConfigurationSnapshot) -> Conversation: ...

    async def get(self, conversation_id: str) -> Conversation: ...

    async def append(self, conversation_id: str, message: Message) -> None: ...

    async def list(self, *, limit: int = 50) -> list[Conversation]: ...

    async def response_history(
        self, conversation_id: str, *, start: int = 0, limit: int = 20
    ) -> tuple[ConfigurationSnapshot, int, int, builtins.list[Message]]: ...


class InMemoryConversationStore:
    """Concurrency-safe embeddable store; durable adapters arrive in Phase 3."""

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def create(self, configuration: ConfigurationSnapshot) -> Conversation:
        conversation = Conversation(configuration=configuration)
        async with self._lock:
            self._conversations[conversation.id] = conversation
        return conversation

    async def get(self, conversation_id: str) -> Conversation:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            return conversation

    async def append(self, conversation_id: str, message: Message) -> None:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            conversation.messages.append(message)

    async def list(self, *, limit: int = 50) -> list[Conversation]:
        async with self._lock:
            conversations = sorted(
                self._conversations.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
            return conversations[:limit]

    async def response_history(
        self, conversation_id: str, *, start: int = 0, limit: int = 20
    ) -> tuple[ConfigurationSnapshot, int, int, builtins.list[Message]]:
        if start < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid response history page")
        conversation = await self.get(conversation_id)
        async with self._lock:
            responses = [m for m in conversation.messages if m.role is MessageRole.ASSISTANT]
            return (
                conversation.configuration,
                len(conversation.messages),
                len(responses),
                list(reversed(responses))[start : start + limit],
            )
