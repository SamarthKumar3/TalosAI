from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from talos.persistence.database import Database
from talos.persistence.models import GuardrailSettingsRow


class SqlGuardrailSettingsStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self) -> Mapping[str, Any] | None:
        async with self._database.sessions() as session:
            row = await session.get(GuardrailSettingsRow, "default")
            return None if row is None else row.settings

    async def save(self, settings: Mapping[str, Any]) -> None:
        async with self._database.sessions.begin() as session:
            row = await session.get(GuardrailSettingsRow, "default")
            payload = _json_mapping(settings)
            if row is None:
                session.add(
                    GuardrailSettingsRow(
                        id="default",
                        settings=payload,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                row.settings = payload
                row.updated_at = datetime.now(UTC)


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (
            [_json_mapping(item) if isinstance(item, Mapping) else item for item in raw]
            if isinstance(raw, list)
            else _json_mapping(raw)
            if isinstance(raw, Mapping)
            else raw
        )
        for key, raw in value.items()
    }
