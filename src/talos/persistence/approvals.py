from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from talos.persistence.database import Database
from talos.persistence.models import ToolApprovalRow
from talos.tools.runtime import ToolError


@dataclass(frozen=True, slots=True)
class ToolProposal:
    id: str
    name: str
    version: str
    arguments: dict[str, object]
    argument_sha256: str
    state: str
    expires_at: datetime
    result: object | None
    error_code: str | None


class SqlToolApprovalStore:
    def __init__(self, database: Database, *, ttl_seconds: int) -> None:
        if not 1 <= ttl_seconds <= 86400:
            raise ToolError("Approval expiry must be between 1 and 86400 seconds")
        self._database = database
        self._ttl = ttl_seconds

    async def propose(self, name: str, version: str, arguments: dict[str, object]) -> ToolProposal:
        now = datetime.now(UTC)
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        row = ToolApprovalRow(
            id=str(uuid4()),
            name=name,
            version=version,
            argument_sha256=digest,
            arguments=arguments,
            state="pending",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self._ttl),
        )
        async with self._database.sessions.begin() as session:
            session.add(row)
        return _proposal(row)

    async def get(self, proposal_id: str) -> ToolProposal | None:
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(ToolApprovalRow).where(ToolApprovalRow.id == proposal_id).with_for_update()
            )
            if row is not None:
                _expire(row, datetime.now(UTC))
            return _proposal(row) if row is not None else None

    async def decide(
        self, proposal_id: str, *, approve: bool, current_version: str | None
    ) -> tuple[ToolProposal, bool] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(ToolApprovalRow).where(ToolApprovalRow.id == proposal_id).with_for_update()
            )
            if row is None:
                return None
            if row.state != "pending":
                _expire(row, now)
                return _proposal(row), False
            if _expiry(row) <= now or row.version != current_version:
                row.state = "expired"
                row.error_code = "expired_or_version_changed"
            elif not approve:
                row.state = "rejected"
            else:
                row.state = "executing"
            if row.state in {"expired", "rejected"}:
                row.arguments = {}
            row.updated_at = now
            return _proposal(row), row.state == "executing"

    async def finish(
        self, proposal_id: str, *, result: object | None = None, error_code: str | None = None
    ) -> ToolProposal:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            row = await session.scalar(
                select(ToolApprovalRow).where(ToolApprovalRow.id == proposal_id).with_for_update()
            )
            if row is None or row.state != "executing":
                raise ToolError("Tool approval is not executing")
            row.state = "uncertain" if error_code else "completed"
            row.result = result
            row.error_code = error_code
            row.arguments = {}
            row.updated_at = now
            return _proposal(row)

    async def reconcile_executing(self) -> int:
        """Mark interrupted side effects uncertain; never execute them a second time."""
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(ToolApprovalRow).where(ToolApprovalRow.state == "executing")
                )
            )
            for row in rows:
                row.state = "uncertain"
                row.error_code = "service_restarted"
                row.arguments = {}
                row.updated_at = now
        return len(rows)


def _proposal(row: ToolApprovalRow) -> ToolProposal:
    return ToolProposal(
        id=row.id,
        name=row.name,
        version=row.version,
        arguments=dict(row.arguments),
        argument_sha256=row.argument_sha256,
        state=row.state,
        expires_at=row.expires_at,
        result=row.result,
        error_code=row.error_code,
    )


def _expiry(row: ToolApprovalRow) -> datetime:
    return (
        row.expires_at if row.expires_at.tzinfo is not None else row.expires_at.replace(tzinfo=UTC)
    )


def _expire(row: ToolApprovalRow, now: datetime) -> None:
    if row.state in {"pending", "executing"} and _expiry(row) <= now:
        row.state = "expired" if row.state == "pending" else "uncertain"
        row.error_code = "expired" if row.state == "expired" else "execution_uncertain"
        row.arguments = {}
        row.updated_at = now
