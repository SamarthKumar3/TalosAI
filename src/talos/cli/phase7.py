from __future__ import annotations

import asyncio
import getpass
import hashlib
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from rich.console import Console

from talos.cli import phase3
from talos.cli.phase3 import CommandContext
from talos.repository import RepositoryError, RepositorySettings, git_root, scan_repository
from talos.repository_diagrams import write_hld
from talos.repository_remote import remote_repository_id


def _repository_id(root: Path | str) -> str:
    if str(root).startswith("https://"):
        return remote_repository_id(str(root))
    return hashlib.sha256(str(git_root(Path(root))).encode()).hexdigest()


def _confirm(action: str, *, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise RepositoryError(f"{action} requires --yes in a non-interactive terminal")
    choice = Console().input(f"{action}? [y/N] ").strip().lower()
    if choice not in {"y", "yes"}:
        raise RepositoryError("Repository action was not confirmed")


async def index_with_context(
    context: CommandContext, path: str, *, yes: bool, github_token: str | None = None
) -> dict[str, object]:
    _confirm("Read and index safe repository source files", yes=yes)
    if path.startswith("https://"):
        async with httpx.AsyncClient(timeout=100) as client:
            response = await client.post(
                f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/url",
                headers={
                    "Authorization": f"Bearer {phase3.local_api_token(context, create=False)}",
                    "Idempotency-Key": str(uuid4()),
                },
                json={"url": path, "token": github_token or os.environ.get("GITHUB_TOKEN")},
            )
            response.raise_for_status()
            return dict(response.json())
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
        else f"/api/v1/repositories/{_repository_id(path)}"
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
            f"{phase3._local_api_base_url(context.environment)}/api/v1/repositories/{_repository_id(path)}",
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
            json={"query": query, "repository_id": _repository_id(path)},
        )
        response.raise_for_status()
        return list(response.json()["hits"])


async def wait_for_index(context: CommandContext, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status = await status_with_context(context, job_id=job_id)
        if status.get("state") == "completed":
            return status
        if status.get("state") in {"failed", "cancelled"}:
            raise RepositoryError(
                f"Repository indexing {status['state']}; check talos repo status {job_id}"
            )
        await asyncio.sleep(0.75)
    raise RepositoryError(
        f"Indexing is still running. Check talos repo status {job_id} before asking questions"
    )


async def select_with_context(context: CommandContext, conversation_id: str, path: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/conversations/{conversation_id}/repository",
            headers={"Authorization": f"Bearer {phase3.local_api_token(context, create=False)}"},
            json={"repository_id": _repository_id(path)},
        )
        if response.status_code in {404, 409}:
            raise RepositoryError(
                "Index first with talos repo index PATH_OR_URL --yes, "
                "then wait for its job to complete"
            )
        response.raise_for_status()


async def ask_with_context(context: CommandContext, query: str, *, path: str) -> str:
    headers = {"Authorization": f"Bearer {phase3.local_api_token(context, create=False)}"}
    base = phase3._local_api_base_url(context.environment)
    async with httpx.AsyncClient(
        timeout=context.config.budgets.max_execution_seconds + 5
    ) as client:
        response = await client.post(f"{base}/api/v1/conversations", headers=headers)
        response.raise_for_status()
        conversation_id = response.json()["id"]
        await select_with_context(context, conversation_id, path)
        response = await client.post(
            f"{base}/api/v1/chat",
            headers=headers,
            json={
                "input": query,
                "conversation_id": conversation_id,
                "repository_mode": True,
            },
        )
        response.raise_for_status()
        return str(response.json()["response"])


def command(
    action: str,
    value: str | None = None,
    *,
    yes: bool = False,
    path: str = ".",
    private: bool = False,
    output: str | None = None,
) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    if action in {"add", "index", "reindex"}:
        github_token = getpass.getpass("GitHub token (Contents read-only): ") if private else None
        result: object = asyncio.run(
            index_with_context(context, value or ".", yes=yes, github_token=github_token)
        )
    elif action == "status":
        result = asyncio.run(status_with_context(context, job_id=value, path=path))
    elif action == "cancel":
        if value is None:
            raise RepositoryError("A repository job ID is required")
        result = asyncio.run(cancel_with_context(context, value))
    elif action == "remove":
        result = asyncio.run(remove_with_context(context, yes=yes, path=path))
    elif action in {"ask", "hld"}:
        query = (
            value
            if action == "ask"
            else "Create a high-level design (HLD) of this repository "
            "as a Mermaid flowchart with cited explanations."
        )
        if not query:
            raise RepositoryError("A repository question is required")
        response_text = asyncio.run(ask_with_context(context, query, path=path))
        result = response_text
        if action == "hld":
            target = write_hld(
                response_text,
                Path(output) if output else context.root / ".talos" / "artifacts",
            )
            result = (
                f"{response_text}\n\nMermaid file: {target}\n"
                f"Cited explanation: {target.with_suffix('.md')}\n"
                "Open the .mmd file in https://mermaid.live "
                "(private code may be shared with that service)."
            )
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
