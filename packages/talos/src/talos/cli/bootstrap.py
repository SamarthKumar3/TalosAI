from __future__ import annotations

import getpass
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from talos.cli.environment import required_project_path
from talos.config import TalosConfig
from talos.credentials import CredentialStore
from talos.errors import ConfigurationError

SecretReader = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ShellSettings:
    project_root: Path
    config_path: Path
    legacy_credential_path: Path
    credential_service_name: str
    history_path: Path
    history_enabled: bool

    @classmethod
    def from_environment(cls, project_root: Path, environment: Mapping[str, str]) -> ShellSettings:
        enabled = environment.get("TALOS_HISTORY_ENABLED", "").strip().lower()
        if enabled not in {"true", "false"}:
            raise ConfigurationError("TALOS_HISTORY_ENABLED must be true or false")
        return cls(
            project_root=project_root.resolve(),
            config_path=required_project_path(project_root, environment, "TALOS_LOCAL_CONFIG_PATH"),
            legacy_credential_path=required_project_path(
                project_root, environment, "TALOS_LEGACY_CREDENTIAL_FILE_PATH"
            ),
            credential_service_name=_required_value(environment, "TALOS_CREDENTIAL_SERVICE_NAME"),
            history_path=required_project_path(project_root, environment, "TALOS_HISTORY_PATH"),
            history_enabled=enabled == "true",
        )


def ensure_provider_credential(
    config: TalosConfig,
    environment: MutableMapping[str, str],
    *,
    store: CredentialStore,
    console: Console,
    secret_reader: SecretReader = getpass.getpass,
) -> bool:
    """Collect the one secret needed by the default agent. Returns True when stored."""
    variable = config.provider.api_key_env
    if environment.get(variable, "").strip():
        return False
    stored_value = store.get(variable)
    if stored_value:
        environment[variable] = stored_value
        return False

    console.print(f"Connect {config.provider.display_name}", style="bold", markup=False)
    console.print("Create an API key, then return here and paste it.")
    link = Text(config.provider.key_url)
    link.stylize(f"link {config.provider.key_url}")
    console.print(link)
    value = secret_reader(f"{config.provider.display_name} API key (input hidden): ").strip()
    if not value:
        raise ConfigurationError("No provider API key was entered")
    store.set(variable, value)
    environment[variable] = value
    console.print("[green]API key saved in the operating system credential vault.[/green]")
    return True


def _required_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return value
