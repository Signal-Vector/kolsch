"""Per-connector error-classification tests — Phase 1.3 (Step 20 fix).

Exercises :func:`llm.providers._errors.classify_http_response` and
:func:`llm.providers._errors.classify_transport_error` exhaustively.
The mapping is the basis of the engine's fallback decision (Error
Model §4.15 + Phase 1.3 patch §2.2); every documented branch is
asserted here so a future code change cannot drift silently.

The Phase 1.3 AGENTS.md FB-R10 hard rule requires this: every
``model.*`` code declares its level AND every connector documents
the response-to-code mapping AND those mappings are testable.
"""

from __future__ import annotations

import httpx

from errors import (
    ModelAuthenticationFailed,
    ModelBadRequest,
    ModelContextOverflow,
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
)
from llm.providers._errors import (
    classify_http_response,
    classify_transport_error,
)
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

# ===========================================================================
# classify_http_response — full branch coverage
# ===========================================================================


def test_429_maps_to_rate_limited() -> None:
    err = classify_http_response(429, "", provider="openrouter", model="x")
    assert isinstance(err, ModelRateLimited)
    assert err.detail["provider"] == "openrouter"
    assert err.detail["model"] == "x"


def test_5xx_maps_to_server_error() -> None:
    for status in (500, 502, 503, 599):
        err = classify_http_response(status, "", provider="p", model="m")
        assert isinstance(err, ModelServerError), f"status {status}"
        assert err.detail["status_code"] == status


def test_401_and_403_map_to_authentication_failed() -> None:
    for status in (401, 403):
        err = classify_http_response(status, "", provider="p", model="m")
        assert isinstance(err, ModelAuthenticationFailed), f"status {status}"


def test_400_with_context_overflow_marker_maps_to_overflow() -> None:
    body = (
        '{"error": {"message": "This model context_length exceeded", '
        '"code": "context_length_exceeded"}}'
    )
    err = classify_http_response(400, body, provider="p", model="m")
    assert isinstance(err, ModelContextOverflow)


def test_400_with_context_window_marker_maps_to_overflow() -> None:
    body = '{"error": {"message": "Input too long for this context_window"}}'
    err = classify_http_response(400, body, provider="p", model="m")
    assert isinstance(err, ModelContextOverflow)


def test_400_without_overflow_marker_maps_to_bad_request() -> None:
    body = '{"error": {"message": "Invalid temperature parameter"}}'
    err = classify_http_response(400, body, provider="p", model="m")
    assert isinstance(err, ModelBadRequest)
    assert "temperature" in err.detail["reason"]


def test_400_with_non_json_body_maps_to_bad_request() -> None:
    err = classify_http_response(400, "raw error text", provider="p", model="m")
    assert isinstance(err, ModelBadRequest)
    # Truncated body surfaces as the reason.
    assert "raw error text" in err.detail["reason"]


def test_400_empty_body_maps_to_bad_request_with_default_reason() -> None:
    err = classify_http_response(400, "", provider="p", model="m")
    assert isinstance(err, ModelBadRequest)
    assert err.detail["reason"] == "malformed request"


def test_non_error_status_returns_none() -> None:
    """200 OK / 3xx redirects are not the classifier's problem — the
    response-parser handles them. Returning None lets the call site
    fall through cleanly."""
    for status in (200, 201, 204, 301, 302):
        assert classify_http_response(status, "", provider="p", model="m") is None, (
            f"status {status}"
        )


def test_unhandled_4xx_returns_none() -> None:
    """4xx statuses outside {400, 401, 403, 429} aren't documented
    in §4.15; the classifier returns None so the call site can
    decide. Future codes added here trigger the AGENTS.md FB-R10
    audit."""
    for status in (402, 404, 409, 422):
        assert classify_http_response(status, "", provider="p", model="m") is None


def test_error_message_extraction_handles_top_level_error_string() -> None:
    """Some providers send `error: "..."` rather than nested."""
    err = classify_http_response(
        400, '{"error": "plain string message"}', provider="p", model="m"
    )
    assert isinstance(err, ModelBadRequest)
    assert err.detail["reason"] == "plain string message"


def test_error_message_extraction_handles_non_dict_payload() -> None:
    """JSON arrays / scalars at top level fall back to the default."""
    err = classify_http_response(400, "[1, 2, 3]", provider="p", model="m")
    assert isinstance(err, ModelBadRequest)
    assert err.detail["reason"] == "malformed request"


# ===========================================================================
# classify_transport_error — branch coverage
# ===========================================================================


def test_timeout_exception_maps_to_model_timeout() -> None:
    err = classify_transport_error(
        httpx.ReadTimeout("read timed out"),
        provider="openrouter",
        model="claude-opus",
        timeout_seconds=30.0,
    )
    assert isinstance(err, ModelTimeout)
    assert err.detail["timeout_seconds"] == 30.0


def test_connect_error_maps_to_model_timeout() -> None:
    """Connection refused / DNS failure: no first byte received, so
    user-visible behaviour matches a timeout."""
    err = classify_transport_error(
        httpx.ConnectError("connection refused"),
        provider="openrouter",
        model="claude-opus",
        timeout_seconds=30.0,
    )
    assert isinstance(err, ModelTimeout)


def test_generic_network_error_maps_to_model_timeout() -> None:
    err = classify_transport_error(
        httpx.NetworkError("network unreachable"),
        provider="p",
        model="m",
        timeout_seconds=30.0,
    )
    assert isinstance(err, ModelTimeout)


def test_unknown_exception_returns_none() -> None:
    """Anything not in the httpx family returns None so the engine
    boundary wraps as PlatformInternalError."""
    err = classify_transport_error(
        ValueError("not a transport thing"),
        provider="p",
        model="m",
        timeout_seconds=30.0,
    )
    assert err is None


# ===========================================================================
# Connector-level staticmethod re-exposure
# ===========================================================================


def test_opencode_exposes_classifiers_as_staticmethods() -> None:
    """Connector code (or its future call layer) can call
    `OpenCodeConnector.classify_http_response(...)` without importing
    from `_errors` directly. Smoke confirms the binding."""
    err = OpenCodeConnector.classify_http_response(
        429, "", provider="opencode", model="gpt-oss-120b"
    )
    assert isinstance(err, ModelRateLimited)


def test_openrouter_exposes_classifiers_as_staticmethods() -> None:
    err = OpenRouterConnector.classify_http_response(
        503, "", provider="openrouter", model="anthropic/x"
    )
    assert isinstance(err, ModelServerError)


def test_openrouter_transport_classifier_smoke() -> None:
    err = OpenRouterConnector.classify_transport_error(
        httpx.WriteTimeout("write timeout"),
        provider="openrouter",
        model="anthropic/x",
        timeout_seconds=15.0,
    )
    assert isinstance(err, ModelTimeout)
    assert err.detail["provider"] == "openrouter"
