from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from talos.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    label: str
    description: str
    enabled: bool
    requires: tuple[str, ...]


class CapabilityRegistry:
    """Validated, immutable view of capabilities selected by external configuration."""

    def __init__(self, capabilities: Mapping[str, Capability]) -> None:
        self._capabilities = dict(capabilities)
        for capability in self._capabilities.values():
            if not capability.enabled:
                continue
            missing = [
                requirement
                for requirement in capability.requires
                if requirement not in self._capabilities
                or not self._capabilities[requirement].enabled
            ]
            if missing:
                raise ConfigurationError(
                    f"Capability {capability.id!r} requires enabled capabilities: "
                    + ", ".join(sorted(missing))
                )

    def enabled(self, capability_id: str) -> bool:
        capability = self._capabilities.get(capability_id)
        return capability is not None and capability.enabled

    def require(self, capability_id: str) -> None:
        if not self.enabled(capability_id):
            raise ConfigurationError(f"Capability {capability_id!r} is not enabled")

    def list(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))


def capability_registry_from_mapping(
    project: Mapping[str, object], registry: Mapping[str, object]
) -> CapabilityRegistry:
    definitions = registry.get("capabilities")
    if not isinstance(definitions, Mapping):
        raise ConfigurationError("Capability registry must define capabilities")
    unknown = set(project) - set(definitions)
    if unknown:
        raise ConfigurationError(
            "Unknown configured capabilities: " + ", ".join(sorted(str(item) for item in unknown))
        )
    capabilities: dict[str, Capability] = {}
    for capability_id, raw_definition in definitions.items():
        capability = _capability_from_mapping(capability_id, raw_definition, project)
        capabilities[capability.id] = capability
    return CapabilityRegistry(capabilities)


def _capability_from_mapping(
    capability_id: object,
    raw_definition: object,
    project: Mapping[str, object],
) -> Capability:
    if not isinstance(capability_id, str) or not isinstance(raw_definition, Mapping):
        raise ConfigurationError("Capability definitions must be mappings keyed by string IDs")
    requires = raw_definition.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        raise ConfigurationError(f"Capability {capability_id!r} has invalid requirements")
    enabled = project.get(capability_id, False)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"Capability {capability_id!r} must be true or false")
    label = _required_capability_text(raw_definition, capability_id, "label")
    description = _required_capability_text(raw_definition, capability_id, "description")
    return Capability(capability_id, label, description, enabled, tuple(requires))


def _required_capability_text(
    definition: Mapping[object, object], capability_id: str, key: str
) -> str:
    value = definition.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Capability {capability_id!r} needs a {key}")
    return value.strip()
