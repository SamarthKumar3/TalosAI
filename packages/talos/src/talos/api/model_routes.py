from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import Field, SecretStr

from talos.api.contracts import StrictModel
from talos.model_profiles import ModelProfileManager


class CredentialSave(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    api_key: SecretStr
    credential_id: str | None = Field(default=None, max_length=100)


class ModelSave(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    credential_id: str = Field(min_length=1, max_length=100)
    context_window_tokens: int = Field(ge=256, le=2000000)
    max_output_tokens: int = Field(ge=1, le=2000000)


def register_model_routes(
    app: FastAPI, manager: ModelProfileManager | None, authorize: Callable[..., Awaitable[None]]
) -> None:
    def required() -> ModelProfileManager:
        if manager is None:
            raise HTTPException(503, "Model management is unavailable")
        return manager

    @app.get("/api/v1/models", dependencies=[Depends(authorize)])
    async def catalog() -> dict[str, Any]:
        return await asyncio.to_thread(required().catalog)

    @app.post("/api/v1/models/credentials", dependencies=[Depends(authorize)])
    async def save_credential(body: CredentialSave) -> dict[str, Any]:
        return await asyncio.to_thread(
            required().add_credential,
            body.label,
            body.provider,
            body.api_key.get_secret_value(),
            body.credential_id,
        )

    @app.post("/api/v1/models/profiles", dependencies=[Depends(authorize)])
    async def save_model(body: ModelSave) -> dict[str, Any]:
        return await asyncio.to_thread(required().add_model, **body.model_dump())

    @app.post("/api/v1/models/refresh", dependencies=[Depends(authorize)])
    async def refresh() -> dict[str, Any]:
        return await asyncio.to_thread(required().refresh)
