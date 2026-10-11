"""Read-only execution debugging shared by SDK, CLI and HTTP adapters."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from talos.conversations import ConversationStore
from talos.domain import Message
from talos.errors import TalosError
from talos.event_store import RuntimeEventStore
from talos.events import EventType
from talos.guardrails import GuardrailService
from talos.observability import ObservabilityService


class WhyError(TalosError):
    code = "why_invalid_target"


def parse_why_offset(arguments: Sequence[str]) -> int:
    if not arguments:
        return -1
    if len(arguments) != 1 or not re.fullmatch(r"-[1-9][0-9]*", arguments[0]):
        raise WhyError("Use /why or /why -N, where N is a positive response number.")
    try:
        return int(arguments[0])
    except ValueError as exc:
        raise WhyError("The requested offset is too large to represent.") from exc


class WhyTarget(BaseModel):
    message_id: str
    run_id: str | None
    offset: int
    response_number: int
    created_at: str


class WhyHistory(BaseModel):
    conversation_id: str
    total_messages: int
    eligible_responses: int
    configured_minimum: int
    start: int
    has_more: bool
    targets: list[WhyTarget]


class WhySection(BaseModel):
    title: str
    status: str = "Observed"
    facts: list[str] = Field(default_factory=list)
    records: list[dict[str, Any]] = Field(default_factory=list)


class WhyReport(BaseModel):
    schema_version: int = 1
    conversation_id: str
    target: WhyTarget
    sections: list[WhySection]
    limitations: list[str]


def safe_evidence(value: Any, guardrails: GuardrailService, *, field: str | None = None) -> Any:
    """Filter already allowlisted metadata; never pass raw prompts/tool payloads here."""
    if isinstance(value, str):
        # Generated UUIDs and digests can contain digit runs resembling PII. Preserve only
        # these strictly shaped identifiers in explicitly allowlisted metadata fields.
        if field in {
            "conversation_id",
            "message_id",
            "run_id",
            "source_id",
            "chunk_id",
        } and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
            return value
        if field in {
            "hash",
            "configuration_hash",
            "argument_sha256",
            "checksum",
            "snapshot",
        } and re.fullmatch(r"[0-9a-f]{64}", value):
            return value
        if guardrails.inspect(value, stage="output").blocked:
            return "Redacted"
        if re.search(r"(?i)(bearer\s+|(?:api[_-]?key|password|secret|token)\s*[:=])", value):
            return "Redacted"
        return value[:500]
    if isinstance(value, Mapping):
        return {str(k): safe_evidence(v, guardrails, field=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_evidence(v, guardrails) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "Unavailable"


def _event_record(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    record = _project(payload, keys)
    for kind in ("knowledge", "repository"):
        if isinstance(record.get(kind), Mapping):
            record[kind] = _project(
                record[kind],
                (
                    "mode",
                    "query_variants",
                    "candidates",
                    "dense_candidates",
                    "sparse_candidates",
                    "returned",
                    "duration_ms",
                ),
            )
    if "sources" in record:
        record["sources"] = [
            _project(
                source,
                (
                    "marker",
                    "source_id",
                    "chunk_id",
                    "path",
                    "start_line",
                    "end_line",
                    "page_number",
                    "checksum",
                    "score",
                    "commit",
                    "dirty",
                    "snapshot",
                ),
            )
            for source in record["sources"]
            if isinstance(source, Mapping)
        ]
    return record


def _target(message: Message, index: int, count: int) -> WhyTarget:
    return WhyTarget(
        message_id=message.id,
        run_id=message.run_id,
        offset=-(index + 1),
        response_number=count - index,
        created_at=message.created_at.isoformat(),
    )


def _project(value: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value and value[key] is not None}


class WhyReportService:
    def __init__(
        self,
        conversations: ConversationStore,
        events: RuntimeEventStore,
        observability: ObservabilityService,
        guardrails: GuardrailService,
    ) -> None:
        self._conversations = conversations
        self._events = events
        self._observability = observability
        self._guardrails = guardrails

    async def history(self, conversation_id: str, *, start: int = 0, limit: int = 20) -> WhyHistory:
        if start < 0 or not 1 <= limit <= 100:
            raise WhyError("Response history requires start >= 0 and limit between 1 and 100.")
        _, total, count, messages = await self._conversations.response_history(
            conversation_id, start=start, limit=limit
        )
        return WhyHistory(
            conversation_id=conversation_id,
            total_messages=total,
            eligible_responses=count,
            configured_minimum=min(10, total),
            start=start,
            has_more=start + len(messages) < count,
            targets=[_target(m, start + i, count) for i, m in enumerate(messages)],
        )

    async def explain_conversation_offset(
        self, conversation_id: str, offset: int = -1, *, expected_message_id: str | None = None
    ) -> WhyReport:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset >= 0:
            raise WhyError("Use a negative offset: -1 is the latest Talos response.")
        snapshot, _, count, messages = await self._conversations.response_history(
            conversation_id, start=-offset - 1, limit=1
        )
        if count == 0:
            raise WhyError("There are no previous Talos responses to inspect in this conversation.")
        if not messages:
            raise WhyError(
                f"This conversation has {count} inspectable responses. "
                f"Valid offsets: -1 through -{count}. Use /why for the latest response."
            )
        target = _target(messages[0], -offset - 1, count)
        if expected_message_id is not None and target.message_id != expected_message_id:
            raise WhyError(
                "Response history changed. Select the response again before inspecting it."
            )
        sections = [
            WhySection(
                title="Configuration snapshot",
                records=[
                    {
                        "version": snapshot.version,
                        "hash": snapshot.content_hash,
                        "provider": snapshot.provider_alias,
                        "model": snapshot.model_id,
                        "pipeline": snapshot.pipeline_id,
                        "prompt_id": snapshot.prompt_id,
                    }
                ],
            )
        ]
        limitations = [
            "This report describes observed execution, not the model's hidden reasoning.",
            "Private prompts, source content, tool arguments and results are redacted or omitted.",
            "Current source contents and revision are not read or verified "
            "against the historical snapshot.",
        ]
        events = await self._events.list_for_run(target.run_id) if target.run_id else []
        # Fail closed if a corrupt link crosses a conversation boundary.
        if any(e.conversation_id != conversation_id for e in events):
            raise WhyError("The target run does not belong to this conversation.")
        trace = await self._observability.trace(target.run_id) if events and target.run_id else None
        if trace is None:
            sections.append(
                WhySection(
                    title="Run metadata",
                    status="Unavailable",
                    facts=["No retained execution evidence is associated with this response."],
                )
            )
            limitations.append("Legacy responses are not matched to runs by position or text.")
        else:
            sections.extend(
                [
                    WhySection(
                        title="Run metadata",
                        records=[
                            _project(
                                trace,
                                (
                                    "run_id",
                                    "state",
                                    "started_at",
                                    "finished_at",
                                    "duration_ms",
                                    "configuration_version",
                                    "configuration_hash",
                                    "provider",
                                    "model",
                                ),
                            )
                        ],
                    ),
                    WhySection(
                        title="Runtime steps",
                        records=[
                            _project(s, ("name", "status", "duration_ms")) for s in trace["spans"]
                        ],
                    ),
                    WhySection(
                        title="Tokens and estimated cost",
                        records=[
                            _project(
                                trace["usage"],
                                (
                                    "input_tokens",
                                    "output_tokens",
                                    "estimated_cost",
                                    "currency",
                                    "cost_status",
                                    "rate_version",
                                    "model_calls",
                                ),
                            )
                        ],
                    ),
                ]
            )
            for title, types, keys in (
                (
                    "Retrieval",
                    {EventType.RETRIEVAL_COMPLETED},
                    ("knowledge", "repository", "enabled"),
                ),
                (
                    "Selected context provenance",
                    {EventType.CONTEXT_RECORDED},
                    ("sources", "score_type"),
                ),
                (
                    "Tools",
                    {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED},
                    ("name", "version", "argument_sha256", "duration_ms", "outcome"),
                ),
                (
                    "Errors and retries",
                    {EventType.MODEL_RETRYING, EventType.RUN_FAILED, EventType.RUN_CANCELLED},
                    ("code", "attempt", "delay_milliseconds", "retryable"),
                ),
            ):
                records = [
                    _event_record(e.payload, keys)
                    for e in sorted(events, key=lambda e: e.sequence)
                    if e.type in types
                ]
                sections.append(
                    WhySection(
                        title=title,
                        status="Observed" if records else "Not recorded",
                        records=records,
                    )
                )
            citations = [
                str(c)
                for e in events
                if e.type is EventType.RESPONSE_COMPLETED
                for c in e.payload.get("citations", ())
                if isinstance(c, str) and re.fullmatch(r"\[[KR][0-9]+\]", c)
            ]
            sections.append(
                WhySection(
                    title="Explicitly cited sources",
                    facts=citations,
                    status="Observed" if citations else "Not recorded",
                )
            )
            sections.append(
                WhySection(
                    title="Observed execution explanation",
                    facts=[
                        f"The retained run is {trace['state']}; "
                        f"it recorded {trace['tool_calls']} tool outcomes "
                        f"and {trace['retrieval_count']} retrieval results.",
                        "Selected context metadata is distinct from candidate counts "
                        "and explicit citations.",
                    ],
                )
            )
        report = WhyReport(
            conversation_id=conversation_id,
            target=target,
            sections=sections,
            limitations=limitations,
        )
        return WhyReport.model_validate(safe_evidence(report.model_dump(), self._guardrails))
