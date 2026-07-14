"""spec-068 Part A — write-time span invariant, cancel arm.

Covers acceptance criterion #1 from
``process/spec-068-guaranteed-span-closure-and-resume-reconciliation.md``:

    Cancelling an exchange while a tool is in-flight results in a
    durable ``tool.failed`` (``error.type="session.interrupted"``)
    for that ``tool_call_id``, emitted by the engine — no dangling
    ``tool.called`` remains on the path.

Also verifies criterion #2 negative-case: the happy path emits
exactly one terminal per span (``tool.result`` and no synthetic
``tool.failed`` piling on top).

The block tool holds an ``asyncio.Event`` the test flips only after
observing ``TOOL_CALLED`` on the log. Cancellation is fired via the
exchange's ``CancelToken`` at that instant so the wedge lives
exactly between the ``tool.called`` emit and the tool return —
i.e. the invariant-violation window the spec exists to close.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from engine import CancelToken, ExecutionContext
from schemas import EventType
from tests.component.test_engine import (
    _build_engine,
    _config_with_tool,
    _new_session_context,
)
from tests.fixtures.mock_llm import (
    MockLLM,
    TextResponse,
    ToolCallResponse,
)
from tools import ToolContext, tool

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Blocking-tool fixture — a single asyncio.Event the test controls.
# ---------------------------------------------------------------------------


class _BlockArgs(BaseModel):
    """Args-model for the block tool. The single field is unused by
    the tool body; it exists so the schema is non-empty (some Mock-LLM
    tool-call-shape assertions want a populated arguments dict).
    """

    marker: str


# Module-scoped mutable holder — the `@tool` decorator freezes the
# function object at import time, so we cannot close over a
# per-test-created Event via a factory. A class attribute is
# rebindable without a `global` statement (ruff PLW0603) and each
# test reseeds `_GateState.event` in its arrange step.
class _GateState:
    """Per-test gate handle shared between the tool body and the test."""

    event: asyncio.Event | None = None


@tool(
    name="block",
    description="Blocks on a test-controlled asyncio.Event; returns {ok: true}.",
    args_schema=_BlockArgs,
)
async def _block_tool(args: _BlockArgs, ctx: ToolContext) -> dict[str, Any]:
    """Wait forever until the gate is set or cancelled.

    Uses ``ctx.cancellation.wait_cancelled()`` (via
    ``asyncio.wait``) to race with the gate so the outer
    ``race_with_cancel`` in ``MinimalFramework`` sees a clean
    ``ExchangeCancelled`` propagate rather than a hung await.

    The tool body's own cancel handling is IRRELEVANT to spec-068 —
    the observer's `ExchangeCancelled` catch fires regardless of
    what the tool body does. But racing here keeps the test fast.
    """
    del args  # unused
    gate = _GateState.event
    assert gate is not None, "test setup must seed _GateState.event"
    if ctx.cancellation is None:
        await gate.wait()
        return {"ok": True}
    gate_wait = asyncio.ensure_future(gate.wait())
    cancel_wait = asyncio.ensure_future(ctx.cancellation.wait_cancelled())
    try:
        await asyncio.wait(
            {gate_wait, cancel_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        gate_wait.cancel()
        cancel_wait.cancel()
    if ctx.cancellation.is_cancelled():
        # Signal cancellation by raising CancelledError so the outer
        # `race_with_cancel` in `MinimalFramework` sees the work
        # completed with cancellation semantics. `ExchangeCancelled`
        # will be raised by the racer's grace-timeout path.
        raise asyncio.CancelledError
    return {"ok": True}


# ---------------------------------------------------------------------------
# Test 1 — cancel mid-tool emits a `tool.failed(session.interrupted)`.
# ---------------------------------------------------------------------------


async def test_cancel_mid_tool_emits_session_interrupted_terminal(
    tmp_path: Path,
) -> None:
    """Cancel between `tool.called` and `tool.result` closes the span.

    Setup:
      - Mock LLM returns one tool call to `block`, then a text reply
        the exchange must not reach.
      - `_block_tool` waits on a module-scoped Event that the test
        never sets; instead the test fires the exchange's
        `CancelToken` as soon as `TOOL_CALLED` lands on the JSONL.
      - `race_with_cancel` in `MinimalFramework` observes the cancel
        flip, grace-timeouts the tool task, and raises
        `ExchangeCancelled`.

    Expected (spec-068 Part A AC #1):
      - The path contains a matching `tool.failed` for the
        `tool_call_id` of the `tool.called`.
      - Its `error.type == "session.interrupted"`.
      - Its actor is `system` (engine-authored via `_emit`, NOT
        model-attributable).
      - The failed event appears AFTER the `tool.called` on the path.
    """
    _GateState.event = asyncio.Event()

    cancel = CancelToken()
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="block",
                args={"marker": "spec_068_part_a"},
                tool_call_id="call_block_1",
            ),
            TextResponse(text="unreachable — cancel fires mid-tool"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        mock,
        extra_tools=[_block_tool],
    )
    base_ctx = await _new_session_context(store)
    ctx = ExecutionContext(
        thread_id=base_ctx.thread_id,
        trigger={"kind": "user_message", "content": "Kick the block tool."},
        cancellation=cancel,
    )

    async def cancel_after_tool_called() -> None:
        """Poll the JSONL for `tool.called` then flip the cancel token.

        Bounded by an outer 5s deadline — a hung wait means the
        observer never emitted `tool.called`, which is a different
        bug and should surface as a test timeout, not a hung suite.
        """
        for _ in range(500):  # 5s at 10ms
            events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
            if any(e.type == EventType.TOOL_CALLED for e in events):
                cancel.cancel()
                return
            await asyncio.sleep(0.01)

    canceller = asyncio.create_task(cancel_after_tool_called())

    # Drain the stream. We do NOT assert on the final event's type
    # here — the exchange may end with `thread.exchange_failed`
    # (typical) or the queue may close via the outer safety-net
    # path. What matters is the span-closure invariant on the log.
    async for _event in engine.execute_stream(_config_with_tool(), ctx):
        pass

    await canceller

    # Assemble the path from the store — the single source of truth.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    called = [e for e in events if e.type == EventType.TOOL_CALLED]
    assert len(called) == 1, (
        f"expected exactly one tool.called for the block tool; "
        f"path shows {len(called)}: "
        f"{[e.data for e in called]}"
    )
    tool_called = called[0]
    called_call_id = tool_called.data["tool_call_id"]

    # AC #1 — the cancelled span has a matching terminal on the path.
    failed = [
        e
        for e in events
        if e.type == EventType.TOOL_FAILED
        and e.data.get("tool_call_id") == called_call_id
    ]
    assert len(failed) == 1, (
        f"expected exactly one tool.failed for {called_call_id!r}; "
        f"got {len(failed)}. Full event-type tuple: "
        f"{tuple(e.type for e in events)}"
    )
    terminal = failed[0]

    # AC #1 — the failure carries the spec-068 error type. Same
    # string the renderer's `_append_synthetic_tool_failed` uses
    # today; Part B will remove that renderer helper in favour of
    # engine-side reconciliation using the SAME shape.
    assert terminal.data["error"]["type"] == "session.interrupted", (
        f"expected error.type=session.interrupted; got {terminal.data['error']}"
    )
    assert "message" in terminal.data["error"], (
        f"session.interrupted terminal missing error.message: {terminal.data['error']}"
    )

    # AC #1 — the terminal is engine-authored, not model-attributable.
    # The engine's `_emit` runs from the observer's cancel arm (and
    # from the exchange finally's registry backstop) with
    # `Actor(kind="system")` — never `agent`.
    assert terminal.actor.kind == "system", (
        f"session.interrupted terminal must have actor.kind=system "
        f"(engine-authored); got {terminal.actor}"
    )

    # AC #1 — ordering: `tool.failed` appears AFTER `tool.called` on
    # the append-only path. This is the invariant the projector /
    # provider consume; a `tool.failed` that lands elsewhere would
    # not pair the two.
    called_idx = events.index(tool_called)
    failed_idx = events.index(terminal)
    assert failed_idx > called_idx, (
        f"tool.failed at index {failed_idx} must follow tool.called "
        f"at index {called_idx}"
    )


# ---------------------------------------------------------------------------
# Happy-path regression — no double terminals when the tool returns cleanly.
# ---------------------------------------------------------------------------


async def test_happy_path_emits_single_tool_result_no_synthetic_failed(
    tmp_path: Path,
) -> None:
    """spec-068 Part A AC #2 — no double terminals on the non-cancel path.

    The observer's `try/finally` closes the span on the success arm
    too. If closure were misordered — e.g. the finally re-emitted
    the terminal — the path would show BOTH `tool.result` AND
    `tool.failed(session.interrupted)` for the same tool_call_id.
    Assert exactly one terminal per span, matching the pre-spec-068
    happy-path shape.
    """
    _GateState.event = asyncio.Event()
    # Pre-set the gate so the tool returns immediately — no cancel.
    assert _GateState.event is not None  # narrow for mypy
    _GateState.event.set()

    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="block",
                args={"marker": "happy_path"},
                tool_call_id="call_happy_1",
            ),
            TextResponse(text="all good"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        mock,
        extra_tools=[_block_tool],
    )
    ctx = await _new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert "all good" in result.data["content"]["text"]

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]

    called = [e for e in events if e.type == EventType.TOOL_CALLED]
    results = [e for e in events if e.type == EventType.TOOL_RESULT]
    failed = [e for e in events if e.type == EventType.TOOL_FAILED]

    assert len(called) == 1, f"expected one tool.called; got {len(called)}"
    assert len(results) == 1, (
        f"expected one tool.result; got {len(results)}. "
        f"Types: {tuple(e.type for e in events)}"
    )
    assert not failed, (
        f"happy path must NOT emit tool.failed; got {len(failed)}. "
        f"Types: {tuple(e.type for e in events)}"
    )
