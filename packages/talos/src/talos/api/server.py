from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI
from redis.asyncio import Redis

from talos.agent import TalosAgent
from talos.api.app import ApiDependencies, ApiSettings, create_app
from talos.cli.environment import find_project_root, load_cli_environment
from talos.config import TalosConfig
from talos.evaluation import EvaluationSettings
from talos.extensions import ExtensionManager
from talos.model_profiles import ModelProfileManager
from talos.persistence import Database, SqlConversationStore, SqlMemoryStore, SqlRuntimeEventStore
from talos.persistence.approvals import SqlToolApprovalStore
from talos.persistence.evaluation import SqlEvaluationStore
from talos.persistence.guardrails import SqlGuardrailSettingsStore
from talos.persistence.knowledge import SqlKnowledgeStore
from talos.persistence.repository import SqlRepositoryStore
from talos.persistence.runtime_settings import SqlRuntimeSettingsStore
from talos.providers.factory import provider_from_config
from talos.repository import RepositorySettings
from talos.skills import Skill


def build_app() -> FastAPI:
    project_root = find_project_root(Path(os.environ.get("TALOS_PROJECT_ROOT", Path.cwd())))
    environment = load_cli_environment(project_root)
    config_path = project_root / environment["TALOS_LOCAL_CONFIG_PATH"]
    config = TalosConfig.from_file(config_path, environment=environment)
    database = Database(environment["DATABASE_URL"])
    conversations = SqlConversationStore(database)
    events = SqlRuntimeEventStore(database)
    memory = SqlMemoryStore(database)
    knowledge = SqlKnowledgeStore(
        database,
        max_attempts=config.rag.max_ingestion_attempts,
        retry_base_seconds=config.rag.retry_base_seconds,
    )
    approvals = SqlToolApprovalStore(
        database, ttl_seconds=int(environment["TALOS_TOOL_APPROVAL_TTL_SECONDS"])
    )
    repositories = SqlRepositoryStore(
        database,
        max_attempts=config.rag.max_ingestion_attempts,
        retry_base_seconds=config.rag.retry_base_seconds,
    )
    guardrails = SqlGuardrailSettingsStore(database)
    runtime_settings = SqlRuntimeSettingsStore(database)
    evaluations = SqlEvaluationStore(database, EvaluationSettings.from_environment(environment))
    agent = TalosAgent(
        config,
        provider_from_config(config, environment=environment),
        conversation_store=conversations,
        event_store=events,
        knowledge_store=knowledge,
        repository_store=repositories,
        memory_store=memory,
        environment=environment,
    )

    extensions = ExtensionManager(project_root, environment, agent)
    extensions.reload_tools()
    models = ModelProfileManager(project_root, environment, agent)
    models.refresh()

    async def notify_knowledge(job_id: str) -> None:
        redis = Redis.from_url(environment["REDIS_URL"], decode_responses=True)
        try:
            await redis.rpush(environment["TALOS_REDIS_QUEUE"], job_id)
        finally:
            await redis.aclose()

    async def reload_skills() -> tuple[Skill, ...]:
        updated = await asyncio.to_thread(
            TalosConfig.from_file, config_path, environment=environment
        )
        return updated.skills

    async def reconcile_interrupted_work() -> None:
        persisted = await runtime_settings.load()
        if persisted is not None:
            advanced_rag = persisted.get("advanced_rag", {})
            memory_settings = persisted.get("memory", {})
            if isinstance(advanced_rag, dict) and isinstance(memory_settings, dict):
                agent.update_runtime_feature_settings(
                    advanced_rag_enabled=bool(advanced_rag.get("enabled")),
                    query_rewriting=bool(advanced_rag.get("query_rewriting")),
                    query_expansion=bool(advanced_rag.get("query_expansion")),
                    dense_retrieval=bool(advanced_rag.get("dense_retrieval")),
                    sparse_retrieval=bool(advanced_rag.get("sparse_retrieval")),
                    reranking=bool(advanced_rag.get("reranking")),
                    memory_enabled=bool(memory_settings.get("enabled")),
                    short_term=bool(memory_settings.get("short_term")),
                    summarization=bool(memory_settings.get("summarization")),
                    long_term=bool(memory_settings.get("long_term")),
                )
        await events.reconcile_interrupted()
        await approvals.reconcile_executing()

    return create_app(
        agent=agent,
        conversations=conversations,
        event_store=events,
        settings=ApiSettings.from_environment(environment),
        dependencies=ApiDependencies(
            models=models,
            extensions=extensions,
            readiness=database.ready,
            knowledge_jobs=knowledge,
            knowledge_notifier=notify_knowledge,
            skill_reloader=reload_skills,
            tool_approvals=approvals,
            repository_jobs=repositories,
            repository_settings=RepositorySettings.from_environment(environment),
            guardrail_settings_store=guardrails,
            runtime_settings_store=runtime_settings,
            evaluation_store=evaluations,
            startup=reconcile_interrupted_work,
        ),
    )


app = build_app()
