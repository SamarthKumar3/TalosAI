from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from talos.errors import ConfigurationError, RunCancelledError, TalosError

if TYPE_CHECKING:
    from talos.providers.base import ModelChunk, ModelRequest


class _StreamingProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]: ...


RetryCallback = Callable[[int, float, TalosError], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ModelRetrySettings:
    max_attempts: int
    base_delay_milliseconds: int
    max_delay_milliseconds: int
    jitter_ratio: float

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ConfigurationError("Model attempts must be between 1 and 10")
        if not 0 <= self.base_delay_milliseconds <= self.max_delay_milliseconds <= 60000:
            raise ConfigurationError("Model retry delays are invalid")
        if not 0 <= self.jitter_ratio <= 1:
            raise ConfigurationError("Model retry jitter must be between 0 and 1")


async def stream_with_retries(
    provider: _StreamingProvider,
    request: ModelRequest,
    settings: ModelRetrySettings,
    *,
    cancelled: asyncio.Event,
    on_retry: RetryCallback | None = None,
) -> AsyncIterator[ModelChunk]:
    """Retry transient model failures only before a chunk has been accepted."""
    for attempt in range(1, settings.max_attempts + 1):
        emitted = False
        try:
            async for chunk in provider.stream(request):
                emitted = True
                yield chunk
            return
        except TalosError as exc:
            if emitted or not exc.retryable or attempt >= settings.max_attempts:
                raise
            delay = _retry_delay(settings, attempt)
            if on_retry is not None:
                await on_retry(attempt + 1, delay, exc)
            try:
                await asyncio.wait_for(cancelled.wait(), timeout=delay)
            except TimeoutError:
                continue
            raise RunCancelledError("The agent run was cancelled") from None


def _retry_delay(settings: ModelRetrySettings, failed_attempt: int) -> float:
    base = min(
        settings.base_delay_milliseconds * (2 ** (failed_attempt - 1)),
        settings.max_delay_milliseconds,
    )
    if base == 0 or settings.jitter_ratio == 0:
        return float(base) / 1000
    spread = base * settings.jitter_ratio
    return max(0.0, random.uniform(base - spread, base + spread)) / 1000
