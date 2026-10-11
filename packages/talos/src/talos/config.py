from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from talos.capabilities import CapabilityRegistry, capability_registry_from_mapping
from talos.domain import ConfigurationSnapshot
from talos.errors import ConfigurationError
from talos.guardrails import GuardrailConfig, guardrail_config_from_mapping
from talos.knowledge import RagSettings
from talos.memory import MemorySettings
from talos.observability import CostRates, ObservabilitySettings
from talos.reliability import ModelRetrySettings
from talos.skills import Skill, validate_skills
from talos.tools.runtime import ToolDefinition

_ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class ExecutionBudgets:
    max_agent_steps: int
    max_tool_calls_per_run: int
    max_tokens_per_run: int
    max_execution_seconds: float

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    alias: str
    display_name: str
    adapter: str
    api_base: str
    api_key_env: str
    key_url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    alias: str
    provider: str
    model_id: str
    streaming: bool
    max_output_tokens: int
    temperature: float
    capabilities: tuple[str, ...]
    cost_rates: CostRates | None = None
    context_window_tokens: int | None = None
    context_safety_margin_tokens: int = 0


@dataclass(frozen=True, slots=True)
class PipelineNode:
    id: str
    kind: str


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    id: str
    version: int
    model_alias: str
    prompt_alias: str
    nodes: tuple[PipelineNode, ...]
    edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TalosConfig:
    version: int
    agent_name: str
    provider: ProviderDefinition
    model: ModelDefinition
    pipeline: PipelineDefinition
    system_prompt: str
    budgets: ExecutionBudgets
    capabilities: CapabilityRegistry
    tools: Mapping[str, ToolDefinition]
    tool_timeout_seconds: float
    tool_max_response_bytes: int
    skills: tuple[Skill, ...]
    rag: RagSettings
    guardrails: GuardrailConfig
    knowledge_prompt: str
    memory: MemorySettings
    model_retry: ModelRetrySettings
    observability: ObservabilitySettings
    raw: Mapping[str, Any]
    content_hash: str

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> TalosConfig:
        env = dict(os.environ if environment is None else environment)
        config_path = Path(path).resolve()
        root = config_path.parent
        project = _load_yaml(config_path, env)

        registry_paths = {
            "providers": _required_env_path(env, "TALOS_PROVIDER_REGISTRY_PATH", root),
            "models": _required_env_path(env, "TALOS_MODEL_REGISTRY_PATH", root),
            "pipeline": _required_env_path(env, "TALOS_PIPELINE_MANIFEST_PATH", root),
            "prompts": _required_env_path(env, "TALOS_PROMPT_REGISTRY_PATH", root),
            "tools": _required_env_path(env, "TALOS_TOOL_REGISTRY_PATH", root),
            "skills": _required_env_path(env, "TALOS_SKILL_REGISTRY_PATH", root),
            "rag": _required_env_path(env, "TALOS_RAG_CONFIG_PATH", root),
            "guardrails": _required_env_path(env, "TALOS_GUARDRAIL_CONFIG_PATH", root),
            "capabilities": _required_env_path(env, "TALOS_CAPABILITY_REGISTRY_PATH", root),
            "reliability": _required_env_path(env, "TALOS_RELIABILITY_CONFIG_PATH", root),
        }
        providers = _load_yaml(registry_paths["providers"], env)
        models = _load_yaml(registry_paths["models"], env)
        pipeline_data = _load_yaml(registry_paths["pipeline"], env)
        prompts = _load_yaml(registry_paths["prompts"], env)
        tool_registry = _load_yaml(registry_paths["tools"], env)
        skill_registry = _load_yaml(registry_paths["skills"], env)
        rag_registry = _load_yaml(registry_paths["rag"], env)
        guardrail_registry = _load_yaml(registry_paths["guardrails"], env)
        capability_registry = _load_yaml(registry_paths["capabilities"], env)
        reliability_registry = _load_yaml(registry_paths["reliability"], env)

        model_config = _mapping(project, "model")
        model_alias = _required_string(model_config, "model")
        model_entries = _mapping(models, "aliases")
        model_entry = _mapping(model_entries, model_alias)
        cost_rate_data = _mapping(model_entry, "cost_rates_per_million_tokens")
        model = ModelDefinition(
            alias=model_alias,
            provider=_required_string(model_entry, "provider"),
            model_id=_required_string(model_entry, "model_id"),
            streaming=_required_bool(model_entry, "streaming"),
            max_output_tokens=_required_int(model_entry, "max_output_tokens"),
            context_window_tokens=_required_int(model_entry, "context_window_tokens"),
            context_safety_margin_tokens=_required_int(model_entry, "context_safety_margin_tokens"),
            temperature=_required_number(model_config, "temperature"),
            capabilities=tuple(_string_list(model_entry, "capabilities")),
            cost_rates=CostRates(
                currency=_required_string(cost_rate_data, "currency"),
                input_per_million=_required_number(cost_rate_data, "input"),
                output_per_million=_required_number(cost_rate_data, "output"),
                version=_required_string(cost_rate_data, "version"),
                observed_on=_required_string(cost_rate_data, "observed_on"),
            ),
        )
        if model.context_window_tokens is None or (
            model.context_window_tokens
            <= model.max_output_tokens + model.context_safety_margin_tokens
        ):
            raise ConfigurationError(
                "Model context window must exceed output and safety reservations"
            )
        configured_provider = _required_string(model_config, "provider")
        if configured_provider != model.provider:
            raise ConfigurationError(
                f"Model alias {model_alias!r} resolves to provider {model.provider!r}, "
                f"not {configured_provider!r}"
            )

        provider_entries = _mapping(providers, "providers")
        provider_entry = _mapping(provider_entries, model.provider)
        provider = ProviderDefinition(
            alias=model.provider,
            display_name=_required_string(provider_entry, "display_name"),
            adapter=_required_string(provider_entry, "adapter"),
            api_base=_required_string(provider_entry, "api_base"),
            api_key_env=_required_string(provider_entry, "api_key_env"),
            key_url=_required_string(provider_entry, "key_url"),
            connect_timeout_seconds=_required_number(provider_entry, "connect_timeout_seconds"),
            read_timeout_seconds=_required_number(provider_entry, "read_timeout_seconds"),
        )

        pipeline = _parse_pipeline(pipeline_data)
        pipeline_capabilities = _mapping(pipeline_data, "capabilities")
        if pipeline_capabilities.get("tools") is True and "tools" not in model.capabilities:
            raise ConfigurationError("Selected model does not support configured tools")
        if pipeline.model_alias != model_alias:
            raise ConfigurationError("Pipeline and project model aliases must match in Phase 1")

        prompt_entries = _mapping(prompts, "prompts")
        prompt_entry = _mapping(prompt_entries, pipeline.prompt_alias)
        prompt_path = _safe_path(root, _required_string(prompt_entry, "path"))
        system_prompt = _read_text(prompt_path)

        budget_data = _mapping(project, "budgets")
        budgets = ExecutionBudgets(
            max_agent_steps=_required_int(budget_data, "max_agent_steps"),
            max_tool_calls_per_run=_required_int(budget_data, "max_tool_calls_per_run"),
            max_tokens_per_run=_required_int(budget_data, "max_tokens_per_run"),
            max_execution_seconds=_required_number(budget_data, "max_execution_seconds"),
        )
        pipeline_budget = _mapping(pipeline_data, "policies")
        if _required_int(pipeline_budget, "max_steps") != budgets.max_agent_steps:
            raise ConfigurationError("Pipeline and project max-agent-step budgets must match")

        agent = _mapping(project, "agent")
        version = _required_int(project, "version")
        agent_name = _required_string(agent, "name")
        tool_entries = _mapping(tool_registry, "tools")
        tool_defaults = _mapping(tool_registry, "defaults")
        tool_timeout = _required_number(tool_defaults, "timeout_seconds")
        tool_max_response = _required_int(tool_defaults, "max_response_bytes")
        if tool_timeout <= 0 or tool_max_response <= 0:
            raise ConfigurationError("Tool limits must be positive")
        tools = {
            name: ToolDefinition(
                name=name,
                version=_required_string(_expect_mapping(item), "version"),
                description=_required_string(_expect_mapping(item), "description"),
                entrypoint=_required_string(_expect_mapping(item), "entrypoint"),
                timeout_seconds=_required_number(_expect_mapping(item), "timeout_seconds"),
                max_response_bytes=_required_int(_expect_mapping(item), "max_response_bytes"),
                side_effect=_required_bool(_expect_mapping(item), "side_effect"),
            )
            for name, item in tool_entries.items()
        }
        capability_data = _mapping(project, "capabilities")
        available = {name for name, enabled in capability_data.items() if enabled is True}
        skills = _load_skills(skill_registry, root)
        capabilities = capability_registry_from_mapping(capability_data, capability_registry)
        validate_skills(skills, available)
        project_rag = _mapping(project, "rag")
        project_retrieval = _mapping(project_rag, "retrieval")
        rag = RagSettings(
            embedding_model=_required_string(rag_registry, "embedding_model"),
            embedding_adapter=_required_string(rag_registry, "embedding_adapter"),
            embedding_api_base=_required_string(rag_registry, "embedding_api_base"),
            embedding_api_key_name=_required_string(rag_registry, "embedding_api_key_name"),
            embedding_timeout_seconds=_required_number(rag_registry, "embedding_timeout_seconds"),
            allowed_extensions=tuple(
                part.strip().lower()
                for part in _required_string(rag_registry, "allowed_extensions").split(",")
                if part.strip()
            ),
            max_pdf_pages=_required_int(rag_registry, "max_pdf_pages"),
            max_pdf_parse_seconds=_required_number(rag_registry, "max_pdf_parse_seconds"),
            max_query_characters=_required_int(rag_registry, "max_query_characters"),
            dimensions=_required_int(rag_registry, "dimensions"),
            chunk_characters=_required_int(rag_registry, "chunk_characters"),
            chunk_overlap=_required_int(rag_registry, "chunk_overlap"),
            top_k=_required_int(rag_registry, "top_k"),
            similarity_threshold=_required_number(rag_registry, "similarity_threshold"),
            max_source_bytes=_required_int(rag_registry, "max_source_bytes"),
            max_chunks=_required_int(rag_registry, "max_chunks"),
            max_ingestion_attempts=_required_int(rag_registry, "max_ingestion_attempts"),
            retry_base_seconds=_required_int(rag_registry, "retry_base_seconds"),
            mode=_required_string(project_rag, "mode"),
            query_rewriting=_required_bool(project_rag, "query_rewriting"),
            query_expansion=_required_bool(project_rag, "query_expansion"),
            max_query_variants=_required_int(rag_registry, "max_query_variants"),
            dense_enabled=_required_bool(project_retrieval, "dense"),
            sparse_enabled=_required_bool(project_retrieval, "sparse"),
            rrf_k=_required_int(rag_registry, "rrf_k"),
            rerank_enabled=_required_bool(project_rag, "reranking"),
            rerank_adapter=_required_string(rag_registry, "rerank_adapter"),
            rerank_model=_required_string(rag_registry, "rerank_model"),
            rerank_api_base=_required_string(rag_registry, "rerank_api_base"),
            rerank_api_key_name=_required_string(rag_registry, "rerank_api_key_name"),
            rerank_timeout_seconds=_required_number(rag_registry, "rerank_timeout_seconds"),
            expansion_synonyms=_required_string(rag_registry, "expansion_synonyms"),
        )
        knowledge_prompt = _knowledge_prompt(rag_registry, root)
        guardrails = guardrail_config_from_mapping(guardrail_registry)
        retry_data = _mapping(reliability_registry, "model_retry")
        model_retry = ModelRetrySettings(
            max_attempts=_required_int(retry_data, "max_attempts"),
            base_delay_milliseconds=_required_int(retry_data, "base_delay_milliseconds"),
            max_delay_milliseconds=_required_int(retry_data, "max_delay_milliseconds"),
            jitter_ratio=_required_number(retry_data, "jitter_ratio"),
        )
        project_memory = _mapping(project, "memory")
        memory_data = _mapping(reliability_registry, "memory")
        memory = MemorySettings(
            short_term_enabled=_required_bool(project_memory, "short_term"),
            summarization_enabled=_required_bool(project_memory, "summarization"),
            summary_after_messages=_required_int(memory_data, "summary_after_messages"),
            keep_recent_messages=_required_int(memory_data, "keep_recent_messages"),
            max_summary_characters=_required_int(memory_data, "max_summary_characters"),
            summary_strategy=_required_string(memory_data, "summary_strategy"),
            long_term_enabled=_required_bool(project_memory, "long_term"),
            max_long_term_items=_required_int(memory_data, "max_long_term_items"),
            max_memory_characters=_required_int(memory_data, "max_memory_characters"),
        )
        if (memory.summarization_enabled or memory.long_term_enabled) and not capabilities.enabled(
            "memory"
        ):
            raise ConfigurationError("Memory settings require the memory capability")
        observability_selection = _mapping(project, "observability")
        observability = replace(
            ObservabilitySettings.from_environment(env),
            tracing_enabled=_required_bool(observability_selection, "tracing"),
            usage_tracking_enabled=_required_bool(observability_selection, "usage_tracking"),
        )
        resolved = {
            "version": version,
            "agent_name": agent_name,
            "capabilities": dict(capability_data),
            "provider": asdict(provider),
            "model": asdict(model),
            "pipeline": asdict(pipeline),
            "system_prompt": system_prompt,
            "system_prompt_sha256": _sha256(system_prompt),
            "budgets": asdict(budgets),
            "memory": asdict(memory),
            "model_retry": asdict(model_retry),
            "observability": asdict(observability),
            "tools": {name: asdict(item) for name, item in tools.items()},
            "tool_defaults": {
                "timeout_seconds": tool_timeout,
                "max_response_bytes": tool_max_response,
            },
            "skills": [{**asdict(skill), "hash": skill.content_hash} for skill in skills],
            "rag": asdict(rag),
            "guardrails": {
                "default_block_message": guardrails.default_block_message,
                "max_custom_items": guardrails.max_custom_items,
                "max_custom_item_characters": guardrails.max_custom_item_characters,
                "categories": {
                    key: {
                        "label": value.label,
                        "detail": value.detail,
                        "rules": [rule.id for rule in value.rules],
                    }
                    for key, value in guardrails.categories.items()
                },
            },
            "knowledge_prompt": knowledge_prompt,
            "knowledge_prompt_sha256": _sha256(knowledge_prompt),
        }
        content_hash = _sha256(_canonical_json(resolved))
        return cls(
            version=version,
            agent_name=agent_name,
            provider=provider,
            model=model,
            pipeline=pipeline,
            system_prompt=system_prompt,
            budgets=budgets,
            capabilities=capabilities,
            tools=MappingProxyType(tools),
            tool_timeout_seconds=tool_timeout,
            tool_max_response_bytes=tool_max_response,
            skills=skills,
            rag=rag,
            guardrails=guardrails,
            memory=memory,
            model_retry=model_retry,
            observability=observability,
            knowledge_prompt=knowledge_prompt,
            raw=MappingProxyType(resolved),
            content_hash=content_hash,
        )

    @classmethod
    def from_snapshot(
        cls, snapshot: ConfigurationSnapshot, *, template: TalosConfig
    ) -> TalosConfig:
        """Rehydrate a secret-free pinned configuration after a process restart."""
        resolved = dict(snapshot.resolved)
        if _sha256(_canonical_json(resolved)) != snapshot.content_hash:
            raise ConfigurationError("Persisted configuration snapshot checksum is invalid")
        if "system_prompt" not in resolved:
            return _rehydrate_legacy_snapshot(snapshot, resolved, template)
        try:
            provider = ProviderDefinition(**dict(_mapping(resolved, "provider")))
            model_data = dict(_mapping(resolved, "model"))
            model_data["capabilities"] = tuple(model_data["capabilities"])
            if isinstance(model_data.get("cost_rates"), Mapping):
                model_data["cost_rates"] = CostRates(**dict(model_data["cost_rates"]))
            model = ModelDefinition(**model_data)
            pipeline_data = _mapping(resolved, "pipeline")
            pipeline = PipelineDefinition(
                id=str(pipeline_data["id"]),
                version=int(pipeline_data["version"]),
                model_alias=str(pipeline_data["model_alias"]),
                prompt_alias=str(pipeline_data["prompt_alias"]),
                nodes=tuple(
                    PipelineNode(id=str(item["id"]), kind=str(item["kind"]))
                    for item in pipeline_data["nodes"]
                ),
                edges=tuple((str(edge[0]), str(edge[1])) for edge in pipeline_data["edges"]),
            )
            budgets = ExecutionBudgets(**dict(_mapping(resolved, "budgets")))
            capability_selection = _mapping(resolved, "capabilities")
            registry = {
                "capabilities": {
                    item.id: {
                        "label": item.label,
                        "description": item.description,
                        "requires": list(item.requires),
                    }
                    for item in template.capabilities.list()
                }
            }
            capabilities = capability_registry_from_mapping(capability_selection, registry)
            tools = {
                str(name): ToolDefinition(**dict(_expect_mapping(item)))
                for name, item in _mapping(resolved, "tools").items()
            }
            skills = tuple(
                Skill(
                    **{
                        **{
                            key: value
                            for key, value in _expect_mapping(item).items()
                            if key != "hash"
                        },
                        "requires": tuple(_expect_mapping(item)["requires"]),
                        "conflicts": tuple(_expect_mapping(item)["conflicts"]),
                    }
                )
                for item in resolved.get("skills", [])
            )
            rag_data = dict(_mapping(resolved, "rag"))
            rag_data["allowed_extensions"] = tuple(rag_data["allowed_extensions"])
            memory_data = dict(_mapping(resolved, "memory"))
            memory_data.setdefault("short_term_enabled", True)
            defaults = _mapping(resolved, "tool_defaults")
            return cls(
                version=snapshot.version,
                agent_name=str(resolved["agent_name"]),
                provider=provider,
                model=model,
                pipeline=pipeline,
                system_prompt=str(resolved["system_prompt"]),
                budgets=budgets,
                capabilities=capabilities,
                tools=MappingProxyType(tools),
                tool_timeout_seconds=float(defaults["timeout_seconds"]),
                tool_max_response_bytes=int(defaults["max_response_bytes"]),
                skills=skills,
                rag=RagSettings(**rag_data),
                guardrails=template.guardrails,
                knowledge_prompt=str(resolved["knowledge_prompt"]),
                memory=MemorySettings(**memory_data),
                model_retry=ModelRetrySettings(**dict(_mapping(resolved, "model_retry"))),
                observability=ObservabilitySettings(**dict(_mapping(resolved, "observability")))
                if isinstance(resolved.get("observability"), Mapping)
                else template.observability,
                raw=MappingProxyType(resolved),
                content_hash=snapshot.content_hash,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError("Persisted configuration snapshot is invalid") from exc

    def snapshot(self) -> ConfigurationSnapshot:
        return ConfigurationSnapshot(
            version=self.version,
            content_hash=self.content_hash,
            provider_alias=self.provider.alias,
            model_alias=self.model.alias,
            model_id=self.model.model_id,
            pipeline_id=self.pipeline.id,
            prompt_id=self.pipeline.prompt_alias,
            resolved=self.raw,
        )


def _rehydrate_legacy_snapshot(
    snapshot: ConfigurationSnapshot,
    resolved: dict[str, Any],
    template: TalosConfig,
) -> TalosConfig:
    """Read pre-Phase-9 snapshots only when their pinned core still matches exactly."""
    try:
        provider = ProviderDefinition(**dict(_mapping(resolved, "provider")))
        model_data = dict(_mapping(resolved, "model"))
        model_data["capabilities"] = template.model.capabilities
        if isinstance(model_data.get("cost_rates"), Mapping):
            model_data["cost_rates"] = CostRates(**dict(model_data["cost_rates"]))
        model = ModelDefinition(**model_data)
        pipeline = _parse_pipeline(_mapping(resolved, "pipeline"))
        budgets = ExecutionBudgets(**dict(_mapping(resolved, "budgets")))
        prompt_checksum = str(resolved["system_prompt_sha256"])
        model_matches = (
            model.alias == template.model.alias
            and model.provider == template.model.provider
            and model.model_id == template.model.model_id
            and model.streaming == template.model.streaming
            and model.max_output_tokens == template.model.max_output_tokens
            and model.temperature == template.model.temperature
        )
        if (
            provider != template.provider
            or not model_matches
            or prompt_checksum != _sha256(template.system_prompt)
        ):
            raise ConfigurationError(
                "Legacy conversation configuration no longer matches the active runtime; "
                "start a new conversation"
            )
        rehydrated: TalosConfig = replace(
            template,
            version=snapshot.version,
            agent_name=str(resolved["agent_name"]),
            provider=provider,
            model=model,
            pipeline=pipeline,
            budgets=budgets,
            raw=MappingProxyType(resolved),
            content_hash=snapshot.content_hash,
        )
        return rehydrated
    except ConfigurationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Persisted legacy configuration snapshot is invalid") from exc


def _parse_pipeline(data: Mapping[str, Any]) -> PipelineDefinition:
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ConfigurationError("Pipeline nodes must be a non-empty list")
    if not isinstance(raw_edges, list):
        raise ConfigurationError("Pipeline edges must be a list")
    nodes = tuple(
        PipelineNode(
            id=_required_string(_expect_mapping(node), "id"),
            kind=_required_string(_expect_mapping(node), "kind"),
        )
        for node in raw_nodes
    )
    if len({node.id for node in nodes}) != len(nodes):
        raise ConfigurationError("Pipeline node IDs must be unique")
    edges: list[tuple[str, str]] = []
    known = {node.id for node in nodes}
    for edge in raw_edges:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or not all(isinstance(item, str) for item in edge)
        ):
            raise ConfigurationError("Each pipeline edge must contain two node IDs")
        source, target = edge
        if source not in known or target not in known:
            raise ConfigurationError("Pipeline edge references an unknown node")
        edges.append((source, target))
    return PipelineDefinition(
        id=_required_string(data, "id"),
        version=_required_int(data, "version"),
        model_alias=_required_string(data, "model_alias"),
        prompt_alias=_required_string(data, "prompt_alias"),
        nodes=nodes,
        edges=tuple(edges),
    )


def _load_skills(registry: Mapping[str, Any], root: Path) -> tuple[Skill, ...]:
    entries = _mapping(registry, "skills")
    maximum = _required_int(registry, "max_file_bytes")
    loaded: list[Skill] = []
    for skill_id, item in sorted(entries.items()):
        entry = _expect_mapping(item)
        if not _required_bool(entry, "enabled"):
            continue
        skill = Skill.from_file(
            _safe_path(root, _required_string(entry, "path")), root=root, max_bytes=maximum
        )
        if skill.id != skill_id:
            raise ConfigurationError(f"Skill registry ID mismatch: {skill_id}")
        loaded.append(skill)
    return tuple(loaded)


def _knowledge_prompt(registry: Mapping[str, Any], root: Path) -> str:
    prompt = _read_text(_safe_path(root, _required_string(registry, "prompt_path")))
    if "{context}" not in prompt:
        raise ConfigurationError("Knowledge prompt must include {context}")
    return prompt


def _load_yaml(path: Path, env: Mapping[str, str]) -> Mapping[str, Any]:
    text = _read_text(path)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env or not env[name]:
            raise ConfigurationError(f"Required environment variable {name} is missing")
        return env[name]

    try:
        loaded = yaml.safe_load(_ENV_PATTERN.sub(replace, text))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path.name}") from exc
    return _expect_mapping(loaded)


def _required_env_path(env: Mapping[str, str], name: str, root: Path) -> Path:
    value = env.get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return _safe_path(root, value)


def _safe_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(f"Configuration path escapes project root: {value}") from exc
    return resolved


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration file: {path.name}") from exc


def _expect_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("Expected a mapping")
    return value


def _mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _expect_mapping(mapping.get(key))


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _required_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{key} must be an integer")
    return value


def _required_bool(mapping: Mapping[str, Any], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _required_number(mapping: Mapping[str, Any], key: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigurationError(f"{key} must be numeric")
    return float(value)


def _string_list(mapping: Mapping[str, Any], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigurationError(f"{key} must be a list of strings")
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
