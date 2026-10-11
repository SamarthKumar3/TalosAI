from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import Field

from talos.api.contracts import StrictModel
from talos.extensions import ExtensionManager


class ToolAdd(StrictModel):
    content: str = Field(min_length=1, max_length=32768)


class SkillChange(StrictModel):
    action: str = Field(pattern=r"^(add|enable|disable)$")
    value: str = Field(min_length=1, max_length=500)


class SkillUpload(StrictModel):
    filename: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=262144)


def register_extension_routes(
    app: FastAPI, manager: ExtensionManager | None, authorize: Callable[..., Awaitable[None]]
) -> None:
    lock = asyncio.Lock()

    def required() -> ExtensionManager:
        if manager is None:
            raise HTTPException(503, "Extension management is unavailable")
        return manager

    @app.get("/api/v1/extensions", dependencies=[Depends(authorize)])
    async def extensions() -> dict[str, Any]:
        selected = required()
        return {
            "tools": list(selected.agent.list_tools()) if selected.agent else [],
            "skills": selected.skills(),
        }

    @app.post("/api/v1/extensions/tools", dependencies=[Depends(authorize)])
    async def add_tool(body: ToolAdd) -> dict[str, Any]:
        async with lock:
            return await asyncio.to_thread(required().add_tool, **body.model_dump())

    @app.post("/api/v1/extensions/tools/reload", dependencies=[Depends(authorize)])
    async def reload_tools() -> dict[str, bool]:
        async with lock:
            await asyncio.to_thread(required().reload_tools)
        return {"reloaded": True}

    @app.post("/api/v1/extensions/skills", dependencies=[Depends(authorize)])
    async def change_skill(body: SkillChange) -> dict[str, bool]:
        async with lock:
            await asyncio.to_thread(required().update_skill, body.action, body.value)
        return {"saved": True}

    @app.post("/api/v1/extensions/skills/upload", dependencies=[Depends(authorize)])
    async def upload_skill(body: SkillUpload) -> dict[str, bool]:
        async with lock:
            await asyncio.to_thread(required().upload_skill, body.filename, body.content)
        return {"saved": True}
