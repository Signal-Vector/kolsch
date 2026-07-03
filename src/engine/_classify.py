"""Engine-boundary exception classifier — cause-chain safety net.

The engine's top-level boundary used to wrap every non-PlatformError
as [`PlatformInternalError`][errors.PlatformInternalError]. That
equated "unknown to the engine" with "the engine crashed," which
obscured terminal conditions from below — most painfully, transport
timeouts that the retry pipeline should have absorbed.

This module is the safety net half. The primary half is per-framework
adapter translation (the CrewAI adapter catches known CrewAI
exception shapes and re-raises as typed
[`PlatformError`][errors.PlatformError] BEFORE reaching the
boundary). Even with the adapter in place, a future framework
release could change its surface; the safety net walks the cause
chain on every boundary catch so the boundary can re-classify based
on the original typed exception even when intermediate layers
flattened it.

**String matching, not isinstance.** The classifier matches on
`f"{type(exc).__module__}.{type(exc).__name__}"` so the engine does
not hard-import any provider SDK at module load. The engine module
must stay loadable without `openai` (or any other provider) on the
PYTHONPATH — provider SDKs are test-group dependencies, not engine
deps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from errors import AgentUnauthorized, ModelUpstreamError

if TYPE_CHECKING:
    from errors import PlatformError


# Module + class qualified names recognised in the __cause__ chain.
# String matching keeps the engine free of provider SDK imports.
_RECOVERABLE_UPSTREAM: frozenset[str] = frozenset(
    {
        # OpenAI SDK typed exceptions. APITimeoutError is a subclass of
        # APIConnectionError; we enumerate the children explicitly so
        # future granularity (e.g., per-error backoff policy) is a
        # row-add, not a rewrite.
        "openai.APITimeoutError",
        "openai.APIConnectionError",
        "openai.RateLimitError",
    }
)

_FATAL_AUTH: frozenset[str] = frozenset(
    {
        "openai.AuthenticationError",
        "openai.PermissionDeniedError",
    }
)

_MAX_DEPTH = 10
"""Pathological-chain guard. Real exception chains rarely exceed 3
hops (provider → framework adapter → engine); ten is generous + safe."""


def classify_from_cause_chain(
    exc: BaseException,
    *,
    agent_id: str = "",
) -> PlatformError | None:
    """Walk the cause chain and re-classify a known provider exception.

    Walks `exc` → `exc.__cause__` → ... up to `_MAX_DEPTH`. Returns a
    typed [`PlatformError`][errors.PlatformError] when a known type
    appears in the chain, `None` otherwise. `None` is the boundary's
    signal to fall back to
    [`PlatformInternalError`][errors.PlatformInternalError].

    Cycle detection via seen-set on `id()` — Python's `__cause__`
    chains can technically loop if user code attaches a cause back
    to an ancestor; without the guard the loop would hang. String
    matching on `module.name` avoids the layering violation of
    hard-importing provider SDKs from engine code.

    Args:
        exc: The exception to classify (typically the engine
            boundary's caught non-PlatformError).
        agent_id: Stamped on
            [`AgentUnauthorized`][errors.AgentUnauthorized] when an
            auth exception classifies.

    Returns:
        A typed `PlatformError` when the chain carries a known
        provider exception, otherwise `None`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < _MAX_DEPTH:
        if id(current) in seen:
            return None
        seen.add(id(current))
        qual = f"{type(current).__module__}.{type(current).__name__}"
        if qual in _RECOVERABLE_UPSTREAM:
            return ModelUpstreamError(
                provider=type(current).__module__,
                http_status=0,
                response=str(current),
            )
        if qual in _FATAL_AUTH:
            return AgentUnauthorized(
                agent_id=agent_id,
                action="model.call",
                principal="user.local",
            )
        current = current.__cause__
        depth += 1
    return None
