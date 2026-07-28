"""TC-CRW-01..04 — adapter-only tests for :class:`CrewAIFramework`.

The integration tier (tests/integration/) runs the full TC-ENG-* set
against CrewAIFramework. These component tests pin adapter-specific
behavior that the cross-framework parametrization can't surface
cleanly — ordering, materialization specifics, retry composition,
and the per-thread serialization assumption.

The Mock LLM is plumbed through a recording-LLM helper so the seam
looks exactly like an engine call would; tests construct the
Framework directly without the engine to keep the surface small.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from engine import CrewAIFramework, LLMRequest, LLMResponse, ToolCall
from tests.fixtures.mock_llm import (
    MockLLM,
    MockLLMScriptedError,
    TextResponse,
    ToolCallResponse,
)

# ---------------------------------------------------------------------------
# Helpers — minimal LLMHandle / ToolHandle stubs that record activity
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Captures every LLMRequest the framework hands us + emits
    BEFORE/AFTER markers so TC-CRW-01 can assert ordering."""

    def __init__(self, mock: MockLLM, *, events: list[str]) -> None:
        self._mock = mock
        self._events = events

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self._events.append(f"llm.before:{len(request.messages)}")
        # Adapt to MockLLM's dict-context shape.
        context: dict[str, Any] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
        }
        try:
            mock_response = await self._mock.agenerate(context)
        except MockLLMScriptedError:
            self._events.append("llm.error")
            raise
        if isinstance(mock_response, TextResponse):
            self._events.append("llm.after:text")
            return LLMResponse(content=mock_response.text)
        if isinstance(mock_response, ToolCallResponse):
            self._events.append("llm.after:tool_call")
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": mock_response.tool_call_id or f"tc_{uuid4()}",
                        "name": mock_response.tool,
                        "arguments": dict(mock_response.args),
                    },
                ],
            )
        msg = f"unsupported: {mock_response!r}"
        raise RuntimeError(msg)


class _RecordingTool:
    """ToolHandle that records BEFORE/AFTER markers + returns a fixed
    payload. Schema mirrors what the engine would emit."""

    def __init__(
        self,
        *,
        name: str,
        result: Any,
        events: list[str],
        schema: dict[str, Any] | None = None,
        description: str = "Records lifecycle markers and returns a fixed payload.",
    ) -> None:
        self.name = name
        self.description = description
        self.schema = schema or {"properties": {"message": {"type": "string"}}}
        self._result = result
        self._events = events
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall) -> Any:
        self._events.append(f"tool.before:{self.name}")
        self.calls.append(call)
        # Yield to other tasks so we can detect concurrent dispatch
        # in TC-CRW-04.
        await asyncio.sleep(0)
        self._events.append(f"tool.after:{self.name}")
        return self._result


# ===========================================================================
# TC-CRW-01 — Lifecycle ordering matches Protocol expectations
# ===========================================================================


async def test_tc_crw_01_lifecycle_ordering() -> None:
    """The Protocol requires BEFORE-LLM-call observability + AFTER. The
    adapter's BaseLLM subclass invokes LLMHandle exactly once per
    iteration in that order; this test pins it.

    Concretely: a tool-call → text-response sequence should produce:
      llm.before, llm.after, tool.before, tool.after,
      llm.before, llm.after — i.e. BEFORE precedes AFTER for each
    pair, and tool dispatch sits between the two LLM calls."""
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="ping",
                args={"message": "ack"},
                tool_call_id="call_1",
            ),
            TextResponse(text="Done."),
        ],
    )
    events: list[str] = []
    llm = _RecordingLLM(mock, events=events)
    tool = _RecordingTool(
        name="ping",
        result={"echoed": "ack"},
        events=events,
    )

    fw = CrewAIFramework()
    final_text = await fw.run_exchange(
        system_prompt="You are an assistant.",
        user_message="Please ping with 'ack'.",
        llm=llm,
        tools=[tool],
        max_iterations=5,
    )

    assert "Done" in final_text

    # The Protocol ordering: every llm.before precedes its llm.after;
    # tool.before precedes tool.after; LLM-1 → tool → LLM-2.
    assert events.count("llm.before:2") >= 1
    assert events.count("llm.after:tool_call") == 1
    assert events.count("tool.before:ping") == 1
    assert events.count("tool.after:ping") == 1
    assert events.count("llm.after:text") == 1

    # Sequence invariants — first LLM call → tool dispatch → second
    # LLM call.
    first_llm_after = events.index("llm.after:tool_call")
    tool_before = events.index("tool.before:ping")
    tool_after = events.index("tool.after:ping")
    final_llm_after = events.index("llm.after:text")
    assert first_llm_after < tool_before < tool_after < final_llm_after


# ===========================================================================
# TC-CRW-02 — LLM-seam captures the post-CrewAI-wrap view (presence test)
# ===========================================================================


async def test_tc_crw_02_seam_captures_post_wrap_view() -> None:
    """The §B.6 amendment requires the engine to capture
    ``model.request.data.messages`` as observed at the LLM seam (post-
    framework-wrap). Under CrewAI, that means the LLMHandle receives
    messages that ALREADY contain CrewAI's role-playing wrap, the
    Task description trailer, and the expected-output preamble — not
    the bare ``system_prompt`` + ``user_message`` the engine handed in.

    Restructured from the original v1 form (which used a trivially
    constructed-equality assertion the reviewer flagged as
    self-confirming): the real value of this test is the WRAP-PRESENCE
    check — the seam-captured messages contain CrewAI-injected text
    that the engine never put there. That's how we know the seam is
    post-wrap, not pre-wrap.
    """
    captured_at_seam: list[LLMRequest] = []

    async def llm(request: LLMRequest) -> LLMResponse:
        captured_at_seam.append(request)
        return LLMResponse(content="OK.")

    fw = CrewAIFramework()
    await fw.run_exchange(
        system_prompt="You are a tester.",
        user_message="Say OK.",
        llm=llm,
        tools=[],
        max_iterations=3,
    )

    assert len(captured_at_seam) == 1
    captured = captured_at_seam[0]

    # The engine handed CrewAI: system="You are a tester.",
    # user="Say OK." — nothing else. Anything else on the seam
    # therefore came from CrewAI's wrap.

    system_msgs = [m for m in captured.messages if m.role == "system"]
    user_msgs = [m for m in captured.messages if m.role == "user"]
    assert system_msgs and user_msgs

    system_text = system_msgs[0].content
    user_text = user_msgs[0].content

    # System wrap markers — these phrases are CrewAI's, not ours.
    # Their presence proves the seam captures POST-wrap.
    assert "Assistant" in system_text  # CrewAI prepends agent.role.
    assert "personal goal" in system_text.lower()  # CrewAI's "Your personal goal is:".

    # User-side wrap markers — CrewAI wraps the Task description with
    # "Current Task: ..." preamble + "expected criteria" trailer.
    assert "Current Task:" in user_text
    assert "expected" in user_text.lower()

    # And the engine's original inputs survive inside the wrap (the
    # post-wrap view contains the pre-wrap content; replay reads from
    # the captured event regardless of the wrap).
    assert "You are a tester." in system_text
    assert "Say OK." in user_text


# ===========================================================================
# TC-CRW-03 — Retry composition: engine owns retry; CrewAI's internal
# retry is disabled
# ===========================================================================


async def test_tc_crw_03_retry_composition() -> None:
    """The engine's RuntimeSpec.max_retries is the only retry budget
    in effect. CrewAIFramework sets Agent(max_retry_limit=0) so a
    raised exception from the LLM bubbles out of run_exchange on the
    first call — the engine sees it ONCE and decides whether to
    retry.

    Test: 1 scripted error → CrewAI does NOT retry → run_exchange
    raises immediately. If CrewAI's internal retry were active, the
    Mock would be called more than once."""
    mock = MockLLM(
        script=[
            ErrorResponse_from_str("model.upstream_error", "down"),
        ],
    )

    async def llm(request: LLMRequest) -> LLMResponse:
        await mock.agenerate({"messages": [m.model_dump() for m in request.messages]})
        # MockLLM raises before returning — this line is unreachable.
        msg = "unreachable"
        raise RuntimeError(msg)

    fw = CrewAIFramework()
    with pytest.raises(Exception, match=r".*"):
        await fw.run_exchange(
            system_prompt="x",
            user_message="x",
            llm=llm,
            tools=[],
            max_iterations=3,
        )

    # CrewAI saw the error EXACTLY ONCE — no internal retry.
    assert len(mock.calls) == 1


# Local helper — `from tests.fixtures.mock_llm import ErrorResponse`
# is fine, but we use a tiny shim here for readability of the assert
# above (script declares one explicit error response).
from tests.fixtures.mock_llm import ErrorResponse  # noqa: E402


def ErrorResponse_from_str(error_type: str, message: str) -> ErrorResponse:  # noqa: N802
    return ErrorResponse(error_type=error_type, message=message)


# ===========================================================================
# TC-CRW-04 — Concurrent tool callbacks don't race
# ===========================================================================


async def test_tc_crw_04_concurrent_tools_serialize_via_loop_bridge() -> None:
    """CrewAI's executor may issue tool callbacks from a worker
    thread (kickoff_async uses asyncio.to_thread). Our adapter
    schedules each tool back on the main loop via
    asyncio.run_coroutine_threadsafe — the engine's per-thread emit
    lock then serializes them.

    This test pins that two tool calls in sequence (the realistic v1
    case — CrewAI's `_handle_native_tool_calls` processes only the
    first call per LLM turn, so we don't see TRUE parallelism but DO
    see serial cross-thread bridging) both complete and produce
    distinct results without state corruption."""
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="ping",
                args={"message": "a"},
                tool_call_id="call_a",
            ),
            ToolCallResponse(
                tool="ping",
                args={"message": "b"},
                tool_call_id="call_b",
            ),
            TextResponse(text="Both done."),
        ],
    )

    events: list[str] = []
    llm = _RecordingLLM(mock, events=events)
    tool = _RecordingTool(
        name="ping",
        result={"ok": True},
        events=events,
    )

    fw = CrewAIFramework()
    final = await fw.run_exchange(
        system_prompt="x",
        user_message="ping twice",
        llm=llm,
        tools=[tool],
        max_iterations=5,
    )

    assert "Both done" in final
    # Both tool calls reached our handle with their distinct args.
    assert len(tool.calls) == 2
    assert {c.arguments["message"] for c in tool.calls} == {"a", "b"}
    # Tool.before / Tool.after appear in pairs — no interleaving
    # because the loop-bridge serializes (each future.result()
    # blocks the worker thread until the main-loop coroutine completes).
    tool_markers = [e for e in events if e.startswith("tool.")]
    pair_count = len(tool_markers) // 2
    for i in range(pair_count):
        assert tool_markers[i * 2] == "tool.before:ping"
        assert tool_markers[i * 2 + 1] == "tool.after:ping"


# ===========================================================================
# TC-CRW-05 — _build_passthrough_schema preserves types for the LLM
# ===========================================================================


def test_tc_crw_05_passthrough_schema_carries_types_and_descriptions() -> None:
    """The passthrough Pydantic model must reflect the source schema's types
    and descriptions so CrewAI's `convert_tools_to_openai_schema` produces
    a fully-typed OpenAI tool spec.

    Losing the type here caused Kimi K2 to emit tool calls with
    `arguments: {}` because the model saw properties like
    `{"url": {"default": null, "title": "Url"}}` and had no signal about
    what value to send. Smarter models (Claude/GPT-4) infer from the
    prompt anyway; Kimi doesn't."""
    from engine.crewai_framework import _build_passthrough_schema

    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "timeout": {"type": "integer", "default": 30, "description": "seconds"},
            "enabled": {"type": "boolean", "description": "toggle"},
        },
        "required": ["url"],
    }
    model = _build_passthrough_schema("fetcher", schema)
    js = model.model_json_schema()

    # `url` (required + string) surfaces as string with description; no default.
    assert js["properties"]["url"]["type"] == "string"
    assert js["properties"]["url"]["description"] == "URL to fetch"
    assert "url" in js["required"]

    # `timeout` (optional + integer): reachable as either integer or null.
    timeout_prop = js["properties"]["timeout"]
    if "anyOf" in timeout_prop:
        assert {e.get("type") for e in timeout_prop["anyOf"]} == {"integer", "null"}
    else:
        assert timeout_prop["type"] == "integer"
    assert timeout_prop["description"] == "seconds"
    assert timeout_prop["default"] == 30

    # `enabled` (optional + boolean): boolean or null.
    enabled_prop = js["properties"]["enabled"]
    if "anyOf" in enabled_prop:
        assert {e.get("type") for e in enabled_prop["anyOf"]} == {"boolean", "null"}
    else:
        assert enabled_prop["type"] == "boolean"


def test_tc_crw_05_passthrough_schema_empty_properties_stays_passthrough() -> None:
    """A tool that declares no properties still gets a working model.

    Regression: the fix must not break tools whose schema omits
    `properties` entirely (some builtins expose a no-argument surface).
    Pydantic's `create_model` silently drops underscore-prefixed fields,
    so the resulting model has empty properties — which is the correct
    "any kwargs accepted" shape for a no-argument surface.
    """
    from engine.crewai_framework import _build_passthrough_schema

    model = _build_passthrough_schema("no_args", {})
    # Should not crash. Empty properties is the correct answer.
    js = model.model_json_schema()
    assert js["type"] == "object"
    assert js.get("properties", {}) == {}


def test_tc_crw_05_passthrough_schema_handles_nullable_union_types() -> None:
    """A property typed as `["string", "null"]` (Pydantic's nullable form)
    still round-trips to a usable Python type."""
    from engine.crewai_framework import _build_passthrough_schema

    schema = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "null"], "description": "opt id"},
        },
        "required": [],
    }
    model = _build_passthrough_schema("tool", schema)
    js = model.model_json_schema()
    id_prop = js["properties"]["id"]
    # Either resolved to `string | null` or plain string — both are
    # acceptable because the field is non-required (so null was already
    # implied by our default). Assert the description survived.
    assert id_prop["description"] == "opt id"
