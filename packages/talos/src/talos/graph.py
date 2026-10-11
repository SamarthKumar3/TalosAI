from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from talos.config import PipelineDefinition, TalosConfig
from talos.context_window import ContextBudget, fit_messages
from talos.domain import Message, MessageRole, Usage
from talos.errors import (
    ConfigurationError,
    ExecutionBudgetExceededError,
    RunCancelledError,
    TalosError,
)
from talos.guardrails import GuardrailService
from talos.knowledge import KnowledgeHit
from talos.providers.base import ModelProvider, ModelRequest
from talos.reliability import stream_with_retries
from talos.repository_index import RepositoryHit
from talos.tools.runtime import ToolError, ToolRegistry

DeltaCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[str, str, str, int, str], Awaitable[None]]
RetryCallback = Callable[[int, float, TalosError], Awaitable[None]]
CompactionCallback = Callable[[Sequence[Message], int], Awaitable[None]]


class _GraphState(TypedDict, total=False):
    user_input: str
    messages: Sequence[Message]
    response: str
    usage: Usage
    steps: int


@dataclass(frozen=True, slots=True)
class GraphResult:
    response: str
    usage: Usage
    citations: tuple[str, ...] = ()


class PipelineExecutor:
    """Execute sequential manifests directly; use LangGraph for branching manifests."""

    def __init__(self, pipeline: PipelineDefinition) -> None:
        self._pipeline = pipeline

    async def execute(
        self,
        *,
        config: TalosConfig,
        provider: ModelProvider,
        messages: Sequence[Message],
        user_input: str,
        emit_delta: DeltaCallback,
        cancelled: asyncio.Event,
        knowledge_hits: Sequence[KnowledgeHit] = (),
        repository_hits: Sequence[RepositoryHit] = (),
        tools: ToolRegistry | None = None,
        emit_tool: ToolCallback | None = None,
        emit_retry: RetryCallback | None = None,
        guardrails: GuardrailService | None = None,
        on_compact: CompactionCallback | None = None,
    ) -> GraphResult:
        context = _ExecutionContext(
            config=config,
            provider=provider,
            emit_delta=emit_delta,
            cancelled=cancelled,
            knowledge_hits=knowledge_hits,
            repository_hits=repository_hits,
            tools=tools,
            emit_tool=emit_tool,
            emit_retry=emit_retry,
            guardrails=guardrails,
            on_compact=on_compact,
        )
        nodes = _ExecutionNodes(context)
        state: _GraphState = {
            "user_input": user_input,
            "messages": messages,
            "response": "",
            "usage": Usage(),
            "steps": 0,
        }
        order = _sequential_order(self._pipeline)
        if order is None:
            result = await _compile_graph(self._pipeline, nodes).ainvoke(state)
        else:
            handlers = nodes.handlers()
            for kind in order:
                if kind not in handlers:
                    raise ConfigurationError(f"Unsupported pipeline node kind: {kind}")
            for kind in order:
                state.update(await handlers[kind](state))
            result = state
        return await _graph_result(result, context)


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    config: TalosConfig
    provider: ModelProvider
    emit_delta: DeltaCallback
    cancelled: asyncio.Event
    knowledge_hits: Sequence[KnowledgeHit]
    repository_hits: Sequence[RepositoryHit]
    tools: ToolRegistry | None
    emit_tool: ToolCallback | None
    emit_retry: RetryCallback | None
    guardrails: GuardrailService | None
    on_compact: CompactionCallback | None


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    response: str
    usage: Usage
    calls: dict[int, dict[str, str]]


class _ExecutionNodes:
    def __init__(self, context: _ExecutionContext) -> None:
        self.context = context

    async def input(self, state: _GraphState) -> dict[str, Any]:
        await asyncio.sleep(0)
        _check_cancelled(self.context.cancelled)
        if not state["user_input"].strip():
            raise ValueError("Input must not be empty")
        return {"steps": _next_step(state, self.context.config)}

    async def context_step(self, state: _GraphState) -> dict[str, Any]:
        await asyncio.sleep(0)
        _check_cancelled(self.context.cancelled)
        return {"steps": _next_step(state, self.context.config)}

    async def persist(self, state: _GraphState) -> dict[str, Any]:
        return await self.context_step(state)

    async def model(self, state: _GraphState) -> dict[str, Any]:
        request_messages = _request_messages(state, self.context)
        history_start = len(request_messages) - len(state["messages"])
        usage = Usage()
        steps = state.get("steps", 0)
        tool_calls_used = 0
        while True:
            _check_cancelled(self.context.cancelled)
            steps = _next_step({"steps": steps}, self.context.config)
            config = self.context.config
            budget = ContextBudget(
                window_tokens=min(
                    config.model.context_window_tokens or config.budgets.max_tokens_per_run,
                    config.budgets.max_tokens_per_run - usage.input_tokens - usage.output_tokens,
                ),
                output_tokens=config.model.max_output_tokens,
                safety_tokens=config.model.context_safety_margin_tokens,
            )
            tools = self.context.tools.model_specs() if self.context.tools is not None else ()
            summary_limit = min(
                config.memory.max_summary_characters, max(128, budget.input_tokens // 16)
            )
            request_messages, compacted, history_start = fit_messages(
                request_messages,
                tools,
                budget,
                history_start=history_start,
                summary_characters=summary_limit,
                retrieval_index=(history_start - 1)
                if self.context.knowledge_hits or self.context.repository_hits
                else None,
            )
            if compacted and self.context.on_compact is not None:
                await self.context.on_compact(compacted, summary_limit)
            turn = await _stream_turn(request_messages, usage, self.context)
            usage = turn.usage
            if not turn.calls:
                return await _complete_response(turn.response, usage, steps, self.context)
            registry = _tool_registry(turn, tool_calls_used, self.context)
            assistant_calls, tool_messages, blocked = await _execute_tool_calls(
                turn.calls, registry, self.context
            )
            if blocked is not None:
                return await _complete_response(blocked, usage, steps, self.context, inspect=False)
            tool_calls_used += len(turn.calls)
            request_messages.append(
                Message(MessageRole.ASSISTANT, "", tool_calls=tuple(assistant_calls))
            )
            request_messages.extend(tool_messages)

    def handlers(self) -> dict[str, Any]:
        return {
            "validated_input": self.input,
            "conversation_context": self.context_step,
            "knowledge_context": self.context_step,
            "chat_model_stream": self.model,
            "persist_response": self.persist,
        }


def _compile_graph(pipeline: PipelineDefinition, nodes: _ExecutionNodes) -> Any:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise ConfigurationError(
            "Branching pipelines need 'pip install talos-runtime[graph]'."
        ) from exc

    graph = StateGraph(_GraphState)
    handlers = nodes.handlers()
    node_ids = {node.id for node in pipeline.nodes}
    incoming = {target for _, target in pipeline.edges}
    outgoing = {source for source, _ in pipeline.edges}
    starts = node_ids - incoming
    ends = node_ids - outgoing
    if len(starts) != 1 or len(ends) != 1:
        raise ConfigurationError("Phase 1 pipelines must have one start and one end node")
    for node in pipeline.nodes:
        handler = handlers.get(node.kind)
        if handler is None:
            raise ConfigurationError(f"Unsupported pipeline node kind: {node.kind}")
        graph.add_node(node.id, handler)
    graph.add_edge(START, next(iter(starts)))
    for source, target in pipeline.edges:
        graph.add_edge(source, target)
    graph.add_edge(next(iter(ends)), END)
    return graph.compile()


def _sequential_order(pipeline: PipelineDefinition) -> tuple[str, ...] | None:
    """Only optimize a complete, acyclic chain; preserve graph semantics otherwise."""
    kinds = {node.id: node.kind for node in pipeline.nodes}
    if len(kinds) != len(pipeline.nodes) or len(pipeline.edges) != len(kinds) - 1:
        return None
    successors: dict[str, str] = {}
    incoming: set[str] = set()
    for source, target in pipeline.edges:
        if source not in kinds or target not in kinds or source in successors or target in incoming:
            return None
        successors[source] = target
        incoming.add(target)
    starts = set(kinds) - incoming
    if len(starts) != 1:
        return None
    current = next(iter(starts))
    visited: set[str] = set()
    order: list[str] = []
    while current not in visited:
        visited.add(current)
        order.append(kinds[current])
        if current not in successors:
            return tuple(order) if len(visited) == len(kinds) else None
        current = successors[current]
    return None


def _request_messages(state: _GraphState, context: _ExecutionContext) -> list[Message]:
    messages = [
        Message(MessageRole.SYSTEM, context.config.system_prompt),
        *[Message(MessageRole.SYSTEM, skill.instructions) for skill in context.config.skills],
    ]
    has_attachments = any(
        message.role is MessageRole.USER and bool(message.attachments)
        for message in state["messages"]
    )
    context_message = _retrieval_context(context, has_attachments=has_attachments)
    if context_message is not None:
        messages.append(context_message)
    messages.extend(state["messages"])
    return messages


def _retrieval_context(
    context: _ExecutionContext, *, has_attachments: bool = False
) -> Message | None:
    if not context.knowledge_hits and not context.repository_hits and not has_attachments:
        return None
    attachment_notice = (
        "\n\nThe current user message includes a conversation-scoped document attachment. "
        "Do not claim that no document is attached. If retrieval returned no useful excerpts, "
        "say that the available excerpts are insufficient."
        if has_attachments
        else ""
    )
    if not context.knowledge_hits and not context.repository_hits:
        return Message(MessageRole.SYSTEM, attachment_notice.strip())
    knowledge = "\n\n".join(
        f"[{hit.marker}] {_knowledge_reference(hit)}\n{hit.chunk.text}"
        for hit in context.knowledge_hits
    )
    repository = "\n\n".join(
        f"[{hit.marker}] {hit.path}:{hit.start_line}-{hit.end_line} "
        f"(commit {hit.commit}, snapshot {hit.snapshot}"
        f"{', dirty' if hit.dirty else ''})\n{hit.text}"
        for hit in context.repository_hits
    )
    separator = "\n\n" if knowledge and repository else ""
    return Message(
        MessageRole.SYSTEM,
        context.config.knowledge_prompt.format(context=knowledge + separator + repository)
        + attachment_notice,
    )


async def _stream_turn(
    messages: Sequence[Message], usage: Usage, context: _ExecutionContext
) -> _ModelTurn:
    request = ModelRequest(
        model=context.config.model.model_id,
        messages=messages,
        max_output_tokens=context.config.model.max_output_tokens,
        temperature=context.config.model.temperature,
        tools=context.tools.model_specs() if context.tools is not None else (),
    )
    response = ""
    calls: dict[int, dict[str, str]] = {}
    async for chunk in stream_with_retries(
        context.provider,
        request,
        context.config.model_retry,
        cancelled=context.cancelled,
        on_retry=context.emit_retry,
    ):
        _check_cancelled(context.cancelled)
        response += chunk.text
        usage = _updated_usage(usage, chunk.usage, context.config)
        for delta in chunk.tool_calls:
            call = calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
            call["id"] += delta.id
            call["name"] += delta.name
            call["arguments"] += delta.arguments
    return _ModelTurn(response, usage, calls)


def _updated_usage(usage: Usage, added: Usage | None, config: TalosConfig) -> Usage:
    if added is None:
        return usage
    updated = Usage(
        usage.input_tokens + added.input_tokens,
        usage.output_tokens + added.output_tokens,
    )
    if updated.input_tokens + updated.output_tokens > config.budgets.max_tokens_per_run:
        raise ExecutionBudgetExceededError("The run exceeded its configured token budget")
    return updated


async def _complete_response(
    response: str,
    usage: Usage,
    steps: int,
    context: _ExecutionContext,
    *,
    inspect: bool = True,
) -> dict[str, Any]:
    if inspect and context.guardrails is not None:
        decision = context.guardrails.inspect(response, stage="output")
        if decision.blocked:
            response = decision.message or context.guardrails.runtime.block_message
    await context.emit_delta(response)
    return {"response": response, "usage": usage, "steps": steps}


def _tool_registry(
    turn: _ModelTurn, tool_calls_used: int, context: _ExecutionContext
) -> ToolRegistry:
    if turn.response:
        raise ConfigurationError("Provider mixed visible text and tool calls")
    if (
        context.tools is None
        or tool_calls_used + len(turn.calls) > context.config.budgets.max_tool_calls_per_run
    ):
        raise ExecutionBudgetExceededError("Maximum tool calls reached")
    return context.tools


async def _execute_tool_calls(
    calls: dict[int, dict[str, str]],
    tools: ToolRegistry,
    context: _ExecutionContext,
) -> tuple[list[dict[str, Any]], list[Message], str | None]:
    assistant_calls: list[dict[str, Any]] = []
    tool_messages: list[Message] = []
    for index in sorted(calls):
        call = calls[index]
        _validate_tool_call(call, context.config)
        arguments = _tool_arguments(call["arguments"])
        blocked = _guarded_value(arguments, "tool_input", context.guardrails)
        if blocked is not None:
            return assistant_calls, tool_messages, blocked
        result = await _invoke_tool(call, arguments, tools, context)
        assistant_calls.append(_assistant_tool_call(call))
        tool_message = Message(
            MessageRole.TOOL,
            json.dumps(result, ensure_ascii=False),
            tool_call_id=call["id"],
        )
        tool_messages.append(tool_message)
        blocked = _guarded_text(tool_message.content, "tool_output", context.guardrails)
        if blocked is not None:
            return assistant_calls, tool_messages, blocked
    return assistant_calls, tool_messages, None


def _validate_tool_call(call: dict[str, str], config: TalosConfig) -> None:
    if (
        not call["id"]
        or not call["name"]
        or len(call["arguments"]) > config.tool_max_response_bytes
    ):
        raise ConfigurationError("Provider returned an invalid tool call")


def _tool_arguments(value: str) -> dict[str, Any]:
    try:
        arguments = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("Provider returned invalid tool arguments") from exc
    if not isinstance(arguments, dict):
        raise ConfigurationError("Provider tool arguments must be an object")
    return arguments


def _guarded_value(
    value: dict[str, Any], stage: str, guardrails: GuardrailService | None
) -> str | None:
    return _guarded_text(json.dumps(value, ensure_ascii=False, sort_keys=True), stage, guardrails)


def _guarded_text(value: str, stage: str, guardrails: GuardrailService | None) -> str | None:
    if guardrails is None:
        return None
    decision = guardrails.inspect(value, stage=stage)  # type: ignore[arg-type]
    return decision.message or guardrails.runtime.block_message if decision.blocked else None


async def _invoke_tool(
    call: dict[str, str],
    arguments: dict[str, Any],
    tools: ToolRegistry,
    context: _ExecutionContext,
) -> Any:
    argument_hash = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    version = tools.version(call["name"])
    started = time.perf_counter()
    try:
        result = await tools.call(call["name"], arguments)
    except ToolError:
        await _emit_tool_result(call["name"], argument_hash, version, started, "failed", context)
        raise
    await _emit_tool_result(call["name"], argument_hash, version, started, "completed", context)
    return result


async def _emit_tool_result(
    name: str,
    argument_hash: str,
    version: str,
    started: float,
    status: str,
    context: _ExecutionContext,
) -> None:
    if context.emit_tool is not None:
        await context.emit_tool(
            name,
            argument_hash,
            version,
            int((time.perf_counter() - started) * 1000),
            status,
        )


def _assistant_tool_call(call: dict[str, str]) -> dict[str, Any]:
    return {
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": call["arguments"]},
    }


async def _graph_result(result: _GraphState, context: _ExecutionContext) -> GraphResult:
    referenced = set(re.findall(r"\[[KR]\d+\]", result["response"]))
    valid = {f"[{hit.marker}]" for hit in context.knowledge_hits}
    valid.update(f"[{hit.marker}]" for hit in context.repository_hits)
    if referenced - valid:
        raise ConfigurationError("Model cited a source that was not supplied")
    blocked = context.guardrails is not None and result["response"].startswith(
        context.guardrails.runtime.block_message
    )
    if context.config.rag.mode == "advanced" and valid and not referenced and not blocked:
        referenced = valid
    response = result["response"]
    if referenced:
        source_block = _source_block(referenced, context)
        response += source_block
        await context.emit_delta(source_block)
    return GraphResult(response, result["usage"], tuple(sorted(referenced)))


def _source_block(referenced: set[str], context: _ExecutionContext) -> str:
    references = {f"[{hit.marker}]": _knowledge_reference(hit) for hit in context.knowledge_hits}
    references.update(
        {
            f"[{hit.marker}]": (
                f"{hit.path}:{hit.start_line}-{hit.end_line} "
                f"(commit {hit.commit}, snapshot {hit.snapshot})"
            )
            for hit in context.repository_hits
        }
    )
    return "\n\nSources:\n" + "\n".join(
        f"- {marker} {references[marker]}" for marker in sorted(referenced)
    )


def _next_step(state: _GraphState, config: TalosConfig) -> int:
    next_step = state.get("steps", 0) + 1
    if next_step > config.budgets.max_agent_steps:
        raise ExecutionBudgetExceededError("Maximum agent steps reached")
    return next_step


def _knowledge_reference(hit: KnowledgeHit) -> str:
    if hit.chunk.page_number is not None:
        return f"{hit.chunk.source_name}:page {hit.chunk.page_number}"
    return f"{hit.chunk.source_name}:{hit.chunk.start_line}-{hit.chunk.end_line}"


def _check_cancelled(cancelled: asyncio.Event) -> None:
    if cancelled.is_set():
        raise RunCancelledError("The agent run was cancelled")
