"""spec-131 Step 5 — the engine-level ``EventObserver`` seam.

``AgentEngine.execute_stream`` is the single choke point every current
and future consumer (chat, headless run, sub-agent/``author_workflow``
runs, the HTTP API, and ``AgentEngine.execute`` itself, which wraps
``execute_stream``) goes through. This file tests the seam in
isolation, at the engine level, per the spec's Step 5 test plan:

1. A spy ``EventObserver`` records every event ``execute_stream``
   yields, in order, with the identical object identity (not a copy).
2. ``observer=None`` (the default) is byte-identical to the engine's
   pre-Step-5 behavior — proven by running the pre-existing engine
   test suite unmodified, not just by a new assertion here.
3. An observer whose ``observe()`` raises must not propagate into the
   generator, must not truncate the stream, and the caller must still
   receive every event.
4. An observer whose ``observe()`` hangs past the timeout bound must
   not stall the generator either — review-round addition (Reviewer B
   found the original raise-only handling left a hang completely
   unbounded).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from engine import engine as engine_module
from schemas import Event, EventType
from tests.component.test_engine import _build_engine, _config, _new_session_context
from tests.fixtures.mock_llm import MockLLM, TextResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import pytest


class _SpyObserver:
    """Records every event handed to ``observe``, verbatim, in order."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def observe(self, event: Event) -> None:
        self.events.append(event)


class _RaisingObserver:
    """An observer whose ``observe`` always raises.

    Simulates a misbehaving hook dispatcher — the engine's call site
    must catch this, not the observer implementation.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def observe(self, event: Event) -> None:
        self.call_count += 1
        msg = "boom — this observer always raises"
        raise RuntimeError(msg)


class _HangingObserver:
    """An observer whose ``observe`` never returns.

    Simulates the empirically-confirmed failure mode a hung/wedged
    hook subprocess dispatcher could produce: no exception, just an
    ``observe`` coroutine that never completes. The engine's
    ``asyncio.wait_for`` bound around the observer call is what must
    terminate this, not anything inside the observer itself.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def observe(self, event: Event) -> None:
        self.call_count += 1
        await asyncio.sleep(10_000)


async def test_spy_observer_called_once_per_event_in_order_same_object(
    tmp_path: Path,
) -> None:
    """The spy sees exactly the events the caller receives, in the
    same order, as the identical object (not a copy)."""
    mock = MockLLM(script=[TextResponse(text="Hi back.")])
    spy = _SpyObserver()
    engine, store, _am = await _build_engine(tmp_path, mock, observer=spy)
    ctx = await _new_session_context(store)

    received: list[Event] = [
        event async for event in engine.execute_stream(_config(), ctx)
    ]

    assert received  # sanity — the exchange produced events
    assert len(spy.events) == len(received)
    # Same order, same objects (identity, not equality) — the observer
    # must not alter what the caller sees, and the engine must not
    # hand the observer a copy.
    for observed, yielded in zip(spy.events, received, strict=True):
        assert observed is yielded

    # The stream ran to completion: the closing agent.message is
    # present and thread.exchange_completed is the final event,
    # confirming the observer saw the full stream through to the end.
    assert EventType.AGENT_MESSAGE in [e.type for e in received]
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED


async def test_observer_none_matches_pre_step5_behavior(tmp_path: Path) -> None:
    """``observer=None`` — the default — must reproduce exactly what
    the pre-Step-5 engine produced: same event count, types, and
    order. This is the narrow in-file complement to the load-bearing
    claim; the real proof is the full pre-existing engine test suite
    passing unmodified (run separately, not inside this test)."""
    mock = MockLLM(script=[TextResponse(text="Hi back.")])
    engine, store, _am = await _build_engine(tmp_path, mock, observer=None)
    ctx = await _new_session_context(store)

    received: list[Event] = [
        event async for event in engine.execute_stream(_config(), ctx)
    ]

    assert received
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED
    # No observer wired — nothing to assert about a spy; the assertion
    # here is simply that the stream behaves exactly as the identical
    # engine.execute_stream call does everywhere else in the existing
    # suite (TC-ENG-01 etc.), which this repeats at a coarse grain.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    assert tuple(e.type for e in received) == tuple(
        e.type for e in events if e.type != EventType.SESSION_OPENED
    )


async def test_raising_observer_does_not_propagate_or_truncate_stream(
    tmp_path: Path,
) -> None:
    """An observer whose ``observe()`` raises on every call must not
    break the generator: the exception must not propagate out of
    ``execute_stream``, and the caller must still receive every event,
    in full, uninterrupted."""
    mock = MockLLM(script=[TextResponse(text="Hi back.")])
    raising = _RaisingObserver()
    engine, store, _am = await _build_engine(tmp_path, mock, observer=raising)
    ctx = await _new_session_context(store)

    # No exception should escape here despite the observer raising on
    # every single event.
    received: list[Event] = [
        event async for event in engine.execute_stream(_config(), ctx)
    ]

    assert received
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED

    # The observer was actually invoked once per event (proving the
    # raise happened and was swallowed, not skipped).
    assert raising.call_count == len(received)

    # Cross-check against the durable log — the raising observer must
    # not have altered what was persisted either.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]
    assert tuple(e.type for e in received) == tuple(e.type for e in engine_events)


async def test_hanging_observer_times_out_and_does_not_stall_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An observer whose ``observe()`` never returns must not hang
    ``execute_stream`` — the engine's internal ``asyncio.wait_for``
    bound must fire on its own, with no external timeout wrapper
    needed by the caller. Confirms the stream still completes and a
    diagnosable warning is logged per timed-out event.

    Patches the module-level timeout constant down to keep the test
    fast (the production value is deliberately a few seconds; waiting
    that long per event, times 7 events, would make this test slow
    without adding coverage)."""
    monkeypatch.setattr(engine_module, "_OBSERVER_TIMEOUT_SECONDS", 0.05)

    mock = MockLLM(script=[TextResponse(text="Hi back.")])
    hanging = _HangingObserver()
    engine, store, _am = await _build_engine(tmp_path, mock, observer=hanging)
    ctx = await _new_session_context(store)

    with caplog.at_level(logging.WARNING, logger="engine"):
        received: list[Event] = await asyncio.wait_for(
            _collect(engine.execute_stream(_config(), ctx)),
            # Generous outer bound purely so a genuine regression
            # (the internal wait_for not firing) fails the test
            # promptly instead of hanging the test run itself; with
            # the internal bound at 0.05s x 7 events this should
            # finish in well under a second.
            timeout=5.0,
        )

    assert received
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED

    # The observer was invoked once per event — it hung every time,
    # and every time the engine gave up on it and moved on.
    assert hanging.call_count == len(received)

    # A warning was logged per timed-out event, with enough detail
    # (event_id, type) to diagnose which observer/event triggered it.
    timeout_records = [
        r for r in caplog.records if "observer.observe exceeded" in r.message
    ]
    assert len(timeout_records) == len(received)
    for record in timeout_records:
        assert "event_id=" in record.message
        assert "type=" in record.message

    # Cross-check against the durable log — the hang must not have
    # altered what was persisted.
    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    engine_events = [e for e in events if e.type != EventType.SESSION_OPENED]
    assert tuple(e.type for e in received) == tuple(e.type for e in engine_events)


async def _collect(stream: AsyncIterator[Event]) -> list[Event]:
    return [event async for event in stream]
