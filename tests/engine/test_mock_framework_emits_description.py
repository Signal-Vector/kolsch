"""bug-024 Test 2 (AC #6) — `MinimalFramework.run_exchange` emits descriptions.

Pins the Layer 2 fix that adds `"description"` to the tool schema
dict `MinimalFramework` builds before calling the LLM. Before the
fix, the emitted list was `[{"name": ..., "schema": ...}]` and
`_tool_to_openai_spec` fell back to `"Tool: <name>"` for every
tool.
"""

from __future__ import annotations

from typing import Any

from engine._crewai_adapter import _tool_to_openai_spec
from engine.framework import LLMRequest, LLMResponse
from engine.mock_framework import MinimalFramework


class _MockToolHandle:
    """Minimal ToolHandle stand-in with the Protocol's three attrs."""

    def __init__(self, *, name: str, description: str, schema: dict[str, Any]) -> None:
        self.name = name
        self.description = description
        self.schema = schema

    async def __call__(self, call: Any) -> Any:  # pragma: no cover — unused
        return {}


class _CapturingLLM:
    """LLMHandle stub that records the request then returns text."""

    def __init__(self) -> None:
        self.captured: LLMRequest | None = None

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.captured = request
        return LLMResponse(content="done")


async def test_run_exchange_emits_description_key() -> None:
    """`run_exchange` includes each tool's description in `LLMRequest.tools`."""
    framework = MinimalFramework(model_name="mock-test")
    llm = _CapturingLLM()
    handle = _MockToolHandle(
        name="do_x",
        description="Do X. Prefer this over bash.",
        schema={"type": "object", "properties": {}},
    )

    result = await framework.run_exchange(
        system_prompt="sys",
        user_message="hi",
        llm=llm,
        tools=[handle],
    )
    assert result == "done"

    assert llm.captured is not None
    assert len(llm.captured.tools) == 1
    emitted = llm.captured.tools[0]
    # The engine-internal shape includes `description` alongside
    # `name`/`schema` so downstream `_tool_to_openai_spec` populates
    # `function.description` from a real string, not the placeholder.
    assert emitted["name"] == "do_x"
    assert emitted["description"] == "Do X. Prefer this over bash."
    assert "schema" in emitted

    # End-to-end: after the OpenAI-spec normalisation the LLM would
    # see the same description — NOT the `"Tool: <name>"` fallback.
    openai_spec = _tool_to_openai_spec(emitted)
    assert openai_spec["type"] == "function"
    assert openai_spec["function"]["description"] == "Do X. Prefer this over bash."
    assert openai_spec["function"]["description"] != "Tool: do_x"
