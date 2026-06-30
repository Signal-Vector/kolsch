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
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
)

if TYPE_CHECKING:
    from errors import PlatformError


# Substrings the OpenAI-compat error body uses to flag context
# overflow. Lowercased before scan; case-insensitive match.
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length",
    "context_window",
    "maximum context",
    "too long",
    "exceeds",
)


_HTTP_AUTH_CODES: frozenset[int] = frozenset({401, 403})
_HTTP_BAD_REQUEST: int = 400
_HTTP_RATE_LIMIT: int = 429
_HTTP_SERVER_ERROR_MIN: int = 500
_HTTP_SERVER_ERROR_MAX: int = 599


def classify_http_response(
    status_code: int,
    body: str,
    *,
    provider: str,
    model: str,
) -> PlatformError | None:
    """Map one HTTP response to a catalogued `model.*` PlatformError.

    Returns `None` for 2xx and other non-error statuses (the
    provider's response-parsing layer takes over). Otherwise returns
    a typed error:

    - 429 -> `ModelRateLimited`
    - 500-599 -> `ModelServerError`
    - 401/403 -> `ModelAuthenticationFailed`
    - 400 with context-overflow markers in body -> `ModelContextOverflow`
    - 400 otherwise -> `ModelBadRequest`

    The mapping is exhaustive over the documented `model.*` surface;
    new codes added later must add a branch here in the same change
    that introduces them.

    Args:
        status_code: HTTP status code from the provider's response.
        body: The raw response body (may be empty).
        provider: The provider name to stamp on the resulting error.
        model: The model id to stamp on the resulting error.

    Returns:
        The matched `PlatformError`, or `None` when the status isn't
        an error class this helper handles.
    """
    if status_code == _HTTP_RATE_LIMIT:
        return ModelRateLimited(provider=provider, model=model)
    if _HTTP_SERVER_ERROR_MIN <= status_code <= _HTTP_SERVER_ERROR_MAX:
        return ModelServerError(provider=provider, model=model, status_code=status_code)
    if status_code in _HTTP_AUTH_CODES:
        return ModelAuthenticationFailed(
            provider=provider, model=model, status_code=status_code
        )
    if status_code == _HTTP_BAD_REQUEST:
        if _is_context_overflow_body(body):
            return ModelContextOverflow(
                provider=provider,
                model=model,
                requested_tokens=0,  # provider rarely reports exact count
            )
        return ModelBadRequest(
            provider=provider,
            model=model,
            status_code=status_code,
            reason=_extract_error_message(body) or "malformed request",
        )
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
