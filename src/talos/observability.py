from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from typing import Any, Protocol

from talos.errors import ConfigurationError
from talos.events import EventType, RuntimeEvent

_ALERT_RUNBOOK = "README.md#when-something-goes-wrong"


@dataclass(frozen=True, slots=True)
class CostRates:
    currency: str
    input_per_million: float
    output_per_million: float
    version: str
    observed_on: str

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        value = (
            input_tokens * self.input_per_million + output_tokens * self.output_per_million
        ) / 1_000_000
        return round(value, 8)


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    tracing_enabled: bool
    usage_tracking_enabled: bool
    retention_max_runs: int
    alert_window_runs: int
    alert_minimum_runs: int
    failure_rate_threshold: float
    slow_run_milliseconds: int
    exporter_enabled: bool
    service_name: str
    exporter_endpoint: str
    sampler_name: str
    sample_ratio: float

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ObservabilitySettings:
        return cls(
            tracing_enabled=_boolean(environment, "TALOS_TRACING_ENABLED"),
            usage_tracking_enabled=_boolean(environment, "TALOS_USAGE_TRACKING_ENABLED"),
            retention_max_runs=_positive_integer(environment, "TALOS_TRACE_RETENTION_MAX_RUNS"),
            alert_window_runs=_positive_integer(environment, "TALOS_ALERT_WINDOW_RUNS"),
            alert_minimum_runs=_positive_integer(environment, "TALOS_ALERT_MINIMUM_RUNS"),
            failure_rate_threshold=_ratio(environment, "TALOS_ALERT_FAILURE_RATE_THRESHOLD"),
            slow_run_milliseconds=_positive_integer(
                environment, "TALOS_ALERT_SLOW_RUN_MILLISECONDS"
            ),
            exporter_enabled=_boolean(environment, "TALOS_OTEL_EXPORTER_ENABLED"),
            service_name=_required(environment, "OTEL_SERVICE_NAME"),
            exporter_endpoint=_required(environment, "OTEL_EXPORTER_OTLP_ENDPOINT"),
            sampler_name=_sampler(environment),
            sample_ratio=_ratio(environment, "OTEL_TRACES_SAMPLER_ARG"),
        )


class ObservableEventStore(Protocol):
    async def list_for_run(self, run_id: str, *, after_sequence: int = 0) -> list[RuntimeEvent]: ...

    async def list_recent_run_ids(self, *, limit: int) -> list[str]: ...


class ObservabilityService:
    def __init__(
        self,
        store: ObservableEventStore,
        settings: ObservabilitySettings,
    ) -> None:
        self._store = store
        self.settings = settings

    async def list_traces(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = min(max(limit, 1), min(self.settings.retention_max_runs, 100))
        run_ids = await self._store.list_recent_run_ids(limit=bounded)
        traces = [await self.trace(run_id) for run_id in run_ids]
        return [trace for trace in traces if trace is not None]

    async def trace_page(
        self,
        *,
        page: int = 1,
        page_size: int = 15,
        model: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        cost_sort: str | None = None,
    ) -> dict[str, Any]:
        """Return a filtered trace page without exposing raw event payloads."""
        bounded_page = max(1, page)
        bounded_size = min(max(1, page_size), 100)
        traces = await self._retained_traces()
        filtered = [
            trace
            for trace in traces
            if (model is None or trace["model"] == model)
            and (provider is None or trace["provider"] == provider)
            and (status is None or trace["state"] == status)
        ]
        if cost_sort in {"asc", "desc"}:
            known = [trace for trace in filtered if trace["usage"]["estimated_cost"] is not None]
            unknown = [trace for trace in filtered if trace["usage"]["estimated_cost"] is None]
            known.sort(
                key=lambda trace: float(trace["usage"]["estimated_cost"]),
                reverse=cost_sort == "desc",
            )
            filtered = known + unknown
        total_items = len(filtered)
        start = (bounded_page - 1) * bounded_size
        total_pages = ceil(total_items / bounded_size) if total_items else 0
        return {
            "traces": filtered[start : start + bounded_size],
            "pagination": {
                "page": bounded_page,
                "page_size": bounded_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_previous": bounded_page > 1,
                "has_next": bounded_page < total_pages,
            },
        }

    async def trace(self, run_id: str) -> dict[str, Any] | None:
        events = await self._store.list_for_run(run_id)
        if not events:
            return None
        ordered = sorted(events, key=lambda event: event.sequence)
        started = ordered[0]
        trace_id = str(started.payload.get("trace_id") or _trace_id(run_id))
        terminal = _terminal_event(ordered)
        state = _run_state(terminal)
        end_time = terminal.occurred_at if terminal is not None else ordered[-1].occurred_at
        spans = _trace_spans(ordered)
        tool_events = [
            event
            for event in ordered
            if event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_FAILED}
        ]
        spans.extend(_tool_span(event) for event in tool_events)
        usage = _usage_from_events(ordered)
        retry_count = sum(event.type is EventType.MODEL_RETRYING for event in ordered)
        retrieval_count = sum(
            _retrieval_returned(event)
            for event in ordered
            if event.type is EventType.RETRIEVAL_COMPLETED
        )
        error = _run_error(terminal)
        root_span_id = str(started.payload.get("root_span_id", ""))
        _complete_span_identity(spans, trace_id, root_span_id)
        spans.insert(0, _root_span(started, trace_id, root_span_id, state, end_time))
        return {
            "trace_id": trace_id,
            "correlation_id": str(started.payload.get("correlation_id", trace_id)),
            "run_id": run_id,
            "conversation_id": started.conversation_id,
            "configuration_version": int(started.payload.get("configuration_version", 0)),
            "configuration_hash": str(started.payload.get("configuration_hash", "")),
            "provider": str(started.payload.get("provider", "unknown")),
            "model": str(started.payload.get("model", "unknown")),
            "state": state,
            "started_at": started.occurred_at.isoformat(),
            "finished_at": terminal.occurred_at.isoformat() if terminal is not None else None,
            "duration_ms": max(0, round((end_time - started.occurred_at).total_seconds() * 1000)),
            "message_count": 2
            if any(event.type is EventType.RESPONSE_COMPLETED for event in ordered)
            else 1,
            "spans": sorted(spans, key=lambda span: (span["started_at"], span["name"])),
            "usage": usage,
            "retrieval_count": retrieval_count,
            "tool_calls": len(tool_events),
            "retries": retry_count,
            "cancelled": state == "cancelled",
            "error": error,
        }

    async def usage(
        self,
        *,
        days: int = 10,
        start_date: date | None = None,
        end_date: date | None = None,
        group_by: str = "time",
        metric: str = "tokens",
    ) -> dict[str, Any]:
        present = await self._retained_traces()
        range_end = end_date or datetime.now(UTC).date()
        range_start = start_date or (range_end - timedelta(days=days - 1))
        if range_start > range_end:
            raise ValueError("Usage start date must not be after end date")
        if (range_end - range_start).days > 365:
            raise ValueError("Usage date range cannot exceed 366 days")
        first = datetime.combine(range_start, time.min, UTC)
        last = datetime.combine(range_end + timedelta(days=1), time.min, UTC)
        present = [
            trace
            for trace in present
            if first <= datetime.fromisoformat(trace["started_at"]) < last
        ]
        input_tokens = sum(int(trace["usage"]["input_tokens"]) for trace in present)
        output_tokens = sum(int(trace["usage"]["output_tokens"]) for trace in present)
        known_costs = [
            float(trace["usage"]["estimated_cost"])
            for trace in present
            if trace["usage"]["estimated_cost"] is not None
        ]
        currencies = {
            str(trace["usage"]["currency"])
            for trace in present
            if trace["usage"]["currency"] is not None
        }
        calendar_days = [
            range_start + timedelta(days=offset)
            for offset in range((range_end - range_start).days + 1)
        ]
        daily = [_daily_point(day, present) for day in calendar_days]
        total_tokens = input_tokens + output_tokens
        model_calls = sum(int(trace["usage"]["model_calls"]) for trace in present)
        total_cost = round(sum(known_costs), 8) if known_costs else None
        sessions = len({trace["conversation_id"] for trace in present})
        messages = sum(int(trace["message_count"]) for trace in present)
        durations = [int(trace["duration_ms"]) for trace in present]
        peak = max(daily, key=lambda point: point["total_tokens"], default=None)
        grouped: dict[str, dict[str, float]] = {}
        for trace in present:
            key = {
                "model": trace["model"],
                "provider": trace["provider"],
            }.get(group_by, "Usage")
            day = str(trace["started_at"])[:10]
            value = (
                float(trace["usage"]["estimated_cost"] or 0)
                if metric == "cost"
                else int(trace["usage"]["input_tokens"]) + int(trace["usage"]["output_tokens"])
            )
            grouped.setdefault(key, {})[day] = grouped.setdefault(key, {}).get(day, 0) + value
        return {
            "runs": len(present),
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "embedding_requests": sum(
                int(trace["usage"]["embedding_requests"]) for trace in present
            ),
            "tool_executions": sum(int(trace["tool_calls"]) for trace in present),
            "retrieval_requests": sum(1 for trace in present if int(trace["retrieval_count"]) > 0),
            "estimated_cost": total_cost,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "cost_status": "estimated" if known_costs and len(currencies) == 1 else "unknown",
            "rate_versions": sorted(
                {
                    str(trace["usage"]["rate_version"])
                    for trace in present
                    if trace["usage"]["rate_version"] is not None
                }
            ),
            "summary": {
                "sessions": sessions,
                "messages": messages,
                "total_tokens": total_tokens,
                "average_latency_ms": round(sum(durations) / len(durations)) if durations else 0,
                "average_cost_per_call": (
                    round(total_cost / model_calls, 8)
                    if total_cost is not None and model_calls
                    else None
                ),
                "daily_average_cost": (
                    round(total_cost / len(calendar_days), 8) if total_cost is not None else None
                ),
                "daily_average_tokens": round(total_tokens / len(calendar_days), 2),
                "average_tokens_per_call": round(total_tokens / model_calls, 2)
                if model_calls
                else 0,
                "peak_usage_date": peak["date"] if peak and peak["total_tokens"] else None,
                "peak_usage_tokens": peak["total_tokens"] if peak else 0,
            },
            "daily": daily,
            "statuses": _named_totals(present, "state"),
            "models": _model_totals(present),
            "usage_series": [
                {
                    "name": name,
                    "points": [
                        {"date": day.isoformat(), "value": values.get(day.isoformat(), 0)}
                        for day in calendar_days
                    ],
                }
                for name, values in sorted(grouped.items())
            ],
            "filter_options": {
                "models": sorted({str(trace["model"]) for trace in present}),
                "providers": sorted({str(trace["provider"]) for trace in present}),
                "statuses": sorted({str(trace["state"]) for trace in present}),
            },
        }

    async def _retained_traces(self) -> list[dict[str, Any]]:
        run_ids = await self._store.list_recent_run_ids(limit=self.settings.retention_max_runs)
        traces = [await self.trace(run_id) for run_id in run_ids]
        return [trace for trace in traces if trace is not None]

    async def alerts(self) -> list[dict[str, Any]]:
        traces = await self.list_traces(limit=self.settings.alert_window_runs)
        if not traces:
            return []
        alerts: list[dict[str, Any]] = []
        failures = [trace for trace in traces if trace["state"] == "failed"]
        failure_rate = len(failures) / len(traces)
        if (
            len(traces) >= self.settings.alert_minimum_runs
            and failure_rate >= self.settings.failure_rate_threshold
        ):
            alerts.append(
                {
                    "code": "sustained_run_failures",
                    "severity": "critical",
                    "message": f"{len(failures)} of the last {len(traces)} runs failed.",
                    "runbook": _ALERT_RUNBOOK,
                }
            )
        auth_failures = [
            trace
            for trace in failures
            if trace["error"] and trace["error"]["code"] == "provider_authentication_error"
        ]
        if auth_failures:
            alerts.append(
                {
                    "code": "provider_authentication_failure",
                    "severity": "critical",
                    "message": "The model provider rejected its configured credential.",
                    "runbook": _ALERT_RUNBOOK,
                }
            )
        slow = [
            trace
            for trace in traces
            if int(trace["duration_ms"]) >= self.settings.slow_run_milliseconds
        ]
        if slow:
            alerts.append(
                {
                    "code": "slow_agent_runs",
                    "severity": "warning",
                    "message": (
                        f"{len(slow)} recent runs exceeded the configured duration threshold."
                    ),
                    "runbook": _ALERT_RUNBOOK,
                }
            )
        return alerts


def _terminal_event(events: list[RuntimeEvent]) -> RuntimeEvent | None:
    terminal_types = {EventType.RUN_COMPLETED, EventType.RUN_CANCELLED, EventType.RUN_FAILED}
    return next((event for event in reversed(events) if event.type in terminal_types), None)


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _daily_point(day: date, traces: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [trace for trace in traces if str(trace["started_at"])[:10] == day.isoformat()]
    input_tokens = sum(int(trace["usage"]["input_tokens"]) for trace in selected)
    output_tokens = sum(int(trace["usage"]["output_tokens"]) for trace in selected)
    costs = [
        float(trace["usage"]["estimated_cost"])
        for trace in selected
        if trace["usage"]["estimated_cost"] is not None
    ]
    durations = [int(trace["duration_ms"]) for trace in selected]
    return {
        "date": day.isoformat(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost": round(sum(costs), 8) if costs else 0,
        "messages": sum(int(trace["message_count"]) for trace in selected),
        "average_latency_ms": round(sum(durations) / len(durations)) if durations else 0,
        "p90_latency_ms": _percentile(durations, 0.9),
        "p95_latency_ms": _percentile(durations, 0.95),
        "p99_latency_ms": _percentile(durations, 0.99),
    }


def _named_totals(traces: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for trace in traces:
        name = str(trace[field])
        totals[name] = totals.get(name, 0) + 1
    return [{"name": name, "value": value} for name, value in sorted(totals.items())]


def _model_totals(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for trace in traces:
        name = str(trace["model"])
        total = int(trace["usage"]["input_tokens"]) + int(trace["usage"]["output_tokens"])
        totals[name] = totals.get(name, 0) + total
    return [{"name": name, "value": value} for name, value in sorted(totals.items())]


def _run_state(terminal: RuntimeEvent | None) -> str:
    if terminal is None:
        return "running"
    return {
        EventType.RUN_COMPLETED: "completed",
        EventType.RUN_CANCELLED: "cancelled",
        EventType.RUN_FAILED: "failed",
    }[terminal.type]


def _trace_spans(events: list[RuntimeEvent]) -> list[dict[str, Any]]:
    projections = (
        _span_projection(event) for event in events if event.type is EventType.SPAN_COMPLETED
    )
    return [projection for projection in projections if projection is not None]


def _run_error(terminal: RuntimeEvent | None) -> dict[str, object] | None:
    if terminal is None or terminal.type is not EventType.RUN_FAILED:
        return None
    return {
        "code": str(terminal.payload.get("code", "unknown")),
        "message": str(terminal.payload.get("message", "Run failed")),
        "retryable": bool(terminal.payload.get("retryable", False)),
    }


def _complete_span_identity(spans: list[dict[str, Any]], trace_id: str, root_span_id: str) -> None:
    for span in spans:
        if not span["trace_id"]:
            span["trace_id"] = trace_id
        if span["parent_span_id"] is None:
            span["parent_span_id"] = root_span_id


def _root_span(
    started: RuntimeEvent,
    trace_id: str,
    root_span_id: str,
    state: str,
    end_time: datetime,
) -> dict[str, Any]:
    return {
        "name": "agent.run",
        "trace_id": trace_id,
        "correlation_id": str(started.payload.get("correlation_id", trace_id)),
        "span_id": root_span_id,
        "parent_span_id": None,
        "started_at": started.occurred_at.isoformat(),
        "duration_ms": max(0, round((end_time - started.occurred_at).total_seconds() * 1000)),
        "status": "error" if state == "failed" else state,
        "attributes": {
            "configuration.version": int(started.payload.get("configuration_version", 0)),
            "provider.alias": str(started.payload.get("provider", "unknown")),
            "model.id": str(started.payload.get("model", "unknown")),
        },
    }


def span_payload(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    started_at: datetime,
    duration_ms: int,
    status: str,
    attributes: Mapping[str, object],
) -> dict[str, object]:
    return {
        "name": name,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "started_at": started_at.isoformat(),
        "duration_ms": max(0, duration_ms),
        "status": status,
        "attributes": _safe_attributes(attributes),
    }


def _span_projection(event: RuntimeEvent) -> dict[str, Any] | None:
    payload = event.payload
    if not isinstance(payload.get("attributes", {}), Mapping):
        return None
    return {
        "name": str(payload.get("name", "unknown")),
        "trace_id": str(payload.get("trace_id", "")),
        "span_id": str(payload.get("span_id", "")),
        "parent_span_id": (
            str(payload["parent_span_id"]) if payload.get("parent_span_id") else None
        ),
        "started_at": str(payload.get("started_at", event.occurred_at.isoformat())),
        "duration_ms": max(0, int(payload.get("duration_ms", 0))),
        "status": str(payload.get("status", "ok")),
        "attributes": dict(payload.get("attributes", {})),
    }


def _tool_span(event: RuntimeEvent) -> dict[str, Any]:
    duration = max(0, int(event.payload.get("duration_ms", 0)))
    return {
        "name": "tool.call",
        "trace_id": "",
        "span_id": event.event_id.replace("-", "")[:16],
        "parent_span_id": None,
        "started_at": event.occurred_at.isoformat(),
        "duration_ms": duration,
        "status": "ok" if event.type is EventType.TOOL_COMPLETED else "error",
        "attributes": _safe_attributes(
            {
                "tool.name": event.payload.get("name"),
                "tool.version": event.payload.get("version"),
                "tool.outcome": event.payload.get("outcome"),
            }
        ),
    }


def _usage_from_events(events: list[RuntimeEvent]) -> dict[str, Any]:
    completed = next(
        (event for event in reversed(events) if event.type is EventType.RESPONSE_COMPLETED),
        None,
    )
    started = events[0]
    raw_usage = completed.payload.get("usage", {}) if completed is not None else {}
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    input_tokens = max(0, int(usage.get("input_tokens", 0)))
    output_tokens = max(0, int(usage.get("output_tokens", 0)))
    rate = started.payload.get("cost_rate")
    estimate = None
    currency = None
    rate_version = None
    observed_on = None
    if isinstance(rate, Mapping):
        currency = str(rate.get("currency", "")) or None
        rate_version = str(rate.get("version", "")) or None
        observed_on = str(rate.get("observed_on", "")) or None
        try:
            estimate = round(
                (
                    input_tokens * float(rate["input_per_million"])
                    + output_tokens * float(rate["output_per_million"])
                )
                / 1_000_000,
                8,
            )
        except (KeyError, TypeError, ValueError):
            estimate = None
    return {
        "model_calls": 1 if completed is not None and input_tokens + output_tokens > 0 else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "embedding_requests": sum(
            1
            for event in events
            if event.type is EventType.SPAN_COMPLETED
            and str(event.payload.get("name", "")).startswith("retrieval.")
        ),
        "estimated_cost": estimate,
        "currency": currency,
        "rate_version": rate_version,
        "rate_observed_on": observed_on,
        "cost_status": "estimated" if estimate is not None else "unknown",
    }


def _retrieval_returned(event: RuntimeEvent) -> int:
    total = 0
    for key in ("knowledge", "repository"):
        item = event.payload.get(key)
        if isinstance(item, Mapping):
            total += max(0, int(item.get("returned", 0)))
    return total


def _safe_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in attributes.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key[:100]] = value if not isinstance(value, str) else value[:300]
    return safe


def _trace_id(run_id: str) -> str:
    import hashlib

    return hashlib.sha256(run_id.encode()).hexdigest()[:32]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return value


def _positive_integer(environment: Mapping[str, str], name: str) -> int:
    try:
        value = int(_required(environment, name))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _sampler(environment: Mapping[str, str]) -> str:
    value = _required(environment, "OTEL_TRACES_SAMPLER").lower()
    if value != "parentbased_traceidratio":
        raise ConfigurationError("OTEL_TRACES_SAMPLER must be parentbased_traceidratio")
    return value


def _ratio(environment: Mapping[str, str], name: str) -> float:
    try:
        value = float(_required(environment, name))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not 0 <= value <= 1:
        raise ConfigurationError(f"{name} must be between zero and one")
    return value


def _boolean(environment: Mapping[str, str], name: str) -> bool:
    value = _required(environment, name).lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return value == "true"
