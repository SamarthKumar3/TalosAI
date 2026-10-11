from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from talos.errors import TalosError

_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[a-z0-9_\-/+=]{12,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
)
_FORBIDDEN_PARTS = {
    ".git",
    ".talos",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".next",
    ".cache",
    "coverage",
}
_FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}


class RepositoryError(TalosError):
    code = "repository_error"


@dataclass(frozen=True, slots=True)
class RepositorySettings:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_line_characters: int
    extensions: tuple[str, ...]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RepositorySettings:
        try:
            result = cls(
                max_files=int(environment["TALOS_REPO_MAX_FILES"]),
                max_file_bytes=int(environment["TALOS_REPO_MAX_FILE_BYTES"]),
                max_total_bytes=int(environment["TALOS_REPO_MAX_TOTAL_BYTES"]),
                max_line_characters=int(environment["TALOS_REPO_MAX_LINE_CHARACTERS"]),
                extensions=tuple(
                    value.strip().lower()
                    for value in environment["TALOS_REPO_EXTENSIONS"].split(",")
                    if value.strip()
                ),
            )
        except (KeyError, ValueError) as exc:
            raise RepositoryError("Repository limits are missing or invalid") from exc
        if (
            not 1 <= result.max_files <= 10000
            or not 1 <= result.max_file_bytes <= 1_000_000
            or not 1 <= result.max_total_bytes <= 10_000_000
            or result.max_total_bytes < result.max_file_bytes
            or not 100 <= result.max_line_characters <= 10000
            or not result.extensions
            or any(not item.startswith(".") for item in result.extensions)
        ):
            raise RepositoryError("Repository limits are outside safe bounds")
        return result


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    content: str
    checksum: str
    language: str


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    identity: str
    name: str
    branch: str
    commit: str
    dirty: bool
    checksum: str
    files: tuple[RepositoryFile, ...]
    skipped: int


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
            input=input_bytes,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError("Git is unavailable or timed out") from exc
    if completed.returncode not in (0, 1) or (
        completed.returncode == 1 and "check-ignore" not in args
    ):
        raise RepositoryError("Git could not inspect the repository")
    return completed.stdout


def git_root(path: Path) -> Path:
    raw = _git(path.resolve(), "rev-parse", "--show-toplevel").decode("utf-8", "replace")
    root = Path(raw.strip()).resolve()
    if not root.is_dir():
        raise RepositoryError("No Git repository was found")
    return root


def _safe_path(root: Path, relative: str, settings: RepositorySettings) -> Path | None:
    pure = PurePosixPath(relative)
    parts = pure.parts
    if (
        not parts
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            part.lower() in _FORBIDDEN_PARTS or part.lower().startswith(".env") for part in parts
        )
        or pure.suffix.lower() in _FORBIDDEN_SUFFIXES
        or pure.suffix.lower() not in settings.extensions
    ):
        return None
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    if not current.is_file():
        return None
    try:
        current.resolve().relative_to(root)
    except ValueError:
        return None
    return current


def validate_repository_file(item: RepositoryFile, settings: RepositorySettings) -> None:
    if _safe_path_shape(item.path, settings) is False:
        raise RepositoryError("Repository upload contains a forbidden path")
    encoded = item.content.encode("utf-8")
    if (
        not encoded
        or len(encoded) > settings.max_file_bytes
        or "\x00" in item.content
        or any(len(line) > settings.max_line_characters for line in item.content.splitlines())
        or _SECRET.search(item.content)
        or hashlib.sha256(encoded).hexdigest() != item.checksum
    ):
        raise RepositoryError("Repository upload contains an unsafe file")


def _safe_path_shape(relative: str, settings: RepositorySettings) -> bool:
    pure = PurePosixPath(relative)
    return bool(
        pure.parts
        and not pure.is_absolute()
        and not any(part in {"", ".", ".."} for part in pure.parts)
        and not any(
            part.lower() in _FORBIDDEN_PARTS or part.lower().startswith(".env")
            for part in pure.parts
        )
        and pure.suffix.lower() in settings.extensions
        and pure.suffix.lower() not in _FORBIDDEN_SUFFIXES
        and "\\" not in relative
        and ":" not in relative
    )


def scan_repository(path: Path, settings: RepositorySettings) -> RepositorySnapshot:
    root = git_root(path)
    branch = _git(root, "branch", "--show-current").decode("utf-8", "replace").strip()
    commit = _git(root, "rev-parse", "HEAD").decode("ascii", "replace").strip()
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal").strip())
    candidates = {
        value.decode("utf-8", "replace").replace("\\", "/")
        for value in _git(root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
        if value
    }
    paths = sorted(candidate for candidate in candidates if _safe_path_shape(candidate, settings))
    ignored = _ignored_paths(root, paths)
    files: list[RepositoryFile] = []
    skipped = len(candidates) - len(paths) + len(ignored)
    total = 0
    for relative in paths:
        if relative in ignored:
            continue
        loaded = _load_repository_file(root, relative, settings)
        if loaded is None:
            skipped += 1
            continue
        item, payload_size = loaded
        total += payload_size
        if len(files) >= settings.max_files or total > settings.max_total_bytes:
            raise RepositoryError("Repository exceeds configured indexing limits")
        files.append(item)
    if not files:
        raise RepositoryError("No safe source files were found for indexing")
    digest = hashlib.sha256(
        "\n".join(f"{item.path}:{item.checksum}" for item in files).encode()
    ).hexdigest()
    return RepositorySnapshot(
        identity=hashlib.sha256(str(root).encode()).hexdigest(),
        name=root.name,
        branch=branch,
        commit=commit,
        dirty=dirty,
        checksum=digest,
        files=tuple(files),
        skipped=skipped,
    )


def _ignored_paths(root: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    ignore_file = root / ".talosignore"
    options = (
        ("-c", f"core.excludesFile={ignore_file}")
        if ignore_file.is_file() and not ignore_file.is_symlink()
        else ()
    )
    output = _git(
        root,
        *options,
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        input_bytes=b"\0".join(path.encode() for path in paths) + b"\0",
    )
    return {
        value.decode("utf-8", "replace").replace("\\", "/")
        for value in output.split(b"\0")
        if value
    }


def _load_repository_file(
    root: Path, relative: str, settings: RepositorySettings
) -> tuple[RepositoryFile, int] | None:
    source = _safe_path(root, relative, settings)
    if source is None:
        return None
    with source.open("rb") as handle:
        payload = handle.read(settings.max_file_bytes + 1)
    if len(payload) > settings.max_file_bytes:
        return None
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    item = RepositoryFile(
        path=relative,
        content=content,
        checksum=hashlib.sha256(payload).hexdigest(),
        language=source.suffix.lower().lstrip("."),
    )
    try:
        validate_repository_file(item, settings)
    except RepositoryError:
        return None
    return item, len(payload)
