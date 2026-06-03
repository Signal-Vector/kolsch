"""TC-FRM-NOCAP-01..02 — FakeFramework runs without an iteration cap.

Regression pins for bug-framework-iteration-cap-removal.md. Pre-fix,
``FakeFramework.run_exchange`` hard-capped the loop at 10 iterations
and raised :class:`FakeFrameworkLoopExceededError` on exhaustion —
which the engine boundary then mis-classified as
``platform.internal_error``. After Step 2, the cap is gone and the
loop terminates only when the LLM stops emitting tool calls OR
``cancellation`` fires.

These tests pin both halves of the new contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine import FakeFramework, LLMRequest, LLMResponse, ToolCall


# ---------------------------------------------------------------------------
# Helpers — minimal LLMHandle / ToolHandle for FakeFramework
# ---------------------------------------------------------------------------


class _ScriptedToolCallLLM:
    """Issues ``n`` tool calls, then one content-only response.

    Each tool call uses a unique id so FakeFramework's tool dispatch
    appends distinguishable tool-result messages.
    """

    def __init__(self, *, tool_call_count: int, final_text: str) -> None:
        self._remaining = tool_call_count
        self._final_text = final_text
        self.calls = 0

    async def __call__(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        self.calls += 1
        if self._remaining > 0:
            i = self.calls
            self._remaining -= 1
            return LLMResponse(
                content=None,
                tool_calls=[{"id": f"call_{i}", "name": "noop", "arguments": {"i": i}}],
            )
        return LLMResponse(content=self._final_text)


class _UnboundedToolCallLLM:
    """Issues tool calls forever. Use with cancellation to terminate."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        self.calls += 1
        return LLMResponse(
            content=None,
            tool_calls=[
                {
                    "id": f"call_{self.calls}",
                    "name": "noop",
                    "arguments": {"i": self.calls},
                },
            ],
        )


class _NoopTool:
    """Echo-style tool — returns its arguments so the result message is
    deterministic for golden-style comparisons."""

    name = "noop"
    schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {"i": {"type": "integer"}},
    }

    async def __call__(self, call: ToolCall) -> Any:
        return {"echoed": call.arguments.get("i")}


# ===========================================================================
# TC-FRM-NOCAP-01 — FakeFramework loops past the old 10-iteration cap
# ===========================================================================


async def test_tc_frm_nocap_01_fake_framework_runs_past_ten_iterations() -> None:
    """Drive a FakeLLM that scripts 15 tool calls then a content-only
    response. Pre-fix, FakeFramework raised
    ``FakeFrameworkLoopExceededError`` at iteration 10 (which the engine
    boundary wrapped as ``platform.internal_error``). Post-fix, the
    loop completes and returns the final text.

    This is the load-bearing regression — if it raises, the cap was
    re-introduced somewhere on the framework path.
    """
    llm = _ScriptedToolCallLLM(tool_call_count=15, final_text="Done.")
    tool = _NoopTool()

    fw = FakeFramework()
    final_text = await fw.run_exchange(
        system_prompt="sys",
        user_message="loop please",
        llm=llm,
        tools=[tool],
    )

    assert final_text == "Done."
    # 15 tool-call turns + 1 content turn = 16 LLM calls observed.
    assert llm.calls == 16


# ===========================================================================
# TC-FRM-NOCAP-02 — Cancellation interrupts an in-progress loop
# ===========================================================================


async def test_tc_frm_nocap_02_fake_framework_observes_cancellation_mid_loop() -> None:
    """Cancellation polling at the top of each iteration is the
    user's only mechanism for stopping a runaway loop (no
    iteration cap, no timeout in v1).

    A counter-based cancellation that flips True on the 3rd poll
    must cause the framework to raise ``asyncio.CancelledError``.
    The unbounded LLM never returns a content-only response — if
    cancellation weren't observed, this test would hang.
    """
    llm = _UnboundedToolCallLLM()
    tool = _NoopTool()

    poll_count = 0

    def cancellation() -> bool:
        nonlocal poll_count
        poll_count += 1
        # Flip on the 3rd poll — the previous two iterations have
        # already dispatched the tool call.
        return poll_count >= 3

    fw = FakeFramework()
    with pytest.raises(asyncio.CancelledError):
        await fw.run_exchange(
            system_prompt="sys",
            user_message="loop forever",
            llm=llm,
            tools=[tool],
            cancellation=cancellation,
        )

    # The LLM saw exactly 2 calls before cancellation fired on the 3rd
    # poll (which happens BEFORE the 3rd LLM call). This pins the
    # latency model: cancellation observed at iteration boundary, no
    # mid-call observation.
    assert llm.calls == 2
