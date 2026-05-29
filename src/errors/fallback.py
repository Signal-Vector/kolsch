"""Model fallback errors — spec §4.15 (Phase 1.3, FB-R5).

Eight error classes mapped 1:1 to the §4.15 catalog table. The
classification (Recoverable vs Fatal) is load-bearing — the engine's
``is_fallback_triggering`` predicate (see :mod:`engine._fallback`)
consults the level to decide whether to walk the chain. Adding a
new ``model.*`` code without declaring its level breaks the
AGENTS.md hard rule (FB-R10).

Recoverable — drive fallback:

- :class:`ModelRateLimited` — HTTP 429.
- :class:`ModelServerError` — HTTP 5xx.
- :class:`ModelTimeout` — provider unresponsive.
- :class:`ModelContextOverflow` — request exceeds the model's
  window; a larger-context model may handle it.

Fatal — abort without consulting the chain:

- :class:`ModelAuthenticationFailed` — HTTP 401/403; same key
  fails everywhere.
- :class:`ModelBadRequest` — HTTP 400; the request itself is
  broken.

Engine-loop terminus:

- :class:`LlmAllModelsFailed` — chain exhausted; the cause chain
  on the emitted ``thread.exchange_failed`` references the
  ``attempts`` field.

Layer-1 validator warning:

- :class:`ConfigFallbackDuplicate` — Diagnostic (warning surface)
  for repeats in the chain; not an error.
"""

from __future__ import annotations

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ModelRateLimited(PlatformError):
    """Provider returned HTTP 429 (rate limit). Recoverable — the
    engine's fallback loop walks to the next chain entry."""

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
    """Provider returned HTTP 5xx. Recoverable — likely transient
    infrastructure failure; another provider may succeed."""

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
    """Provider did not respond within the configured timeout.
    Recoverable — another provider may answer."""

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
    """Request exceeded the model's context window. Recoverable —
    a different model may have a larger window."""

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
    """Provider rejected credentials (HTTP 401/403). Fatal — the
    same key would fail against the next chain entry too."""

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
    """Provider returned HTTP 400 — the request itself is malformed.
    Fatal — the chain won't help; the request is broken."""

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
    """Every model in the fallback chain (primary + each entry)
    returned a Recoverable error. The exchange ends.

    ``detail.attempts`` is the full ordered list of attempts the
    engine tried, one entry per attempt:
    ``[{"model": {provider, model}, "error_code": str, "detail": dict}, ...]``.
    Replay reconstructs the full failure trail from this field.
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
    """An entry in ``fallback_models`` is the same canonical
    ``{provider, model}`` as the primary or another chain entry.

    Diagnostic, not Fatal — the user may have intentionally listed
    the same model twice (e.g., to retry the primary). Surfaces at
    `kolsch run` startup so the user sees the pattern rather than
    silently in logs.

    Maps to ``config.fallback_duplicate`` per Error Model §4.15.
    Phase 1.3 chose ``Diagnostic`` since the catalog has no Warning
    tier; if one lands later, this class moves.
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
