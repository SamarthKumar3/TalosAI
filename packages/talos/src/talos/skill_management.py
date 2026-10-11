from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from talos.config import TalosConfig
from talos.errors import ConfigurationError
from talos.skills import Skill


class SkillRegistryManager:
    """Atomically update the project-owned skill registry, never skill instructions."""

    def __init__(self, root: Path, environment: Mapping[str, str]) -> None:
        self.root = root.resolve()
        self.environment = dict(environment)
        config_value = self.environment.get("TALOS_LOCAL_CONFIG_PATH", "talos.yaml")
        self.config_path = (self.root / config_value).resolve()
        try:
            self.config_path.relative_to(self.root)
        except ValueError as exc:
            raise ConfigurationError("Talos config escapes the project root") from exc
        value = self.environment.get("TALOS_SKILL_REGISTRY_PATH", "")
        if not value:
            raise ConfigurationError("TALOS_SKILL_REGISTRY_PATH is missing")
        self.registry = (self.root / value).resolve()
        try:
            self.registry.relative_to(self.root)
        except ValueError as exc:
            raise ConfigurationError("Skill registry escapes the project root") from exc
        if self.registry.is_symlink() or not self.registry.is_file():
            raise ConfigurationError("Skill registry must be a regular project file")

    def update(self, action: str, value: str) -> TalosConfig:
        if action not in {"add", "enable", "disable"}:
            raise ConfigurationError("Unsupported skill action")
        original = self.registry.read_bytes()
        document: Any = yaml.safe_load(original)
        if not isinstance(document, dict) or not isinstance(document.get("skills"), dict):
            raise ConfigurationError("Skill registry is invalid")
        entries = document["skills"]
        if action == "add":
            path = (self.root / value).resolve()
            try:
                relative = path.relative_to(self.root)
            except ValueError as exc:
                raise ConfigurationError("Skill path escapes the project root") from exc
            maximum = int(self.environment["TALOS_SKILL_MAX_FILE_BYTES"])
            skill = Skill.from_file(path, root=self.root, max_bytes=maximum)
            if skill.id in entries:
                raise ConfigurationError("Skill ID already exists")
            entries[skill.id] = {"path": relative.as_posix(), "enabled": True}
        else:
            if value not in entries:
                raise ConfigurationError("Skill ID was not found")
            entry = entries[value]
            if not isinstance(entry, dict):
                raise ConfigurationError("Skill registry entry is invalid")
            entry["enabled"] = action == "enable"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".talos-skills-", suffix=".yaml", dir=self.registry.parent
        )
        candidate = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(document, stream, sort_keys=False)
            os.chmod(candidate, 0o600)
            selected = dict(self.environment)
            selected["TALOS_SKILL_REGISTRY_PATH"] = str(candidate)
            config = TalosConfig.from_file(self.config_path, environment=selected)
            if self.registry.read_bytes() != original:
                raise ConfigurationError("Skill registry changed concurrently; retry")
            os.replace(candidate, self.registry)
            return config
        finally:
            candidate.unlink(missing_ok=True)
