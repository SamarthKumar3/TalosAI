"""Read bounded GitHub snapshots without cloning or executing repository code."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import re
import stat
import zipfile
from urllib.parse import quote, urlsplit

import httpx

from talos.repository import (
    RepositoryError,
    RepositoryFile,
    RepositorySettings,
    RepositorySnapshot,
    _safe_path_shape,
    validate_repository_file,
)

_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def github_repository(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not _NAME.fullmatch(part) or part in {".", ".."} for part in parts)
    ):
        raise RepositoryError("Use a repository URL such as https://github.com/owner/repository")
    owner, name = parts
    name = name.removesuffix(".git")
    if not name or name in {".", ".."}:
        raise RepositoryError("A GitHub repository name is required")
    return owner, name


def repository_url(url: str) -> str:
    owner, name = github_repository(url)
    return f"https://github.com/{owner.lower()}/{name.lower()}"


def remote_repository_id(url: str) -> str:
    return hashlib.sha256(repository_url(url).encode()).hexdigest()


def _check_response(response: httpx.Response) -> None:
    if response.status_code in {401, 403, 404, 429}:
        raise RepositoryError(
            "GitHub could not read this repository. Check the URL, rate limit, or supply a "
            "fine-grained GitHub token with Contents read access to this repository."
        )
    if not response.is_success:
        raise RepositoryError("GitHub could not download this repository; try again later")


async def fetch_github_repository(
    url: str,
    settings: RepositorySettings,
    *,
    token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RepositorySnapshot:
    """A token is used for this request only; it is never stored in a snapshot or job."""
    canonical = repository_url(url)
    owner, name = github_repository(canonical)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        if not re.fullmatch(r"[A-Za-z0-9_]+", token) or len(token) > 255:
            raise RepositoryError("Invalid GitHub token format")
        headers["Authorization"] = f"Bearer {token}"
    api = f"https://api.github.com/repos/{owner}/{name}"
    try:
        async with (
            asyncio.timeout(90),
            httpx.AsyncClient(
                transport=transport,
                timeout=20,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            response = await client.get(api, headers=headers)
            _check_response(response)
            branch = str(response.json()["default_branch"])
            response = await client.get(f"{api}/commits/{quote(branch, safe='')}", headers=headers)
            _check_response(response)
            commit = str(response.json()["sha"])
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise RepositoryError("GitHub returned an invalid commit")
            response = await client.get(f"{api}/zipball/{commit}", headers=headers)
            if response.status_code != 302:
                _check_response(response)
                raise RepositoryError("GitHub did not provide a repository archive")
            location = response.headers.get("location", "")
            target = urlsplit(location)
            if target.scheme != "https" or target.netloc != "codeload.github.com":
                raise RepositoryError("GitHub returned an unexpected archive host")
            # Do not forward the Authorization header to the archive host or follow redirects.
            payload = bytearray()
            maximum = min(settings.max_total_bytes * 4, 32 * 1024 * 1024)
            async with client.stream("GET", location) as archive:
                _check_response(archive)
                async for chunk in archive.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > maximum:
                        raise RepositoryError(
                            "Repository archive is too large; index a local checkout instead"
                        )
        return await asyncio.to_thread(
            snapshot_from_archive, bytes(payload), canonical, branch, commit, settings
        )
    except (httpx.HTTPError, TimeoutError, KeyError, ValueError):
        # Provider errors can contain signed archive URLs or request headers. Never echo them.
        raise RepositoryError(
            "Could not read the GitHub repository; check access and try again"
        ) from None


def snapshot_from_archive(
    payload: bytes,
    url: str,
    branch: str,
    commit: str,
    settings: RepositorySettings,
) -> RepositorySnapshot:
    files: list[RepositoryFile] = []
    total = skipped = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 50000:
                raise RepositoryError("Repository has too many archive entries")
            if sum(entry.file_size for entry in entries) > min(
                settings.max_total_bytes * 8, 80 * 1024 * 1024
            ):
                raise RepositoryError("Expanded repository archive exceeds the safe download limit")
            ignores: list[str] = []
            for entry in entries:
                if entry.filename.count("/") == 1 and entry.filename.endswith("/.talosignore"):
                    if entry.file_size <= settings.max_file_bytes:
                        ignores = archive.read(entry).decode("utf-8").splitlines()
            seen: set[str] = set()
            for entry in sorted(entries, key=lambda item: item.filename):
                prefix = entry.filename.partition("/")[0]
                path = entry.filename.partition("/")[2]
                if entry.is_dir():
                    continue
                mode = entry.external_attr >> 16
                if (
                    prefix in {"", ".", ".."}
                    or not _safe_path_shape(path, settings)
                    or stat.S_ISLNK(mode)
                    or entry.flag_bits & 1
                    or entry.file_size > settings.max_file_bytes
                    or any(
                        fnmatch.fnmatch(path, pattern.strip())
                        for pattern in ignores
                        if pattern.strip() and not pattern.startswith("#")
                    )
                ):
                    skipped += 1
                    continue
                with archive.open(entry) as handle:
                    raw = handle.read(settings.max_file_bytes + 1)
                try:
                    text = raw.decode("utf-8")
                    item = RepositoryFile(
                        path, text, hashlib.sha256(raw).hexdigest(), path.rsplit(".", 1)[-1].lower()
                    )
                    validate_repository_file(item, settings)
                except (UnicodeDecodeError, RepositoryError):
                    skipped += 1
                    continue
                if path in seen:
                    raise RepositoryError("Repository archive contains duplicate paths")
                seen.add(path)
                total += len(raw)
                if total > settings.max_total_bytes or len(files) >= settings.max_files:
                    raise RepositoryError(
                        "Repository exceeds indexing limits; index a smaller local checkout"
                    )
                files.append(item)
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError, OSError) as exc:
        raise RepositoryError("GitHub returned an unreadable archive") from exc
    if not files:
        raise RepositoryError("No safe supported source files were found in this repository")
    return RepositorySnapshot(
        identity=remote_repository_id(url),
        name=url.removeprefix("https://"),
        branch=branch,
        commit=commit,
        dirty=False,
        checksum=hashlib.sha256(
            "\n".join(f"{item.path}:{item.checksum}" for item in files).encode()
        ).hexdigest(),
        files=tuple(files),
        skipped=skipped,
    )
