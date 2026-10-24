"""Engine-boundary exception classifier — cause-chain safety net.

The engine's top-level boundary used to wrap every non-PlatformError
as [`PlatformInternalError`][errors.PlatformInternalError]. That
equated "unknown to the engine" with "the engine crashed," which
obscured terminal conditions from below — most painfully, transport
timeouts that the retry pipeline should have absorbed.

This module is the safety net half. The primary half is the LLM
call seam (bug-145): `engine._crewai_adapter.make_llm_callable` hands
every raw provider exception to the building connector's
`classify_call_error` hook and re-raises it as a typed
[`PlatformError`][errors.PlatformError] BEFORE the fallback loop and
the boundary see it; the CrewAI framework adapter also translates the
CrewAI exception shapes it knows. Even with those in place, a path
the seam doesn't cover (a connector that returns `None`, a future
framework release) could still surface a raw exception; the safety
net walks the cause chain on every boundary catch so the boundary can
re-classify based on the original typed exception even when
intermediate layers flattened it.

Two passes, in order: the name tables below (unchanged since before
bug-145, so their mappings — e.g. `openai.AuthenticationError` →
`agent.unauthorized` — keep winning), then a status branch that maps
any remaining link with an `int` `status_code` >= 400 and a `response`
through the connectors' shared pure mapper
`llm.providers._errors.classify_http_response`. `platform.internal_error`
is therefore left for genuinely unknown exceptions.

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

_HTTP_ERROR_MIN = 400

_STATUS_WHERE = "engine._classify.cause_chain"
"""`where` stamped on status-branch errors, so the log shows the safety
net — not the connector call seam — classified them."""


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
    chain = _cause_chain(exc)
    for current in chain:
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
    for current in chain:
        classified = _classify_status(current)
        if classified is not None:
            return classified
    return None


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """`exc` and its `__cause__` ancestors, cycle-guarded, at most `_MAX_DEPTH`."""
    seen: set[int] = set()
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_DEPTH:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    return chain


def _classify_status(link: BaseException) -> PlatformError | None:
    """Map a duck-typed HTTP status error through the shared mapper (bug-145)."""
    status = getattr(link, "status_code", None)
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or status < _HTTP_ERROR_MIN
        or not hasattr(link, "response")
    ):
        return None
    from llm.providers._errors import (  # noqa: PLC0415 — keep the engine import graph narrow
        classify_http_response,
        response_body,
    )

    return classify_http_response(
        status,
        response_body(link),
        provider=type(link).__module__,
        model="unknown",
        where=_STATUS_WHERE,
    )
