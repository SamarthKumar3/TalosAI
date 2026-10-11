from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from talos.knowledge import EmbeddingTransientError, KnowledgeError, RagSettings


class OpenAICompatibleEmbedder:
    """Embedding adapter for an OpenAI-compatible /embeddings endpoint."""

    def __init__(
        self, settings: RagSettings, *, environment: Mapping[str, str] | None = None
    ) -> None:
        parsed = urlparse(settings.embedding_api_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise KnowledgeError("Embedding API base must be an HTTPS URL")
        if not settings.embedding_api_key_name.isidentifier():
            raise KnowledgeError("Embedding credential variable name is invalid")
        key = (environment or {}).get(settings.embedding_api_key_name, "")
        if not key:
            raise KnowledgeError(
                f"Embedding credential {settings.embedding_api_key_name} is unavailable"
            )
        self._settings = settings
        self._key = key

    async def embed(self, text: str) -> tuple[float, ...]:
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.embedding_timeout_seconds
            ) as client:
                response = await client.post(
                    self._settings.embedding_api_base.rstrip("/") + "/embeddings",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": self._settings.embedding_model, "input": text},
                )
                response.raise_for_status()
                payload: Any = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                raise EmbeddingTransientError(
                    "Embedding provider is temporarily unavailable"
                ) from exc
            raise KnowledgeError("Embedding provider rejected the request") from exc
        except httpx.RequestError as exc:
            raise EmbeddingTransientError("Embedding provider is temporarily unavailable") from exc
        except ValueError as exc:
            raise KnowledgeError("Embedding provider request failed") from exc
        if not isinstance(payload, dict):
            raise KnowledgeError("Embedding provider response is invalid")
        records = payload.get("data")
        if not isinstance(records, list) or len(records) != 1:
            raise KnowledgeError("Embedding provider returned an invalid result count")
        record = records[0]
        if not isinstance(record, dict):
            raise KnowledgeError("Embedding provider response is invalid")
        vector = record.get("embedding")
        if not isinstance(vector, list) or len(vector) != self._settings.dimensions:
            raise KnowledgeError("Embedding provider returned incompatible dimensions")
        if any(
            isinstance(item, bool) or not isinstance(item, (float, int)) or not math.isfinite(item)
            for item in vector
        ):
            raise KnowledgeError("Embedding provider returned invalid vector values")
        norm = math.sqrt(sum(float(item) ** 2 for item in vector))
        if not norm:
            raise KnowledgeError("Embedding provider returned a zero vector")
        return tuple(float(item) / norm for item in vector)
