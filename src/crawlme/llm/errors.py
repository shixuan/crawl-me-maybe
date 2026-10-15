"""LLM dependency, budget and provider errors using the common error contract."""

from crawlme.errors import Error, Kind, Scope


class DependencyError(Error):
    scope = Scope.DEPENDENCY
    source = "llm"
    code = "dependency_missing"
    kind = Kind.NOT_FOUND


class TokenBudgetError(Error):
    """Raised before a call that would exceed the task token budget."""

    source = "budget"
    code = "token_limit"
    kind = Kind.LIMIT_REACHED


class RemoteError(Error):
    """Provider failure with its original status, code and error type."""

    scope = Scope.DEPENDENCY
    source = "llm"
    code = "remote_rejected"

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        stage: str = "",
        status_code: int | None = None,
        provider_code: str = "",
        provider_type: str = "",
        kind: Kind = Kind.UNKNOWN,
    ) -> None:
        self.message = message
        self.model = model
        self.stage = stage
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_type = provider_type
        labels = [value for value in (stage, model, str(status_code or ""), provider_code, provider_type) if value]
        super().__init__(
            f"LLM remote error [{', '.join(labels)}]: {message}",
            kind=kind,
            details={
                "stage": stage,
                "model": model,
                "status_code": status_code,
                "provider_code": provider_code,
                "provider_type": provider_type,
            },
        )
