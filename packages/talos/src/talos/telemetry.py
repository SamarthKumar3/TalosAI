from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

from talos.observability import ObservabilitySettings

_configure_lock = Lock()
_configured = False


def configure_opentelemetry(settings: ObservabilitySettings) -> None:
    global _configured
    if _configured or not settings.tracing_enabled or not settings.exporter_enabled:
        return
    with _configure_lock:
        if _configured:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        except ImportError as exc:
            from talos.errors import ConfigurationError

            raise ConfigurationError(
                "External tracing needs 'pip install talos-runtime[telemetry]'. "
                "For Docker, build with --build-arg TALOS_RUNTIME_EXTRAS=service,telemetry."
            ) from exc
        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.service_name}),
            sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio)),
        )
        exporter = OTLPSpanExporter(
            endpoint=settings.exporter_endpoint,
            insecure=settings.exporter_endpoint.startswith("http://"),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True


class OpenTelemetryRecorder:
    def __init__(self, settings: ObservabilitySettings) -> None:
        configure_opentelemetry(settings)
        # Local traces use the runtime event store and require no telemetry libraries.
        self._tracer: Tracer | None = None
        if settings.tracing_enabled and settings.exporter_enabled:
            from opentelemetry import trace

            self._tracer = trace.get_tracer("talos.runtime")

    def record(
        self,
        name: str,
        *,
        started_at: datetime,
        duration_ms: int,
        attributes: Mapping[str, object],
        status: str,
    ) -> None:
        if self._tracer is None:
            return
        start_ns = int(started_at.timestamp() * 1_000_000_000)
        end_ns = int(
            (started_at + timedelta(milliseconds=max(0, duration_ms))).timestamp() * 1_000_000_000
        )
        span = self._tracer.start_span(
            name,
            start_time=start_ns,
            attributes={
                key: value
                for key, value in attributes.items()
                if isinstance(value, (str, bool, int, float))
            },
        )
        if status != "ok":
            span.set_attribute("talos.status", status)
        span.end(end_time=end_ns)
