from __future__ import annotations

import asyncio
from typing import Protocol

from talos.events import EventType, RuntimeEvent


class RuntimeEventStore(Protocol):
    async def list_tool_events(self, conversation_id: str) -> list[RuntimeEvent]: ...

    async def append(self, event: RuntimeEvent) -> None: ...

    async def list_for_run(self, run_id: str, *, after_sequence: int = 0) -> list[RuntimeEvent]: ...

    async def list_recent_run_ids(self, *, limit: int) -> list[str]: ...


class InMemoryRuntimeEventStore:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event: RuntimeEvent) -> None:
        async with self._lock:
            self.events.append(event)

    async def list_tool_events(self, conversation_id: str) -> list[RuntimeEvent]:
        async with self._lock:
            return [
                event
                for event in self.events
                if event.conversation_id == conversation_id
                and event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}
            ]

    async def list_for_run(self, run_id: str, *, after_sequence: int = 0) -> list[RuntimeEvent]:
        async with self._lock:
            return [
                event
                for event in self.events
                if event.run_id == run_id and event.sequence > after_sequence
            ]

    async def list_recent_run_ids(self, *, limit: int) -> list[str]:
        if limit < 1:
            raise ValueError("Trace list limit must be positive")
        async with self._lock:
            latest: dict[str, RuntimeEvent] = {}
            for event in self.events:
                current = latest.get(event.run_id)
                if current is None or event.occurred_at > current.occurred_at:
                    latest[event.run_id] = event
            return [
                event.run_id
                for event in sorted(
                    latest.values(), key=lambda item: item.occurred_at, reverse=True
                )[:limit]
            ]
