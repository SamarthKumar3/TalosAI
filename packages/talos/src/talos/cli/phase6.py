from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import httpx
from rich.console import Console

from talos.cli import phase3
from talos.cli.phase3 import CommandContext
from talos.errors import ConfigurationError
from talos.knowledge import InMemoryKnowledgeStore, KnowledgeService


def _safe_source(root: Path, path: str) -> Path:
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    root_path = root.resolve()
    try:
        relative_input = candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("Knowledge file must stay inside the Talos project") from exc
    current = root_path
    for part in relative_input.parts:
        if part == "..":
            raise ConfigurationError("Knowledge file traversal is not allowed")
        if part in {".", ""}:
            continue
        current = current / part
        if current.is_symlink():
            raise ConfigurationError("Knowledge file symlinks are not allowed")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ConfigurationError("Knowledge file must stay inside the Talos project") from exc
    if not resolved.is_file():
        raise ConfigurationError("Knowledge file was not found")
    return resolved


async def add_with_context(context: CommandContext, path: str) -> str:
    source = _safe_source(context.root, path)
    maximum = context.config.rag.max_source_bytes
    payload = await asyncio.to_thread(_read_limited, source, maximum + 1)
    if len(payload) > maximum:
        raise ConfigurationError("Knowledge file exceeds the configured size limit")
    if source.suffix.lower() == ".pdf":
        content = base64.b64encode(payload).decode("ascii")
    else:
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationError("Knowledge file must be UTF-8 text") from exc
    KnowledgeService(
        InMemoryKnowledgeStore(), context.config.rag, environment=context.environment
    ).validate_source(source.name, content)
    token = phase3.local_api_token(context, create=False)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{phase3._local_api_base_url(context.environment)}/api/v1/knowledge",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": source.name, "content": content},
        )
        response.raise_for_status()
        return str(response.json()["job_id"])


def _read_limited(source: Path, maximum_bytes: int) -> bytes:
    with source.open("rb") as handle:
        return handle.read(maximum_bytes)


def add(path: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    job_id = asyncio.run(add_with_context(context, path))
    Console().print(f"Knowledge ingestion queued: {job_id}", markup=False)
    return 0


def status(job_id: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    token = phase3.local_api_token(context, create=False)
    response = httpx.get(
        f"{phase3._local_api_base_url(context.environment)}/api/v1/knowledge/jobs/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    Console().print(str(response.json()), markup=False)
    return 0


def cancel(job_id: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    token = phase3.local_api_token(context, create=False)
    response = httpx.post(
        f"{phase3._local_api_base_url(context.environment)}/api/v1/knowledge/jobs/{job_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    Console().print(str(response.json()), markup=False)
    return 0


def search(query: str) -> int:
    context = phase3.load_context(require_provider=False, interactive=False)
    token = phase3.local_api_token(context, create=False)
    response = httpx.post(
        f"{phase3._local_api_base_url(context.environment)}/api/v1/knowledge/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query},
        timeout=10,
    )
    response.raise_for_status()
    hits = response.json()["hits"]
    Console().print(
        "\n".join(
            f"[{hit['marker']}] {hit['source_name']}"
            + (
                f":page {hit['page_number']}"
                if hit.get("page_number")
                else f":{hit['start_line']}-{hit['end_line']}"
            )
            for hit in hits
        )
        or "No knowledge matches.",
        markup=False,
    )
    return 0
