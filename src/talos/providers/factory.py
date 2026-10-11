from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any, cast

from talos.config import TalosConfig
from talos.errors import ConfigurationError
from talos.providers.base import ModelProvider

_ALLOWED_ADAPTER_PREFIX = "talos.providers."


def provider_from_config(
    config: TalosConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> ModelProvider:
    env = os.environ if environment is None else environment
    api_key = env.get(config.provider.api_key_env, "")
    module_name, separator, class_name = config.provider.adapter.partition(":")
    if not separator or not module_name.startswith(_ALLOWED_ADAPTER_PREFIX):
        raise ConfigurationError("Provider adapter is not in the Talos allowlist")
    try:
        module = importlib.import_module(module_name)
        adapter_type: Any = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError("Configured provider adapter could not be loaded") from exc
    provider = adapter_type(
        api_base=config.provider.api_base,
        api_key=api_key,
        connect_timeout_seconds=config.provider.connect_timeout_seconds,
        read_timeout_seconds=config.provider.read_timeout_seconds,
    )
    return cast(ModelProvider, provider)
