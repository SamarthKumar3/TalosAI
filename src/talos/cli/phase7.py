from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from rich.console import Console

from talos.cli import phase3
from talos.cli.phase3 import CommandContext
from talos.repository import RepositoryError, RepositorySettings, git_root, scan_repository


def _repository_id(root: Path) -> str:
    return hashlib.sha256(str(git_root(root)).encode()).hexdigest()


def _confirm(action: str, *, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise RepositoryError(f"{action} requires --yes in a non-interactive terminal")
    choice = Console().input(f"{action}? [y/N] ").strip().lower()
    if choice not in {"y", "yes"}:
        raise RepositoryError("Repository action was not confirmed")


async def index_with_context(context: CommandContext, path: str, *, yes: bool) -> dict[str, object]:
    _confirm("Read and index safe repository source files", yes=yes)
    candidate = Path(path)
    root = git_root(candidate if candidate.is_absolute() else Path.cwd() / candidate)
    snapshot = await asyncio.to_thread(
        scan_repository, root, RepositorySettings.from_environment(context.environment)
    )
    token = phase3.local_api_token(context, create=False)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/index",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "consent": True,
                "identity": snapshot.identity,
                "name": snapshot.name,
                "branch": snapshot.branch,
                "commit": snapshot.commit,
                "dirty": snapshot.dirty,
                "checksum": snapshot.checksum,
                "files": [
                    {
                        "path": item.path,
                        "content": item.content,
                        "checksum": item.checksum,
                        "language": item.language,
                    }
                    for item in snapshot.files
                ],
                "skipped": snapshot.skipped,
            },
        )
        response.raise_for_status()
        return {
            **response.json(),
            "repository_id": snapshot.identity,
            "file_count": len(snapshot.files),
            "skipped": snapshot.skipped,
            "dirty": snapshot.dirty,
        }


async def status_with_context(
    context: CommandContext, *, job_id: str | None = None, path: str = "."
) -> dict[str, object]:
    token = phase3.local_api_token(context, create=False)
    suffix = (
        f"/api/v1/repositories/jobs/{job_id}"
        if job_id is not None
        else f"/api/v1/repositories/{_repository_id(Path(path))}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{phase3._local_api_base_url(context.environment)}{suffix}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return dict(response.json())


async def cancel_with_context(context: CommandContext, job_id: str) -> dict[str, object]:
    token = phase3.local_api_token(context, create=False)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/jobs/{job_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return dict(response.json())


async def remove_with_context(
    context: CommandContext, *, yes: bool, path: str = "."
) -> dict[str, object]:
    _confirm("Remove the indexed repository data (source files remain untouched)", yes=yes)
    token = phase3.local_api_token(context, create=False)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.delete(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/{_repository_id(Path(path))}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return dict(response.json())


async def search_with_context(
    context: CommandContext, query: str, *, path: str = "."
) -> list[dict[str, object]]:
    token = phase3.local_api_token(context, create=False)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "repository_id": _repository_id(Path(path))},
        )
        response.raise_for_status()
        return list(response.json()["hits"])


def command(action: str, value: str | None = None, *, yes: bool = False, path: str = ".") -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    if action in {"add", "index", "reindex"}:
        result: object = asyncio.run(index_with_context(context, value or ".", yes=yes))
    elif action == "status":
        result = asyncio.run(status_with_context(context, job_id=value, path=path))
    elif action == "cancel":
        if value is None:
            raise RepositoryError("A repository job ID is required")
        result = asyncio.run(cancel_with_context(context, value))
    elif action == "remove":
        result = asyncio.run(remove_with_context(context, yes=yes, path=path))
    elif action == "search":
        if value is None:
            raise RepositoryError("A repository query is required")
        hits = asyncio.run(search_with_context(context, value, path=path))
        result = [
            f"[{hit['marker']}] {hit['path']}:{hit['start_line']}-{hit['end_line']} "
            f"(commit {hit['commit']}, snapshot {hit['snapshot']})"
            for hit in hits
        ]
    else:
        raise RepositoryError("Unknown repository command")
    Console().print(str(result), markup=False)
    return 0
