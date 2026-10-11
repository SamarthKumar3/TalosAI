from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from talos.domain import ConfigurationSnapshot, RunResult
from talos.errors import ConfigurationError, EvaluationError, TalosError


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    max_cases_per_dataset: int
    max_input_characters: int
    max_expected_output_characters: int
    max_attempts: int
    evaluator_version: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> EvaluationSettings:
        return cls(
            max_cases_per_dataset=_positive_integer(
                environment, "TALOS_EVALUATION_MAX_CASES_PER_DATASET"
            ),
            max_input_characters=_positive_integer(
                environment, "TALOS_EVALUATION_MAX_INPUT_CHARACTERS"
            ),
            max_expected_output_characters=_positive_integer(
                environment, "TALOS_EVALUATION_MAX_EXPECTED_OUTPUT_CHARACTERS"
            ),
            max_attempts=_positive_integer(environment, "TALOS_EVALUATION_MAX_ATTEMPTS"),
            evaluator_version=_required(environment, "TALOS_EVALUATOR_VERSION"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    ordinal: int
    input: str
    expected_output: str
    expected_citations: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationOutput:
    agent_run_id: str
    response: str
    citations: tuple[str, ...]
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float | None


@dataclass(frozen=True, slots=True)
class EvaluationClaim:
    run_id: str
    attempt: int
    evaluator_version: str
    seed: int
    baseline: ConfigurationSnapshot
    candidate: ConfigurationSnapshot
    cases: tuple[EvaluationCase, ...]
    gates: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    exact_match: float
    citation_recall: float
    passed: bool


class EvaluationRunStore(Protocol):
    async def is_cancelled(self, run_id: str, attempt: int) -> bool: ...

    async def completed_variants(self, run_id: str) -> set[tuple[str, str]]: ...

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
    ) -> None: ...

    async def complete(self, run_id: str, attempt: int) -> bool: ...


EvaluationRunner = Callable[
    [ConfigurationSnapshot, EvaluationCase, int], Awaitable[EvaluationOutput]
]


async def execute_evaluation(
    claim: EvaluationClaim,
    *,
    store: EvaluationRunStore,
    runner: EvaluationRunner,
) -> bool:
    completed = await store.completed_variants(claim.run_id)
    variants = (("baseline", claim.baseline), ("candidate", claim.candidate))
    for case in claim.cases:
        for variant, configuration in variants:
            if (case.id, variant) in completed:
                continue
            if await store.is_cancelled(claim.run_id, claim.attempt):
                return False
            try:
                output = await runner(configuration, case, claim.seed)
            except TalosError as exc:
                output = EvaluationOutput(
                    agent_run_id="",
                    response="",
                    citations=(),
                    latency_ms=0,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost=None,
                )
                score = EvaluationScore(0.0, 0.0, False)
                error_code: str | None = exc.code
            else:
                score = score_output(case, output)
                error_code = None
            await store.save_result(
                run_id=claim.run_id,
                attempt=claim.attempt,
                case=case,
                variant=variant,
                output=output,
                score=score,
                error_code=error_code,
            )
    return await store.complete(claim.run_id, claim.attempt)


def output_from_run(
    result: RunResult, *, started_at: float, estimated_cost: float | None
) -> EvaluationOutput:
    return EvaluationOutput(
        agent_run_id=result.run_id,
        response=result.response,
        citations=result.citations,
        latency_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        estimated_cost=estimated_cost,
    )


def score_output(case: EvaluationCase, output: EvaluationOutput) -> EvaluationScore:
    expected = _normalize(case.expected_output)
    actual = _normalize(output.response)
    exact = actual == expected
    exact_match = 1.0 if exact else 0.0
    if case.expected_citations:
        actual_citations = {_normalize_citation(value) for value in output.citations}
        expected_citations = {_normalize_citation(value) for value in case.expected_citations}
        citation_recall = len(actual_citations & expected_citations) / len(expected_citations)
        all_citations_present = expected_citations <= actual_citations
    else:
        citation_recall = 1.0
        all_citations_present = True
    return EvaluationScore(
        exact_match=exact_match,
        citation_recall=round(citation_recall, 6),
        passed=exact and all_citations_present,
    )


def validate_dataset_cases(
    cases: Sequence[Mapping[str, object]], settings: EvaluationSettings
) -> None:
    if not cases or len(cases) > settings.max_cases_per_dataset:
        raise EvaluationError(
            f"Evaluation datasets require 1-{settings.max_cases_per_dataset} cases"
        )
    for case in cases:
        input_value = case.get("input")
        expected = case.get("expected_output")
        if not isinstance(input_value, str) or not input_value.strip():
            raise EvaluationError("Every evaluation case requires non-empty input")
        if not isinstance(expected, str) or not expected.strip():
            raise EvaluationError("Every evaluation case requires a non-empty expected output")
        if len(input_value) > settings.max_input_characters:
            raise EvaluationError("Evaluation case input exceeds its configured limit")
        if len(expected) > settings.max_expected_output_characters:
            raise EvaluationError("Evaluation expected output exceeds its configured limit")


def dataset_checksum(name: str, version: int, cases: Sequence[Mapping[str, object]]) -> str:
    payload = {"name": name.strip(), "version": version, "cases": list(cases)}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def configuration_differences(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[str, ...]:
    differences: list[str] = []

    def visit(left: object, right: object, path: str) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in sorted(set(left) | set(right)):
                visit(left.get(key), right.get(key), f"{path}.{key}" if path else str(key))
            return
        if left != right:
            differences.append(path)

    visit(baseline, candidate, "")
    return tuple(differences)


def summarize_results(
    results: Sequence[Mapping[str, object]], gates: Mapping[str, float]
) -> dict[str, object]:
    by_variant = {
        variant: [item for item in results if item.get("variant") == variant]
        for variant in ("baseline", "candidate")
    }
    summary = {variant: _variant_summary(items) for variant, items in by_variant.items()}
    baseline = summary["baseline"]
    candidate = summary["candidate"]
    assert isinstance(baseline, dict) and isinstance(candidate, dict)
    delta = round(_number(candidate["pass_rate"]) - _number(baseline["pass_rate"]), 6)
    reasons = _gate_failures(candidate, delta, gates)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "pass_rate_delta": delta,
        "gate": {"passed": not reasons, "reasons": reasons, "thresholds": dict(gates)},
    }


def _variant_summary(items: Sequence[Mapping[str, object]]) -> dict[str, object]:
    count = len(items)
    latencies = sorted(_integer(item.get("latency_ms")) for item in items)
    costs = [
        _number(item.get("estimated_cost"))
        for item in items
        if isinstance(item.get("estimated_cost"), (int, float))
    ]

    def rate(matches: int) -> float:
        return round(matches / count, 6) if count else 0.0

    def average(key: str) -> float:
        total = sum(_number(item.get(key)) for item in items)
        return round(total / count, 6) if count else 0.0

    return {
        "cases": count,
        "pass_rate": rate(sum(1 for item in items if item.get("passed") is True)),
        "failure_rate": rate(sum(1 for item in items if item.get("error_code") is not None)),
        "exact_match": average("exact_match"),
        "citation_recall": average("citation_recall"),
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "input_tokens": sum(_integer(item.get("input_tokens")) for item in items),
        "output_tokens": sum(_integer(item.get("output_tokens")) for item in items),
        "estimated_cost": round(sum(costs), 8) if len(costs) == count else None,
    }


def _gate_failures(
    candidate: Mapping[str, object], delta: float, gates: Mapping[str, float]
) -> list[str]:
    checks = (
        (
            _number(candidate["pass_rate"]) < gates.get("minimum_candidate_pass_rate", 0.0),
            "candidate_pass_rate_below_minimum",
        ),
        (
            delta < -gates.get("maximum_pass_rate_drop", 1.0),
            "candidate_pass_rate_regressed",
        ),
        (
            _number(candidate["failure_rate"]) > gates.get("maximum_candidate_failure_rate", 1.0),
            "candidate_failure_rate_above_maximum",
        ),
        (
            _number(candidate["p95_latency_ms"])
            > gates.get("maximum_candidate_p95_latency_ms", math.inf),
            "candidate_p95_latency_above_maximum",
        ),
    )
    return [reason for failed, reason in checks if failed]


def response_checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _integer(value: object) -> int:
    return int(_number(value))


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _normalize_citation(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().casefold())


def _percentile(values: Sequence[int], quantile: float) -> int:
    if not values:
        return 0
    index = max(0, math.ceil(len(values) * quantile) - 1)
    return values[index]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return value


def _positive_integer(environment: Mapping[str, str], name: str) -> int:
    try:
        value = int(_required(environment, name))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value
