"""Named model/key mappings over the existing provider contracts."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from talos.agent import TalosAgent
from talos.cli.environment import required_project_path
from talos.config import ModelDefinition, ProviderDefinition, _load_yaml
from talos.errors import ConfigurationError
from talos.model_credentials import ModelCredentialVault
from talos.providers.base import ModelProvider
from talos.providers.factory import provider_from_config


class ModelProfileManager:
    def __init__(self, root: Path, environment: Mapping[str, str], agent: TalosAgent) -> None:
        self.root = root
        self.environment = dict(environment)
        self.agent = agent
        self.lock = threading.RLock()
        self.vault = ModelCredentialVault(
            required_project_path(root, environment, "TALOS_MODEL_CREDENTIALS_PATH")
        )

    def _providers(self) -> dict[str, ProviderDefinition]:
        path = required_project_path(self.root, self.environment, "TALOS_PROVIDER_REGISTRY_PATH")
        entries = _load_yaml(path, self.environment)["providers"]
        return {
            alias: ProviderDefinition(
                alias=alias,
                display_name=item["display_name"],
                adapter=item["adapter"],
                api_base=item["api_base"],
                api_key_env=item["api_key_env"],
                key_url=item["key_url"],
                connect_timeout_seconds=float(item["connect_timeout_seconds"]),
                read_timeout_seconds=float(item["read_timeout_seconds"]),
            )
            for alias, item in entries.items()
            if item["adapter"]
            in {
                "talos.providers.openai:OpenAIProvider",
                "talos.providers.openai_compatible:OpenAICompatibleProvider",
            }
        }

    def _state(self) -> dict[str, Any]:
        state = self.vault.load()
        if not isinstance(state["credentials"], dict) or not isinstance(state["models"], dict):
            raise ConfigurationError("Model credential registry is invalid")
        return state

    def _credentials(self, state: dict[str, Any]) -> dict[str, dict[str, str]]:
        credentials = {
            "env:" + alias: {
                "label": provider.display_name + " · CLI/environment",
                "provider": alias,
            }
            for alias, provider in self._providers().items()
            if self.environment.get(provider.api_key_env)
        }
        credentials.update(
            {
                name: {"label": entry["label"], "provider": entry["provider"]}
                for name, entry in state["credentials"].items()
            }
        )
        return credentials

    def catalog(self) -> dict[str, Any]:
        with self.lock:
            state = self._state()
            credentials = self._credentials(state)
            current = self.agent.config
            return {
                "providers": [
                    {"id": key, "label": value.display_name}
                    for key, value in self._providers().items()
                ],
                "credentials": [{"id": key, **value} for key, value in credentials.items()],
                "models": [
                    {
                        "id": current.model.alias,
                        "label": current.model.model_id + " · configured",
                        "model_id": current.model.model_id,
                        "provider": current.provider.alias,
                        "credential_id": "env:" + current.provider.alias,
                    },
                    *[{"id": key, **value} for key, value in state["models"].items()],
                ],
                "default_model_id": current.model.alias,
            }

    def add_credential(
        self, label: str, provider: str, api_key: str, credential_id: str | None = None
    ) -> dict[str, Any]:
        if provider not in self._providers():
            raise ConfigurationError("Choose a configured provider")
        if not 1 <= len(label.strip()) <= 100 or not re.fullmatch(r"[\x21-\x7e]{1,4096}", api_key):
            raise ConfigurationError("Use a key label and a non-empty API key without whitespace")
        with self.lock:
            state = self._state()
            if credential_id is not None:
                existing = state["credentials"].get(credential_id)
                if existing is None or existing["provider"] != provider:
                    raise ConfigurationError("Saved key was not found for this provider")
            elif len(state["credentials"]) >= 100:
                raise ConfigurationError("At most 100 API keys can be saved")
            identifier = credential_id or "key_" + uuid4().hex
            if any(
                name != identifier and item["label"].casefold() == label.strip().casefold()
                for name, item in state["credentials"].items()
            ):
                raise ConfigurationError(
                    "Key label already exists; replace the saved key or use another label"
                )
            state["credentials"][identifier] = {
                "label": label.strip(),
                "provider": provider,
                "api_key": api_key,
            }
            self.vault.save(state)
            self.refresh()
            return self.catalog()

    def add_model(
        self,
        label: str,
        model_id: str,
        credential_id: str,
        context_window_tokens: int,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        if not 1 <= len(label.strip()) <= 100 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", model_id
        ):
            raise ConfigurationError("Use a model label and a valid provider model ID")
        safety = self.agent.config.model.context_safety_margin_tokens
        usable_window = min(context_window_tokens, self.agent.config.budgets.max_tokens_per_run)
        if (
            not 256 <= context_window_tokens <= 2000000
            or not 1 <= max_output_tokens < usable_window - safety
        ):
            raise ConfigurationError(
                "Context window must leave room for output and the safety margin"
            )
        with self.lock:
            state = self._state()
            credential = self._credentials(state).get(credential_id)
            if credential is None:
                raise ConfigurationError("Choose a saved API key")
            if len(state["models"]) >= 200:
                raise ConfigurationError("At most 200 models can be saved")
            if any(
                item["label"].casefold() == label.strip().casefold()
                for item in state["models"].values()
            ):
                raise ConfigurationError("Model label already exists")
            state["models"]["profile_" + uuid4().hex] = {
                "label": label.strip(),
                "model_id": model_id,
                "credential_id": credential_id,
                "provider": credential["provider"],
                "context_window_tokens": context_window_tokens,
                "max_output_tokens": max_output_tokens,
            }
            self.vault.save(state)
            return self.catalog()

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            state = self._state()
            values = {
                "TALOS_MODEL_" + key.upper(): item["api_key"]
                for key, item in state["credentials"].items()
            }
            self.agent.refresh_provider_credentials(values)
            return self.catalog()

    def runtime(self, identifier: str) -> tuple[ProviderDefinition, ModelDefinition, ModelProvider]:
        with self.lock:
            state = self._state()
            entry = state["models"].get(identifier)
            if entry is None:
                raise ConfigurationError("Saved model was not found; refresh model configuration")
            credential = self._credentials(state).get(entry["credential_id"])
            if credential is None or credential["provider"] != entry["provider"]:
                raise ConfigurationError("The model's API key is unavailable")
            provider = self._providers().get(entry["provider"])
            if provider is None:
                raise ConfigurationError("The model's provider is unavailable")
            environment = dict(self.environment)
            if not entry["credential_id"].startswith("env:"):
                reference = "TALOS_MODEL_" + entry["credential_id"].upper()
                provider = replace(provider, api_key_env=reference)
                environment[reference] = state["credentials"][entry["credential_id"]]["api_key"]
            model = replace(
                self.agent.config.model,
                alias=identifier,
                provider=provider.alias,
                model_id=entry["model_id"],
                context_window_tokens=entry["context_window_tokens"],
                max_output_tokens=entry["max_output_tokens"],
                cost_rates=None,
            )
            config = replace(self.agent.config, provider=provider, model=model)
            return provider, model, provider_from_config(config, environment=environment)
