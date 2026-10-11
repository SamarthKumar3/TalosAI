from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RESPONSE_DELTA = "response.delta"
    RESPONSE_COMPLETED = "response.completed"
    USAGE_RECORDED = "usage.recorded"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    GUARDRAIL_BLOCKED = "guardrail.blocked"
    RETRIEVAL_COMPLETED = "retrieval.completed"
    CONTEXT_RECORDED = "context.recorded"
    SPAN_COMPLETED = "span.completed"

    MODEL_RETRYING = "model.retrying"
    MEMORY_SUMMARY_CREATED = "memory.summary_created"
    MEMORY_COMPACTING = "memory.compacting"
    RUN_RECONCILED = "run.reconciled"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: EventType
    run_id: str
    conversation_id: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
