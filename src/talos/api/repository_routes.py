"""URL ingestion and explicit conversation repository selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException

from talos.agent import TalosAgent
from talos.api.contracts import RepositorySelectionRequest, RepositoryUrlRequest
from talos.conversations import ConversationStore
from talos.persistence.repository import SqlRepositoryStore
from talos.repository import RepositorySettings
from talos.repository_remote import fetch_github_repository


def register_repository_url_routes(
    app: FastAPI,
    agent: TalosAgent,
    conversations: ConversationStore,
    store: SqlRepositoryStore | None,
    settings: RepositorySettings | None,
    notify: Callable[[str], Awaitable[None]] | None,
    authorize: Callable[..., Awaitable[None]],
) -> None:
    @app.post("/api/v1/repositories/url", status_code=202, dependencies=[Depends(authorize)])
    async def index_url(
        body: RepositoryUrlRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        if store is None or settings is None:
            raise HTTPException(503, "Repository indexing is unavailable")
        if idempotency_key is not None and len(idempotency_key) > 128:
            raise HTTPException(400, "Idempotency key is too long")
        snapshot = await fetch_github_repository(body.url, settings, token=body.token)
        job_id = await store.enqueue(
            snapshot, agent.config.rag.embedding_model, idempotency_key or str(uuid4())
        )
        if notify is not None:
            await notify(job_id)
        return {
            "job_id": job_id,
            "repository_id": snapshot.identity,
            "file_count": len(snapshot.files),
            "skipped": snapshot.skipped,
        }

    @app.post(
        "/api/v1/conversations/{conversation_id}/repository", dependencies=[Depends(authorize)]
    )
    async def select_repository(
        conversation_id: str,
        body: RepositorySelectionRequest,
    ) -> dict[str, object]:
        await conversations.get(conversation_id)
        selected = None
        if body.repository_id is not None:
            if store is None:
                raise HTTPException(503, "Repository indexing is unavailable")
            summary = await store.summary(body.repository_id)
            if summary is None:
                raise HTTPException(404, "Repository was not found")
            if not summary["active_version"]:
                raise HTTPException(409, "Repository indexing must finish before selection")
            selected = {"id": body.repository_id, "name": str(summary["name"])}
        await conversations.select_repository(conversation_id, selected)
        return {"repository": selected}
