"""Per-connector error-classification tests — Phase 1.3 (Step 20 fix).

Exercises :func:`llm.providers._errors.classify_http_response` and
:func:`llm.providers._errors.classify_transport_error` exhaustively.
The mapping is the basis of the engine's fallback decision (Error
Model §4.15 + Phase 1.3 patch §2.2); every documented branch is
asserted here so a future code change cannot drift silently.

The Phase 1.3 AGENTS.md FB-R10 hard rule requires this: every
``model.*`` code declares its level AND every connector documents
the response-to-code mapping AND those mappings are testable.

bug-145 made the HTTP mapping total over status >= 400 (a deliberate
contract change: 402/404/409/422 used to return ``None``) and added
:func:`llm.providers._errors.classify_openai_compat_exception`, the
call-time mapper behind every OpenAI-compatible connector's
``classify_call_error`` hook. Its exceptions are real ``openai`` /
``httpx`` / CrewAI objects in the shapes CrewAI 1.14 re-raises.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, ClassVar

import httpx
import openai
import pytest
from crewai.utilities.exceptions.context_window_exceeding_exception import (
    CONTEXT_LIMIT_ERRORS,
    LLMContextLengthExceededError,
)

import llm.providers._errors as errors_module
from errors import (
    ModelAuthenticationFailed,
    ModelBadRequest,
    ModelContextOverflow,
    ModelNotFound,
    ModelRateLimited,
    ModelServerError,
    ModelTimeout,
    ModelUpstreamError,
    PlatformError,
)
from llm.protocol import ProviderConfig
from llm.providers._errors import (
    classify_http_response,
    classify_openai_compat_exception,
    classify_transport_error,
)
from llm.providers.claude_code import ClaudeCodeConnector
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


def test_every_error_status_maps_to_a_typed_error() -> None:
    """AC 3 (bug-145, deliberate contract change): every status >= 400
    maps to a typed ``model.*`` error. The pre-bug-145 contract
    returned ``None`` for 402/404/409/422 "so the call site can
    decide" — but no call site ever did, so those surfaced as
    ``platform.internal_error``."""
    for status in range(400, 600):
        err = classify_http_response(status, "", provider="p", model="m")
        assert isinstance(err, PlatformError), f"status {status}"
        assert err.code.startswith("model."), f"status {status}"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (402, ModelUpstreamError),
        (404, ModelNotFound),
        (408, ModelTimeout),
        (409, ModelUpstreamError),
        (413, ModelBadRequest),
        (418, ModelBadRequest),
        (422, ModelBadRequest),
        (451, ModelBadRequest),
        (600, ModelBadRequest),
    ],
)
def test_newly_mapped_statuses(status: int, expected: type[PlatformError]) -> None:
    body = '{"error": {"message": "upstream said no"}}'
    err = classify_http_response(
        status, body, provider="p", model="m", timeout_seconds=12.0
    )
    assert isinstance(err, expected)
    assert err.where == "llm.connector.call"
    if isinstance(err, ModelUpstreamError):
        assert err.detail["http_status"] == status
        assert err.detail["response"] == "upstream said no"
    elif isinstance(err, ModelTimeout):
        assert err.detail["timeout_seconds"] == 12.0
    else:
        assert err.detail["status_code"] == status
        assert err.detail["reason"] == "upstream said no"


def test_404_empty_body_uses_default_reason() -> None:
    err = classify_http_response(404, "", provider="opencode", model="kimi-k2.6")
    assert isinstance(err, ModelNotFound)
    assert err.detail == {
        "provider": "opencode",
        "model": "kimi-k2.6",
        "status_code": 404,
        "reason": "model not found",
    }


@pytest.mark.parametrize("status", [413, 422])
def test_413_and_422_with_overflow_marker_map_to_overflow(status: int) -> None:
    body = '{"error": {"message": "input is too long for this model"}}'
    err = classify_http_response(status, body, provider="p", model="m")
    assert isinstance(err, ModelContextOverflow)


def test_overflow_markers_are_a_superset_of_crewai() -> None:
    """AC 4: CrewAI's executor silently summarises on any error text
    matching ``CONTEXT_LIMIT_ERRORS``. Every phrase must classify as
    overflow here, so a ``ModelBadRequest`` reason can never carry one."""
    assert CONTEXT_LIMIT_ERRORS, "CrewAI moved its marker list"
    for phrase in CONTEXT_LIMIT_ERRORS:
        body = json.dumps({"error": {"message": f"Error: {phrase.upper()} (8192)"}})
        err = classify_http_response(400, body, provider="p", model="m")
        assert isinstance(err, ModelContextOverflow), phrase


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


# ===========================================================================
# classify_openai_compat_exception — the call-time mapper (bug-145)
# ===========================================================================

_REQUEST = httpx.Request("POST", "http://x/v1/chat/completions")
_STATUS_CLASSES: dict[int, type[openai.APIStatusError]] = {
    400: openai.BadRequestError,
    401: openai.AuthenticationError,
    403: openai.PermissionDeniedError,
    404: openai.NotFoundError,
    409: openai.ConflictError,
    422: openai.UnprocessableEntityError,
    429: openai.RateLimitError,
}


def _status_error(status: int, body: str = "") -> openai.APIStatusError:
    """Build the exception the openai SDK raises for `status` + `body`."""
    response = httpx.Response(status, request=_REQUEST, text=body)
    cls = _STATUS_CLASSES.get(
        status,
        openai.InternalServerError if status >= 500 else openai.APIStatusError,
    )
    try:
        parsed: object = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return cls(f"Error code: {status} - {body}", response=response, body=parsed)


def _classify(exc: BaseException) -> PlatformError | None:
    return classify_openai_compat_exception(
        exc, provider="opencode", model="glm-5", timeout_seconds=180.0
    )


def _raised_from(outer: BaseException, inner: BaseException) -> BaseException:
    """Return `outer` raised `from inner` — the real `__cause__` wiring."""
    try:
        try:
            raise inner
        except BaseException as caught:
            raise outer from caught
    except BaseException as result:  # noqa: BLE001 — returning the chain
        return result


_OVERFLOW_BODY = (
    '{"error": {"message": "This model\'s maximum context length is 8192"}}'
)
_MISSING_SESSION_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "MissingSessionID",
            "message": "Request is missing x-opencode-session header",
        },
    }
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            LLMContextLengthExceededError("maximum context length"),
            ModelContextOverflow,
            id="crewai-overflow",
        ),
        pytest.param(
            _status_error(400, _OVERFLOW_BODY), ModelContextOverflow, id="400-overflow"
        ),
        pytest.param(
            _status_error(413, _OVERFLOW_BODY), ModelContextOverflow, id="413-overflow"
        ),
        pytest.param(
            _status_error(422, _OVERFLOW_BODY), ModelContextOverflow, id="422-overflow"
        ),
        pytest.param(
            _status_error(400, _MISSING_SESSION_BODY), ModelBadRequest, id="400"
        ),
        pytest.param(_status_error(422, '{"error": "bad"}'), ModelBadRequest, id="422"),
        pytest.param(_status_error(401), ModelAuthenticationFailed, id="401"),
        pytest.param(_status_error(403), ModelAuthenticationFailed, id="403"),
        pytest.param(_status_error(404), ModelNotFound, id="404"),
        pytest.param(_status_error(402), ModelUpstreamError, id="402"),
        pytest.param(_status_error(409), ModelUpstreamError, id="409"),
        pytest.param(_status_error(408), ModelTimeout, id="408"),
        pytest.param(_status_error(429), ModelRateLimited, id="429"),
        pytest.param(_status_error(500), ModelServerError, id="500"),
        pytest.param(_status_error(503), ModelServerError, id="503"),
        pytest.param(_status_error(599), ModelServerError, id="599"),
        pytest.param(_status_error(418), ModelBadRequest, id="other-4xx"),
        pytest.param(httpx.ReadTimeout("slow"), ModelTimeout, id="httpx-timeout"),
        pytest.param(httpx.ConnectError("refused"), ModelTimeout, id="httpx-connect"),
        pytest.param(httpx.NetworkError("down"), ModelTimeout, id="httpx-network"),
        pytest.param(
            openai.APITimeoutError(request=_REQUEST), ModelTimeout, id="sdk-timeout"
        ),
        pytest.param(
            openai.APIConnectionError(request=_REQUEST),
            ModelTimeout,
            id="sdk-connection",
        ),
    ],
)
def test_call_exception_table(
    exc: BaseException, expected: type[PlatformError]
) -> None:
    """AC 2: one row per table line."""
    err = _classify(exc)
    assert isinstance(err, expected)
    assert err.detail["provider"] == "opencode"


def test_missing_session_400_carries_provider_message() -> None:
    """The ses_0cd8df9d incident body: reason is the provider's message."""
    err = _classify(_status_error(400, _MISSING_SESSION_BODY))
    assert isinstance(err, ModelBadRequest)
    assert err.detail["status_code"] == 400
    assert err.detail["model"] == "glm-5"
    assert "x-opencode-session" in err.detail["reason"]


def test_unrecognised_exception_returns_none() -> None:
    assert _classify(RuntimeError("boom")) is None


def test_crewai_value_error_from_not_found_maps_to_not_found() -> None:
    """CrewAI: `raise ValueError(f"Model {model} not found: {e}") from e`."""
    exc = _raised_from(ValueError("Model m not found"), _status_error(404))
    err = _classify(exc)
    assert isinstance(err, ModelNotFound)


def test_crewai_overflow_from_bad_request_maps_to_overflow() -> None:
    """CrewAI: `raise LLMContextLengthExceededError(str(e)) from e`."""
    inner = _status_error(400, _OVERFLOW_BODY)
    exc = _raised_from(LLMContextLengthExceededError(str(inner)), inner)
    assert isinstance(_classify(exc), ModelContextOverflow)


def test_crewai_connection_error_from_sdk_timeout_maps_to_timeout() -> None:
    """CrewAI: `raise ConnectionError(...) from e` for APIConnectionError."""
    exc = _raised_from(
        ConnectionError("Failed to connect to OpenAI API"),
        openai.APITimeoutError(request=_REQUEST),
    )
    err = _classify(exc)
    assert isinstance(err, ModelTimeout)
    assert err.detail["timeout_seconds"] == 180.0


def test_crewai_self_cycle_terminates() -> None:
    """CrewAI: `raise e from e` — `e.__cause__ is e`. Must not hang."""
    exc = _status_error(429)
    exc.__cause__ = exc
    assert isinstance(_classify(exc), ModelRateLimited)


def test_unrecognised_self_cycle_returns_none() -> None:
    exc = RuntimeError("loop")
    exc.__cause__ = exc
    assert _classify(exc) is None


def test_chain_deeper_than_cap_returns_none() -> None:
    """A recognised link 12 deep is past the 10-link cap."""
    exc: BaseException = _status_error(429)
    for depth in range(11):
        exc = _raised_from(RuntimeError(f"wrap {depth}"), exc)
    assert _classify(exc) is None


def test_recognised_link_within_cap_is_found() -> None:
    exc: BaseException = _status_error(429)
    for depth in range(9):
        exc = _raised_from(RuntimeError(f"wrap {depth}"), exc)
    assert isinstance(_classify(exc), ModelRateLimited)


def test_duck_typed_status_error_falls_back_to_body_attribute() -> None:
    """An unreadable `response.text` (e.g. an unread stream) falls back
    to the SDK's parsed `body`."""

    class _UnreadResponse:
        headers: ClassVar[dict[str, str]] = {}

        @property
        def text(self) -> str:
            msg = "response not read"
            raise RuntimeError(msg)

    class _StatusError(Exception):
        status_code = 400
        response = _UnreadResponse()
        body: ClassVar[dict[str, Any]] = {
            "error": {"message": "temperature out of range"}
        }

    err = _classify(_StatusError("400"))
    assert isinstance(err, ModelBadRequest)
    assert err.detail["reason"] == "temperature out of range"


def test_bool_status_code_is_not_a_status() -> None:
    class _Weird(Exception):
        status_code = True
        response = object()

    assert _classify(_Weird()) is None


def test_errors_module_imports_no_provider_sdk() -> None:
    """The engine safety net reuses this module, so it must never import
    a provider SDK (or CrewAI) — exceptions are recognised by duck
    typing and qualified name."""
    source = Path(errors_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"openai", "anthropic", "crewai", "litellm"}


# ===========================================================================
# ProviderConnector.classify_call_error (bug-145 protocol)
# ===========================================================================


def _provider_config(
    name: str, type_name: str, timeout_seconds: int | None = None
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=type_name,
        api_key_secret=None,
        base_url=None,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize(
    ("connector", "type_name", "default_timeout"),
    [
        (OpenCodeConnector(), "opencode", 180.0),
        (OpenRouterConnector(), "openrouter", 30.0),
    ],
)
def test_openai_compat_connectors_stamp_name_and_default_timeout(
    connector: OpenCodeConnector | OpenRouterConnector,
    type_name: str,
    default_timeout: float,
) -> None:
    cfg = _provider_config(f"{type_name}_go", type_name)
    err = connector.classify_call_error(
        openai.APITimeoutError(request=_REQUEST), provider_config=cfg, model_id="m1"
    )
    assert isinstance(err, ModelTimeout)
    assert err.detail == {
        "provider": f"{type_name}_go",
        "model": "m1",
        "timeout_seconds": default_timeout,
    }


@pytest.mark.parametrize("connector", [OpenCodeConnector(), OpenRouterConnector()])
def test_openai_compat_connectors_honour_configured_timeout(
    connector: OpenCodeConnector | OpenRouterConnector,
) -> None:
    cfg = _provider_config("p", connector.type_name, timeout_seconds=45)
    err = connector.classify_call_error(
        httpx.ReadTimeout("slow"), provider_config=cfg, model_id="m"
    )
    assert isinstance(err, ModelTimeout)
    assert err.detail["timeout_seconds"] == 45.0


@pytest.mark.parametrize("connector", [OpenCodeConnector(), OpenRouterConnector()])
def test_openai_compat_connectors_delegate_status_mapping(
    connector: OpenCodeConnector | OpenRouterConnector,
) -> None:
    cfg = _provider_config("p", connector.type_name)
    err = connector.classify_call_error(
        _status_error(400, _MISSING_SESSION_BODY), provider_config=cfg, model_id="m"
    )
    assert isinstance(err, ModelBadRequest)
    assert (
        connector.classify_call_error(
            RuntimeError("x"), provider_config=cfg, model_id="m"
        )
        is None
    )


def test_claude_code_connector_classifies_nothing() -> None:
    """`_ClaudeCodeLLM` already raises typed `model.*` errors."""
    cfg = _provider_config("claude_code", "claude_code")
    assert (
        ClaudeCodeConnector().classify_call_error(
            _status_error(500), provider_config=cfg, model_id="sonnet"
        )
        is None
    )
