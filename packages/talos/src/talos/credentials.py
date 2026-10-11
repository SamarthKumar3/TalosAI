from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import keyring
from dotenv import dotenv_values
from keyring.errors import KeyringError

from talos.errors import ConfigurationError

_CREDENTIAL_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> bool: ...


class KeyringCredentialStore:
    """Provider credentials backed by the operating system credential vault."""

    def __init__(self, service_name: str) -> None:
        if not service_name.strip():
            raise ConfigurationError("TALOS_CREDENTIAL_SERVICE_NAME must not be empty")
        self._service_name = service_name

    def get(self, name: str) -> str | None:
        _validate_name(name)
        try:
            return keyring.get_password(self._service_name, name)
        except KeyringError as exc:
            raise ConfigurationError(
                "The operating system credential vault is unavailable"
            ) from exc

    def set(self, name: str, value: str) -> None:
        _validate_name(name)
        _validate_value(value)
        try:
            keyring.set_password(self._service_name, name, value)
        except KeyringError as exc:
            raise ConfigurationError(
                "The operating system credential vault rejected the credential"
            ) from exc

    def delete(self, name: str) -> bool:
        _validate_name(name)
        try:
            if keyring.get_password(self._service_name, name) is None:
                return False
            keyring.delete_password(self._service_name, name)
            return True
        except KeyringError as exc:
            raise ConfigurationError(
                "The operating system credential vault could not delete the credential"
            ) from exc


def migrate_legacy_credential_file(path: Path, store: CredentialStore) -> tuple[str, ...]:
    """Move valid legacy credentials into the vault, deleting plaintext after success."""
    if not path.is_file():
        return ()
    credentials: list[tuple[str, str]] = []
    for name, value in dotenv_values(path).items():
        if value is None or not value:
            continue
        _validate_name(name)
        _validate_value(value)
        credentials.append((name, value))
    if not credentials:
        path.unlink()
        return ()
    for name, value in credentials:
        store.set(name, value)
    path.unlink()
    return tuple(name for name, _ in credentials)


def _validate_name(name: str) -> None:
    if not _CREDENTIAL_NAME.fullmatch(name):
        raise ConfigurationError("Credential environment variable name is invalid")


def _validate_value(value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ConfigurationError("Provider API key must be a non-empty single-line value")
