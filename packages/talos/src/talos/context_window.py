from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from talos.domain import Message, MessageRole
from talos.errors import ExecutionBudgetExceededError


@dataclass(frozen=True, slots=True)
class ContextBudget:
    window_tokens: int
    output_tokens: int
    safety_tokens: int

    @property
    def input_tokens(self) -> int:
        return self.window_tokens - self.output_tokens - self.safety_tokens

    def __post_init__(self) -> None:
        if self.input_tokens <= 0:
            raise ValueError("The model context window must exceed output and safety reservations")


def token_upper_bound(messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()) -> int:
    """Conservative UTF-8 budget until a provider supplies its exact token count.

    Common byte-level BPE encodings cannot use more content tokens than UTF-8 bytes.
    Per-message and tool overhead leave additional room for provider framing.
    """
    total = 32
    for message in messages:
        total += 32 + len(message.content.encode("utf-8"))
        total += len(message.role.value.encode("utf-8"))
        total += len(json.dumps(message.tool_calls, ensure_ascii=False).encode("utf-8"))
        if message.tool_call_id:
            total += len(message.tool_call_id.encode("utf-8"))
    if tools:
        total += 64 + len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
    return total


def extractive_summary(messages: Sequence[Message], max_characters: int) -> str:
    source = "\n".join(
        f"{message.role.value}: {' '.join(message.content.split())}"
        for message in messages
        if message.role in {MessageRole.USER, MessageRole.ASSISTANT, MessageRole.SYSTEM}
    )
    return source[-max_characters:]


def fit_messages(
    messages: Sequence[Message],
    tools: Sequence[dict[str, Any]],
    budget: ContextBudget,
    *,
    history_start: int,
    summary_characters: int,
    retrieval_index: int | None = None,
) -> tuple[list[Message], list[Message], int]:
    """Keep pinned instructions and the newest user turn, compacting oldest history."""
    selected = list(messages)
    removed: list[Message] = []
    summary: Message | None = None
    while token_upper_bound(selected, tools) > budget.input_tokens:
        if summary is not None:
            selected.remove(summary)
            summary = None
        # The current user turn is last; never silently cut it or a pinned instruction.
        if len(selected) <= history_start + 1:
            if retrieval_index is not None:
                selected.pop(retrieval_index)
                history_start -= 1
                retrieval_index = None
                continue
            raise ExecutionBudgetExceededError(
                "The current request cannot fit in the configured model context window"
            )
        candidate = selected[history_start]
        if candidate.role in {MessageRole.TOOL} or candidate.tool_calls:
            raise ExecutionBudgetExceededError(
                "Tool context cannot fit in the configured model context window"
            )
        selected.pop(history_start)
        removed.append(candidate)
        text = extractive_summary(removed, summary_characters)
        summary = Message(MessageRole.SYSTEM, f"Compacted conversation context:\n{text}")
        selected.insert(history_start, summary)
        # If the summary itself takes too much space, reduce its bound before dropping
        # more messages. A configured minimum keeps this deterministic and bounded.
        if (
            len(selected) == history_start + 2
            and token_upper_bound(selected, tools) > budget.input_tokens
        ):
            selected.remove(summary)
            summary = None
    return selected, removed, history_start
