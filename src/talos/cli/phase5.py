from __future__ import annotations

import asyncio
import sys

import httpx
from rich.console import Console

from talos.cli import phase3
from talos.cli.phase3 import CommandContext
from talos.config import TalosConfig
from talos.errors import ConfigurationError
from talos.skill_management import SkillRegistryManager


async def update_with_context(context: CommandContext, action: str, value: str) -> TalosConfig:
    config = await asyncio.to_thread(
        SkillRegistryManager(context.root, context.environment).update, action, value
    )
    try:
        token = phase3.local_api_token(context, create=False)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{phase3._local_api_base_url(context.environment)}/api/v1/skills/reload",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException, ConfigurationError):
        Console(stderr=True).print(
            "Skill registry saved. Start or restart the API to load it.", markup=False
        )
    return config


def update(action: str, value: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    config = asyncio.run(update_with_context(context, action, value))
    Console().print(
        f"Skill registry saved; active skills: {', '.join(item.id for item in config.skills)}",
        markup=False,
    )
    return 0


def decide_tool(approval_id: str, *, approve: bool) -> int:
    if approve:
        if not sys.stdin.isatty():
            raise ConfigurationError("Tool approval requires an interactive terminal")
        response_text = input(f"Approve exact tool proposal {approval_id}? [y/N] ")
        if response_text.strip().lower() != "y":
            approve = False
    context = phase3.load_context(require_provider=False, interactive=False)
    token = phase3.local_api_token(context, create=False)
    response = httpx.post(
        f"{phase3._local_api_base_url(context.environment)}"
        f"/api/v1/tools/approvals/{approval_id}/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={"approve": approve},
        timeout=context.config.tool_timeout_seconds + 10,
    )
    response.raise_for_status()
    Console().print(str(response.json()), markup=False)
    return 0


def tool_status(approval_id: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    token = phase3.local_api_token(context, create=False)
    response = httpx.get(
        f"{phase3._local_api_base_url(context.environment)}/api/v1/tools/approvals/{approval_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    Console().print(str(response.json()), markup=False)
    return 0
