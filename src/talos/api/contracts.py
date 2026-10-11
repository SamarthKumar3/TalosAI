from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageAttachmentResponse(StrictModel):
    source_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=200)


class ChatRequest(StrictModel):
    input: str = Field(min_length=1)
    conversation_id: str | None = None
    attachments: list[MessageAttachmentResponse] = Field(default_factory=list, max_length=10)


class ContextEstimateRequest(StrictModel):
    draft: str = Field(max_length=100000)
    conversation_id: str | None = None


class UsageResponse(StrictModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(StrictModel):
    run_id: str
    conversation_id: str
    response: str
    model: str
    usage: UsageResponse
    configuration_version: int
    citations: list[str] = Field(default_factory=list)


class ConversationCreateRequest(StrictModel):
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=100)


class ConversationCreateResponse(StrictModel):
    id: str
    configuration_version: int
    created_at: datetime
    model_profile_id: str | None = None
    model_id: str | None = None
    provider: str | None = None


class ToolUsageResponse(StrictModel):
    name: str
    status: Literal["completed", "failed"]


class MessageResponse(StrictModel):
    id: str
    run_id: str | None = None
    role: str
    content: str
    created_at: datetime
    attachments: list[MessageAttachmentResponse] = Field(default_factory=list)
    tool_usages: list[ToolUsageResponse] = Field(default_factory=list)


class ConversationResponse(ConversationCreateResponse):
    title: str = "New conversation"
    messages: list[MessageResponse]


class AgentResponse(StrictModel):
    name: str
    provider: str
    model_alias: str
    model: str
    pipeline: str
    configuration_version: int
    configuration_hash: str


class GuardrailSettingsUpdate(StrictModel):
    block_message: str = Field(min_length=1, max_length=800)
    custom_keywords: list[str] = Field(default_factory=list, max_length=100)
    custom_profanity: list[str] = Field(default_factory=list, max_length=100)
    add_secrets: list[str] = Field(default_factory=list, max_length=100)
    remove_secret_ids: list[str] = Field(default_factory=list, max_length=100)


class AdvancedRagSettingsUpdate(StrictModel):
    enabled: bool
    query_rewriting: bool
    query_expansion: bool
    dense_retrieval: bool
    sparse_retrieval: bool
    reranking: bool


class MemorySettingsUpdate(StrictModel):
    enabled: bool
    short_term: bool
    summarization: bool
    long_term: bool


class RuntimeFeatureSettingsUpdate(StrictModel):
    advanced_rag: AdvancedRagSettingsUpdate
    memory: MemorySettingsUpdate


class ToolInvokeRequest(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolApprovalDecisionRequest(StrictModel):
    approve: bool


class MemoryCreateRequest(StrictModel):
    content: str = Field(min_length=1, max_length=10000)
    conversation_id: str
    source_message_id: str | None = None


class KnowledgeCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    conversation_id: str | None = None


class KnowledgeSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)


class RepositoryFileRequest(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    content: str
    checksum: str = Field(min_length=64, max_length=64)
    language: str = Field(min_length=1, max_length=32)


class RepositoryIndexRequest(StrictModel):
    consent: bool
    identity: str = Field(min_length=64, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    branch: str = Field(max_length=200)
    commit: str = Field(min_length=40, max_length=64)
    dirty: bool
    checksum: str = Field(min_length=64, max_length=64)
    files: list[RepositoryFileRequest]
    skipped: int = Field(ge=0)


class RepositorySearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    repository_id: str | None = None


class ErrorBody(StrictModel):
    code: str
    message: str
    retryable: bool
    correlation_id: str
    details: dict[str, Any] | None = None


class EvaluationCaseCreate(StrictModel):
    input: str = Field(min_length=1)
    expected_output: str = Field(min_length=1)
    expected_citations: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)


class EvaluationDatasetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    description: str = Field(default="", max_length=2000)
    cases: list[EvaluationCaseCreate] = Field(min_length=1)


class EvaluationGates(StrictModel):
    minimum_candidate_pass_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    maximum_pass_rate_drop: float = Field(default=0.0, ge=0.0, le=1.0)
    maximum_candidate_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    maximum_candidate_p95_latency_ms: float = Field(default=30000.0, gt=0.0)


class EvaluationRunCreate(StrictModel):
    dataset_id: str = Field(min_length=36, max_length=36)
    baseline_configuration_hash: str = Field(min_length=64, max_length=64)
    candidate_configuration_hash: str = Field(min_length=64, max_length=64)
    seed: int = Field(default=0, ge=0)
    gates: EvaluationGates = Field(default_factory=EvaluationGates)
