from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from talos.errors import ConfigurationError

GuardrailStage = Literal["input", "retrieval", "tool_input", "tool_output", "output", "memory"]


@dataclass(frozen=True, slots=True)
class GuardrailRule:
    id: str
    expression: str


@dataclass(frozen=True, slots=True)
class GuardrailCategory:
    id: str
    label: str
    detail: str
    rules: tuple[GuardrailRule, ...]


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    default_block_message: str
    max_custom_items: int
    max_custom_item_characters: int
    categories: Mapping[str, GuardrailCategory]


@dataclass(frozen=True, slots=True)
class CustomSecret:
    id: str
    fingerprint: str
    token_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeGuardrailSettings:
    block_message: str
    custom_keywords: tuple[str, ...] = ()
    custom_profanity: tuple[str, ...] = ()
    custom_secrets: tuple[CustomSecret, ...] = ()


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    blocked: bool
    category: str | None = None
    rule_id: str | None = None
    message: str | None = None
    safe_content: str | None = None


class GuardrailService:
    """Deterministic, local guardrails. No classifier or external service is invoked."""

    def __init__(self, config: GuardrailConfig) -> None:
        self.config = config
        self._compiled = {
            category.id: tuple((rule.id, re.compile(rule.expression)) for rule in category.rules)
            for category in config.categories.values()
        }
        self._runtime = RuntimeGuardrailSettings(block_message=config.default_block_message)

    @property
    def runtime(self) -> RuntimeGuardrailSettings:
        return self._runtime

    def public_settings(self) -> dict[str, Any]:
        return {
            "block_message": self._runtime.block_message,
            "custom_keywords": list(self._runtime.custom_keywords),
            "custom_profanity": list(self._runtime.custom_profanity),
            "custom_secrets": [
                {"id": item.id, "fingerprint": item.fingerprint}
                for item in self._runtime.custom_secrets
            ],
            "immutable": [
                {
                    "id": category.id,
                    "label": category.label,
                    "detail": category.detail,
                    "rule_count": len(category.rules),
                }
                for category in self.config.categories.values()
            ],
        }

    def persistence_settings(self) -> dict[str, Any]:
        return {
            "block_message": self._runtime.block_message,
            "custom_keywords": list(self._runtime.custom_keywords),
            "custom_profanity": list(self._runtime.custom_profanity),
            "custom_secrets": [
                {
                    "id": item.id,
                    "fingerprint": item.fingerprint,
                    "token_hash": item.token_hash,
                }
                for item in self._runtime.custom_secrets
            ],
        }

    def restore(self, value: Mapping[str, Any]) -> None:
        raw_secrets = value.get("custom_secrets", [])
        if not isinstance(raw_secrets, list):
            raise ConfigurationError("Persisted custom secrets are invalid")
        secrets: list[CustomSecret] = []
        for item in raw_secrets:
            if not isinstance(item, dict):
                raise ConfigurationError("Persisted custom secret is invalid")
            identifier = item.get("id")
            fingerprint = item.get("fingerprint")
            token_hash = item.get("token_hash")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9a-f]{16}", identifier)
                or not isinstance(fingerprint, str)
                or not isinstance(token_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", token_hash)
            ):
                raise ConfigurationError("Persisted custom secret is invalid")
            secrets.append(CustomSecret(identifier, fingerprint, token_hash))
        block_message = value.get("block_message")
        keywords = value.get("custom_keywords", [])
        profanity = value.get("custom_profanity", [])
        if (
            not isinstance(block_message, str)
            or not isinstance(keywords, list)
            or not all(isinstance(item, str) for item in keywords)
            or not isinstance(profanity, list)
            or not all(isinstance(item, str) for item in profanity)
        ):
            raise ConfigurationError("Persisted guardrail settings are invalid")
        self._runtime = RuntimeGuardrailSettings(
            block_message=block_message,
            custom_keywords=self._validated_items(keywords, "custom keyword"),
            custom_profanity=self._validated_items(profanity, "custom profanity"),
            custom_secrets=tuple(secrets),
        )

    def update(
        self,
        *,
        block_message: str,
        custom_keywords: Sequence[str],
        custom_profanity: Sequence[str],
        add_secrets: Sequence[str] = (),
        remove_secret_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        message = block_message.strip()
        if not message or len(message) > self.config.max_custom_item_characters * 4:
            raise ConfigurationError("Guardrail blocker message is invalid")
        keywords = self._validated_items(custom_keywords, "custom keyword")
        profanity = self._validated_items(custom_profanity, "custom profanity")
        retained = [
            item for item in self._runtime.custom_secrets if item.id not in remove_secret_ids
        ]
        for secret in self._validated_items(add_secrets, "custom secret"):
            if re.search(r"[\s,;]", secret):
                raise ConfigurationError("Custom secrets must be single tokens")
            digest = hashlib.sha256(secret.casefold().encode()).hexdigest()
            if not any(digest.startswith(item.id) for item in retained):
                retained.append(
                    CustomSecret(
                        id=digest[:16],
                        fingerprint=f"••••{secret[-4:]}" if len(secret) >= 4 else "••••",
                        token_hash=digest,
                    )
                )
        if len(retained) > self.config.max_custom_items:
            raise ConfigurationError("Too many custom secrets")
        self._runtime = RuntimeGuardrailSettings(
            block_message=message,
            custom_keywords=keywords,
            custom_profanity=profanity,
            custom_secrets=tuple(retained),
        )
        return self.public_settings()

    def inspect(self, value: str, *, stage: GuardrailStage) -> GuardrailDecision:
        del stage  # All immutable categories apply at every trust boundary.
        for category_id, rules in self._compiled.items():
            for rule_id, pattern in rules:
                match = pattern.search(value)
                if match is not None:
                    return self._blocked(
                        category_id,
                        rule_id,
                        match.group(0),
                        value=value,
                        span=match.span(),
                    )
        normalized = value.casefold()
        for keyword in self._runtime.custom_keywords:
            start = normalized.find(keyword.casefold())
            if start >= 0:
                return self._blocked(
                    "custom_keyword",
                    "custom_keyword",
                    value[start : start + len(keyword)],
                    value=value,
                    span=(start, start + len(keyword)),
                )
        for word in self._runtime.custom_profanity:
            match = re.search(rf"(?i)(?<!\w){re.escape(word)}(?!\w)", value)
            if match is not None:
                return self._blocked(
                    "profanity",
                    "custom_profanity",
                    match.group(0),
                    value=value,
                    span=match.span(),
                )
        custom_hashes = {item.token_hash for item in self._runtime.custom_secrets}
        for match in re.finditer(r"[^\s,;]+", value):
            token = match.group(0)
            if hashlib.sha256(token.casefold().encode()).hexdigest() in custom_hashes:
                return self._blocked(
                    "secrets",
                    "custom_secret",
                    token,
                    value=value,
                    span=match.span(),
                )
        return GuardrailDecision(False)

    def require_safe(self, value: str, *, stage: GuardrailStage) -> GuardrailDecision:
        return self.inspect(value, stage=stage)

    def _redact_for_storage(self, value: str) -> str:
        redactions = self._configured_redactions(value)
        redactions.extend(self._custom_redactions(value))
        selected = _non_overlapping_redactions(redactions)
        safe = value
        for start, end, replacement in reversed(selected):
            safe = safe[:start] + replacement + safe[end:]
        return safe

    def _configured_redactions(self, value: str) -> list[tuple[int, int, str]]:
        redactions: list[tuple[int, int, str]] = []
        for category_id, rules in self._compiled.items():
            for _, pattern in rules:
                for match in pattern.finditer(value):
                    redactions.append(
                        (*match.span(), _category_replacement(category_id, match.group(0)))
                    )
        return redactions

    def _custom_redactions(self, value: str) -> list[tuple[int, int, str]]:
        redactions: list[tuple[int, int, str]] = []
        normalized = value.casefold()
        for keyword in self._runtime.custom_keywords:
            start = 0
            while (found := normalized.find(keyword.casefold(), start)) >= 0:
                matched = value[found : found + len(keyword)]
                redactions.append((found, found + len(keyword), _censor(matched)))
                start = found + len(keyword)
        for word in self._runtime.custom_profanity:
            for match in re.finditer(rf"(?i)(?<!\w){re.escape(word)}(?!\w)", value):
                redactions.append((*match.span(), _censor(match.group(0))))
        custom_hashes = {item.token_hash for item in self._runtime.custom_secrets}
        for match in re.finditer(r"[^\s,;]+", value):
            if hashlib.sha256(match.group(0).casefold().encode()).hexdigest() in custom_hashes:
                redactions.append((*match.span(), _mask_sensitive(match.group(0))))
        return redactions

    def _blocked(
        self,
        category_id: str,
        rule_id: str,
        matched: str,
        *,
        value: str,
        span: tuple[int, int],
    ) -> GuardrailDecision:
        if category_id in {"secrets", "pii"}:
            detail = "detected secrets in the input"
            replacement = _mask_sensitive_match(matched)
        elif category_id == "profanity":
            replacement = _censor(matched)
            detail = f'detected "{replacement}"'
        elif category_id == "custom_keyword":
            replacement = _censor(matched)
            detail = "detected a custom blocked keyword"
        else:
            category = self.config.categories[category_id]
            detail = category.detail
            replacement = "*" * len(matched)
        del span, replacement
        safe_content = self._redact_for_storage(value)
        return GuardrailDecision(
            blocked=True,
            category=category_id,
            rule_id=rule_id,
            message=f"{self._runtime.block_message}- {detail}",
            safe_content=safe_content,
        )

    def _validated_items(self, values: Sequence[str], label: str) -> tuple[str, ...]:
        if len(values) > self.config.max_custom_items:
            raise ConfigurationError(f"Too many {label} entries")
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = value.strip()
            if not clean or len(clean) > self.config.max_custom_item_characters:
                raise ConfigurationError(f"Invalid {label}")
            folded = clean.casefold()
            if folded not in seen:
                result.append(clean)
                seen.add(folded)
        return tuple(result)


def guardrail_config_from_mapping(data: Mapping[str, Any]) -> GuardrailConfig:
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise ConfigurationError("Guardrail rules must be a non-empty mapping")
    categories: dict[str, GuardrailCategory] = {}
    for category_id, raw_category in raw_rules.items():
        category = _guardrail_category(category_id, raw_category)
        categories[category.id] = category
    message = data.get("default_block_message")
    maximum = data.get("max_custom_items")
    item_length = data.get("max_custom_item_characters")
    if not _valid_guardrail_settings(message, maximum, item_length):
        raise ConfigurationError("Guardrail settings are invalid")
    assert isinstance(message, str) and isinstance(maximum, int) and isinstance(item_length, int)
    return GuardrailConfig(
        default_block_message=message.strip(),
        max_custom_items=maximum,
        max_custom_item_characters=item_length,
        categories=MappingProxyType(categories),
    )


def _category_replacement(category_id: str, matched: str) -> str:
    if category_id in {"secrets", "pii"}:
        return _mask_sensitive_match(matched)
    if category_id == "profanity":
        return _censor(matched)
    return "*" * len(matched)


def _non_overlapping_redactions(
    redactions: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    selected: list[tuple[int, int, str]] = []
    covered_until = -1
    for start, end, replacement in sorted(
        redactions, key=lambda item: (item[0], -(item[1] - item[0]))
    ):
        if start >= covered_until:
            selected.append((start, end, replacement))
            covered_until = end
    return selected


def _guardrail_category(category_id: object, raw_category: object) -> GuardrailCategory:
    if not isinstance(category_id, str) or not isinstance(raw_category, dict):
        raise ConfigurationError("Guardrail category is invalid")
    raw_patterns = raw_category.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise ConfigurationError(f"Guardrail category {category_id!r} has no patterns")
    rules = tuple(_guardrail_rule(item) for item in raw_patterns)
    label = raw_category.get("label")
    detail = raw_category.get("detail")
    if not isinstance(label, str) or not isinstance(detail, str):
        raise ConfigurationError("Guardrail category metadata is invalid")
    return GuardrailCategory(category_id, label, detail, rules)


def _guardrail_rule(item: object) -> GuardrailRule:
    if not isinstance(item, dict):
        raise ConfigurationError("Guardrail rule is invalid")
    rule_id = item.get("id")
    expression = item.get("expression")
    if not isinstance(rule_id, str) or not isinstance(expression, str):
        raise ConfigurationError("Guardrail rule fields are invalid")
    try:
        re.compile(expression)
    except re.error as exc:
        raise ConfigurationError(f"Guardrail regex {rule_id!r} is invalid") from exc
    return GuardrailRule(rule_id, expression)


def _valid_guardrail_settings(message: object, maximum: object, item_length: object) -> bool:
    return bool(
        isinstance(message, str)
        and message.strip()
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and maximum >= 1
        and isinstance(item_length, int)
        and not isinstance(item_length, bool)
        and item_length >= 1
    )


def _censor(value: str) -> str:
    if len(value) <= 1:
        return "*"
    if len(value) == 2:
        return value[0] + "*"
    return value[0] + ("*" * (len(value) - 2)) + value[-1]


def _mask_sensitive(value: str) -> str:
    """Retain at most ten percent of a secret/PII value and irreversibly mask the rest."""
    visible = len(value) // 10
    return value[:visible] + ("*" * (len(value) - visible))


def _mask_sensitive_match(value: str) -> str:
    assignment = _split_labeled_value(
        value,
        (
            "api key",
            "api_key",
            "api-key",
            "access token",
            "access_token",
            "access-token",
            "secret",
            "password",
            "passwd",
            "pwd",
        ),
        separator_required=True,
    )
    if assignment is not None:
        prefix, sensitive = assignment
        return prefix + _mask_sensitive(sensitive)
    labeled_pii = _split_labeled_value(
        value,
        (
            "my name is",
            "full name",
            "phone number",
            "phone",
            "mobile number",
            "mobile",
            "telephone number",
            "telephone",
        ),
        separator_required=False,
    )
    if labeled_pii is not None:
        prefix, sensitive = labeled_pii
        return prefix + _mask_sensitive(sensitive)
    if value.casefold().startswith("bearer "):
        return value[:7] + _mask_sensitive(value[7:])
    return _mask_sensitive(value)


def _split_labeled_value(
    value: str,
    labels: tuple[str, ...],
    *,
    separator_required: bool,
) -> tuple[str, str] | None:
    normalized = value.casefold()
    for label in labels:
        label_start = normalized.find(label)
        if label_start < 0:
            continue
        cursor = label_start + len(label)
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] in ":=-":
            cursor += 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
        elif separator_required:
            continue
        if cursor < len(value):
            return value[:cursor], value[cursor:]
    return None
