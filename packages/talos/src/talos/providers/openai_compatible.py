from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from talos.domain import Usage
from talos.errors import ProviderAuthenticationError, ProviderError
from talos.providers.base import ModelChunk, ModelRequest, ModelResult, ToolCallDelta


class OpenAICompatibleProvider:
    """Streaming adapter for configured OpenAI-compatible HTTP endpoints."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ProviderAuthenticationError("The configured provider API key is missing")
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )

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
        payload = {
            "model": request.model,
            "messages": [_message_payload(message) for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = request.tools
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            async with client.stream(
                "POST",
                f"{self._api_base}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code in {401, 403}:
                    raise ProviderAuthenticationError("The model provider rejected the API key")
                if response.status_code == 429 or response.status_code >= 500:
                    raise ProviderError(
                        f"The model provider returned HTTP {response.status_code}", retryable=True
                    )
                if response.is_error:
                    raise ProviderError(f"The model provider returned HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    yield _parse_chunk(data)
        except httpx.TimeoutException as exc:
            raise ProviderError("The model provider request timed out", retryable=True) from exc
        except httpx.NetworkError as exc:
            raise ProviderError("The model provider could not be reached", retryable=True) from exc
        finally:
            if owns_client:
                await client.aclose()


def _parse_chunk(data: str) -> ModelChunk:
    try:
        value: dict[str, Any] = json.loads(data)
        choices = value.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if not isinstance(text, str):
            raise TypeError
        usage_data = value.get("usage")
        usage = None
        if isinstance(usage_data, dict):
            usage = Usage(
                input_tokens=int(usage_data.get("prompt_tokens", 0)),
                output_tokens=int(usage_data.get("completion_tokens", 0)),
            )
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise TypeError
        calls = []
        for item in delta.get("tool_calls") or []:
            function = item.get("function") or {}
            calls.append(
                ToolCallDelta(
                    index=int(item["index"]),
                    id=str(item.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or ""),
                )
            )
        return ModelChunk(
            text=text, usage=usage, finish_reason=finish_reason, tool_calls=tuple(calls)
        )
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProviderError("The model provider returned an invalid streaming event") from exc


def _message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = list(message.tool_calls)
    return payload
