from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from talos.errors import ConfigurationError


def find_project_root(start: Path) -> Path:
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "talos.yaml").is_file() and (candidate / "config").is_dir():
            return candidate
    raise ConfigurationError(
        "No Talos project was found. Run this command inside a directory containing talos.yaml."
    )


def load_cli_environment(
    project_root: Path,
    *,
    process_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load safe defaults, local overrides, credentials, then process overrides."""
    environment: dict[str, str] = {}
    for relative_path in (".env.example", ".env"):
        path = project_root / relative_path
        if not path.is_file():
            continue
        for name, value in dotenv_values(path).items():
            if value is not None:
                environment[name] = value
    environment.update(os.environ if process_environment is None else process_environment)
    return environment


def required_project_path(project_root: Path, environment: Mapping[str, str], name: str) -> Path:
    raw_value = environment.get(name, "").strip()
    if not raw_value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    candidate = Path(raw_value)
    resolved = (
        (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    )
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must stay within the Talos project root") from exc
    return resolved
