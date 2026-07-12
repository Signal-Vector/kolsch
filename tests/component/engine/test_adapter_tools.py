"""Bug-engine-tools-wiring-gap.md Step F — model.request carries tools.

Step F's Task 4 + 5: ``model.request.data.tools`` must reflect
the tool schemas the framework forwarded to the LLM. Before
the fix, the JSONL was silent on whether the model saw any
tools — Engine I3 (Materialization Rule) wants every input
that contributed to the model's context recorded.

The change is in two emit paths:

- :class:`engine._fallback.FallbackLLMHandle._emit_request` —
  the production path (Phase 1.3 wraps every exchange in
  the fallback handle).
- :class:`engine.observers._ObservedLLMHandle.__call__` —
  the pre-Phase-1.3 observer, still used by tests that
  wire ``_ObservedLLMHandle`` directly.

This test pins the FallbackLLMHandle path (production-facing)
via the engine's full ``execute`` flow with a declared tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from schemas import EventType
from tests.component.test_engine import (
    _build_engine,
    _config_with_tool,
    _new_session_context,
)
from tests.fixtures.mock_llm import MockLLM, TextResponse
from tools import ToolContext, tool

if TYPE_CHECKING:
    from pathlib import Path


class _BeepArgs(BaseModel):
    count: int


@tool(
    name="beep",
    description="Emit N beeps for the test's tool-schema assertion",
    args_schema=_BeepArgs,
)
async def _beep_tool(args: _BeepArgs, ctx: ToolContext) -> dict[str, Any]:
    _ = ctx
    return {"beeps": "*" * args.count}


async def test_model_request_carries_tool_schemas_when_agent_declares_tools(
    tmp_path: Path,
) -> None:
    """Step F Task 5 — pin the request-payload assertion.

    Setup: engine wired with a declared `beep` tool (registered
    via the test fixture's `extra_tools` path, which is the
    programmatic equivalent of the helper agent's
    `declared` ref list). Mock LLM emits one text response —
    no tool_use, so this test pins the request-side schema
    transmission, not the response-side dispatch.

    Expected: the captured model.request event includes a
    `data.tools` array with the beep schema. Both `name`
    and `schema.type=object` are present (CrewAI / litellm
    needs these for the model's tool_use protocol).
    """
    mock = MockLLM(script=[TextResponse(text="acknowledged.")])
    engine, store, _am = await _build_engine(
        tmp_path,
        mock,
        extra_tools=[_beep_tool],
    )
    ctx = await _new_session_context(store)
    await engine.execute(_config_with_tool(), ctx)

    # The mock LLM also captures `request.tools` in its context —
    # cross-check that what the LLM saw matches what landed on
    # the event.
    mock_saw = mock.calls[0].context.get("tools")
    assert isinstance(mock_saw, list)
    assert len(mock_saw) == 1
    mock_first = mock_saw[0]
    assert isinstance(mock_first, dict)
    assert mock_first.get("name") == "beep"

    # The model.request event on the JSONL. The observer normalises
    # tools to the OpenAI spec envelope before emitting so the recorded
    # event matches what actually crosses an LLM wire (see
    # process/bug-observer-records-pre-normalisation-tool-shape.md).
    # The direct-to-LLM handoff still uses the engine's internal shape
    # (`mock_saw` above); the divergence is intentional and documented
    # in the observer + fallback docstrings.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    requests = [e for e in events if e.type == EventType.MODEL_REQUEST]
    assert len(requests) == 1
    captured_tools = requests[0].data.get("tools")
    assert isinstance(captured_tools, list), (
        f"model.request.data.tools must be a list; got {captured_tools!r}. "
        f"Bug-engine-tools-wiring-gap.md Step F regressed."
    )
    assert len(captured_tools) == 1
    captured_first = captured_tools[0]
    assert captured_first.get("type") == "function"
    function = captured_first.get("function")
    assert isinstance(function, dict)
    assert function.get("name") == "beep"
    parameters = function.get("parameters")
    assert isinstance(parameters, dict)
    assert parameters.get("type") == "object"
    # Pydantic-derived: properties + required reflect _BeepArgs.
    properties = parameters.get("properties", {})
    assert "count" in properties


async def test_model_request_carries_empty_tools_when_agent_has_no_tools(
    tmp_path: Path,
) -> None:
    """Companion test: no declared tools → ``tools: []``.

    Pins that the field is always present (default `[]`)
    rather than absent — replay code can rely on the key.
    """
    mock = MockLLM(script=[TextResponse(text="ack.")])
    engine, store, _am = await _build_engine(tmp_path, mock)
    ctx = await _new_session_context(store)
    # Use the no-tool config (the bare _config(), not
    # _config_with_tool which carries the standard variant).
    from tests.component.test_engine import _config

    await engine.execute(_config(), ctx)

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    requests = [e for e in events if e.type == EventType.MODEL_REQUEST]
    assert len(requests) == 1
    assert requests[0].data.get("tools") == []
