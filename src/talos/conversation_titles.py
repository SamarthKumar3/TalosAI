"""Conservative display titles; never truncate a potential secret into visibility."""

import re

from talos.guardrails import GuardrailService

_SENSITIVE_TITLE = re.compile(
    r"(?i)(?:\b(?:sk[-*]|gh[pousr]_|github_pat_|AIza|AKIA|ASIA)\S*"
    r"|\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|pwd|bearer)\b"
    r"|-----BEGIN|\beyJ[A-Za-z0-9_-]+\."
    r"|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|\b[A-Za-z0-9_/-]{40,}\b|[•*]{4,})"
)


def conversation_title(value: str, guardrails: GuardrailService | None = None) -> str:
    if not value.strip():
        return "New conversation"
    if _SENSITIVE_TITLE.search(value) or (
        guardrails is not None and guardrails.inspect(value, stage="memory").blocked
    ):
        return "Private conversation"
    safe = " ".join(value.split())
    return safe[:48] + "..." if len(safe) > 48 else safe
