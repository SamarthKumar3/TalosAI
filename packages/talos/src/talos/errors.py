from __future__ import annotations


class TalosError(Exception):
    """Base exception with a stable machine-readable code."""

    code = "talos_error"
    retryable = False


class ConfigurationError(TalosError):
    code = "configuration_error"


class ProviderError(TalosError):
    code = "provider_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_error"


class ExecutionBudgetExceededError(TalosError):
    code = "execution_budget_exceeded"


class ExecutionTimeoutError(TalosError):
    code = "execution_timeout"


class ConversationNotFoundError(TalosError):
    code = "conversation_not_found"


class RunCancelledError(TalosError):
    code = "run_cancelled"


class RunInProgressError(TalosError):
    code = "run_in_progress"
    retryable = True


class EvaluationError(TalosError):
    code = "evaluation_error"
