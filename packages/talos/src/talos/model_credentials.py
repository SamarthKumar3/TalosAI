"""Authenticated local credential storage, excluded from configuration exports."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from talos.errors import ConfigurationError


class ModelCredentialVault:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        self.key_path = self.directory / "master.key"
        self.data_path = self.directory / "models.enc"

    def _checked(self, path: Path) -> None:
        if path.resolve() != path.absolute() or (path.exists() and not path.is_file()):
            raise ConfigurationError("Model credential storage must not use symlinks")
        if path.exists() and os.name != "nt" and path.stat().st_mode & 0o077:
            raise ConfigurationError("Model credential files require owner-only permissions")

    def _cipher(self, *, create: bool) -> Fernet:
        if self.directory.resolve() != self.directory:
            raise ConfigurationError("Model credential directory must not use symlinks")
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._checked(self.key_path)
        if not self.key_path.exists() and create:
            if self.data_path.exists():
                raise ConfigurationError("Credential encryption key is missing; restore its backup")
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        try:
            if self.key_path.stat().st_size != 44:
                raise ValueError("Invalid key")
            return Fernet(self.key_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Model credential encryption key is unavailable") from exc

    def load(self) -> dict[str, Any]:
        self._checked(self.data_path)
        if not self.data_path.exists():
            return {"credentials": {}, "models": {}}
        if self.data_path.stat().st_size > 1048576:
            raise ConfigurationError("Model credential storage exceeds its size limit")
        try:
            value = json.loads(self._cipher(create=False).decrypt(self.data_path.read_bytes()))
            if not isinstance(value, dict) or set(value) != {"credentials", "models"}:
                raise ValueError("Invalid storage")
            return value
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise ConfigurationError(
                "Model credentials could not be decrypted; restore a backup"
            ) from exc

    def save(self, value: dict[str, Any]) -> None:
        ciphertext = self._cipher(create=True).encrypt(json.dumps(value).encode())
        if len(ciphertext) > 1048576:
            raise ConfigurationError("Model credential storage exceeds its size limit")
        self._checked(self.data_path)
        descriptor, name = tempfile.mkstemp(prefix=".models-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.data_path)
        finally:
            temporary.unlink(missing_ok=True)
