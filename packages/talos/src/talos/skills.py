from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from talos.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    version: str
    name: str
    description: str
    instructions: str
    content_hash: str
    requires: tuple[str, ...]
    conflicts: tuple[str, ...]

    @classmethod
    def from_file(cls, path: Path, *, root: Path, max_bytes: int) -> Skill:
        resolved = _validated_skill_path(path, root, max_bytes)
        text = resolved.read_text(encoding="utf-8")
        metadata, instructions = _parse_skill_document(text)
        requires = _string_list(metadata, "requires")
        conflicts = _string_list(metadata, "conflicts")
        return cls(
            id=metadata["id"],
            version=metadata["version"],
            name=metadata["name"],
            description=metadata["description"],
            instructions=instructions.strip(),
            content_hash=hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest(),
            requires=tuple(requires),
            conflicts=tuple(conflicts),
        )


def _validated_skill_path(path: Path, root: Path, max_bytes: int) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigurationError("Skill path escapes its configured root") from exc
    if not resolved.is_file() or resolved.stat().st_size > max_bytes:
        raise ConfigurationError("Skill is missing or exceeds its size limit")
    return resolved


def _parse_skill_document(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ConfigurationError("Skill front matter is missing")
    front, separator, instructions = text.removeprefix("---\n").partition("\n---\n")
    if not separator or not instructions.strip():
        raise ConfigurationError("Skill instructions or front matter are invalid")
    try:
        metadata: Any = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise ConfigurationError("Skill front matter is invalid YAML") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        raise ConfigurationError("Unsupported skill schema")
    for key in ("id", "version", "name", "description"):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ConfigurationError(f"Skill {key} is required")
    return metadata, instructions


def _string_list(metadata: dict[str, Any], key: str) -> list[str]:
    values = metadata.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ConfigurationError(f"Skill {key} must be a string list")
    return values


def validate_skills(skills: tuple[Skill, ...], capabilities: set[str]) -> None:
    ids = [skill.id for skill in skills]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Duplicate skill ID")
    for skill in skills:
        if not set(skill.requires) <= capabilities:
            raise ConfigurationError(f"Skill {skill.id} requires unavailable capabilities")
        if set(skill.conflicts) & set(ids):
            raise ConfigurationError(f"Skill {skill.id} conflicts with an enabled skill")
