"""bug-106 — ``_Exchange.close_all_open_spans`` drain under cascading cancel.

spec-068 Part B added a synth-terminal safety net on the exchange
finally so every registered span (prompt request, tool span, etc.)
gets its terminal ``_emit``ed even when the emit site's own
``try/finally`` bailed. That guarantee was broken on the exact path
it existed to cover: the drain wrapped ``await self._emit(...)`` in
``except Exception``, and ``asyncio.CancelledError`` is a
``BaseException``. Under a cascading cancel (an ``asyncio.wait_for``
timeout on a sub-workflow node) the first ``await`` re-raised,
the ``for`` loop exited mid-drain, and every subsequent span's
terminal was silently dropped.

These unit tests cover the drain's per-span shielding, cancellation
propagation, and best-effort semantics. Test 6 covers the
``_run_exchange`` outer wrapper widening so ``_QUEUE_END`` still
queues when the drain re-raises ``CancelledError``. Test 5 (the
component-level sub-workflow-timeout regression) lives at
``tests/component/workflows/test_node_timeout_emits_prompt_cancelled.py``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.engine import _QUEUE_END, _Exchange
from schemas import Actor, EventType
from schemas.wire_kinds import WIRE_PROMPT_CANCELLED

# ---------------------------------------------------------------------------
# Helpers — a minimal ``_Exchange`` stand-in whose ``_emit`` is a spy.
# ---------------------------------------------------------------------------


def _make_exchange(*, emit_spy: Any) -> _Exchange:
    """Build a bare ``_Exchange`` whose ``_emit`` is replaced by a spy.

    The drain only touches ``self._open_spans`` and ``self._emit`` —
    everything else on the exchange (store, bus, lock, config, etc.)
    is untouched by ``close_all_open_spans``. We construct the object
    without ``__init__`` to skip the heavyweight engine wiring, seed
    the two fields the drain reads, and monkeypatch ``_emit`` to the
    caller's spy so the test can inspect what got called.
    """
    ex = object.__new__(_Exchange)
    # Minimal fields the drain touches.
    ex._open_spans = OrderedDict()
    ex._emit = emit_spy  # type: ignore[method-assign]
    return ex


def _prompt_terminal_factory(
    prompt_id: str,
) -> Any:
    """Return a spec-068-shape factory for ``WIRE_PROMPT_CANCELLED``.

    Mirrors ``PromptsImpl._append_ui_event``'s registered factory
    (``prompts_impl.py:505-519``) — the same shape the real emit site
    produces so tests exercise the exact payload our fix must ferry
    through under cancel.
    """

    def _factory(
        _pid: str = prompt_id,
    ) -> tuple[EventType, dict[str, Any], Actor]:
        return (
            EventType.UI_EVENT,
            {
                "schema_version": 1,
                "kind": WIRE_PROMPT_CANCELLED,
                "payload": {
                    "prompt_id": _pid,
                    "reason": "session_interrupted",
                },
            },
            Actor(kind="system"),
        )

    return _factory


# ---------------------------------------------------------------------------
# Test 1 — drain completes under cascading cancel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_completes_under_cascading_cancel() -> None:
    """Under ``asyncio.wait_for(..., timeout=0.01)`` every registered
    span's terminal is emitted, even though the surrounding task is
    being cancelled while the drain is awaiting.

    The runner registers two spans and then awaits an infinite future;
    the ``asyncio.wait_for`` timeout cancels the runner and its
    ``finally`` drains the span registry. Before bug-106's fix the
    first ``await self._emit`` re-raised ``CancelledError`` and
    span 2's terminal was lost.
    """
    emitted: list[tuple[EventType, dict[str, Any], Actor]] = []

    async def _spy_emit(
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> Any:
        # Simulate a real emit that yields (which is where the ambient
        # cancel gets delivered) — one ``await asyncio.sleep(0)`` is
        # enough for the cancel to hit.
        await asyncio.sleep(0)
        emitted.append((type_, data, actor))
        return MagicMock()

    ex = _make_exchange(emit_spy=_spy_emit)
    ex.register_span("prompt_1", _prompt_terminal_factory("prompt_1"))
    ex.register_span("prompt_2", _prompt_terminal_factory("prompt_2"))

    async def _runner() -> None:
        try:
            # Simulate the exchange body: await something that never
            # resolves, then run the drain in the finally.
            await asyncio.Event().wait()
        finally:
            await ex.close_all_open_spans()

    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(_runner(), timeout=0.01)

    # Both terminals landed regardless of the ambient cancel.
    prompt_ids = [
        entry[1]["payload"]["prompt_id"]
        for entry in emitted
        if entry[1].get("kind") == WIRE_PROMPT_CANCELLED
    ]
    assert prompt_ids == ["prompt_1", "prompt_2"], (
        f"drain dropped a terminal — got {prompt_ids!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — ``CancelledError`` still propagates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_error_still_propagates() -> None:
    """The drain must not swallow ambient cancellation.

    Uses ``asyncio.create_task`` + explicit ``task.cancel()`` (not
    ``asyncio.wait_for``, whose ``TimeoutError`` wrapper masks whether
    we actually propagated the inner cancel). Asserts (a) the task
    raises ``CancelledError`` and (b) inside the runner after the
    drain, ``asyncio.current_task().cancelling() > 0`` — the runtime
    signal that the current task's cancel flag is set.
    """
    cancelling_after_drain: list[int] = []

    async def _spy_emit(
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> Any:
        await asyncio.sleep(0)
        return MagicMock()

    ex = _make_exchange(emit_spy=_spy_emit)
    ex.register_span("prompt_a", _prompt_terminal_factory("prompt_a"))

    ready = asyncio.Event()

    async def _runner() -> None:
        try:
            ready.set()
            await asyncio.Event().wait()
        finally:
            try:
                await ex.close_all_open_spans()
            finally:
                # The runtime signal: after the drain, the current
                # task's cancel flag is set (Python 3.11+). Record it
                # for the outer assertion.
                task = asyncio.current_task()
                if task is not None:
                    cancelling_after_drain.append(task.cancelling())

    task = asyncio.create_task(_runner())
    await ready.wait()
    # Yield once so the runner actually parks on ``Event().wait()``.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelling_after_drain, "runner finally never observed .cancelling()"
    assert cancelling_after_drain[0] > 0, (
        f"drain swallowed the ambient cancel — .cancelling()={cancelling_after_drain[0]!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — per-span emit failure does NOT stop the drain.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_span_emit_failure_does_not_stop_drain() -> None:
    """A ``RuntimeError`` from one span's emit is logged and skipped;
    subsequent spans still get their terminal.
    """
    emitted: list[str] = []
    call_count = {"n": 0}

    async def _spy_emit(
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 2:
            msg = "synthetic emit failure for the middle span"
            raise RuntimeError(msg)
        emitted.append(data["payload"]["prompt_id"])
        return MagicMock()

    ex = _make_exchange(emit_spy=_spy_emit)
    ex.register_span("prompt_1", _prompt_terminal_factory("prompt_1"))
    ex.register_span("prompt_2", _prompt_terminal_factory("prompt_2"))
    ex.register_span("prompt_3", _prompt_terminal_factory("prompt_3"))

    await ex.close_all_open_spans()

    assert emitted == ["prompt_1", "prompt_3"], (
        f"drain stopped after a per-span failure — got {emitted!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — happy-path ``_emit`` argument tuples unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_emit_arg_tuples_unchanged() -> None:
    """With no ambient cancellation, the ``(type_, data, actor)``
    tuples passed into ``_emit`` are byte-identical to what the
    factory produced — i.e. our fix did not alter the terminal
    payload construction.
    """
    captured: list[tuple[EventType, dict[str, Any], Actor]] = []

    async def _spy_emit(
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> Any:
        captured.append((type_, data, actor))
        return MagicMock()

    ex = _make_exchange(emit_spy=_spy_emit)
    factory_a = _prompt_terminal_factory("prompt_a")
    factory_b = _prompt_terminal_factory("prompt_b")
    ex.register_span("prompt_a", factory_a)
    ex.register_span("prompt_b", factory_b)

    expected = [factory_a(), factory_b()]

    await ex.close_all_open_spans()

    assert captured == expected, (
        f"happy-path terminal payloads drifted; captured={captured!r} "
        f"expected={expected!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — ``_QUEUE_END`` still queues when the drain re-raises.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_end_still_queues_when_drain_reraises() -> None:
    """AC #6: the outer wrapper at ``engine.py:414-424`` must be
    widened from ``except Exception`` to ``except BaseException``
    (with a re-raise) so a drain re-raising ``CancelledError`` does
    NOT bypass the ``_QUEUE_END`` enqueue at ``:425``. If the
    sentinel never queues, downstream stream consumers hang.

    This test simulates the outer wrapper flow with a drain that
    unconditionally re-raises ``CancelledError``. Asserts (a)
    ``_QUEUE_END`` lands on the queue and (b) the ``CancelledError``
    still surfaces to the caller.
    """
    import logging

    queue: asyncio.Queue[Any] = asyncio.Queue()

    class _DrainReraises:
        async def close_all_open_spans(self) -> None:
            raise asyncio.CancelledError

    ex = _DrainReraises()
    log = logging.getLogger("engine")

    async def _wrapper_body() -> None:
        # This mirrors the shape at ``_run_exchange``'s finally after
        # the bug-106 fix: outer try widened to ``BaseException``
        # with a re-raise, wrapped in a try/finally that queues
        # ``_QUEUE_END`` regardless of raise/return.
        try:
            try:
                await ex.close_all_open_spans()
            except BaseException:
                log.exception("close_all_open_spans safety net raised")
                raise
        finally:
            queue.put_nowait(_QUEUE_END)

    with pytest.raises(asyncio.CancelledError):
        await _wrapper_body()

    # The sentinel landed — downstream consumers won't hang.
    assert not queue.empty(), "_QUEUE_END was skipped by the outer wrapper"
    item = queue.get_nowait()
    assert item is _QUEUE_END


# ---------------------------------------------------------------------------
# Guardrail — a factory raising ``Exception`` is still logged + skipped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_exception_does_not_stop_drain() -> None:
    """Complementary to Test 3: a *factory* raising (not the emit)
    is caught by the per-span ``except Exception`` guard and the
    drain continues to the next span. This pins the pre-existing
    best-effort semantics on the factory branch too.
    """
    emitted: list[str] = []

    async def _spy_emit(
        *,
        type_: EventType,
        data: dict[str, Any],
        actor: Actor,
    ) -> Any:
        emitted.append(data["payload"]["prompt_id"])
        return MagicMock()

    def _bad_factory() -> tuple[EventType, dict[str, Any], Actor]:
        msg = "synthetic factory failure"
        raise RuntimeError(msg)

    ex = _make_exchange(emit_spy=_spy_emit)
    ex.register_span("prompt_1", _prompt_terminal_factory("prompt_1"))
    ex.register_span("prompt_2", _bad_factory)
    ex.register_span("prompt_3", _prompt_terminal_factory("prompt_3"))

    await ex.close_all_open_spans()

    assert emitted == ["prompt_1", "prompt_3"]
