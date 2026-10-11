from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from talos.agent import TalosAgent
from talos.errors import ConfigurationError
from talos.skill_management import SkillRegistryManager
from talos.skills import Skill
from talos.tools.declarative import manifest_callable, parse_manifest
from talos.tools.runtime import ToolRegistry
from talos.tools.sandbox import reviewed_source, run_python


class ExtensionManager:
    """Manage non-executable YAML tools and separately sandboxed Python tools."""

    def __init__(
        self, root: Path, environment: Mapping[str, str], agent: TalosAgent | None = None
    ) -> None:
        self.root = root.resolve()
        self.environment = dict(environment)
        self.agent = agent
        self.registry = self.root / "config" / "extensions.yaml"
        self._loaded: dict[str, str] = {}

    def _entries(self) -> dict[str, Any]:
        if not self.registry.exists():
            return {}
        if (
            self.registry.resolve() != self.registry.absolute()
            or self.registry.stat().st_size > 1048576
        ):
            raise ConfigurationError("Extension registry must be a bounded regular file")
        try:
            value = yaml.safe_load(self.registry.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeError) as exc:
            raise ConfigurationError("Extension registry is invalid YAML") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("tools"), dict)
        ):
            raise ConfigurationError("Extension registry is invalid")
        return dict(value["tools"])

    def _register(self, manifest: dict[str, Any]) -> None:
        name = manifest["name"]
        identity = hashlib.sha256(yaml.safe_dump(manifest).encode()).hexdigest()
        if name in self._loaded:
            if self._loaded[name] != identity:
                raise ConfigurationError("Restart after changing an already loaded tool")
            return
        if manifest["kind"] == "python":
            reviewed_source(self.root, manifest)
        callback = manifest_callable(manifest, partial(run_python, self.root))
        if self.agent is None:
            ToolRegistry({}, timeout_seconds=10, max_response_bytes=32768).register(callback)
        else:
            self.agent.register_tool(
                callback,
                name=name,
                side_effect=manifest["kind"] == "python",
                model_enabled=manifest["kind"] != "python",
            )
        self._loaded[name] = identity

    def reload_tools(self) -> None:
        for name in self._entries():
            self._register(self.registered_tool(name))

    def registered_tool(self, name: str) -> dict[str, Any]:
        entry = self._entries().get(name)
        if entry is None:
            raise ConfigurationError("Tool is not registered")
        return self._read_manifest(name, entry)

    def _read_manifest(self, name: str, entry: Any) -> dict[str, Any]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256"}
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
        ):
            raise ConfigurationError("Invalid tool registry entry")
        path = self.root / entry["path"]
        if (
            path.resolve().parent != self.root / "tools"
            or path.resolve() != path.absolute()
            or path.suffix != ".yaml"
            or not path.is_file()
            or path.stat().st_size > 32768
        ):
            raise ConfigurationError("Tool manifest must be a regular managed YAML file")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ConfigurationError("Tool manifest changed; register a reviewed new tool")
        try:
            manifest = parse_manifest(content.decode("utf-8"))
        except UnicodeError as exc:
            raise ConfigurationError("Tool manifest must be UTF-8") from exc
        if manifest["name"] != name:
            raise ConfigurationError("Tool registry name does not match manifest")
        return manifest

    def add_tool(self, *, content: str, allow_python: bool = False) -> dict[str, Any]:
        manifest = parse_manifest(content)
        if manifest["kind"] == "python" and not allow_python:
            raise ConfigurationError(
                "Python tools require CLI review; use a template or calculator in the UI"
            )
        if manifest["kind"] == "python":
            reviewed_source(self.root, manifest)
        name = manifest["name"]
        entries = self._entries()
        if name in entries or (self.agent is not None and name in self.agent.list_tools()):
            raise ConfigurationError("Tool name already exists; choose a new name")
        directory = self.root / "tools"
        if directory.resolve() != directory.absolute():
            raise ConfigurationError("Tools directory must not use symlinks")
        directory.mkdir(exist_ok=True)
        path = directory / (name + ".yaml")
        encoded = yaml.safe_dump(manifest, sort_keys=False).encode()
        try:
            with path.open("xb") as stream:
                stream.write(encoded)
        except FileExistsError as exc:
            raise ConfigurationError("Tool file already exists; choose a new name") from exc
        entries[name] = {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".extensions-", dir=self.registry.parent)
        candidate = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump({"schema_version": 1, "tools": entries}, stream)
            os.chmod(candidate, 0o600)
            self._register(manifest)
            os.replace(candidate, self.registry)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            candidate.unlink(missing_ok=True)
        return {
            "name": name,
            "path": entries[name]["path"],
            "configuration_version": self.agent.config.version if self.agent else None,
        }

    def skills(self) -> list[dict[str, Any]]:
        manager = SkillRegistryManager(self.root, self.environment)
        value = yaml.safe_load(manager.registry.read_text(encoding="utf-8"))
        return [
            {"id": name, "enabled": bool(entry.get("enabled")), "path": entry["path"]}
            for name, entry in value["skills"].items()
        ]

    def update_skill(self, action: str, value: str) -> None:
        config = SkillRegistryManager(self.root, self.environment).update(action, value)
        if self.agent is not None:
            self.agent.set_skills(config.skills)

    def upload_skill(self, filename: str, content: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.md", filename):
            raise ConfigurationError("Use a simple Markdown filename")
        maximum = int(self.environment["TALOS_SKILL_MAX_FILE_BYTES"])
        try:
            encoded = content.replace("\r\n", "\n").encode()
        except UnicodeError as exc:
            raise ConfigurationError("Skill content must contain valid Unicode") from exc
        if len(encoded) > maximum:
            raise ConfigurationError("Skill exceeds its configured size limit")
        directory = self.root / "skills"
        if directory.resolve() != directory.absolute():
            raise ConfigurationError("Skill directory must not use symlinks")
        path = directory / filename
        directory.mkdir(exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(encoded)
        except FileExistsError as exc:
            raise ConfigurationError("Skill file already exists; choose another filename") from exc
        try:
            Skill.from_file(path, root=self.root, max_bytes=maximum)
            self.update_skill("add", path.relative_to(self.root).as_posix())
        except Exception:
            path.unlink(missing_ok=True)
            raise
