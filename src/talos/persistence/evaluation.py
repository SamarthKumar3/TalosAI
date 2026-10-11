from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from talos.domain import ConfigurationSnapshot
from talos.errors import EvaluationError
from talos.evaluation import (
    EvaluationCase,
    EvaluationClaim,
    EvaluationOutput,
    EvaluationScore,
    EvaluationSettings,
    configuration_differences,
    dataset_checksum,
    response_checksum,
    summarize_results,
    validate_dataset_cases,
)
from talos.persistence.database import Database
from talos.persistence.models import (
    ConfigurationVersionRow,
    EvaluationCaseRow,
    EvaluationDatasetRow,
    EvaluationResultRow,
    EvaluationRunRow,
    JobRow,
)


class SqlEvaluationStore:
    def __init__(self, database: Database, settings: EvaluationSettings) -> None:
        self._database = database
        self.settings = settings

    async def create_dataset(
        self,
        *,
        name: str,
        version: int,
        description: str,
        cases: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        validate_dataset_cases(cases, self.settings)
        checksum = dataset_checksum(name, version, cases)
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            existing = await session.scalar(
                select(EvaluationDatasetRow).where(
                    or_(
                        EvaluationDatasetRow.content_hash == checksum,
                        and_(
                            EvaluationDatasetRow.name == name.strip(),
                            EvaluationDatasetRow.version == version,
                        ),
                    )
                )
            )
            if existing is not None:
                if existing.content_hash != checksum:
                    raise EvaluationError("Dataset name and version are immutable")
                return await self._dataset_payload(session, existing)
            dataset = EvaluationDatasetRow(
                id=str(uuid4()),
                name=name.strip(),
                version=version,
                description=description.strip(),
                content_hash=checksum,
                created_at=now,
            )
            session.add(dataset)
            await session.flush()
            for ordinal, item in enumerate(cases, 1):
                session.add(
                    EvaluationCaseRow(
                        id=str(uuid4()),
                        dataset_id=dataset.id,
                        ordinal=ordinal,
                        input=str(item["input"]).strip(),
                        expected_output=str(item["expected_output"]).strip(),
                        expected_citations=_strings(item.get("expected_citations", [])),
                        tags=_strings(item.get("tags", [])),
                    )
                )
            await session.flush()
            return await self._dataset_payload(session, dataset)

    async def list_datasets(self, *, limit: int = 50) -> list[dict[str, object]]:
        bounded = min(max(limit, 1), 100)
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(EvaluationDatasetRow)
                    .order_by(EvaluationDatasetRow.created_at.desc())
                    .limit(bounded)
                )
            )
            return [await self._dataset_payload(session, row, include_cases=False) for row in rows]

    async def dataset(self, dataset_id: str) -> dict[str, object] | None:
        async with self._database.sessions() as session:
            row = await session.get(EvaluationDatasetRow, dataset_id)
            return None if row is None else await self._dataset_payload(session, row)

    async def configurations(self, *, limit: int = 100) -> list[dict[str, object]]:
        async with self._database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ConfigurationVersionRow)
                    .order_by(ConfigurationVersionRow.created_at.desc())
                    .limit(min(max(limit, 1), 100))
                )
            )
        return [
            {
                "hash": row.content_hash,
                "version": row.schema_version,
                "provider": _nested_string(row.resolved, "provider", "alias"),
                "model": _nested_string(row.resolved, "model", "model_id"),
                "pipeline": _nested_string(row.resolved, "pipeline", "id"),
                "created_at": _aware(row.created_at).isoformat(),
            }
            for row in rows
        ]

    async def enqueue(
        self,
        *,
        dataset_id: str,
        baseline_configuration_hash: str,
        candidate_configuration_hash: str,
        seed: int,
        gates: Mapping[str, float],
        idempotency_key: str,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            existing_job = await session.scalar(
                select(JobRow).where(JobRow.idempotency_key == idempotency_key)
            )
            if existing_job is not None:
                existing_run = await session.scalar(
                    select(EvaluationRunRow).where(EvaluationRunRow.job_id == existing_job.id)
                )
                if existing_job.job_type != "evaluation_run" or existing_run is None:
                    raise EvaluationError("Idempotency key was used for another operation")
                if (
                    existing_run.dataset_id != dataset_id
                    or existing_run.baseline_configuration_hash != baseline_configuration_hash
                    or existing_run.candidate_configuration_hash != candidate_configuration_hash
                    or existing_run.seed != seed
                ):
                    raise EvaluationError("Idempotency key was used for different evaluation input")
                return _run_payload(existing_run, existing_job, result_count=0)
            dataset = await session.get(EvaluationDatasetRow, dataset_id)
            if dataset is None:
                raise EvaluationError("Evaluation dataset was not found")
            baseline = await session.scalar(
                select(ConfigurationVersionRow).where(
                    ConfigurationVersionRow.content_hash == baseline_configuration_hash
                )
            )
            candidate = await session.scalar(
                select(ConfigurationVersionRow).where(
                    ConfigurationVersionRow.content_hash == candidate_configuration_hash
                )
            )
            if baseline is None or candidate is None:
                raise EvaluationError("Both evaluation configurations must be persisted snapshots")
            if baseline.content_hash == candidate.content_hash:
                raise EvaluationError("Baseline and candidate configurations must be different")
            run_id = str(uuid4())
            job_id = str(uuid4())
            job = JobRow(
                id=job_id,
                job_type="evaluation_run",
                idempotency_key=idempotency_key,
                state="pending",
                attempt_count=0,
                cancellation_requested=False,
                input_reference=run_id,
                created_at=now,
                updated_at=now,
            )
            run = EvaluationRunRow(
                id=run_id,
                dataset_id=dataset_id,
                job_id=job_id,
                state="pending",
                baseline_configuration_hash=baseline.content_hash,
                candidate_configuration_hash=candidate.content_hash,
                changed_fields=list(
                    configuration_differences(baseline.resolved, candidate.resolved)
                ),
                evaluator_version=self.settings.evaluator_version,
                seed=seed,
                gates=dict(gates),
                created_at=now,
            )
            session.add_all([job, run])
            return _run_payload(run, job, result_count=0)

    async def list_runs(self, *, limit: int = 50) -> list[dict[str, object]]:
        bounded = min(max(limit, 1), 100)
        async with self._database.sessions() as session:
            rows = list(
                await session.execute(
                    select(EvaluationRunRow, JobRow)
                    .join(JobRow, JobRow.id == EvaluationRunRow.job_id)
                    .order_by(EvaluationRunRow.created_at.desc())
                    .limit(bounded)
                )
            )
            payloads: list[dict[str, object]] = []
            for run, job in rows:
                count = await session.scalar(
                    select(func.count(EvaluationResultRow.id)).where(
                        EvaluationResultRow.evaluation_run_id == run.id
                    )
                )
                payloads.append(_run_payload(run, job, result_count=int(count or 0)))
            return payloads

    async def run(self, run_id: str) -> dict[str, object] | None:
        async with self._database.sessions() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                return None
            job = await session.get(JobRow, run.job_id)
            if job is None:
                raise EvaluationError("Evaluation job is missing")
            rows = list(
                await session.execute(
                    select(EvaluationResultRow, EvaluationCaseRow)
                    .join(EvaluationCaseRow, EvaluationCaseRow.id == EvaluationResultRow.case_id)
                    .where(EvaluationResultRow.evaluation_run_id == run_id)
                    .order_by(EvaluationCaseRow.ordinal, EvaluationResultRow.variant)
                )
            )
            payload = _run_payload(run, job, result_count=len(rows))
            payload["results"] = [
                {
                    "case_id": result.case_id,
                    "ordinal": case.ordinal,
                    "variant": result.variant,
                    "agent_run_id": result.agent_run_id,
                    "response_checksum": result.response_checksum,
                    "exact_match": result.exact_match,
                    "citation_recall": result.citation_recall,
                    "passed": result.passed,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "estimated_cost": result.estimated_cost,
                    "error_code": result.error_code,
                }
                for result, case in rows
            ]
            return payload

    async def cancel(self, run_id: str) -> dict[str, object] | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run = await session.scalar(
                select(EvaluationRunRow).where(EvaluationRunRow.id == run_id).with_for_update()
            )
            if run is None:
                return None
            job = await session.get(JobRow, run.job_id)
            if job is None:
                raise EvaluationError("Evaluation job is missing")
            if job.state in {"pending", "processing"}:
                job.cancellation_requested = True
                job.state = "cancelled"
                job.lease_expires_at = None
                job.updated_at = now
                run.state = "cancelled"
                run.completed_at = now
            return _run_payload(run, job, result_count=0)

    async def claim_next(self, *, lease_seconds: int = 300) -> EvaluationClaim | None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.job_type == "evaluation_run",
                    JobRow.cancellation_requested.is_(False),
                    or_(
                        JobRow.state == "pending",
                        and_(JobRow.state == "processing", JobRow.lease_expires_at < now),
                    ),
                )
                .order_by(JobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            run = await session.get(EvaluationRunRow, job.input_reference)
            if run is None:
                job.state = "failed"
                job.error_code = "missing_evaluation_run"
                return None
            if job.attempt_count >= self.settings.max_attempts:
                job.state = "failed"
                job.error_code = "attempts_exhausted"
                run.state = "failed"
                run.error_code = job.error_code
                run.completed_at = now
                return None
            dataset = await session.get(EvaluationDatasetRow, run.dataset_id)
            baseline = await session.scalar(
                select(ConfigurationVersionRow).where(
                    ConfigurationVersionRow.content_hash == run.baseline_configuration_hash
                )
            )
            candidate = await session.scalar(
                select(ConfigurationVersionRow).where(
                    ConfigurationVersionRow.content_hash == run.candidate_configuration_hash
                )
            )
            if dataset is None or baseline is None or candidate is None:
                job.state = "failed"
                job.error_code = "evaluation_input_missing"
                run.state = "failed"
                run.error_code = job.error_code
                run.completed_at = now
                return None
            cases = list(
                await session.scalars(
                    select(EvaluationCaseRow)
                    .where(EvaluationCaseRow.dataset_id == dataset.id)
                    .order_by(EvaluationCaseRow.ordinal)
                )
            )
            job.state = "processing"
            job.attempt_count += 1
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            run.state = "processing"
            return EvaluationClaim(
                run_id=run.id,
                attempt=job.attempt_count,
                evaluator_version=run.evaluator_version,
                seed=run.seed,
                baseline=_snapshot(baseline),
                candidate=_snapshot(candidate),
                cases=tuple(_case(row) for row in cases),
                gates=run.gates,
            )

    async def renew(self, run_id: str, attempt: int, *, lease_seconds: int = 300) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == run.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
            ):
                return False
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            return True

    async def is_cancelled(self, run_id: str, attempt: int) -> bool:
        async with self._database.sessions() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                return True
            job = await session.get(JobRow, run.job_id)
            return bool(
                job is None
                or job.cancellation_requested
                or job.state != "processing"
                or job.attempt_count != attempt
            )

    async def completed_variants(self, run_id: str) -> set[tuple[str, str]]:
        async with self._database.sessions() as session:
            rows = list(
                await session.execute(
                    select(EvaluationResultRow.case_id, EvaluationResultRow.variant).where(
                        EvaluationResultRow.evaluation_run_id == run_id
                    )
                )
            )
            return {(case_id, variant) for case_id, variant in rows}

    async def save_result(
        self,
        *,
        run_id: str,
        attempt: int,
        case: EvaluationCase,
        variant: str,
        output: EvaluationOutput,
        score: EvaluationScore,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                raise EvaluationError("Evaluation run was not found")
            job = await session.scalar(
                select(JobRow).where(JobRow.id == run.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
            ):
                raise EvaluationError("Evaluation run was cancelled or lost its lease")
            existing = await session.scalar(
                select(EvaluationResultRow).where(
                    EvaluationResultRow.evaluation_run_id == run_id,
                    EvaluationResultRow.case_id == case.id,
                    EvaluationResultRow.variant == variant,
                )
            )
            if existing is not None:
                return
            session.add(
                EvaluationResultRow(
                    id=str(uuid4()),
                    evaluation_run_id=run_id,
                    case_id=case.id,
                    variant=variant,
                    agent_run_id=output.agent_run_id,
                    response_checksum=response_checksum(output.response),
                    exact_match=score.exact_match,
                    citation_recall=score.citation_recall,
                    passed=score.passed,
                    latency_ms=output.latency_ms,
                    input_tokens=output.input_tokens,
                    output_tokens=output.output_tokens,
                    estimated_cost=output.estimated_cost,
                    error_code=error_code,
                    created_at=now,
                )
            )

    async def complete(self, run_id: str, attempt: int) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == run.job_id).with_for_update()
            )
            if (
                job is None
                or job.state != "processing"
                or job.cancellation_requested
                or job.attempt_count != attempt
            ):
                return False
            rows = list(
                await session.scalars(
                    select(EvaluationResultRow).where(
                        EvaluationResultRow.evaluation_run_id == run_id
                    )
                )
            )
            case_count = await session.scalar(
                select(func.count(EvaluationCaseRow.id)).where(
                    EvaluationCaseRow.dataset_id == run.dataset_id
                )
            )
            if len(rows) != int(case_count or 0) * 2:
                raise EvaluationError("Evaluation results are incomplete")
            result_values = [_result_mapping(row) for row in rows]
            run.summary = summarize_results(result_values, run.gates)
            run.state = "completed"
            run.completed_at = now
            job.state = "completed"
            job.output_reference = run.id
            job.lease_expires_at = None
            job.updated_at = now
            return True

    async def fail(self, run_id: str, attempt: int, code: str) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions.begin() as session:
            run = await session.get(EvaluationRunRow, run_id)
            if run is None:
                return False
            job = await session.scalar(
                select(JobRow).where(JobRow.id == run.job_id).with_for_update()
            )
            if job is None or job.attempt_count != attempt or job.cancellation_requested:
                return False
            job.state = "failed"
            job.error_code = code
            job.lease_expires_at = None
            job.updated_at = now
            run.state = "failed"
            run.error_code = code
            run.completed_at = now
            return True

    async def _dataset_payload(
        self, session: Any, row: EvaluationDatasetRow, *, include_cases: bool = True
    ) -> dict[str, object]:
        cases = list(
            await session.scalars(
                select(EvaluationCaseRow)
                .where(EvaluationCaseRow.dataset_id == row.id)
                .order_by(EvaluationCaseRow.ordinal)
            )
        )
        payload: dict[str, object] = {
            "id": row.id,
            "name": row.name,
            "version": row.version,
            "description": row.description,
            "content_hash": row.content_hash,
            "case_count": len(cases),
            "created_at": _aware(row.created_at).isoformat(),
        }
        if include_cases:
            payload["cases"] = [
                {
                    "id": item.id,
                    "ordinal": item.ordinal,
                    "input": item.input,
                    "expected_output": item.expected_output,
                    "expected_citations": item.expected_citations,
                    "tags": item.tags,
                }
                for item in cases
            ]
        return payload


def _run_payload(run: EvaluationRunRow, job: JobRow, *, result_count: int) -> dict[str, object]:
    return {
        "id": run.id,
        "dataset_id": run.dataset_id,
        "state": run.state,
        "baseline_configuration_hash": run.baseline_configuration_hash,
        "candidate_configuration_hash": run.candidate_configuration_hash,
        "changed_fields": run.changed_fields,
        "evaluator_version": run.evaluator_version,
        "seed": run.seed,
        "gates": run.gates,
        "summary": run.summary,
        "error_code": run.error_code or job.error_code,
        "attempt_count": job.attempt_count,
        "cancellation_requested": job.cancellation_requested,
        "result_count": result_count,
        "created_at": _aware(run.created_at).isoformat(),
        "completed_at": _aware(run.completed_at).isoformat() if run.completed_at else None,
    }


def _snapshot(row: ConfigurationVersionRow) -> ConfigurationSnapshot:
    resolved = row.resolved
    return ConfigurationSnapshot(
        version=row.schema_version,
        content_hash=row.content_hash,
        provider_alias=_nested_string(resolved, "provider", "alias"),
        model_alias=_nested_string(resolved, "model", "alias"),
        model_id=_nested_string(resolved, "model", "model_id"),
        pipeline_id=_nested_string(resolved, "pipeline", "id"),
        prompt_id=_nested_string(resolved, "pipeline", "prompt_alias"),
        resolved=resolved,
    )


def _case(row: EvaluationCaseRow) -> EvaluationCase:
    return EvaluationCase(
        id=row.id,
        ordinal=row.ordinal,
        input=row.input,
        expected_output=row.expected_output,
        expected_citations=tuple(row.expected_citations),
        tags=tuple(row.tags),
    )


def _result_mapping(row: EvaluationResultRow) -> dict[str, object]:
    return {
        "variant": row.variant,
        "passed": row.passed,
        "exact_match": row.exact_match,
        "citation_recall": row.citation_recall,
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "estimated_cost": row.estimated_cost,
        "error_code": row.error_code,
    }


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvaluationError("Evaluation citations and tags must be string lists")
    return [item.strip() for item in value if item.strip()]


def _nested_string(value: Mapping[str, Any], parent: str, key: str) -> str:
    nested = value.get(parent)
    if not isinstance(nested, Mapping) or not isinstance(nested.get(key), str):
        raise EvaluationError("Persisted evaluation configuration is invalid")
    return str(nested[key])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
