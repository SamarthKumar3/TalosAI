from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from talos.errors import ConfigurationError

_doctor_lock = Lock()


async def request_host_doctor(directory: Path) -> dict[str, Any]:
    """Bounded, serialized IPC for the one fixed read-only host command."""
    return await asyncio.to_thread(_request_host_doctor, directory)


def _request_host_doctor(directory: Path) -> dict[str, Any]:
    if not _doctor_lock.acquire(blocking=False):
        raise ConfigurationError("Diagnostics are already running. Wait and try again.")
    request = directory / f"{uuid4().hex}.request"
    response = request.with_suffix(".response")
    try:
        heartbeat = directory / "heartbeat"
        if (
            directory.is_symlink()
            or not heartbeat.is_file()
            or time.time() - heartbeat.stat().st_mtime > 10
        ):
            raise ConfigurationError(
                "Host diagnostics are unavailable. Open Talos using 'talos ui' and try again."
            )
        try:
            request.touch(exist_ok=False)
            deadline = time.monotonic() + 30
            while not response.is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.2)
            if response.is_symlink() or response.stat().st_size > 65536:
                raise ConfigurationError("Invalid diagnostic response")
            result = json.loads(response.read_text(encoding="utf-8"))
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("healthy"), bool)
                or not isinstance(result.get("checks"), list)
            ):
                raise ConfigurationError("Invalid diagnostic response")
            checks = result["checks"]
            if len(checks) > 20 or not all(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("status") in {"ok", "error"}
                and isinstance(item.get("message"), str)
                and len(item["message"]) <= 512
                for item in checks
            ):
                raise ConfigurationError("Invalid diagnostic checks")
            return {"healthy": result["healthy"], "checks": checks}
        except (TimeoutError, OSError, ValueError) as exc:
            raise ConfigurationError(
                "Diagnostics timed out. Run 'talos doctor' in your terminal."
            ) from exc
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
    finally:
        _doctor_lock.release()
