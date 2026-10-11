from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from redis.exceptions import RedisError

from talos.agent import TalosAgent
from talos.api.contracts import (
    AgentResponse,
    ChatRequest,
    ChatResponse,
    ContextEstimateRequest,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationResponse,
    ErrorBody,
    EvaluationDatasetCreate,
    EvaluationRunCreate,
    GuardrailSettingsUpdate,
    KnowledgeCreateRequest,
    KnowledgeSearchRequest,
    MemoryCreateRequest,
    MessageAttachmentResponse,
    MessageResponse,
    RepositoryIndexRequest,
    RepositorySearchRequest,
    RuntimeFeatureSettingsUpdate,
    ToolApprovalDecisionRequest,
    ToolInvokeRequest,
    ToolUsageResponse,
    UsageResponse,
)
from talos.api.extension_routes import register_extension_routes
from talos.api.model_routes import register_model_routes
from talos.conversations import ConversationStore
from talos.domain import MessageAttachment
from talos.errors import ConfigurationError, ConversationNotFoundError, TalosError
from talos.event_store import RuntimeEventStore
from talos.events import EventType, RuntimeEvent
from talos.extensions import ExtensionManager
from talos.model_profiles import ModelProfileManager
from talos.persistence.approvals import SqlToolApprovalStore, ToolProposal
from talos.persistence.evaluation import SqlEvaluationStore
from talos.persistence.guardrails import SqlGuardrailSettingsStore
from talos.persistence.knowledge import SqlKnowledgeStore
from talos.persistence.repository import SqlRepositoryStore
from talos.persistence.runtime_settings import SqlRuntimeSettingsStore
from talos.repository import (
    RepositoryError,
    RepositoryFile,
    RepositorySettings,
    RepositorySnapshot,
    validate_repository_file,
)
from talos.skills import Skill
from talos.why import WhyHistory, WhyReport

ReadinessCheck = Callable[[], Awaitable[bool]]

_EVALUATION_STORAGE_UNAVAILABLE = "Evaluation storage is unavailable"
_TOOL_APPROVAL_UNAVAILABLE = "Tool approval is unavailable"
_KNOWLEDGE_INGESTION_UNAVAILABLE = "Knowledge ingestion is unavailable"
_REPOSITORY_INDEXING_UNAVAILABLE = "Repository indexing is unavailable"
_BAD_REQUEST_RESPONSE: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorBody, "description": "Invalid request"}
}
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "Resource not found"}
}
_PAYLOAD_TOO_LARGE_RESPONSE: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorBody, "description": "Request payload is too large"}
}
_SERVICE_UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {"model": ErrorBody, "description": "Required local service is unavailable"}
}
_BAD_REQUEST_OR_NOT_FOUND: dict[int | str, dict[str, Any]] = (
    _BAD_REQUEST_RESPONSE | _NOT_FOUND_RESPONSE
)
_STORAGE_RESPONSES: dict[int | str, dict[str, Any]] = (
    _NOT_FOUND_RESPONSE | _SERVICE_UNAVAILABLE_RESPONSE
)
_MUTATION_RESPONSES: dict[int | str, dict[str, Any]] = (
    _BAD_REQUEST_OR_NOT_FOUND | _SERVICE_UNAVAILABLE_RESPONSE
)
_CHAT_RESPONSES: dict[int | str, dict[str, Any]] = _MUTATION_RESPONSES | _PAYLOAD_TOO_LARGE_RESPONSE


@dataclass(frozen=True, slots=True)
class ApiSettings:
    host: str
    port: int
    allowed_origins: tuple[str, ...]
    local_token: str
    max_input_characters: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ApiSettings:
        remote = _boolean(environment, "TALOS_REMOTE_ACCESS_ENABLED")
        if remote:
            raise ConfigurationError(
                "Remote API access is disabled until authentication and TLS are configured"
            )
        configured_host = _required(environment, "TALOS_API_HOST")
        host = (
            configured_host if configured_host in {"127.0.0.1", "localhost", "::1"} else "127.0.0.1"
        )
        token = _required(environment, "TALOS_LOCAL_API_TOKEN")
        if token == "replace-with-random-local-token":
            raise ConfigurationError(
                "TALOS_LOCAL_API_TOKEN is still the unsafe example value; run 'talos start'"
            )
        port = _positive_integer(environment, "TALOS_API_PORT")
        max_input = _positive_integer(environment, "TALOS_MAX_INPUT_CHARACTERS")
        origins = tuple(
            value.strip()
            for value in _required(environment, "TALOS_API_ALLOWED_ORIGINS").split(",")
            if value.strip()
        )
        if not origins:
            raise ConfigurationError("TALOS_API_ALLOWED_ORIGINS must not be empty")
        return cls(host, port, origins, token, max_input)


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    models: ModelProfileManager | None = None
    extensions: ExtensionManager | None = None
    readiness: ReadinessCheck | None = None
    knowledge_jobs: SqlKnowledgeStore | None = None
    knowledge_notifier: Callable[[str], Awaitable[None]] | None = None
    skill_reloader: Callable[[], Awaitable[tuple[Skill, ...]]] | None = None
    tool_approvals: SqlToolApprovalStore | None = None
    repository_jobs: SqlRepositoryStore | None = None
    repository_settings: RepositorySettings | None = None
    guardrail_settings_store: SqlGuardrailSettingsStore | None = None
    runtime_settings_store: SqlRuntimeSettingsStore | None = None
    evaluation_store: SqlEvaluationStore | None = None
    startup: Callable[[], Awaitable[None]] | None = None


def create_app(
    *,
    agent: TalosAgent,
    conversations: ConversationStore,
    event_store: RuntimeEventStore,
    settings: ApiSettings,
    dependencies: ApiDependencies | None = None,
) -> FastAPI:
    runtime = dependencies or ApiDependencies()
    readiness = runtime.readiness
    knowledge_jobs = runtime.knowledge_jobs
    knowledge_notifier = runtime.knowledge_notifier
    skill_reloader = runtime.skill_reloader
    tool_approvals = runtime.tool_approvals
    repository_jobs = runtime.repository_jobs
    repository_settings = runtime.repository_settings
    guardrail_settings_store = runtime.guardrail_settings_store
    runtime_settings_store = runtime.runtime_settings_store
    evaluation_store = runtime.evaluation_store
    startup = runtime.startup

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if startup is not None:
            await startup()
        yield

    app = FastAPI(title="TalosAI API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
    )
    guardrails_loaded = False

    async def ensure_guardrails_loaded() -> None:
        nonlocal guardrails_loaded
        if guardrails_loaded or guardrail_settings_store is None:
            return
        persisted = await guardrail_settings_store.load()
        if persisted is not None:
            agent.restore_guardrail_settings(persisted)
        guardrails_loaded = True

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        await asyncio.sleep(0)
        expected = f"Bearer {settings.local_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="A valid local API token is required")

    @app.middleware("http")
    async def correlation(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.exception_handler(TalosError)
    async def talos_error(request: Request, exc: TalosError) -> JSONResponse:
        status = 404 if isinstance(exc, ConversationNotFoundError) else 400
        body = ErrorBody(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            correlation_id=request.state.correlation_id,
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Any:
        if request.url.path == "/api/v1/models/credentials":
            return JSONResponse(
                status_code=422,
                content={
                    "code": "validation_error",
                    "message": "Check the API key form fields",
                    "correlation_id": request.state.correlation_id,
                },
            )
        return await request_validation_exception_handler(request, exc)

    _register_core_routes(
        app,
        agent,
        conversations,
        event_store,
        settings,
        readiness,
        knowledge_jobs,
        authorize,
        ensure_guardrails_loaded,
        runtime.models,
    )
    _register_evaluation_routes(app, evaluation_store, knowledge_notifier, authorize)
    _register_settings_routes(
        app,
        agent,
        runtime_settings_store,
        guardrail_settings_store,
        authorize,
        ensure_guardrails_loaded,
    )
    register_extension_routes(app, runtime.extensions, authorize)
    register_model_routes(app, runtime.models, authorize)
    _register_tool_routes(
        app, agent, tool_approvals, skill_reloader, authorize, ensure_guardrails_loaded
    )
    _register_knowledge_routes(
        app, agent, conversations, knowledge_jobs, knowledge_notifier, authorize
    )
    _register_repository_routes(
        app,
        agent,
        repository_jobs,
        repository_settings,
        knowledge_notifier,
        authorize,
    )
    return app


def _register_core_routes(
    app: FastAPI,
    agent: TalosAgent,
    conversations: ConversationStore,
    event_store: RuntimeEventStore,
    settings: ApiSettings,
    readiness: ReadinessCheck | None,
    knowledge_jobs: SqlKnowledgeStore | None,
    authorize: Callable[..., Awaitable[None]],
    ensure_guardrails_loaded: Callable[[], Awaitable[None]],
    models: ModelProfileManager | None,
) -> None:
    @app.post("/api/v1/doctor", dependencies=[Depends(authorize)])
    async def run_doctor() -> dict[str, Any]:
        import os
        from pathlib import Path

        from talos.diagnostics import request_host_doctor

        directory = Path(os.environ.get("TALOS_PROJECT_ROOT", ".")) / ".talos/doctor"
        return await request_host_doctor(directory)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return await _readiness_response(readiness)

    @app.get("/api/v1/agent", dependencies=[Depends(authorize)])
    async def get_agent() -> AgentResponse:
        config = agent.config
        return AgentResponse(
            name=config.agent_name,
            provider=config.provider.alias,
            model_alias=config.model.alias,
            model=config.model.model_id,
            pipeline=config.pipeline.id,
            configuration_version=config.version,
            configuration_hash=config.content_hash,
        )

    @app.get("/api/v1/capabilities", dependencies=[Depends(authorize)])
    async def list_capabilities() -> dict[str, object]:
        return {
            "capabilities": [
                {
                    "id": item.id,
                    "label": item.label,
                    "description": item.description,
                    "enabled": item.enabled,
                    "requires": list(item.requires),
                }
                for item in agent.list_capabilities()
            ]
        }

    @app.get("/api/v1/memories", dependencies=[Depends(authorize)])
    async def list_memories(limit: int = 100) -> dict[str, object]:
        return {
            "memories": [
                {
                    "id": item.id,
                    "content": item.content,
                    "source_conversation_id": item.source_conversation_id,
                    "source_message_id": item.source_message_id,
                    "created_at": item.created_at.isoformat(),
                }
                for item in await agent.list_memories(limit=limit)
            ]
        }

    @app.post("/api/v1/memories", dependencies=[Depends(authorize)], status_code=201)
    async def create_memory(body: MemoryCreateRequest) -> dict[str, object]:
        await ensure_guardrails_loaded()
        item = await agent.remember(
            body.content,
            conversation_id=body.conversation_id,
            source_message_id=body.source_message_id,
        )
        return {"id": item.id, "created_at": item.created_at.isoformat()}

    @app.delete(
        "/api/v1/memories/{memory_id}",
        dependencies=[Depends(authorize)],
        responses=_NOT_FOUND_RESPONSE,
    )
    async def delete_memory(memory_id: str) -> dict[str, object]:
        if not await agent.delete_memory(memory_id):
            raise HTTPException(status_code=404, detail="Memory was not found")
        return {"id": memory_id, "deleted": True}

    @app.post(
        "/api/v1/conversations",
        dependencies=[Depends(authorize)],
    )
    async def create_conversation(
        body: ConversationCreateRequest | None = None,
    ) -> ConversationCreateResponse:
        selected = body.model_profile_id if body else None
        if selected and selected != agent.config.model.alias:
            if models is None:
                raise HTTPException(503, "Model management is unavailable")
            definition, model, provider = await asyncio.to_thread(models.runtime, selected)
            conversation = await agent.new_conversation(
                model=model, provider_definition=definition, provider=provider
            )
        else:
            conversation = await agent.new_conversation()
        return ConversationCreateResponse(
            id=conversation.id,
            configuration_version=conversation.configuration.version,
            created_at=conversation.created_at,
            model_profile_id=conversation.configuration.model_alias,
            model_id=conversation.configuration.model_id,
            provider=conversation.configuration.provider_alias,
        )

    @app.get(
        "/api/v1/conversations",
        dependencies=[Depends(authorize)],
    )
    async def list_conversations(limit: int = 50) -> list[ConversationResponse]:
        await ensure_guardrails_loaded()
        return [
            _conversation_response(item, title=agent.conversation_title(item))
            for item in await conversations.list(limit=limit)
        ]

    @app.get(
        "/api/v1/conversations/{conversation_id}",
        dependencies=[Depends(authorize)],
    )
    async def get_conversation(conversation_id: str) -> ConversationResponse:
        await ensure_guardrails_loaded()
        conversation = await conversations.get(conversation_id)
        tool_events = await event_store.list_tool_events(conversation_id)
        return _conversation_response(
            conversation, tool_events, title=agent.conversation_title(conversation)
        )

    @app.get("/api/v1/conversations/{conversation_id}/why", dependencies=[Depends(authorize)])
    async def get_why(
        conversation_id: str, offset: int = -1, expected_message_id: str | None = None
    ) -> WhyReport:
        return await agent.why_report(
            conversation_id, offset, expected_message_id=expected_message_id
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/why/history", dependencies=[Depends(authorize)]
    )
    async def get_why_history(conversation_id: str, start: int = 0, limit: int = 20) -> WhyHistory:
        return await agent.why_history(conversation_id, start=start, limit=limit)

    @app.post("/api/v1/context/estimate", dependencies=[Depends(authorize)])
    async def estimate_context(body: ContextEstimateRequest) -> dict[str, Any]:
        _validate_input(body.draft, settings.max_input_characters)
        return await agent.context_estimate(body.draft, body.conversation_id)

    @app.post(
        "/api/v1/chat",
        dependencies=[Depends(authorize)],
        responses=_CHAT_RESPONSES,
    )
    async def chat(
        body: ChatRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> ChatResponse:
        await ensure_guardrails_loaded()
        _validate_input(body.input, settings.max_input_characters)
        _validate_idempotency_key(idempotency_key)
        attachments = await _validated_message_attachments(body, knowledge_jobs)
        result = await agent.run(
            body.input,
            conversation_id=body.conversation_id,
            idempotency_key=idempotency_key,
            correlation_id=request.state.correlation_id,
            attachments=attachments,
        )
        return ChatResponse(
            run_id=result.run_id,
            conversation_id=result.conversation_id,
            response=result.response,
            model=result.model,
            usage=UsageResponse(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            ),
            configuration_version=result.configuration_version,
            citations=list(result.citations),
        )

    @app.post(
        "/api/v1/chat/stream",
        dependencies=[Depends(authorize)],
        responses=_CHAT_RESPONSES,
    )
    async def stream_chat(
        body: ChatRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None),
    ) -> StreamingResponse:
        await ensure_guardrails_loaded()
        _validate_input(body.input, settings.max_input_characters)
        _validate_idempotency_key(idempotency_key)
        attachments = await _validated_message_attachments(body, knowledge_jobs)

        event_stream = agent.stream(
            body.input,
            conversation_id=body.conversation_id,
            idempotency_key=idempotency_key,
            correlation_id=request.state.correlation_id,
            attachments=attachments,
        )
        first_event = await anext(event_stream)

        async def events() -> AsyncIterator[str]:
            yield _sse(first_event)
            try:
                async for event in event_stream:
                    yield _sse(event)
            except TalosError:
                return

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize)])
    async def cancel_run(run_id: str) -> dict[str, object]:
        return {"run_id": run_id, "cancellation_requested": agent.cancel(run_id)}

    @app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
    async def replay_events(run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "event_id": event.event_id,
                "type": event.type.value,
                "run_id": event.run_id,
                "conversation_id": event.conversation_id,
                "sequence": event.sequence,
                "schema_version": event.schema_version,
                "payload": dict(event.payload),
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in await event_store.list_for_run(
                run_id,
                after_sequence=after_sequence,
            )
        ]

    @app.get("/api/v1/traces", dependencies=[Depends(authorize)])
    async def list_traces(
        page: int = Query(default=1, ge=1),
        model: str | None = None,
        provider: str | None = None,
        status: Literal["running", "completed", "cancelled", "failed"] | None = None,
        cost_sort: Literal["asc", "desc"] | None = None,
    ) -> dict[str, object]:
        return await agent.trace_page(
            page=page,
            page_size=15,
            model=model,
            provider=provider,
            status=status,
            cost_sort=cost_sort,
        )

    @app.get(
        "/api/v1/traces/{run_id}",
        dependencies=[Depends(authorize)],
        responses=_NOT_FOUND_RESPONSE,
    )
    async def get_trace(run_id: str) -> dict[str, Any]:
        trace = await agent.trace(run_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace was not found")
        return trace

    @app.get(
        "/api/v1/usage",
        dependencies=[Depends(authorize)],
        responses=_BAD_REQUEST_RESPONSE,
    )
    async def get_usage(
        days: int = Query(default=10),
        start_date: date | None = None,
        end_date: date | None = None,
        group_by: Literal["time", "model", "provider"] = "time",
        metric: Literal["tokens", "cost"] = "tokens",
    ) -> dict[str, Any]:
        if days not in {10, 30, 60}:
            raise HTTPException(status_code=422, detail="days must be 10, 30, or 60")
        try:
            return await agent.usage_report(
                days=days,
                start_date=start_date,
                end_date=end_date,
                group_by=group_by,
                metric=metric,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/alerts", dependencies=[Depends(authorize)])
    async def get_alerts() -> dict[str, object]:
        return {"alerts": await agent.observability_alerts()}


def _register_evaluation_routes(
    app: FastAPI,
    evaluation_store: SqlEvaluationStore | None,
    knowledge_notifier: Callable[[str], Awaitable[None]] | None,
    authorize: Callable[..., Awaitable[None]],
) -> None:
    @app.get(
        "/api/v1/evaluation-configurations",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
    )
    async def list_evaluation_configurations(limit: int = 100) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        return {"configurations": await store.configurations(limit=limit)}

    @app.get(
        "/api/v1/evaluation-datasets",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
    )
    async def list_evaluation_datasets(limit: int = 50) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        return {"datasets": await store.list_datasets(limit=limit)}

    @app.post(
        "/api/v1/evaluation-datasets",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
        status_code=201,
    )
    async def create_evaluation_dataset(body: EvaluationDatasetCreate) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        return await store.create_dataset(
            name=body.name,
            version=body.version,
            description=body.description,
            cases=[item.model_dump(mode="json") for item in body.cases],
        )

    @app.get(
        "/api/v1/evaluation-datasets/{dataset_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def get_evaluation_dataset(dataset_id: str) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        dataset = await store.dataset(dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Evaluation dataset was not found")
        return dataset

    @app.get(
        "/api/v1/evaluation-runs",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
    )
    async def list_evaluation_runs(limit: int = 50) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        return {"runs": await store.list_runs(limit=limit)}

    @app.post(
        "/api/v1/evaluation-runs",
        dependencies=[Depends(authorize)],
        responses=_MUTATION_RESPONSES,
        status_code=202,
    )
    async def create_evaluation_run(
        body: EvaluationRunCreate,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        _validate_idempotency_key(idempotency_key)
        run = await store.enqueue(
            dataset_id=body.dataset_id,
            baseline_configuration_hash=body.baseline_configuration_hash,
            candidate_configuration_hash=body.candidate_configuration_hash,
            seed=body.seed,
            gates=body.gates.model_dump(),
            idempotency_key=idempotency_key or str(uuid4()),
        )
        await _notify_job(knowledge_notifier, str(run["id"]))
        run["status_url"] = f"/api/v1/evaluation-runs/{run['id']}"
        return run

    @app.get(
        "/api/v1/evaluation-runs/{run_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def get_evaluation_run(run_id: str) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        run = await store.run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Evaluation run was not found")
        return run

    @app.post(
        "/api/v1/evaluation-runs/{run_id}/cancel",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def cancel_evaluation_run(run_id: str) -> dict[str, object]:
        store = _require_evaluation_store(evaluation_store)
        run = await store.cancel(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Evaluation run was not found")
        return run


def _register_settings_routes(
    app: FastAPI,
    agent: TalosAgent,
    runtime_settings_store: SqlRuntimeSettingsStore | None,
    guardrail_settings_store: SqlGuardrailSettingsStore | None,
    authorize: Callable[..., Awaitable[None]],
    ensure_guardrails_loaded: Callable[[], Awaitable[None]],
) -> None:
    @app.get("/api/v1/config", dependencies=[Depends(authorize)])
    async def get_config() -> dict[str, Any]:
        return dict(agent.config.raw)

    @app.get("/api/v1/runtime-settings", dependencies=[Depends(authorize)])
    async def get_runtime_settings() -> dict[str, Any]:
        return agent.runtime_feature_settings()

    @app.patch("/api/v1/runtime-settings", dependencies=[Depends(authorize)])
    async def update_runtime_settings(
        body: RuntimeFeatureSettingsUpdate,
    ) -> dict[str, Any]:
        result = agent.update_runtime_feature_settings(
            advanced_rag_enabled=body.advanced_rag.enabled,
            query_rewriting=body.advanced_rag.query_rewriting,
            query_expansion=body.advanced_rag.query_expansion,
            dense_retrieval=body.advanced_rag.dense_retrieval,
            sparse_retrieval=body.advanced_rag.sparse_retrieval,
            reranking=body.advanced_rag.reranking,
            memory_enabled=body.memory.enabled,
            short_term=body.memory.short_term,
            summarization=body.memory.summarization,
            long_term=body.memory.long_term,
        )
        if runtime_settings_store is not None:
            await runtime_settings_store.save(result)
        return result

    @app.get("/api/v1/guardrails", dependencies=[Depends(authorize)])
    async def get_guardrails() -> dict[str, Any]:
        await ensure_guardrails_loaded()
        return agent.guardrail_settings()

    @app.patch("/api/v1/guardrails", dependencies=[Depends(authorize)])
    async def update_guardrails(body: GuardrailSettingsUpdate) -> dict[str, Any]:
        await ensure_guardrails_loaded()
        result = agent.update_guardrail_settings(
            block_message=body.block_message,
            custom_keywords=tuple(body.custom_keywords),
            custom_profanity=tuple(body.custom_profanity),
            add_secrets=tuple(body.add_secrets),
            remove_secret_ids=tuple(body.remove_secret_ids),
        )
        if guardrail_settings_store is not None:
            await guardrail_settings_store.save(agent.persisted_guardrail_settings())
        return result


def _register_tool_routes(
    app: FastAPI,
    agent: TalosAgent,
    tool_approvals: SqlToolApprovalStore | None,
    skill_reloader: Callable[[], Awaitable[tuple[Skill, ...]]] | None,
    authorize: Callable[..., Awaitable[None]],
    ensure_guardrails_loaded: Callable[[], Awaitable[None]],
) -> None:
    @app.get("/api/v1/tools", dependencies=[Depends(authorize)])
    async def list_tools() -> dict[str, object]:
        return {"tools": list(agent.list_tools())}

    @app.post(
        "/api/v1/tools/{name}/invoke",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
    )
    async def invoke_tool(name: str, body: ToolInvokeRequest) -> Any:
        await ensure_guardrails_loaded()
        agent.validate_tool_arguments(name, body.arguments)
        if agent.tool_requires_approval(name):
            store = _require_tool_approval_store(tool_approvals)
            proposal = await store.propose(name, agent.tool_version(name), body.arguments)
            return JSONResponse(
                status_code=202,
                content={
                    "approval_id": proposal.id,
                    "state": proposal.state,
                    "name": name,
                    "version": proposal.version,
                    "argument_sha256": proposal.argument_sha256,
                    "expires_at": proposal.expires_at.isoformat(),
                },
            )
        result = await agent.call_tool(name, body.arguments)
        return {"name": name, "result": result}

    @app.get(
        "/api/v1/tools/approvals/{approval_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def get_tool_approval(approval_id: str) -> dict[str, object]:
        store = _require_tool_approval_store(tool_approvals)
        proposal = await store.get(approval_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Tool approval was not found")
        return _approval_response(proposal)

    @app.post(
        "/api/v1/tools/approvals/{approval_id}/decision",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def decide_tool_approval(
        approval_id: str, body: ToolApprovalDecisionRequest
    ) -> dict[str, object]:
        store = _require_tool_approval_store(tool_approvals)
        proposal = await store.get(approval_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Tool approval was not found")
        return await _decide_tool_approval(agent, store, proposal, approval_id, body.approve)

    @app.get("/api/v1/skills", dependencies=[Depends(authorize)])
    async def list_skills() -> dict[str, object]:
        return {
            "skills": [
                {"id": item.id, "version": item.version, "hash": item.content_hash}
                for item in agent.list_skills()
            ]
        }

    @app.post(
        "/api/v1/skills/reload",
        dependencies=[Depends(authorize)],
        responses=_SERVICE_UNAVAILABLE_RESPONSE,
    )
    async def reload_skills() -> dict[str, object]:
        if skill_reloader is None:
            raise HTTPException(status_code=503, detail="Skill reload is unavailable")
        skills = await skill_reloader()
        agent.set_skills(skills)
        return {
            "configuration_version": agent.config.version,
            "skills": [item.id for item in skills],
        }


def _register_knowledge_routes(
    app: FastAPI,
    agent: TalosAgent,
    conversations: ConversationStore,
    knowledge_jobs: SqlKnowledgeStore | None,
    knowledge_notifier: Callable[[str], Awaitable[None]] | None,
    authorize: Callable[..., Awaitable[None]],
) -> None:
    @app.get("/api/v1/knowledge", dependencies=[Depends(authorize)])
    async def list_knowledge() -> dict[str, object]:
        return {
            "sources": [
                {
                    "id": item.id,
                    "name": item.name,
                    "checksum": item.checksum,
                    "chunk_count": item.chunk_count,
                    "embedding_model": item.embedding_model,
                }
                for item in await agent.list_knowledge()
            ]
        }

    @app.post(
        "/api/v1/knowledge",
        dependencies=[Depends(authorize)],
        responses=_MUTATION_RESPONSES,
        status_code=202,
    )
    async def create_knowledge(
        body: KnowledgeCreateRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        store = _require_knowledge_store(knowledge_jobs)
        agent.validate_knowledge_source(body.name, body.content)
        if body.conversation_id is not None:
            await conversations.get(body.conversation_id)
        if idempotency_key is not None and len(idempotency_key) > 128:
            raise HTTPException(status_code=400, detail="Idempotency key is too long")
        job_id = await store.enqueue(
            body.name,
            body.content,
            idempotency_key or str(uuid4()),
            conversation_id=body.conversation_id,
        )
        await _notify_job(knowledge_notifier, job_id)
        return {"job_id": job_id, "status_url": f"/api/v1/knowledge/jobs/{job_id}"}

    @app.get(
        "/api/v1/knowledge/jobs/{job_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def knowledge_job(job_id: str) -> dict[str, object]:
        store = _require_knowledge_store(knowledge_jobs)
        status = await store.job_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Knowledge job was not found")
        return status

    @app.get(
        "/api/v1/knowledge/{source_id}/preview",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def preview_knowledge(source_id: str, conversation_id: str) -> dict[str, object]:
        store = _require_knowledge_store(knowledge_jobs)
        await conversations.get(conversation_id)
        preview = await store.attachment_preview(source_id, conversation_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="Conversation attachment was not found")
        return preview

    @app.post(
        "/api/v1/knowledge/jobs/{job_id}/cancel",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def cancel_knowledge_job(job_id: str) -> dict[str, object]:
        store = _require_knowledge_store(knowledge_jobs)
        status = await store.cancel(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Knowledge job was not found")
        return status

    @app.post("/api/v1/knowledge/search", dependencies=[Depends(authorize)])
    async def search_knowledge(body: KnowledgeSearchRequest) -> dict[str, object]:
        hits = await agent.search_knowledge(body.query)
        return {
            "hits": [
                {
                    "marker": hit.marker,
                    "source_id": hit.chunk.source_id,
                    "source_name": hit.chunk.source_name,
                    "start_line": hit.chunk.start_line,
                    "end_line": hit.chunk.end_line,
                    "page_number": hit.chunk.page_number,
                    "checksum": hit.chunk.checksum,
                    "score": hit.score,
                    "text": hit.chunk.text,
                }
                for hit in hits
            ]
        }


def _register_repository_routes(
    app: FastAPI,
    agent: TalosAgent,
    repository_jobs: SqlRepositoryStore | None,
    repository_settings: RepositorySettings | None,
    knowledge_notifier: Callable[[str], Awaitable[None]] | None,
    authorize: Callable[..., Awaitable[None]],
) -> None:
    @app.post(
        "/api/v1/repositories/index",
        dependencies=[Depends(authorize)],
        responses=_MUTATION_RESPONSES,
        status_code=202,
    )
    async def index_repository(
        body: RepositoryIndexRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        store, settings = _require_repository_dependencies(repository_jobs, repository_settings)
        if idempotency_key is not None and len(idempotency_key) > 128:
            raise HTTPException(status_code=400, detail="Idempotency key is too long")
        snapshot = _validated_repository_snapshot(body, settings)
        job_id = await store.enqueue(
            snapshot, agent.config.rag.embedding_model, idempotency_key or str(uuid4())
        )
        await _notify_job(knowledge_notifier, job_id)
        return {"job_id": job_id, "status_url": f"/api/v1/repositories/jobs/{job_id}"}

    @app.get(
        "/api/v1/repositories/jobs/{job_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def repository_job(job_id: str) -> dict[str, object]:
        store = _require_repository_store(repository_jobs)
        status = await store.status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Repository job was not found")
        return status

    @app.post(
        "/api/v1/repositories/jobs/{job_id}/cancel",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def cancel_repository_job(job_id: str) -> dict[str, object]:
        store = _require_repository_store(repository_jobs)
        status = await store.cancel(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Repository job was not found")
        return status

    @app.get(
        "/api/v1/repositories/{repository_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def repository_summary(repository_id: str) -> dict[str, object]:
        store = _require_repository_store(repository_jobs)
        summary = await store.summary(repository_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Repository was not found")
        return summary

    @app.delete(
        "/api/v1/repositories/{repository_id}",
        dependencies=[Depends(authorize)],
        responses=_STORAGE_RESPONSES,
    )
    async def remove_repository(repository_id: str) -> dict[str, object]:
        store = _require_repository_store(repository_jobs)
        if not await store.remove(repository_id):
            raise HTTPException(status_code=404, detail="Repository was not found")
        return {"id": repository_id, "removed": True}

    @app.post("/api/v1/repositories/search", dependencies=[Depends(authorize)])
    async def search_repository(body: RepositorySearchRequest) -> dict[str, object]:
        hits = await agent.search_repository(body.query, repository_id=body.repository_id)
        return {
            "hits": [
                {
                    "marker": hit.marker,
                    "path": hit.path,
                    "language": hit.language,
                    "symbol": hit.symbol,
                    "start_line": hit.start_line,
                    "end_line": hit.end_line,
                    "commit": hit.commit,
                    "dirty": hit.dirty,
                    "snapshot": hit.snapshot,
                    "checksum": hit.checksum,
                    "score": hit.score,
                    "text": hit.text,
                }
                for hit in hits
            ]
        }


async def _readiness_response(readiness: ReadinessCheck | None) -> JSONResponse:
    healthy = True if readiness is None else await readiness()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "not_ready"},
    )


def _require_evaluation_store(store: SqlEvaluationStore | None) -> SqlEvaluationStore:
    if store is None:
        raise HTTPException(status_code=503, detail=_EVALUATION_STORAGE_UNAVAILABLE)
    return store


def _require_tool_approval_store(store: SqlToolApprovalStore | None) -> SqlToolApprovalStore:
    if store is None:
        raise HTTPException(status_code=503, detail=_TOOL_APPROVAL_UNAVAILABLE)
    return store


def _require_knowledge_store(store: SqlKnowledgeStore | None) -> SqlKnowledgeStore:
    if store is None:
        raise HTTPException(status_code=503, detail=_KNOWLEDGE_INGESTION_UNAVAILABLE)
    return store


def _require_repository_store(store: SqlRepositoryStore | None) -> SqlRepositoryStore:
    if store is None:
        raise HTTPException(status_code=503, detail=_REPOSITORY_INDEXING_UNAVAILABLE)
    return store


def _require_repository_dependencies(
    store: SqlRepositoryStore | None,
    settings: RepositorySettings | None,
) -> tuple[SqlRepositoryStore, RepositorySettings]:
    if settings is None:
        raise HTTPException(status_code=503, detail=_REPOSITORY_INDEXING_UNAVAILABLE)
    return _require_repository_store(store), settings


async def _notify_job(
    notifier: Callable[[str], Awaitable[None]] | None,
    job_id: str,
) -> None:
    if notifier is None:
        return
    try:
        await notifier(job_id)
    except RedisError:
        # The database is authoritative; workers also scan pending jobs.
        pass


def _repository_files(
    body: RepositoryIndexRequest,
    settings: RepositorySettings,
) -> tuple[RepositoryFile, ...]:
    if (
        not body.files
        or len(body.files) > settings.max_files
        or any(char in body.name for char in "/\\")
    ):
        raise RepositoryError("Repository manifest is invalid")
    files = tuple(
        RepositoryFile(
            path=item.path,
            content=item.content,
            checksum=item.checksum,
            language=item.language,
        )
        for item in body.files
    )
    if len({item.path for item in files}) != len(files):
        raise RepositoryError("Repository manifest contains duplicate paths")
    return files


def _validate_repository_files(
    files: tuple[RepositoryFile, ...], settings: RepositorySettings
) -> None:
    total = 0
    for item in files:
        validate_repository_file(item, settings)
        if item.language != item.path.rsplit(".", 1)[-1].lower():
            raise RepositoryError("Repository language metadata is invalid")
        total += len(item.content.encode("utf-8"))
    if total > settings.max_total_bytes:
        raise RepositoryError("Repository manifest exceeds its size limit")


def _validated_repository_snapshot(
    body: RepositoryIndexRequest,
    settings: RepositorySettings,
) -> RepositorySnapshot:
    if not body.consent:
        raise RepositoryError("Repository indexing requires explicit consent")
    if not re.fullmatch(r"[0-9a-f]{64}", body.identity) or not re.fullmatch(
        r"[0-9a-f]{40,64}", body.commit
    ):
        raise RepositoryError("Repository identity or commit is invalid")
    files = _repository_files(body, settings)
    _validate_repository_files(files, settings)
    checksum = hashlib.sha256(
        "\n".join(
            f"{item.path}:{item.checksum}" for item in sorted(files, key=lambda item: item.path)
        ).encode()
    ).hexdigest()
    if checksum != body.checksum:
        raise RepositoryError("Repository manifest checksum does not match")
    return RepositorySnapshot(
        identity=body.identity,
        name=body.name,
        branch=body.branch,
        commit=body.commit,
        dirty=body.dirty,
        checksum=body.checksum,
        files=files,
        skipped=body.skipped,
    )


async def _decide_tool_approval(
    agent: TalosAgent,
    store: SqlToolApprovalStore,
    proposal: ToolProposal,
    approval_id: str,
    approve: bool,
) -> dict[str, object]:
    try:
        version = agent.tool_version(proposal.name)
    except TalosError:
        version = None
    decision = await store.decide(approval_id, approve=approve, current_version=version)
    assert decision is not None
    selected, claimed = decision
    if not claimed:
        return _approval_response(selected)
    try:
        result = await agent.call_tool(
            selected.name,
            selected.arguments,
            approval=lambda name, digest: _matches_approval(selected, name, digest),
            expected_version=selected.version,
        )
    except TalosError:
        selected = await store.finish(approval_id, error_code="execution_uncertain")
    else:
        selected = await store.finish(approval_id, result=result)
    return _approval_response(selected)


def _validate_idempotency_key(value: str | None) -> None:
    if value is None:
        return
    if not 1 <= len(value) <= 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise HTTPException(status_code=400, detail="Idempotency key is invalid")


async def _validated_message_attachments(
    body: ChatRequest, knowledge_jobs: SqlKnowledgeStore | None
) -> tuple[MessageAttachment, ...]:
    if not body.attachments:
        return ()
    if body.conversation_id is None:
        raise HTTPException(status_code=400, detail="Attachments require a conversation")
    store = _require_knowledge_store(knowledge_jobs)
    validated: list[MessageAttachment] = []
    identifiers: set[str] = set()
    for requested in body.attachments:
        if requested.source_id in identifiers:
            raise HTTPException(status_code=400, detail="Attachment references must be unique")
        source = await store.attachment(requested.source_id, body.conversation_id)
        if source is None or source.name != requested.name:
            raise HTTPException(status_code=404, detail="Conversation attachment was not found")
        identifiers.add(requested.source_id)
        validated.append(MessageAttachment(source.id, source.name))
    return tuple(validated)


def _conversation_response(
    conversation: Any,
    tool_events: list[RuntimeEvent] | None = None,
    *,
    title: str = "New conversation",
) -> ConversationResponse:
    tools: dict[str, list[ToolUsageResponse]] = {}
    for event in tool_events or []:
        name = event.payload.get("name")
        if not isinstance(name, str) or not name:
            continue
        usage = ToolUsageResponse(
            name=name, status="completed" if event.type is EventType.TOOL_COMPLETED else "failed"
        )
        entries = tools.setdefault(event.run_id, [])
        if usage not in entries:
            entries.append(usage)
    return ConversationResponse(
        title=title,
        id=conversation.id,
        configuration_version=conversation.configuration.version,
        created_at=conversation.created_at,
        model_profile_id=conversation.configuration.model_alias,
        model_id=conversation.configuration.model_id,
        provider=conversation.configuration.provider_alias,
        messages=[
            MessageResponse(
                id=message.id,
                run_id=message.run_id,
                tool_usages=tools.get(message.run_id or "", [])
                if message.role.value == "assistant"
                else [],
                role=message.role.value,
                content=message.content,
                created_at=message.created_at,
                attachments=[
                    MessageAttachmentResponse(source_id=item.source_id, name=item.name)
                    for item in message.attachments
                ],
            )
            for message in conversation.messages
        ],
    )


def _approval_response(proposal: ToolProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "name": proposal.name,
        "version": proposal.version,
        "argument_sha256": proposal.argument_sha256,
        "state": proposal.state,
        "expires_at": proposal.expires_at.isoformat(),
        "error_code": proposal.error_code,
        "result": proposal.result,
    }


async def _matches_approval(proposal: ToolProposal, name: str, digest: str) -> bool:
    await asyncio.sleep(0)
    return proposal.name == name and proposal.argument_sha256 == digest


def _sse(event: RuntimeEvent) -> str:
    data = {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "run_id": event.run_id,
        "conversation_id": event.conversation_id,
        "sequence": event.sequence,
        "occurred_at": event.occurred_at.isoformat(),
        "type": event.type.value,
        "payload": dict(event.payload),
    }
    return f"id: {event.event_id}\nevent: {event.type.value}\ndata: {json.dumps(data)}\n\n"


def _validate_input(value: str, maximum: int) -> None:
    if len(value) > maximum:
        raise HTTPException(status_code=413, detail="Chat input exceeds the configured limit")


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


def _boolean(environment: Mapping[str, str], name: str) -> bool:
    value = _required(environment, name).lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return value == "true"
