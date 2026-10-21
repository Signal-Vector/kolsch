"""Shared HTTP-response to `model.*` error mapping helpers.

The reference providers (OpenCode, OpenRouter, plus planned siblings)
speak OpenAI-compatible JSON error bodies. The mapping from HTTP
status plus body to a catalogued `model.*` PlatformError is the same
shape across all of them; concentrating it here means a new provider
that picks up the same shape doesn't reimplement the classification
and can't drift from the canonical error-model table.

Every public function here is pure — no I/O, no time, no env. Tests
exercise the branches in isolation without the provider machinery
(`tests.component.llm.test_provider_errors`).

[`classify_openai_compat_exception`][llm.providers._errors.classify_openai_compat_exception]
is the call-time entry point: it walks the exception a connector's own
`crewai.LLM` raised (including CrewAI's re-wrapped shapes) and maps the
first recognised link through the two helpers below. OpenAI-compatible
connectors' `classify_call_error` hooks delegate to it (bug-145).

When a provider deviates from the OpenAI-compat shape (Bedrock's
sigv4 wrap, Anthropic's distinct error code field), the provider
module adds its own helper that falls back to
[`classify_http_response`][llm.providers._errors.classify_http_response]
for the shared status-code branches. Any deviation must be documented
in that provider's module docstring.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from errors import (
    ModelAuthenticationFailed,
    ModelBadRequest,
    ModelContextOverflow,
    ModelNotFound,
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
    ModelUpstreamError,
)

if TYPE_CHECKING:
    from errors import PlatformError


# Substrings the OpenAI-compat error body uses to flag context
# overflow. Lowercased before scan; case-insensitive match. MUST stay a
# superset of CrewAI's `CONTEXT_LIMIT_ERRORS` (bug-145): CrewAI's
# executor silently summarises on any error whose text matches one of
# its phrases, so a `ModelBadRequest` reason carrying such a phrase
# would still trip that unrecorded path. `test_provider_errors` imports
# CrewAI's list and asserts every phrase is detected here.
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length",
    "context_window",
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "expected a string with maximum length",
    "too long",
    "exceeds",
)


_HTTP_ERROR_MIN: int = 400
_HTTP_AUTH_CODES: frozenset[int] = frozenset({401, 403})
# Statuses whose body may report a context overflow: 400 (OpenAI's
# shape), 413 (payload too large), 422 (validation-style gateways).
_HTTP_OVERFLOW_CANDIDATES: frozenset[int] = frozenset({400, 413, 422})
_HTTP_BAD_REQUEST_CODES: frozenset[int] = frozenset({400, 422})
_HTTP_NOT_FOUND: int = 404
# 402 (out of credits on THIS provider) and 409 (conflict that already
# survived the SDK's retries) — another chain entry can answer.
_HTTP_UPSTREAM_CODES: frozenset[int] = frozenset({402, 409})
_HTTP_REQUEST_TIMEOUT: int = 408
_HTTP_RATE_LIMIT: int = 429
_HTTP_SERVER_ERROR_MIN: int = 500
_HTTP_SERVER_ERROR_MAX: int = 599

# How far `classify_openai_compat_exception` walks `__cause__`; matches
# the engine safety net's `_MAX_DEPTH`.
_MAX_CAUSE_DEPTH: int = 10

# Recognised by qualified class name so this module never imports a
# provider SDK (the engine reuses it). Matched against every class in
# the exception's MRO, so subclasses match too.
_CREWAI_OVERFLOW_NAMES: frozenset[str] = frozenset(
    {
        "crewai.utilities.exceptions.context_window_exceeding_exception."
        "LLMContextLengthExceededError",
    }
)
_SDK_TRANSPORT_NAMES: frozenset[str] = frozenset(
    {"openai.APITimeoutError", "openai.APIConnectionError"}
)


def classify_http_response(  # noqa: PLR0911 — one return per row of the status table
    status_code: int,
    body: str,
    *,
    provider: str,
    model: str,
    timeout_seconds: float = 0.0,
) -> PlatformError | None:
    """Map one HTTP response to a catalogued `model.*` PlatformError.

    Returns `None` for statuses below 400 (the provider's
    response-parsing layer takes over). Every status >= 400 maps to a
    typed error (bug-145 made the mapping total):

    - 400 / 413 / 422 with context-overflow markers in body ->
      `ModelContextOverflow`
    - 400 / 422 otherwise -> `ModelBadRequest`
    - 401 / 403 -> `ModelAuthenticationFailed`
    - 404 -> `ModelNotFound`
    - 402 / 409 -> `ModelUpstreamError`
    - 408 -> `ModelTimeout`
    - 429 -> `ModelRateLimited`
    - 500-599 -> `ModelServerError`
    - any other status >= 400 -> `ModelBadRequest`

    New `model.*` codes added later must add a branch here in the same
    change that introduces them.

    Args:
        status_code: HTTP status code from the provider's response.
        body: The raw response body (may be empty).
        provider: The provider name to stamp on the resulting error.
        model: The model id to stamp on the resulting error.
        timeout_seconds: The configured client timeout, stamped on the
            `ModelTimeout` a 408 maps to; `0.0` when unknown.

    Returns:
        The matched `PlatformError`, or `None` when the status is
        below 400.
    """
    if status_code < _HTTP_ERROR_MIN:
        return None
    if status_code == _HTTP_RATE_LIMIT:
        return ModelRateLimited(provider=provider, model=model)
    if _HTTP_SERVER_ERROR_MIN <= status_code <= _HTTP_SERVER_ERROR_MAX:
        return ModelServerError(provider=provider, model=model, status_code=status_code)
    if status_code in _HTTP_AUTH_CODES:
        return ModelAuthenticationFailed(
            provider=provider, model=model, status_code=status_code
        )
    if status_code in _HTTP_OVERFLOW_CANDIDATES and _is_context_overflow_body(body):
        return ModelContextOverflow(
            provider=provider,
            model=model,
            requested_tokens=0,  # provider rarely reports exact count
        )
    reason = _extract_error_message(body)
    if status_code == _HTTP_NOT_FOUND:
        return ModelNotFound(
            provider=provider,
            model=model,
            status_code=status_code,
            reason=reason or "model not found",
        )
    if status_code in _HTTP_UPSTREAM_CODES:
        return ModelUpstreamError(
            provider=provider,
            http_status=status_code,
            response=reason or "",
            where="llm.connector.call",
        )
    if status_code == _HTTP_REQUEST_TIMEOUT:
        return ModelTimeout(
            provider=provider, model=model, timeout_seconds=timeout_seconds
        )
    # 400 / 422 and every other status >= 400.
    return ModelBadRequest(
        provider=provider,
        model=model,
        status_code=status_code,
        reason=reason or "malformed request",
    )


def classify_openai_compat_exception(
    exc: BaseException,
    *,
    provider: str,
    model: str,
    timeout_seconds: float,
) -> PlatformError | None:
    """Map an OpenAI-compatible `crewai.LLM` call exception to a `model.*` error.

    The shared body of every OpenAI-compatible connector's
    `classify_call_error` hook (bug-145). Walks `exc` and its
    `__cause__` chain (cycle-guarded — CrewAI re-raises most SDK
    errors as `raise e from e`, a self-cycle — and capped at 10 links);
    the first recognised link wins:

    - CrewAI's `LLMContextLengthExceededError` -> `ModelContextOverflow`.
    - Anything with an `int` `status_code` and a `response` (the
      `openai.APIStatusError` family, by duck typing) ->
      [`classify_http_response`][llm.providers._errors.classify_http_response]
      on the response body. This also covers CrewAI's
      `ValueError("Model … not found") from NotFoundError`.
    - `httpx` transport failures ->
      [`classify_transport_error`][llm.providers._errors.classify_transport_error].
    - `openai.APITimeoutError` / `openai.APIConnectionError` (by
      qualified name; also reached through CrewAI's
      `ConnectionError … from` them) -> `ModelTimeout`.

    Pure: no I/O, and no provider SDK is imported.

    Args:
        exc: The exception the connector's `crewai.LLM` raised.
        provider: The provider name to stamp on the resulting error.
        model: The model id to stamp on the resulting error.
        timeout_seconds: The configured client timeout.

    Returns:
        The classified `PlatformError`, or `None` when no link in the
        chain is recognised.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    depth = 0
    while link is not None and depth < _MAX_CAUSE_DEPTH and id(link) not in seen:
        seen.add(id(link))
        classified = _classify_link(
            link, provider=provider, model=model, timeout_seconds=timeout_seconds
        )
        if classified is not None:
            return classified
        link = link.__cause__
        depth += 1
    return None


def classify_transport_error(
    exc: BaseException,
    *,
    provider: str,
    model: str,
    timeout_seconds: float,
) -> PlatformError | None:
    """Map a transport-level exception to a catalogued `model.*` error.

    Treats both `httpx.TimeoutException` subclasses and connect-time
    failures (DNS, connection refused) as `ModelTimeout` — the
    user-visible effect is identical to a timeout when no first byte
    has arrived. Returns `None` for unrecognised transport failures so
    the caller can let them propagate to the engine boundary's
    `PlatformInternalError` wrap.

    Args:
        exc: The transport exception raised inside the provider call.
        provider: The provider name to stamp on the resulting error.
        model: The model id to stamp on the resulting error.
        timeout_seconds: The configured timeout, surfaced in the error.

    Returns:
        `ModelTimeout` for timeouts and connect-time failures, `None`
        otherwise.
    """
    import httpx  # noqa: PLC0415

    if isinstance(exc, httpx.TimeoutException):
        return ModelTimeout(
            provider=provider, model=model, timeout_seconds=timeout_seconds
        )
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        # No first byte received -> treat as timeout per the provider
        # docstring's "out-of-band failures" branch.
        return ModelTimeout(
            provider=provider, model=model, timeout_seconds=timeout_seconds
        )
    return None


def _classify_link(
    link: BaseException,
    *,
    provider: str,
    model: str,
    timeout_seconds: float,
) -> PlatformError | None:
    """Classify one link of a cause chain, or return `None`."""
    names = _qualified_names(link)
    if names & _CREWAI_OVERFLOW_NAMES:
        return ModelContextOverflow(provider=provider, model=model, requested_tokens=0)
    status = getattr(link, "status_code", None)
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and hasattr(link, "response")
    ):
        classified = classify_http_response(
            status,
            _response_body(link),
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        if classified is not None:
            return classified
    transport = classify_transport_error(
        link, provider=provider, model=model, timeout_seconds=timeout_seconds
    )
    if transport is not None:
        return transport
    if names & _SDK_TRANSPORT_NAMES:
        return ModelTimeout(
            provider=provider, model=model, timeout_seconds=timeout_seconds
        )
    return None


def _qualified_names(exc: BaseException) -> set[str]:
    """Return `module.QualName` for every class in `exc`'s MRO."""
    return {f"{cls.__module__}.{cls.__qualname__}" for cls in type(exc).__mro__}


def _response_body(link: BaseException) -> str:
    """Best-effort body text of a status error: `response.text`, else `body`.

    `httpx.Response.text` raises on an unread streaming response, so
    any failure falls back to the SDK's parsed `body` attribute.
    """
    try:
        text = link.response.text  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — duck-typed; any failure means "no text"
        text = None
    if isinstance(text, str):
        return text
    body = getattr(link, "body", None)
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, default=str)
    except (TypeError, ValueError):
        return str(body)


def _is_context_overflow_body(body: str) -> bool:
    """Scan an OpenAI-compatible error body for context-overflow markers."""
    if not body:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def _extract_error_message(body: str) -> str | None:
    """Pull the human-readable message out of an OpenAI-compat error body.

    Best-effort: returns `None` when the body isn't JSON or doesn't
    follow the documented shape. The caller substitutes a default.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # Body wasn't JSON — return it verbatim (truncated) so the
        # caller can still surface SOME context.
        return body[:200] if body else None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    if isinstance(err, str):
        return err
    return None
