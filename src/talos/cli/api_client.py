from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import cast

import httpx

from talos.config import TalosConfig
from talos.domain import Conversation
from talos.events import EventType, RuntimeEvent
from talos.why import WhyError, WhyReport


class ApiTalosAgent:
    """Shell-compatible adapter over the local authenticated FastAPI runtime."""

    def __init__(
        self,
        *,
        config: TalosConfig,
        api_base: str,
        token: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._api_base = api_base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_seconds
        self._client = client
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def new_conversation(self) -> Conversation:
        async with self._client_context() as client:
            response = await client.post(
                f"{self._api_base}/api/v1/conversations",
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
        return Conversation(
            id=str(payload["id"]),
            configuration=self.config.snapshot(),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )

    async def get_conversation(self, conversation_id: str) -> Conversation:
        async with self._client_context() as client:
            response = await client.get(
                f"{self._api_base}/api/v1/conversations/{conversation_id}",
                headers=self._headers,
            )
            if response.status_code == 404:
                raise WhyError("Conversation was not found")
            response.raise_for_status()
            payload = response.json()
            return Conversation(id=str(payload["id"]), configuration=self.config.snapshot())

    def list_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self.config.tools))

    def list_skills(self) -> tuple[object, ...]:
        return self.config.skills

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        async with self._client_context() as client:
            response = await client.post(
                f"{self._api_base}/api/v1/tools/{name}/invoke",
                headers=self._headers,
                json={"arguments": arguments},
            )
            response.raise_for_status()
            payload = response.json()
            if response.status_code == 202:
                return cast(dict[str, object], payload)
            return payload["result"]

    async def list_knowledge(self) -> list[dict[str, object]]:
        async with self._client_context() as client:
            response = await client.get(f"{self._api_base}/api/v1/knowledge", headers=self._headers)
            response.raise_for_status()
            return list(response.json()["sources"])

    async def search_knowledge(self, query: str) -> list[dict[str, object]]:
        async with self._client_context() as client:
            response = await client.post(
                f"{self._api_base}/api/v1/knowledge/search",
                headers=self._headers,
                json={"query": query},
            )
            response.raise_for_status()
            return list(response.json()["hits"])

    async def trace(self, run_id: str | None = None) -> dict[str, object] | None:
        path = f"traces/{run_id}" if run_id is not None else "traces?page=1"
        async with self._client_context() as client:
            response = await client.get(f"{self._api_base}/api/v1/{path}", headers=self._headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            if run_id is None:
                traces = payload["traces"]
                return traces[0] if traces else None
            return cast(dict[str, object], payload)

    async def why_report(self, conversation_id: str, offset: int = -1) -> WhyReport:
        async with self._client_context() as client:
            response = await client.get(
                f"{self._api_base}/api/v1/conversations/{conversation_id}/why",
                params={"offset": offset},
                headers=self._headers,
            )
            if response.status_code in {400, 404}:
                raise WhyError(str(response.json().get("message", "Could not inspect response")))
            response.raise_for_status()
            return WhyReport.model_validate(response.json())

    async def usage_report(self) -> dict[str, object]:
        async with self._client_context() as client:
            response = await client.get(f"{self._api_base}/api/v1/usage", headers=self._headers)
            response.raise_for_status()
            return cast(dict[str, object], response.json())

    async def observability_alerts(self) -> list[dict[str, object]]:
        async with self._client_context() as client:
            response = await client.get(f"{self._api_base}/api/v1/alerts", headers=self._headers)
            response.raise_for_status()
            return list(response.json()["alerts"])

    async def stream(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        body = {"input": prompt, "conversation_id": conversation_id}
        async with self._client_context() as client:
            async with client.stream(
                "POST",
                f"{self._api_base}/api/v1/chat/stream",
                headers=self._headers,
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = json.loads(line[5:].strip())
                    yield RuntimeEvent(
                        event_id=str(payload["event_id"]),
                        type=EventType(str(payload["type"])),
                        run_id=str(payload["run_id"]),
                        conversation_id=str(payload["conversation_id"]),
                        sequence=int(payload["sequence"]),
                        schema_version=int(payload["schema_version"]),
                        occurred_at=datetime.fromisoformat(str(payload["occurred_at"])),
                        payload=payload["payload"],
                    )

    def cancel(self, run_id: str) -> bool:
        task = asyncio.create_task(self._cancel(run_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    async def _cancel(self, run_id: str) -> None:
        async with self._client_context() as client:
            await client.post(
                f"{self._api_base}/api/v1/runs/{run_id}/cancel",
                headers=self._headers,
            )

    def _client_context(self) -> _ClientContext:
        return _ClientContext(self._client, self._timeout)


class _ClientContext:
    def __init__(self, client: httpx.AsyncClient | None, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._owns_client = client is None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def __aexit__(self, *args: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
