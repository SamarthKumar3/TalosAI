from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from talos.domain import Message, Usage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    messages: Sequence[Message]
    max_output_tokens: int
    temperature: float
    tools: Sequence[dict[str, Any]] = ()


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class ModelChunk:
    text: str = ""
    usage: Usage | None = None
    finish_reason: str | None = None
    tool_calls: tuple[ToolCallDelta, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    usage: Usage
    finish_reason: str | None = None


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResult: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...
