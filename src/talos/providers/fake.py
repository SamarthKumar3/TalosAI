from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from talos.domain import Usage
from talos.providers.base import ModelChunk, ModelRequest, ModelResult


class FakeModelProvider:
    """Deterministic provider for tests, examples, and offline development."""

    def __init__(
        self,
        response: str | Callable[[ModelRequest], str] = "Talos is ready.",
        *,
        chunk_size: int = 8,
        usage: Usage | None = None,
        error: Exception | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        self._response = response
        self._chunk_size = chunk_size
        self._usage = usage or Usage(input_tokens=4, output_tokens=4)
        self._error = error
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResult:
        text = ""
        usage = Usage()
        finish_reason: str | None = None
        async for chunk in self.stream(request):
            text += chunk.text
            if chunk.usage is not None:
                usage = chunk.usage
            finish_reason = chunk.finish_reason or finish_reason
        return ModelResult(text=text, usage=usage, finish_reason=finish_reason)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        response = self._response(request) if callable(self._response) else self._response
        for offset in range(0, len(response), self._chunk_size):
            yield ModelChunk(text=response[offset : offset + self._chunk_size])
        yield ModelChunk(usage=self._usage, finish_reason="stop")
