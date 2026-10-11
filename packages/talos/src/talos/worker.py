from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from redis.asyncio import Redis

from talos.agent import TalosAgent
from talos.cli.environment import find_project_root, load_cli_environment
from talos.config import TalosConfig
from talos.domain import ConfigurationSnapshot
from talos.evaluation import (
    EvaluationCase,
    EvaluationOutput,
    EvaluationSettings,
    execute_evaluation,
    output_from_run,
)
from talos.knowledge import EmbeddingTransientError, KnowledgeService
from talos.persistence.database import Database
from talos.persistence.evaluation import SqlEvaluationStore
from talos.persistence.knowledge import SqlKnowledgeStore
from talos.persistence.repository import SqlRepositoryStore
from talos.providers.factory import provider_from_config
from talos.repository import RepositorySettings
from talos.repository_index import RepositoryIndexService, files_from_payload

logger = logging.getLogger("talos.worker")


async def _renew_lease(store: SqlKnowledgeStore, source_id: str, attempt: int) -> None:
    while True:
        await asyncio.sleep(20)
        if not await store.renew(source_id, attempt):
            return


async def _renew_repository_lease(store: SqlRepositoryStore, version_id: str, attempt: int) -> None:
    while True:
        await asyncio.sleep(20)
        if not await store.renew(version_id, attempt):
            return


async def _renew_evaluation_lease(store: SqlEvaluationStore, run_id: str, attempt: int) -> None:
    while True:
        await asyncio.sleep(20)
        if not await store.renew(run_id, attempt):
            return


async def run_worker() -> None:
    redis_url = os.environ["REDIS_URL"]
    queue_name = os.environ["TALOS_REDIS_QUEUE"]
    poll_seconds = int(os.environ["TALOS_WORKER_POLL_SECONDS"])
    client = Redis.from_url(redis_url, decode_responses=True)
    project_root = find_project_root(Path(os.environ.get("TALOS_PROJECT_ROOT", Path.cwd())))
    environment = load_cli_environment(project_root)
    config = TalosConfig.from_file(
        project_root / environment["TALOS_LOCAL_CONFIG_PATH"], environment=environment
    )
    database = Database(environment["DATABASE_URL"])
    store = SqlKnowledgeStore(
        database,
        max_attempts=config.rag.max_ingestion_attempts,
        retry_base_seconds=config.rag.retry_base_seconds,
    )
    knowledge = KnowledgeService(store, config.rag, environment=environment)
    repositories = SqlRepositoryStore(
        database,
        max_attempts=config.rag.max_ingestion_attempts,
        retry_base_seconds=config.rag.retry_base_seconds,
    )
    evaluations = SqlEvaluationStore(database, EvaluationSettings.from_environment(environment))
    repository_index = RepositoryIndexService(
        repositories,
        knowledge.embedder,
        config.rag,
        RepositorySettings.from_environment(environment),
    )
    try:
        await client.ping()
        logger.info("worker.ready", extra={"queue": queue_name})
        while True:
            if await _process_evaluation(evaluations, config, environment, store, repositories):
                continue
            if await _process_repository(repositories, repository_index):
                continue
            if await _process_knowledge(store, knowledge):
                continue
            item = await client.blpop(queue_name, timeout=poll_seconds)
            if item is not None:
                # The queue carries IDs only. The next loop claims durable state.
                logger.debug("worker.job_notification")
    finally:
        await client.aclose()
        await database.close()


async def _process_evaluation(
    evaluations: SqlEvaluationStore,
    config: TalosConfig,
    environment: Mapping[str, str],
    knowledge_store: SqlKnowledgeStore,
    repositories: SqlRepositoryStore,
) -> bool:
    claim = await evaluations.claim_next()
    if claim is None:
        return False
    heartbeat = asyncio.create_task(
        _renew_evaluation_lease(evaluations, claim.run_id, claim.attempt)
    )

    async def run_case(
        snapshot: ConfigurationSnapshot, case: EvaluationCase, seed: int
    ) -> EvaluationOutput:
        selected = TalosConfig.from_snapshot(snapshot, template=config)
        isolated_agent = TalosAgent(
            selected,
            provider_from_config(selected, environment=environment),
            knowledge_store=knowledge_store,
            repository_store=repositories,
            environment=environment,
        )
        started_at = time.perf_counter()
        result = await isolated_agent.run(
            case.input,
            correlation_id=f"evaluation:{claim.run_id}:{case.id}:{seed}",
        )
        rates = selected.model.cost_rates
        cost = (
            rates.estimate(result.usage.input_tokens, result.usage.output_tokens) if rates else None
        )
        return output_from_run(result, started_at=started_at, estimated_cost=cost)

    try:
        completed = await execute_evaluation(claim, store=evaluations, runner=run_case)
        logger.info(
            "evaluation.completed" if completed else "evaluation.cancelled",
            extra={"run_id": claim.run_id},
        )
    except Exception:
        await evaluations.fail(claim.run_id, claim.attempt, "evaluation_execution_failed")
        logger.exception("evaluation.failed", extra={"run_id": claim.run_id})
    finally:
        await _stop_heartbeat(heartbeat)
    return True


async def _process_repository(
    repositories: SqlRepositoryStore, repository_index: RepositoryIndexService
) -> bool:
    claim = await repositories.claim_next()
    if claim is None:
        return False
    repository_id, version_id, payload, attempt = claim
    heartbeat = asyncio.create_task(_renew_repository_lease(repositories, version_id, attempt))
    try:
        published = await repository_index.build(
            repository_id, version_id, files_from_payload(payload), attempt=attempt
        )
        if published:
            logger.info("repository.indexed", extra={"version_id": version_id})
    except EmbeddingTransientError:
        await repositories.fail(version_id, "embedding_transient", attempt=attempt, transient=True)
        logger.warning("repository.embedding_retry", extra={"version_id": version_id})
    except Exception:
        await repositories.fail(version_id, "index_failed", attempt=attempt)
        logger.exception("repository.index_failed", extra={"version_id": version_id})
    finally:
        await _stop_heartbeat(heartbeat)
    return True


async def _process_knowledge(store: SqlKnowledgeStore, knowledge: KnowledgeService) -> bool:
    claim = await store.claim_next()
    if claim is None:
        return False
    source_id, name, content, attempt = claim
    heartbeat = asyncio.create_task(_renew_lease(store, source_id, attempt))
    try:
        if name.lower().endswith(".pdf"):
            await knowledge.ingest_pdf(name, content, source_id=source_id, lease_attempt=attempt)
        else:
            await knowledge.ingest_text(name, content, source_id=source_id, lease_attempt=attempt)
        logger.info("knowledge.ingested", extra={"source_id": source_id})
    except EmbeddingTransientError:
        retrying = await store.retry(source_id, "embedding_transient", lease_attempt=attempt)
        logger.warning(
            "knowledge.embedding_retry" if retrying else "knowledge.embedding_not_retried",
            extra={"source_id": source_id, "attempt": attempt},
        )
    except Exception:
        await store.fail(source_id, "ingestion_failed", lease_attempt=attempt)
        logger.exception("knowledge.ingestion_failed", extra={"source_id": source_id})
    finally:
        await _stop_heartbeat(heartbeat)
    return True


async def _stop_heartbeat(heartbeat: asyncio.Task[None]) -> None:
    heartbeat.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat


def main() -> None:
    logging.basicConfig(level=os.environ.get("TALOS_LOG_LEVEL", "INFO"))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
