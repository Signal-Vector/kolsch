"""Model fallback errors.

Eight classes that drive the engine's fallback loop. Classification
(Recoverable vs Fatal) is load-bearing — the engine's
`is_fallback_triggering` predicate (see `engine._fallback`) consults
the level to decide whether to walk the chain. See the
[error model](../explanation/error-model.md) for the
classification contract.

Recoverable — drive fallback:

- `ModelRateLimited` — HTTP 429.
- `ModelServerError` — HTTP 5xx.
- `ModelTimeout` — provider unresponsive.
- `ModelContextOverflow` — request exceeds the model's window; a
  larger-context model may handle it.

Fatal — abort without consulting the chain:

- `ModelAuthenticationFailed` — HTTP 401/403; the same key fails
  everywhere.
- `ModelBadRequest` — HTTP 400; the request itself is broken.

Engine-loop terminus:

- `LlmAllModelsFailed` — chain exhausted; the cause chain on the
  emitted `thread.exchange_failed` references the `attempts` field.

Layer-1 validator warning:

- `ConfigFallbackDuplicate` — Diagnostic surface for repeats in the
  chain; not a failure.
"""

from __future__ import annotations

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ModelRateLimited(PlatformError):
    """Raised when the provider returned HTTP 429.

    Recoverable — the engine's fallback loop walks to the next chain
    entry.
    """

    code = "model.rate_limited"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        model: str,
        retry_after_seconds: float | None = None,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        detail: dict[str, Any] = {"provider": provider, "model": model}
        if retry_after_seconds is not None:
            detail["retry_after_seconds"] = retry_after_seconds
        super().__init__(
            message=f"Provider {provider!r} rate-limited model {model!r}",
            where=where,
            detail=detail,
        )


class ModelServerError(PlatformError):
    """Raised when the provider returned HTTP 5xx.

    Recoverable — likely a transient infrastructure failure; another
    provider may succeed.
    """

    code = "model.server_error"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        model: str,
        status_code: int,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        super().__init__(
            message=(
                f"Provider {provider!r} returned HTTP {status_code} for model {model!r}"
            ),
            where=where,
            detail={
                "provider": provider,
                "model": model,
                "status_code": status_code,
            },
        )


class ModelTimeout(PlatformError):
    """Raised when the provider did not respond within the configured timeout.

    Recoverable — another provider may answer.
    """

    code = "model.timeout"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        model: str,
        timeout_seconds: float,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        super().__init__(
            message=(
                f"Provider {provider!r} timed out after {timeout_seconds}s "
                f"for model {model!r}"
            ),
            where=where,
            detail={
                "provider": provider,
                "model": model,
                "timeout_seconds": timeout_seconds,
            },
        )


class ModelContextOverflow(PlatformError):
    """Raised when the request exceeded the model's context window.

    Recoverable — a different model may have a larger window.
    """

    code = "model.context_overflow"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        model: str,
        requested_tokens: int,
        max_tokens: int | None = None,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        detail: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "requested_tokens": requested_tokens,
        }
        if max_tokens is not None:
            detail["max_tokens"] = max_tokens
        super().__init__(
            message=(
                f"Model {model!r} on provider {provider!r} cannot fit "
                f"{requested_tokens} tokens"
            ),
            where=where,
            detail=detail,
        )


class ModelAuthenticationFailed(PlatformError):
    """Raised when the provider rejected credentials (HTTP 401/403).

    Fatal — the same key would fail against the next chain entry too.
    """

    code = "model.authentication_failed"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        model: str,
        status_code: int,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        super().__init__(
            message=(
                f"Provider {provider!r} rejected credentials (HTTP "
                f"{status_code}) for model {model!r}"
            ),
            where=where,
            detail={
                "provider": provider,
                "model": model,
                "status_code": status_code,
            },
        )


class ModelBadRequest(PlatformError):
    """Raised when the provider returned HTTP 400.

    Fatal — the chain won't help; the request itself is malformed.
    """

    code = "model.bad_request"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        model: str,
        status_code: int,
        reason: str,
        *,
        where: str = "llm.connector.call",
    ) -> None:
        super().__init__(
            message=(
                f"Provider {provider!r} rejected request (HTTP "
                f"{status_code}) for model {model!r}: {reason}"
            ),
            where=where,
            detail={
                "provider": provider,
                "model": model,
                "status_code": status_code,
                "reason": reason,
            },
        )


class LlmAllModelsFailed(PlatformError):
    """Raised when every model in the fallback chain returned Recoverable.

    The exchange ends. `detail.attempts` is the full ordered list of
    attempts the engine tried — one entry per attempt with shape
    `{"model": {provider, model}, "error_code": str, "detail": dict}`
    — so replay can reconstruct the full failure trail.
    """

    code = "llm.all_models_failed"
    level = Level.FATAL

    def __init__(
        self,
        primary: dict[str, str],
        attempts: list[dict[str, Any]],
        reason: str,
        *,
        where: str = "engine.fallback.execute",
    ) -> None:
        super().__init__(
            message=(
                f"All {len(attempts)} model attempts failed (primary "
                f"{primary.get('provider')}/{primary.get('model')}): {reason}"
            ),
            where=where,
            detail={
                "primary": dict(primary),
                "attempts": list(attempts),
                "reason": reason,
            },
        )


class ConfigFallbackDuplicate(PlatformError):
    """Raised when a `fallback_models` entry repeats the primary or another entry.

    Diagnostic, not Fatal — the user may have intentionally listed
    the same model twice (for example, to retry the primary).
    Surfaces at `kolsch run` startup so the duplication is visible
    rather than buried in logs. The class lives at Diagnostic since
    the error model doesn't yet have a Warning tier; if one lands
    later, this class moves.
    """

    code = "config.fallback_duplicate"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        agent: str,
        model: dict[str, str],
        positions: list[int],
        *,
        where: str = "assets.assembler.fallback_validate",
    ) -> None:
        super().__init__(
            message=(
                f"Agent {agent!r}: model {model.get('provider')}/"
                f"{model.get('model')} appears at positions {positions} "
                f"in the fallback chain"
            ),
            where=where,
            detail={
                "agent": agent,
                "model": dict(model),
                "positions": list(positions),
            },
        )
