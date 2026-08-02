"""Test 8 (spec-118): real-`claude` round-trip through the `claude_code` seam.

Module-level `pytestmark = pytest.mark.e2e` so the CI gate's
`-m "not e2e"` deselects it (mirrors spec-105/111). Requires a
logged-in `claude` under subscription auth (no `ANTHROPIC_API_KEY`).
Skips (not fails) when `claude` isn't on PATH.

The test drives the SAME seam a live agent uses: `build_llm` →
`make_llm_callable` → an `LLMRequest`, and asserts the returned
`LLMResponse.content` reflects a live Claude reply. `LLMResponse.content`
is exactly what the engine projects into `agent.message`, so a passing
assertion here is the "the agent's agent.message reflects the reply"
guarantee at the provider boundary — without booting the whole engine.
"""

from __future__ import annotations

import shutil

import pytest

from engine._crewai_adapter import make_llm_callable
from engine.framework import LLMRequest, Message
from llm.protocol import ProviderConfig
from llm.providers.claude_code import ClaudeCodeConnector

pytestmark = pytest.mark.e2e


def _claude_available() -> bool:
    return shutil.which("claude") is not None


async def test_real_claude_roundtrip_returns_reply() -> None:
    if not _claude_available():
        pytest.skip("`claude` binary not on PATH — install Claude Code to run.")

    connector = ClaudeCodeConnector()
    provider_config = ProviderConfig(
        name="claude_code",
        type="claude_code",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )
    crewai_llm = await connector.build_llm("sonnet", provider_config, None)
    callable_ = make_llm_callable(crewai_llm)

    request = LLMRequest(
        messages=[
            Message(
                role="system",
                content="You are a terse assistant. Reply with a single word.",
            ),
            Message(role="user", content="Reply with exactly one word: pong"),
        ],
        model="sonnet",
    )
    response = await callable_(request)

    assert response.content is not None
    assert "pong" in response.content.lower(), (
        f"live claude reply did not contain 'pong': {response.content!r}"
    )
    # v1: no kolsch tool-calling.
    assert response.tool_calls is None
