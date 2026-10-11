from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from talos.capabilities import CapabilityRegistry
from talos.config import ModelDefinition, ProviderDefinition, TalosConfig
from talos.context_window import ContextBudget, token_upper_bound
from talos.conversation_titles import conversation_title
from talos.conversations import ConversationStore, InMemoryConversationStore
from talos.domain import Conversation, Message, MessageAttachment, MessageRole, RunResult, Usage
from talos.errors import (
    ConfigurationError,
    ExecutionBudgetExceededError,
    ExecutionTimeoutError,
    RunCancelledError,
    RunInProgressError,
    TalosError,
)
from talos.event_store import InMemoryRuntimeEventStore, RuntimeEventStore
from talos.events import EventType, RuntimeEvent
from talos.graph import GraphResult, PipelineExecutor
from talos.guardrails import GuardrailService
from talos.knowledge import InMemoryKnowledgeStore, KnowledgeHit, KnowledgeService, KnowledgeStore
from talos.memory import InMemoryMemoryStore, MemoryRecord, MemoryService, MemoryStore
from talos.observability import ObservabilityService, span_payload
from talos.providers.base import ModelProvider
from talos.providers.factory import provider_from_config
from talos.repository import RepositorySettings
from talos.repository_index import RepositoryHit, RepositoryIndexService, RepositoryStore
from talos.skills import Skill, validate_skills
from talos.telemetry import OpenTelemetryRecorder
from talos.tools.runtime import Approval, ToolRegistry
from talos.why import WhyHistory, WhyReport, WhyReportService, safe_evidence

_QUEUE_END = object()


class TalosAgent:
    """Async, embeddable facade over the single Talos runtime."""

    def conversation_title(self, conversation: Conversation) -> str:
        prompt = next((m.content for m in conversation.messages if m.role is MessageRole.USER), "")
        return conversation_title(prompt, self._guardrails)

    def __init__(
        self,
        config: TalosConfig,
        provider: ModelProvider,
        *,
        conversation_store: ConversationStore | None = None,
        event_store: RuntimeEventStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        repository_store: RepositoryStore | None = None,
        memory_store: MemoryStore | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._current_config = config
        self._store = conversation_store or InMemoryConversationStore()
        self._event_store = event_store or InMemoryRuntimeEventStore()
        self._configs = {config.content_hash: config}
        self._providers = {config.content_hash: provider}
        self._tool_registries = {
            config.content_hash: ToolRegistry(
                config.tools,
                timeout_seconds=config.tool_timeout_seconds,
                max_response_bytes=config.tool_max_response_bytes,
            )
        }
        self._environment = dict(os.environ if environment is None else environment)
        self._guardrails = GuardrailService(config.guardrails)
        self._knowledge = KnowledgeService(
            knowledge_store or InMemoryKnowledgeStore(),
            config.rag,
            environment=self._environment,
        )
        self._memory_store = memory_store or InMemoryMemoryStore()
        self._memory = MemoryService(self._memory_store, config.memory)
        self._repository_store = repository_store
        self._repository = (
            RepositoryIndexService(
                repository_store,
                self._knowledge.embedder,
                config.rag,
                RepositorySettings.from_environment(self._environment),
            )
            if repository_store is not None
            else None
        )
        self._cancellations: dict[str, asyncio.Event] = {}
        self._observability = ObservabilityService(self._event_store, config.observability)
        self._telemetry = OpenTelemetryRecorder(config.observability)

    @classmethod
    def from_config(
        cls,
        path: str,
        *,
        environment: Mapping[str, str] | None = None,
        provider: ModelProvider | None = None,
        conversation_store: ConversationStore | None = None,
        event_store: RuntimeEventStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        repository_store: RepositoryStore | None = None,
        memory_store: MemoryStore | None = None,
    ) -> TalosAgent:
        config = TalosConfig.from_file(path, environment=environment)
        selected_provider = provider or provider_from_config(config, environment=environment)
        return cls(
            config,
            selected_provider,
            conversation_store=conversation_store,
            event_store=event_store,
            knowledge_store=knowledge_store,
            repository_store=repository_store,
            environment=environment,
            memory_store=memory_store,
        )

    @classmethod
    async def create(
        cls,
        config: TalosConfig,
        *,
        provider: ModelProvider,
        conversation_store: ConversationStore | None = None,
        event_store: RuntimeEventStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        repository_store: RepositoryStore | None = None,
        environment: Mapping[str, str] | None = None,
        memory_store: MemoryStore | None = None,
    ) -> TalosAgent:
        return cls(
            config,
            provider,
            conversation_store=conversation_store,
            event_store=event_store,
            knowledge_store=knowledge_store,
            repository_store=repository_store,
            environment=environment,
            memory_store=memory_store,
        )

    @property
    def config(self) -> TalosConfig:
        return self._current_config

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await self._store.get(conversation_id)

    async def new_conversation(
        self,
        *,
        model: ModelDefinition | None = None,
        provider_definition: ProviderDefinition | None = None,
        provider: ModelProvider | None = None,
    ) -> Conversation:
        current = self._current_config
        config = current
        if model is not None:
            if provider_definition is None or provider is None:
                raise ConfigurationError("A model selection requires its provider and credentials")
            if model.provider != provider_definition.alias:
                raise ConfigurationError("Model and provider do not match")
            pipeline = replace(current.pipeline, model_alias=model.alias)
            resolved = dict(current.raw)
            resolved.update(
                provider=asdict(provider_definition), model=asdict(model), pipeline=asdict(pipeline)
            )
            config = replace(
                self._updated_config(current, resolved),
                model=model,
                provider=provider_definition,
                pipeline=pipeline,
            )
            self._configs[config.content_hash] = config
            self._providers[config.content_hash] = provider
            self._tool_registries[config.content_hash] = self._tool_registries[
                current.content_hash
            ].fork()
        return await self._store.create(config.snapshot())

    def refresh_provider_credentials(self, credentials: Mapping[str, str]) -> None:
        """Refresh server-side credentials without changing secret-free pinned snapshots."""
        environment = {**self._environment, **credentials}
        replacements = {
            key: provider_from_config(config, environment=environment)
            for key, config in self._configs.copy().items()
            if config.provider.api_key_env in credentials
        }
        self._environment.update(credentials)
        self._providers.update(replacements)

    async def context_estimate(
        self, draft: str, conversation_id: str | None = None
    ) -> dict[str, Any]:
        conversation = await self._store.get(conversation_id) if conversation_id else None
        config = (
            self._conversation_runtime(conversation)[0] if conversation else self._current_config
        )
        budget = ContextBudget(
            window_tokens=min(
                config.model.context_window_tokens or config.budgets.max_tokens_per_run,
                config.budgets.max_tokens_per_run,
            ),
            output_tokens=config.model.max_output_tokens,
            safety_tokens=config.model.context_safety_margin_tokens,
        )
        history = (
            [message for message in conversation.messages if message.role is not MessageRole.NOTICE]
            if conversation and config.memory.short_term_enabled
            else []
        )
        messages = [
            Message(MessageRole.SYSTEM, config.system_prompt),
            *(Message(MessageRole.SYSTEM, skill.instructions) for skill in config.skills),
            *history,
            Message(MessageRole.USER, draft),
        ]
        tools = self._tool_registries[config.content_hash].model_specs()
        return {
            "used_tokens": token_upper_bound(messages, tools),
            "usable_input_tokens": budget.input_tokens,
            "context_window_tokens": config.model.context_window_tokens or budget.window_tokens,
            "reserved_output_tokens": budget.output_tokens,
            "estimated": True,
            "includes_retrieval": False,
        }

    def set_config(self, config: TalosConfig, *, provider: ModelProvider) -> None:
        """Select configuration for future conversations without changing existing ones."""
        self._current_config = config
        self._configs[config.content_hash] = config
        self._providers[config.content_hash] = provider
        self._tool_registries[config.content_hash] = ToolRegistry(
            config.tools,
            timeout_seconds=config.tool_timeout_seconds,
            max_response_bytes=config.tool_max_response_bytes,
        )
        self._knowledge = KnowledgeService(
            self._knowledge.store, config.rag, environment=self._environment
        )
        self._memory = MemoryService(self._memory_store, config.memory)
        self._observability = ObservabilityService(self._event_store, config.observability)
        self._telemetry = OpenTelemetryRecorder(config.observability)
        if self._repository_store is not None:
            self._repository = RepositoryIndexService(
                self._repository_store,
                self._knowledge.embedder,
                config.rag,
                RepositorySettings.from_environment(self._environment),
            )

    def list_tools(self) -> tuple[str, ...]:
        return self._tool_registries[self._current_config.content_hash].list()

    def guardrail_settings(self) -> dict[str, Any]:
        return self._guardrails.public_settings()

    def persisted_guardrail_settings(self) -> dict[str, Any]:
        return self._guardrails.persistence_settings()

    def restore_guardrail_settings(self, value: Mapping[str, Any]) -> None:
        self._guardrails.restore(value)

    def update_guardrail_settings(
        self,
        *,
        block_message: str,
        custom_keywords: tuple[str, ...],
        custom_profanity: tuple[str, ...],
        add_secrets: tuple[str, ...] = (),
        remove_secret_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self._guardrails.update(
            block_message=block_message,
            custom_keywords=custom_keywords,
            custom_profanity=custom_profanity,
            add_secrets=add_secrets,
            remove_secret_ids=remove_secret_ids,
        )

    def runtime_feature_settings(self) -> dict[str, Any]:
        config = self._current_config
        return {
            "configuration_version": config.version,
            "advanced_rag": {
                "enabled": config.rag.mode == "advanced",
                "query_rewriting": config.rag.query_rewriting,
                "query_expansion": config.rag.query_expansion,
                "dense_retrieval": config.rag.dense_enabled,
                "sparse_retrieval": config.rag.sparse_enabled,
                "reranking": config.rag.rerank_enabled,
            },
            "memory": {
                "enabled": config.capabilities.enabled("memory"),
                "short_term": config.memory.short_term_enabled,
                "summarization": config.memory.summarization_enabled,
                "long_term": config.memory.long_term_enabled,
            },
        }

    def update_runtime_feature_settings(
        self,
        *,
        advanced_rag_enabled: bool,
        query_rewriting: bool,
        query_expansion: bool,
        dense_retrieval: bool,
        sparse_retrieval: bool,
        reranking: bool,
        memory_enabled: bool,
        short_term: bool,
        summarization: bool,
        long_term: bool,
    ) -> dict[str, Any]:
        current = self._current_config
        if advanced_rag_enabled and not (dense_retrieval or sparse_retrieval):
            raise ConfigurationError(
                "Advanced RAG requires dense or sparse retrieval to remain enabled"
            )
        rag = replace(
            current.rag,
            mode="advanced" if advanced_rag_enabled else "basic",
            query_rewriting=query_rewriting,
            query_expansion=query_expansion,
            dense_enabled=dense_retrieval if advanced_rag_enabled else True,
            sparse_enabled=sparse_retrieval,
            rerank_enabled=reranking,
        )
        memory = replace(
            current.memory,
            short_term_enabled=memory_enabled and short_term,
            summarization_enabled=memory_enabled and summarization,
            long_term_enabled=memory_enabled and long_term,
        )
        capabilities = CapabilityRegistry(
            {
                item.id: replace(item, enabled=memory_enabled) if item.id == "memory" else item
                for item in current.capabilities.list()
            }
        )
        resolved = dict(current.raw)
        resolved_capabilities = dict(resolved.get("capabilities", {}))
        resolved_capabilities["memory"] = memory_enabled
        resolved["capabilities"] = resolved_capabilities
        resolved["rag"] = asdict(rag)
        resolved["memory"] = asdict(memory)
        updated = replace(
            self._updated_config(current, resolved),
            rag=rag,
            memory=memory,
            capabilities=capabilities,
        )
        self.set_config(updated, provider=self._providers[current.content_hash])
        return self.runtime_feature_settings()

    def tool_requires_approval(self, name: str) -> bool:
        return self._tool_registries[self._current_config.content_hash].requires_approval(name)

    def tool_version(self, name: str) -> str:
        return self._tool_registries[self._current_config.content_hash].version(name)

    def validate_tool_arguments(self, name: str, arguments: Mapping[str, Any]) -> None:
        self._tool_registries[self._current_config.content_hash].validate_arguments(name, arguments)

    def register_tool(
        self,
        function: Any,
        *,
        name: str | None = None,
        side_effect: bool = True,
        model_enabled: bool = False,
    ) -> None:
        current = self._current_config
        registry = self._tool_registries[current.content_hash].fork()
        registry.register(
            function,
            name=name,
            side_effect=side_effect,
            model_enabled=model_enabled,
        )
        selected = name or function.__name__
        resolved = dict(current.raw)
        entries = list(resolved.get("sdk_tools", []))
        entries.append(
            {
                "name": selected,
                "side_effect": side_effect,
                "model_enabled": model_enabled,
                "implementation_sha256": registry.version(selected),
                "schema_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "signature": str(inspect.signature(function)),
                            "description": inspect.getdoc(function),
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
            }
        )
        resolved["sdk_tools"] = entries
        updated = self._updated_config(current, resolved)
        self.set_config(updated, provider=self._providers[current.content_hash])
        self._tool_registries[updated.content_hash] = registry

    def register_tool_manifest(self, content: str, *, sandbox_root: str | None = None) -> None:
        """Add a bounded YAML tool, or a reviewed Python tool with an explicit sandbox root."""
        from functools import partial
        from pathlib import Path

        from talos.tools.declarative import manifest_callable, parse_manifest

        manifest = parse_manifest(content)
        runner = None
        if manifest["kind"] == "python":
            if sandbox_root is None:
                raise ConfigurationError("Python manifests require an explicit sandbox_root")
            from talos.tools.sandbox import reviewed_source, run_python

            root = Path(sandbox_root).resolve()
            reviewed_source(root, manifest)
            runner = partial(run_python, root)
        self.register_tool(
            manifest_callable(manifest, runner),
            name=manifest["name"],
            side_effect=manifest["kind"] == "python",
            model_enabled=manifest["kind"] != "python",
        )

    def list_skills(self) -> tuple[Skill, ...]:
        return self._current_config.skills

    def add_skill(self, skill: Skill) -> None:
        """Create a new configuration snapshot for future conversations."""
        self.set_skills((*self._current_config.skills, skill))

    def set_skills(self, skills: tuple[Skill, ...]) -> None:
        """Replace enabled skills for future conversations; existing snapshots remain pinned."""
        current = self._current_config
        capabilities = current.raw.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise ConfigurationError("Configuration capabilities are unavailable")
        validate_skills(skills, {name for name, enabled in capabilities.items() if enabled is True})
        resolved = dict(current.raw)
        resolved["skills"] = [{**asdict(item), "hash": item.content_hash} for item in skills]
        updated = replace(self._updated_config(current, resolved), skills=skills)
        registry = self._tool_registries[current.content_hash].fork()
        self.set_config(updated, provider=self._providers[current.content_hash])
        self._tool_registries[updated.content_hash] = registry

    @staticmethod
    def _updated_config(current: TalosConfig, resolved: dict[str, Any]) -> TalosConfig:
        resolved["version"] = current.version + 1
        digest = hashlib.sha256(
            json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return replace(
            current,
            version=current.version + 1,
            raw=MappingProxyType(resolved),
            content_hash=digest,
        )

    async def trace(self, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id is not None:
            return await self._observability.trace(run_id)
        traces = await self._observability.list_traces(limit=1)
        return traces[0] if traces else None

    def why_service(self) -> WhyReportService:
        return WhyReportService(
            self._store, self._event_store, self._observability, self._guardrails
        )

    async def why_report(
        self, conversation_id: str, offset: int = -1, *, expected_message_id: str | None = None
    ) -> WhyReport:
        return await self.why_service().explain_conversation_offset(
            conversation_id, offset, expected_message_id=expected_message_id
        )

    async def why_history(
        self, conversation_id: str, *, start: int = 0, limit: int = 20
    ) -> WhyHistory:
        return await self.why_service().history(conversation_id, start=start, limit=limit)

    async def traces(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return await self._observability.list_traces(limit=limit)

    async def trace_page(
        self,
        *,
        page: int,
        page_size: int = 15,
        model: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        cost_sort: str | None = None,
    ) -> dict[str, Any]:
        return await self._observability.trace_page(
            page=page,
            page_size=page_size,
            model=model,
            provider=provider,
            status=status,
            cost_sort=cost_sort,
        )

    async def usage_report(
        self,
        *,
        days: int = 10,
        start_date: date | None = None,
        end_date: date | None = None,
        group_by: str = "time",
        metric: str = "tokens",
    ) -> dict[str, Any]:
        return await self._observability.usage(
            days=days,
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            metric=metric,
        )

    async def observability_alerts(self) -> list[dict[str, Any]]:
        return await self._observability.alerts()

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        approval: Approval | None = None,
        expected_version: str | None = None,
    ) -> Any:
        decision = self._guardrails.inspect(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            stage="tool_input",
        )
        if decision.blocked:
            raise ConfigurationError(decision.message or "Tool input was blocked")
        registry = self._tool_registries[self._current_config.content_hash]
        if expected_version is not None and registry.version(name) != expected_version:
            raise ConfigurationError("Tool version changed after approval")
        return await registry.call(name, arguments, approval=approval)

    async def ingest_knowledge(self, name: str, content: str) -> Any:
        if name.lower().endswith(".pdf"):
            return await self._knowledge.ingest_pdf(name, content)
        return await self._knowledge.ingest_text(name, content)

    def validate_knowledge_source(self, name: str, content: str) -> None:
        self._knowledge.validate_source(name, content)

    async def search_knowledge(self, query: str) -> Any:
        return await self._knowledge.search(query)

    async def search_repository(self, query: str, *, repository_id: str | None = None) -> Any:
        if self._repository is None:
            return []
        return await self._repository.search(query, repository_id=repository_id)

    async def list_knowledge(self) -> Any:
        return await self._knowledge.store.list_sources()

    def list_capabilities(self) -> tuple[Any, ...]:
        return self._current_config.capabilities.list()

    async def remember(
        self,
        content: str,
        *,
        conversation_id: str,
        source_message_id: str | None = None,
    ) -> MemoryRecord:
        self._current_config.capabilities.require("memory")
        await self._store.get(conversation_id)
        decision = self._guardrails.inspect(content, stage="memory")
        if decision.blocked:
            raise ConfigurationError(decision.message or "Memory input was blocked")
        return await self._memory.remember(
            content,
            source_conversation_id=conversation_id,
            source_message_id=source_message_id,
        )

    async def list_memories(self, *, limit: int = 100) -> list[MemoryRecord]:
        self._current_config.capabilities.require("memory")
        if limit < 1 or limit > self._current_config.memory.max_long_term_items:
            raise ConfigurationError("Memory list limit is outside the configured bounds")
        return await self._memory.list(limit=limit)

    async def delete_memory(self, memory_id: str) -> bool:
        self._current_config.capabilities.require("memory")
        return await self._memory.delete(memory_id)

    async def run(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        attachments: Sequence[MessageAttachment] = (),
    ) -> RunResult:
        completed: RuntimeEvent | None = None
        async for event in self.stream(
            prompt,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            attachments=attachments,
        ):
            if event.type is EventType.RESPONSE_COMPLETED:
                completed = event
        if completed is None:
            raise RuntimeError("Agent run ended without a response")
        usage_data = completed.payload["usage"]
        return RunResult(
            run_id=completed.run_id,
            conversation_id=completed.conversation_id,
            response=str(completed.payload["response"]),
            model=str(completed.payload["model"]),
            usage=Usage(
                input_tokens=int(usage_data["input_tokens"]),
                output_tokens=int(usage_data["output_tokens"]),
            ),
            configuration_version=int(completed.payload["configuration_version"]),
            citations=tuple(completed.payload.get("citations", ())),
        )

    async def stream(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        attachments: Sequence[MessageAttachment] = (),
    ) -> AsyncIterator[RuntimeEvent]:
        _validate_run_request(prompt, conversation_id, idempotency_key)
        _validate_message_attachments(attachments, conversation_id)
        conversation = (
            await self.new_conversation()
            if conversation_id is None
            else await self._store.get(conversation_id)
        )
        config, provider = self._conversation_runtime(conversation)
        run_id = _run_id(conversation.id, idempotency_key)
        replay = await _replay_events(self._event_store, run_id, idempotency_key)
        if replay is not None:
            for event in replay:
                yield event
            return

        knowledge = KnowledgeService(
            self._knowledge.store, config.rag, environment=self._environment
        )
        memory = MemoryService(self._memory_store, config.memory)
        repository = _repository_service(
            self._repository_store, knowledge, config, self._environment
        )
        cancelled = asyncio.Event()
        self._cancellations[run_id] = cancelled
        execution = _AgentExecution(
            agent=self,
            conversation=conversation,
            config=config,
            provider=provider,
            prompt=prompt,
            run_id=run_id,
            correlation_id=correlation_id,
            cancelled=cancelled,
            knowledge=knowledge,
            memory=memory,
            repository=repository,
            attachments=attachments,
        )
        task = asyncio.create_task(execution.execute())
        try:
            while True:
                item = await execution.queue.get()
                if item is _QUEUE_END:
                    break
                assert isinstance(item, RuntimeEvent)
                yield item
            await task
            if execution.raised is not None:
                raise execution.raised
        finally:
            if not task.done():
                cancelled.set()
                task.cancel()

    def _conversation_runtime(
        self, conversation: Conversation
    ) -> tuple[TalosConfig, ModelProvider]:
        content_hash = conversation.configuration.content_hash
        config = self._configs.get(content_hash)
        if config is None:
            config = TalosConfig.from_snapshot(
                conversation.configuration, template=self._current_config
            )
            self._configs[config.content_hash] = config
            current = self._current_config
            if config.tools == current.tools and config.raw.get("sdk_tools") == current.raw.get(
                "sdk_tools"
            ):
                self._tool_registries[config.content_hash] = self._tool_registries[
                    current.content_hash
                ].fork()
            else:
                self._tool_registries[config.content_hash] = ToolRegistry(
                    config.tools,
                    timeout_seconds=config.tool_timeout_seconds,
                    max_response_bytes=config.tool_max_response_bytes,
                )
        provider = self._providers.get(content_hash)
        if provider is None:
            provider = provider_from_config(config, environment=self._environment)
            self._providers[config.content_hash] = provider
        return config, provider

    def cancel(self, run_id: str) -> bool:
        cancellation = self._cancellations.get(run_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True


def _validate_run_request(
    prompt: str, conversation_id: str | None, idempotency_key: str | None
) -> None:
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    if idempotency_key is not None and conversation_id is None:
        raise ConfigurationError("Idempotent chat requires an existing conversation")


def _validate_message_attachments(
    attachments: Sequence[MessageAttachment], conversation_id: str | None
) -> None:
    if attachments and conversation_id is None:
        raise ConfigurationError("Message attachments require an existing conversation")
    if len(attachments) > 10:
        raise ConfigurationError("A message cannot reference more than 10 attachments")
    identifiers = [item.source_id for item in attachments]
    if len(identifiers) != len(set(identifiers)):
        raise ConfigurationError("Message attachment references must be unique")
    if any(not item.source_id.strip() or not item.name.strip() for item in attachments):
        raise ConfigurationError("Message attachment references are invalid")


def _run_id(conversation_id: str, idempotency_key: str | None) -> str:
    if idempotency_key is None:
        return str(uuid4())
    return str(uuid5(NAMESPACE_URL, f"talos:{conversation_id}:{idempotency_key}"))


async def _replay_events(
    event_store: RuntimeEventStore, run_id: str, idempotency_key: str | None
) -> list[RuntimeEvent] | None:
    if idempotency_key is None:
        return None
    existing = await event_store.list_for_run(run_id)
    if not existing:
        return None
    terminal = {EventType.RUN_COMPLETED, EventType.RUN_CANCELLED, EventType.RUN_FAILED}
    if existing[-1].type not in terminal:
        raise RunInProgressError("An equivalent agent run is already active")
    return existing


def _repository_service(
    store: RepositoryStore | None,
    knowledge: KnowledgeService,
    config: TalosConfig,
    environment: Mapping[str, str],
) -> RepositoryIndexService | None:
    if store is None:
        return None
    return RepositoryIndexService(
        store,
        knowledge.embedder,
        config.rag,
        RepositorySettings.from_environment(environment),
    )


class _AgentExecution:
    def __init__(
        self,
        *,
        agent: TalosAgent,
        conversation: Conversation,
        config: TalosConfig,
        provider: ModelProvider,
        prompt: str,
        run_id: str,
        correlation_id: str | None,
        cancelled: asyncio.Event,
        knowledge: KnowledgeService,
        memory: MemoryService,
        repository: RepositoryIndexService | None,
        attachments: Sequence[MessageAttachment],
    ) -> None:
        self.agent = agent
        self.conversation = conversation
        self.config = config
        self.provider = provider
        self.prompt = prompt
        self.run_id = run_id
        self.correlation_id = correlation_id
        self.cancelled = cancelled
        self.knowledge = knowledge
        self.memory = memory
        self.repository = repository
        self.attachments = tuple(attachments)
        self.queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue(maxsize=64)
        self.sequence = 0
        self.raised: BaseException | None = None
        self.trace_id = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        self._compaction_notified = False
        self.root_span_id = uuid4().hex[:16]

    async def emit(self, event_type: EventType, payload: Mapping[str, Any]) -> None:
        self.sequence += 1
        event = RuntimeEvent(
            type=event_type,
            run_id=self.run_id,
            conversation_id=self.conversation.id,
            sequence=self.sequence,
            payload=payload,
        )
        await self.agent._event_store.append(event)
        await self.queue.put(event)

    async def emit_span(
        self,
        name: str,
        started_at: datetime,
        started_clock: float,
        attributes: Mapping[str, object],
        *,
        status: str = "ok",
    ) -> None:
        if not self.config.observability.tracing_enabled:
            return
        duration_ms = round((time.perf_counter() - started_clock) * 1000)
        self.agent._telemetry.record(
            name,
            started_at=started_at,
            duration_ms=duration_ms,
            attributes=attributes,
            status=status,
        )
        await self.emit(
            EventType.SPAN_COMPLETED,
            span_payload(
                name=name,
                trace_id=self.trace_id,
                span_id=uuid4().hex[:16],
                parent_span_id=self.root_span_id,
                started_at=started_at,
                duration_ms=duration_ms,
                status=status,
                attributes=attributes,
            ),
        )

    async def execute(self) -> None:
        try:
            await self._run()
        except RunCancelledError as exc:
            self.raised = exc
            await self.emit(EventType.RUN_CANCELLED, {"code": exc.code, "message": str(exc)})
        except TimeoutError:
            timeout_error = ExecutionTimeoutError("The agent run timed out")
            self.raised = timeout_error
            await self.emit(
                EventType.RUN_FAILED,
                {"code": timeout_error.code, "message": str(timeout_error)},
            )
        except TalosError as exc:
            self.raised = exc
            await self.emit(
                EventType.RUN_FAILED,
                {"code": exc.code, "message": str(exc), "retryable": exc.retryable},
            )
        except Exception as exc:
            self.raised = exc
            await self.emit(
                EventType.RUN_FAILED,
                {"code": "internal_error", "message": "The agent run failed"},
            )
        finally:
            self.agent._cancellations.pop(self.run_id, None)
            await self.queue.put(_QUEUE_END)

    async def _run(self) -> None:
        await self.emit(EventType.RUN_STARTED, self._started_payload())
        decision = self.agent._guardrails.inspect(self.prompt, stage="input")
        if decision.blocked:
            await self._complete_blocked_input(decision)
            return
        user_message = Message(MessageRole.USER, self.prompt, attachments=self.attachments)
        history = await self._load_memory_context()
        await self.agent._store.append(self.conversation.id, user_message)
        async with asyncio.timeout(self.config.budgets.max_execution_seconds):
            hits, repository_hits = await self._retrieve(history)
            result = await self._call_model(history, user_message, hits, repository_hits)
        self._validate_usage(result.usage)
        await self._complete_model_response(result)

    def _started_payload(self) -> dict[str, Any]:
        return {
            "configuration_version": self.config.version,
            "configuration_hash": self.config.content_hash,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id or self.trace_id,
            "root_span_id": self.root_span_id,
            "provider": self.config.provider.alias,
            "model": self.config.model.model_id,
            "cost_rate": (
                asdict(self.config.model.cost_rates)
                if self.config.model.cost_rates is not None
                else None
            ),
        }

    async def _complete_blocked_input(self, decision: Any) -> None:
        response = decision.message or self.config.guardrails.default_block_message
        safe_prompt = decision.safe_content or ("*" * len(self.prompt))
        await self.agent._store.append(
            self.conversation.id,
            Message(MessageRole.USER, safe_prompt, attachments=self.attachments),
        )
        await self.emit(
            EventType.GUARDRAIL_BLOCKED,
            {"category": decision.category, "rule_id": decision.rule_id, "stage": "input"},
        )
        await self.agent._store.append(
            self.conversation.id, Message(MessageRole.ASSISTANT, response, run_id=self.run_id)
        )
        await self.emit(EventType.RESPONSE_DELTA, {"text": response})
        await self.emit(
            EventType.RESPONSE_COMPLETED,
            {
                "response": response,
                "model": "guardrail",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "configuration_version": self.config.version,
                "citations": (),
            },
        )
        await self.emit(EventType.RUN_COMPLETED, {})

    async def _load_memory_context(self) -> Sequence[Message]:
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        history, summary, summary_created = await self.memory.context_for(self.conversation)
        await self.emit_span(
            "memory.load",
            started_at,
            started_clock,
            {"message_count": len(history), "summary_loaded": summary is not None},
        )
        if summary_created and summary is not None:
            await self._record_compaction(summary)
        return history

    async def _record_compaction(self, summary: Any) -> None:
        if self._compaction_notified:
            return
        await self.emit(EventType.MEMORY_COMPACTING, {})
        await self.agent._store.append(
            self.conversation.id,
            Message(MessageRole.NOTICE, "Context automatically compacted"),
        )
        await self.emit(
            EventType.MEMORY_SUMMARY_CREATED,
            {
                "summary_id": summary.id,
                "start_sequence": summary.start_sequence,
                "end_sequence": summary.end_sequence,
                "source_sha256": summary.source_checksum,
            },
        )
        self._compaction_notified = True

    async def _compact_context(self, removed: Sequence[Message], max_characters: int) -> None:
        summary = await self.memory.compact_removed(self.conversation, removed, max_characters)
        if summary is not None:
            await self._record_compaction(summary)

    async def _retrieve(
        self, history: Sequence[Message]
    ) -> tuple[list[KnowledgeHit], list[RepositoryHit]]:
        queries = tuple(item.content for item in history if item.role is MessageRole.USER)
        enabled = any(node.kind == "knowledge_context" for node in self.config.pipeline.nodes)
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        hits = await self._knowledge_hits(queries, enabled)
        repository_hits = await self._repository_hits(queries, enabled)
        await self._emit_retrieval(hits, repository_hits)
        await self.emit_span(
            "retrieval.fusion",
            started_at,
            started_clock,
            {
                "retrieval.mode": self.knowledge.last_metrics.mode,
                "retrieval.knowledge_count": len(hits),
                "retrieval.repository_count": len(repository_hits),
                "retrieval.query_variants": self.knowledge.last_metrics.query_variants,
            },
        )
        return hits, repository_hits

    async def _knowledge_hits(self, history: Sequence[str], enabled: bool) -> list[KnowledgeHit]:
        if not enabled:
            return []
        hits = await self.knowledge.search(
            self.prompt[: self.config.rag.max_query_characters],
            history=history,
            conversation_id=self.conversation.id,
        )
        return [
            hit
            for hit in hits
            if not self.agent._guardrails.inspect(hit.chunk.text, stage="retrieval").blocked
        ]

    async def _repository_hits(self, history: Sequence[str], enabled: bool) -> list[RepositoryHit]:
        if self.repository is None or not enabled:
            return []
        hits = await self.repository.search(
            self.prompt[: self.config.rag.max_query_characters], history=history
        )
        return [
            hit
            for hit in hits
            if not self.agent._guardrails.inspect(hit.text, stage="retrieval").blocked
        ]

    async def _emit_retrieval(
        self, hits: Sequence[KnowledgeHit], repository_hits: Sequence[RepositoryHit]
    ) -> None:
        enabled = any(n.kind == "knowledge_context" for n in self.config.pipeline.nodes)
        if not enabled:
            await self.emit(
                EventType.RETRIEVAL_COMPLETED,
                {
                    "enabled": False,
                    "knowledge": None,
                    "repository": None,
                },
            )
            return
        repository_metrics = self.repository.last_metrics if self.repository is not None else None
        await self.emit(
            EventType.RETRIEVAL_COMPLETED,
            {
                "enabled": enabled,
                "knowledge": {
                    "mode": self.knowledge.last_metrics.mode,
                    "query_variants": self.knowledge.last_metrics.query_variants,
                    "candidates": self.knowledge.last_metrics.candidates,
                    "dense_candidates": self.knowledge.last_metrics.dense_candidates,
                    "sparse_candidates": self.knowledge.last_metrics.sparse_candidates,
                    "returned": len(hits),
                    "duration_ms": self.knowledge.last_metrics.duration_ms,
                },
                "repository": (
                    {
                        "mode": repository_metrics.mode,
                        "query_variants": repository_metrics.query_variants,
                        "candidates": repository_metrics.candidates,
                        "dense_candidates": repository_metrics.dense_candidates,
                        "sparse_candidates": repository_metrics.sparse_candidates,
                        "returned": len(repository_hits),
                        "duration_ms": repository_metrics.duration_ms,
                    }
                    if repository_metrics is not None
                    else None
                ),
            },
        )

    async def _call_model(
        self,
        history: Sequence[Message],
        user_message: Message,
        hits: Sequence[KnowledgeHit],
        repository_hits: Sequence[RepositoryHit],
    ) -> GraphResult:
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        sources = [
            {
                "marker": f"[{h.marker}]",
                "source_id": h.chunk.source_id,
                "chunk_id": h.chunk.id,
                "path": h.chunk.source_name,
                "start_line": h.chunk.start_line,
                "end_line": h.chunk.end_line,
                "page_number": h.chunk.page_number,
                "checksum": h.chunk.checksum,
                "score": h.score,
            }
            for h in hits
        ] + [
            {
                "marker": f"[{h.marker}]",
                "path": h.path,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "commit": h.commit,
                "dirty": h.dirty,
                "snapshot": h.snapshot,
                "checksum": h.checksum,
                "score": h.score,
            }
            for h in repository_hits
        ]
        await self.emit(
            EventType.CONTEXT_RECORDED,
            safe_evidence(
                {
                    "sources": sources,
                    "score_type": "cosine" if self.config.rag.mode == "basic" else "hybrid ranking",
                },
                self.agent._guardrails,
            ),
        )
        result = await PipelineExecutor(self.config.pipeline).execute(
            config=self.config,
            provider=self.provider,
            messages=(*history, user_message),
            user_input=self.prompt,
            emit_delta=self._emit_delta,
            cancelled=self.cancelled,
            knowledge_hits=hits,
            repository_hits=repository_hits,
            tools=self.agent._tool_registries[self.config.content_hash],
            emit_tool=self._emit_tool,
            emit_retry=self._emit_retry,
            guardrails=self.agent._guardrails,
            on_compact=self._compact_context,
        )
        await self.emit_span(
            "model.call",
            started_at,
            started_clock,
            {
                "provider.alias": self.config.provider.alias,
                "model.alias": self.config.model.alias,
                "model.id": self.config.model.model_id,
                "usage.input_tokens": result.usage.input_tokens,
                "usage.output_tokens": result.usage.output_tokens,
            },
        )
        return result

    async def _emit_delta(self, text: str) -> None:
        await self.emit(EventType.RESPONSE_DELTA, {"text": text})

    async def _emit_tool(
        self, name: str, argument_hash: str, version: str, duration_ms: int, outcome: str
    ) -> None:
        event_type = EventType.TOOL_COMPLETED if outcome == "completed" else EventType.TOOL_FAILED
        await self.emit(
            event_type,
            {
                "name": name,
                "version": version,
                "argument_sha256": argument_hash,
                "duration_ms": duration_ms,
                "outcome": outcome,
            },
        )

    async def _emit_retry(self, attempt: int, delay: float, exc: TalosError) -> None:
        await self.emit(
            EventType.MODEL_RETRYING,
            {
                "attempt": attempt,
                "delay_milliseconds": round(delay * 1000),
                "code": exc.code,
            },
        )

    def _validate_usage(self, usage: Usage) -> None:
        if usage.input_tokens + usage.output_tokens > self.config.budgets.max_tokens_per_run:
            raise ExecutionBudgetExceededError("The run exceeded its configured token budget")

    async def _complete_model_response(self, result: GraphResult) -> None:
        await self.agent._store.append(
            self.conversation.id,
            Message(MessageRole.ASSISTANT, result.response, run_id=self.run_id),
        )
        await self.emit(
            EventType.RESPONSE_COMPLETED,
            {
                "response": result.response,
                "model": self.config.model.model_id,
                "usage": {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
                "configuration_version": self.config.version,
                "citations": result.citations,
            },
        )
        await self.emit(EventType.RUN_COMPLETED, {})
