"""bug-144 Test 3 — session headers + kolsch User-Agent on the wire.

Builds the REAL client each connector returns, swaps in an
`httpx.MockTransport` that records the outgoing request and answers
with a canned chat completion, and makes one call. No network, no
tokens. The client is rebuilt from the connector-configured
`default_headers` (CrewAI's own `_get_client_params` path), so what is
asserted is exactly what would leave the process.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from llm import LLMCallContext, ProviderConfig, provider_config_from_yaml
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector
from llm.user_agent import kolsch_user_agent

_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _config(name: str, **raw: Any) -> ProviderConfig:
    return provider_config_from_yaml(
        name,
        {"base_url": "https://gateway.invalid/v1", **raw},
    )


def _call_and_capture(llm: Any) -> httpx.Request:
    """Route `llm`'s sync OpenAI client through a recording transport."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_COMPLETION)

    llm.client_params = {
        "http_client": httpx.Client(transport=httpx.MockTransport(handler))
    }
    llm._client = None  # rebuilt lazily from _get_client_params()
    reply = llm.call([{"role": "user", "content": "hello"}])
    assert reply == "hi"
    assert len(captured) == 1
    return captured[0]


def test_kolsch_user_agent_is_installed_version() -> None:
    from importlib.metadata import version

    assert kolsch_user_agent() == f"kolsch/{version('kolsch')}"


async def test_opencode_sends_session_header_and_kolsch_user_agent() -> None:
    llm = await OpenCodeConnector().build_llm(
        "some-model",
        _config("opencode"),
        "sk-test",
        context=LLMCallContext.for_session("ses_test"),
    )

    request = _call_and_capture(llm)

    assert request.url.path.endswith("/chat/completions")
    assert request.headers["x-opencode-session"] == "ses_test"
    assert request.headers["user-agent"] == kolsch_user_agent()
    assert json.loads(request.content)["model"] == "some-model"


async def test_openrouter_sends_kolsch_user_agent_and_no_session_header() -> None:
    llm = await OpenRouterConnector().build_llm(
        "anthropic/claude-haiku-4-5",
        _config("openrouter"),
        "sk-test",
        context=LLMCallContext.for_session("ses_test"),
    )

    request = _call_and_capture(llm)

    assert request.headers["user-agent"] == kolsch_user_agent()
    assert "x-opencode-session" not in request.headers


async def test_request_policy_override_replaces_session_header() -> None:
    llm = await OpenCodeConnector().build_llm(
        "some-model",
        _config("opencode", request_policy={"session_headers": ["x-session-affinity"]}),
        "sk-test",
        context=LLMCallContext.for_session("ses_test"),
    )

    request = _call_and_capture(llm)

    assert request.headers["x-session-affinity"] == "ses_test"
    assert "x-opencode-session" not in request.headers
    assert request.headers["user-agent"] == kolsch_user_agent()


async def test_request_policy_empty_list_disables_session_headers() -> None:
    llm = await OpenCodeConnector().build_llm(
        "some-model",
        _config("opencode", request_policy={"session_headers": []}),
        "sk-test",
        context=None,  # no key needed once the policy sends no session header
    )

    request = _call_and_capture(llm)

    assert "x-opencode-session" not in request.headers
    assert request.headers["user-agent"] == kolsch_user_agent()


async def test_request_policy_adds_session_header_to_openrouter() -> None:
    """Any OpenAI-compatible provider can opt in from config alone."""
    llm = await OpenRouterConnector().build_llm(
        "anthropic/claude-haiku-4-5",
        _config("openrouter", request_policy={"session_headers": ["X-Session-Id"]}),
        "sk-test",
        context=LLMCallContext.for_session("ses_or"),
    )

    request = _call_and_capture(llm)

    assert request.headers["x-session-id"] == "ses_or"


@pytest.mark.parametrize(
    "parameters",
    [
        {"default_headers": {"x-opencode-session": "hijack", "User-Agent": "evil/1"}},
        {"extra_headers": {"x-opencode-session": "hijack"}},
        {"headers": {"User-Agent": "evil/1"}},
        {"client_params": {"default_headers": {"User-Agent": "evil/1"}}},
    ],
)
async def test_agent_parameters_cannot_override_policy_headers(
    parameters: dict[str, Any],
) -> None:
    llm = await OpenCodeConnector().build_llm(
        "some-model",
        _config("opencode"),
        "sk-test",
        parameters={"temperature": 0.2, **parameters},
        context=LLMCallContext.for_session("ses_test"),
    )

    request = _call_and_capture(llm)

    assert request.headers["x-opencode-session"] == "ses_test"
    assert request.headers["user-agent"] == kolsch_user_agent()
    body = json.loads(request.content)
    assert body["temperature"] == 0.2
