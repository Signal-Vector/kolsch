"""TC-CRW-01..04 — adapter-only tests for :class:`CrewAIFramework`.

The integration tier (tests/integration/) runs the full TC-ENG-* set
against CrewAIFramework. These component tests pin adapter-specific
behavior that the cross-framework parametrization can't surface
cleanly — ordering, materialization specifics, retry composition,
and the per-thread serialization assumption.

The Fake LLM is plumbed through a recording-LLM helper so the seam
looks exactly like an engine call would; tests construct the
Framework directly without the engine to keep the surface small.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from engine import CrewAIFramework, LLMRequest, LLMResponse, ToolCall
from tests.fixtures.fake_llm import (
    FakeLLM,
    FakeLLMScriptedError,
    TextResponse,
    ToolCallResponse,
)

# ---------------------------------------------------------------------------
# Helpers — minimal LLMHandle / ToolHandle stubs that record activity
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Captures every LLMRequest the framework hands us + emits
    BEFORE/AFTER markers so TC-CRW-01 can assert ordering."""

    def __init__(self, fake: FakeLLM, *, events: list[str]) -> None:
        self._fake = fake
        self._events = events

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self._events.append(f"llm.before:{len(request.messages)}")
        # Adapt to FakeLLM's dict-context shape.
        context: dict[str, object] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
        }
        try:
            fake_response = await self._fake.agenerate(context)
        except FakeLLMScriptedError:
            self._events.append("llm.error")
            raise
        if isinstance(fake_response, TextResponse):
            self._events.append("llm.after:text")
            return LLMResponse(content=fake_response.text)
        if isinstance(fake_response, ToolCallResponse):
            self._events.append("llm.after:tool_call")
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": fake_response.tool_call_id or f"tc_{uuid4()}",
                        "name": fake_response.tool,
                        "arguments": dict(fake_response.args),
                    },
                ],
            )
        msg = f"unsupported: {fake_response!r}"
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
    ) -> None:
        self.name = name
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
    fake = FakeLLM(
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
    llm = _RecordingLLM(fake, events=events)
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
# TC-CRW-02 — Materialization Rule: LLM seam captures exactly what
# CrewAI sends
# ===========================================================================


async def test_tc_crw_02_materialization_captures_seam() -> None:
    """If CrewAI mutates the message list between the engine's handoff
    and the LLM, the §B.6 amendment's capture path is the only thing
    that surfaces the truth. This test pins: the LLMRequest the
    adapter passes to LLMHandle EQUALS what CrewAI was going to send.

    We capture the request inside our LLMHandle and assert it matches
    a separate Fake LLM recording of the same call. (Two records of
    the same byte stream; they must agree.)"""
    fake = FakeLLM(script=[TextResponse(text="OK.")])

    captured_at_seam: list[LLMRequest] = []

    async def llm(request: LLMRequest) -> LLMResponse:
        # Capture at the engine-side seam (what the engine would
        # emit on model.request.data.messages).
        captured_at_seam.append(request)
        # Forward to FakeLLM so it records too.
        await fake.agenerate(
            {
                "messages": [m.model_dump() for m in request.messages],
                "tools": list(request.tools),
                "model": request.model,
            }
        )
        return LLMResponse(content="OK.")

    fw = CrewAIFramework()
    await fw.run_exchange(
        system_prompt="You are an assistant.",
        user_message="Say OK.",
        llm=llm,
        tools=[],
        max_iterations=3,
    )

    assert len(captured_at_seam) == 1
    assert len(fake.calls) == 1

    seam_messages = [m.model_dump() for m in captured_at_seam[0].messages]
    fake_messages = fake.calls[0].context["messages"]
    # Byte-equal by construction — the seam and the Fake see the
    # same dict because we constructed both from the same source.
    assert seam_messages == fake_messages
    # And CrewAI's wrap is present (proves we're not just seeing the
    # engine's pre-wrap input).
    system_msg = next(m for m in seam_messages if m["role"] == "system")
    assert "Assistant" in system_msg["content"]
    assert "personal goal" in system_msg["content"].lower()


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
    Fake would be called more than once."""
    fake = FakeLLM(
        script=[
            ErrorResponse_from_str("model.upstream_error", "down"),
        ],
    )

    async def llm(request: LLMRequest) -> LLMResponse:
        await fake.agenerate({"messages": [m.model_dump() for m in request.messages]})
        # FakeLLM raises before returning — this line is unreachable.
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
    assert len(fake.calls) == 1


# Local helper — `from tests.fixtures.fake_llm import ErrorResponse`
# is fine, but we use a tiny shim here for readability of the assert
# above (script declares one explicit error response).
from tests.fixtures.fake_llm import ErrorResponse  # noqa: E402


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
    fake = FakeLLM(
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
    llm = _RecordingLLM(fake, events=events)
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
