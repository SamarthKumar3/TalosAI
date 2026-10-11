from __future__ import annotations

import asyncio
import time
import webbrowser
from urllib.parse import urlparse

import httpx

from talos.cli.doctor_bridge import start_doctor_host
from talos.cli.phase3 import (
    CommandContext,
    reset_ui_stack,
    start_web,
    wait_until_ready,
)
from talos.errors import ConfigurationError, TalosError


async def open_ui_with_context(
    context: CommandContext,
    *,
    ensure_core: bool,
) -> bool:
    url = _web_url(context.environment)
    start_doctor_host(context.root)
    if ensure_core:
        if reset_ui_stack(context) != 0:
            raise TalosError("Local Talos services could not be rebuilt and started")
        await wait_until_ready(context.environment)
        await _wait_for_web(url, context.environment)
        return _open_browser(url)

    api_ready = await _endpoint_ready(
        f"{context.environment['TALOS_API_BASE_URL'].rstrip('/')}/health/ready"
    )
    web_ready = await _endpoint_ready(f"{url}/api/health")
    if api_ready and web_ready:
        return _open_browser(url)
    if not api_ready:
        await wait_until_ready(context.environment)
    if not web_ready and start_web(context) != 0:
        raise TalosError("The local Talos Web service could not be started")
    await _wait_for_web(url, context.environment)
    return _open_browser(url)


async def _endpoint_ready(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(url)
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _open_browser(url: str) -> bool:
    if not webbrowser.open(url, new=2):
        raise TalosError(f"Could not open a browser. Open {url} manually.")
    return True


async def _wait_for_web(url: str, environment: dict[str, str]) -> None:
    try:
        timeout = int(environment["TALOS_WEB_STARTUP_TIMEOUT_SECONDS"])
    except (KeyError, ValueError) as exc:
        raise ConfigurationError("TALOS_WEB_STARTUP_TIMEOUT_SECONDS must be an integer") from exc
    if timeout <= 0:
        raise ConfigurationError("TALOS_WEB_STARTUP_TIMEOUT_SECONDS must be greater than zero")
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{url}/api/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise ConfigurationError(
        f"Talos Web did not become ready at {url} before the configured timeout"
    )


def _web_url(environment: dict[str, str]) -> str:
    value = environment.get("TALOS_WEB_URL", "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ConfigurationError("TALOS_WEB_URL must be a valid loopback HTTP(S) URL")
    return value
