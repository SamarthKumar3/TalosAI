from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    NOTICE = "notice"


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    source_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    attachments: tuple[MessageAttachment, ...] = ()
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    version: int
    content_hash: str
    provider_alias: str
    model_alias: str
    model_id: str
    pipeline_id: str
    prompt_id: str
    resolved: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved", MappingProxyType(dict(self.resolved)))


@dataclass(slots=True)
class Conversation:
    configuration: ConfigurationSnapshot
    id: str = field(default_factory=lambda: str(uuid4()))
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    conversation_id: str
    response: str
    model: str
    usage: Usage
    configuration_version: int
    citations: tuple[str, ...] = ()
