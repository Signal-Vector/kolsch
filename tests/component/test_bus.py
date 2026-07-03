"""Component tests for src/bus — TC-BUS-01..07.

Spec: docs/explanation/event-bus-contract.md +
Module API Reference §4. The bus is in-process by default; the
suite is parametrised over the backend so the same cases run against
Redis when that backend ships (TC-BUS-06 / spec §10 DB-R2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus import EventBus, InProcessEventBus
from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    *,
    event_type: EventType = EventType.TOOL_FAILED,
    thread_id: ThreadId | None = None,
    session_id: SessionId | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id or ThreadId(f"th_{uuid4()}"),
        session_id=session_id or SessionId(f"ses_{uuid4()}"),
        type=event_type,
        data=data or {},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


class _Recorder:
    """Subscriber handler that records every received event."""

    def __init__(self) -> None:
        self.received: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.received.append(event)


# ---------------------------------------------------------------------------
# Fixtures — parametrised over the bus backend
# ---------------------------------------------------------------------------


@pytest.fixture(params=["in_process"], ids=lambda v: f"backend={v}")
async def bus(request: pytest.FixtureRequest) -> AsyncIterator[EventBus]:
    """The bus under test. Add ``"redis"`` to ``params`` once that
    backend ships and the entire suite runs against it (TC-BUS-06).
    """
    backend_name: str = request.param
    if backend_name == "in_process":
        b = InProcessEventBus()
        yield b
        await b.close()
    else:  # pragma: no cover — slot for future backends
        pytest.skip(f"backend {backend_name!r} not yet shipped")


# ---------------------------------------------------------------------------
# TC-BUS-01 — published event reaches matching subscriber
# ---------------------------------------------------------------------------


async def test_matching_subscriber_receives_published_event(
    bus: EventBus,
) -> None:
    recorder = _Recorder()
    await bus.subscribe("tool.failed", recorder)

    ev = _event(event_type=EventType.TOOL_FAILED)
    await bus.publish(ev)

    assert len(recorder.received) == 1
    delivered = recorder.received[0]
    # Byte-identical event (TC-BUS-01 spec).
    assert delivered.event_id == ev.event_id
    assert delivered.model_dump() == ev.model_dump()


# ---------------------------------------------------------------------------
# TC-BUS-02 — non-matching subscriber not notified
# ---------------------------------------------------------------------------


async def test_non_matching_subscriber_not_notified(bus: EventBus) -> None:
    recorder = _Recorder()
    await bus.subscribe("tool.failed", recorder)

    await bus.publish(_event(event_type=EventType.THREAD_EXCHANGE_STARTED))

    assert recorder.received == []


# ---------------------------------------------------------------------------
# TC-BUS-03 — prefix wildcard matches
# ---------------------------------------------------------------------------


async def test_prefix_wildcard_matches_every_tool_event(bus: EventBus) -> None:
    recorder = _Recorder()
    await bus.subscribe("tool.*", recorder)

    await bus.publish(_event(event_type=EventType.TOOL_CALLED))
    await bus.publish(_event(event_type=EventType.TOOL_RESULT))
    await bus.publish(_event(event_type=EventType.TOOL_FAILED))
    # Non-matching: must NOT be delivered.
    await bus.publish(_event(event_type=EventType.MODEL_REQUEST))

    types = [e.type for e in recorder.received]
    assert types == [
        EventType.TOOL_CALLED,
        EventType.TOOL_RESULT,
        EventType.TOOL_FAILED,
    ]


async def test_full_wildcard_matches_everything(bus: EventBus) -> None:
    recorder = _Recorder()
    await bus.subscribe("*", recorder)

    await bus.publish(_event(event_type=EventType.TOOL_CALLED))
    await bus.publish(_event(event_type=EventType.MODEL_REQUEST))
    await bus.publish(_event(event_type=EventType.SESSION_OPENED))

    assert len(recorder.received) == 3


async def test_exact_pattern_does_not_match_prefix_sibling(
    bus: EventBus,
) -> None:
    # `tool.failed` (exact) must NOT match `tool.called`. Tighten the
    # spec's prefix-wildcard intent: the dot is part of the exact match,
    # not a wildcard separator.
    recorder = _Recorder()
    await bus.subscribe("tool.failed", recorder)

    await bus.publish(_event(event_type=EventType.TOOL_CALLED))
    await bus.publish(_event(event_type=EventType.TOOL_FAILED))

    assert [e.type for e in recorder.received] == [EventType.TOOL_FAILED]


# ---------------------------------------------------------------------------
# TC-BUS-04 — attribute filter works
# ---------------------------------------------------------------------------


async def test_attribute_filter_restricts_delivery_by_thread(
    bus: EventBus,
) -> None:
    th_a = ThreadId(f"th_{uuid4()}")
    th_b = ThreadId(f"th_{uuid4()}")

    recorder = _Recorder()
    await bus.subscribe("*", recorder, filters={"thread_id": th_a})

    await bus.publish(_event(event_type=EventType.USER_MESSAGE, thread_id=th_a))
    await bus.publish(_event(event_type=EventType.USER_MESSAGE, thread_id=th_b))
    await bus.publish(_event(event_type=EventType.TOOL_CALLED, thread_id=th_a))

    assert len(recorder.received) == 2
    assert all(e.thread_id == th_a for e in recorder.received)


async def test_typo_in_filter_key_delivers_nothing(bus: EventBus) -> None:
    """A filter on a non-existent field MUST deliver nothing.

    Otherwise a typo (`thread_idd` instead of `thread_id`) silently
    broadens delivery to every event — the opposite of the caller's
    intent. Pinning the safer semantics here.
    """
    recorder = _Recorder()
    await bus.subscribe(
        "*",
        recorder,
        filters={"thread_idd": ThreadId(f"th_{uuid4()}")},  # typo
    )

    await bus.publish(_event(event_type=EventType.USER_MESSAGE))
    await bus.publish(_event(event_type=EventType.TOOL_CALLED))

    assert recorder.received == []


async def test_attribute_filter_with_topic_pattern(bus: EventBus) -> None:
    th_a = ThreadId(f"th_{uuid4()}")
    th_b = ThreadId(f"th_{uuid4()}")

    recorder = _Recorder()
    # Compound: topic prefix AND attribute filter.
    await bus.subscribe(
        "thread.exchange_*",
        recorder,
        filters={"thread_id": th_a},
    )

    # Matches the wildcard AND the filter:
    await bus.publish(
        _event(event_type=EventType.THREAD_EXCHANGE_STARTED, thread_id=th_a)
    )
    # Matches the wildcard, FAILS the filter:
    await bus.publish(
        _event(event_type=EventType.THREAD_EXCHANGE_STARTED, thread_id=th_b)
    )
    # FAILS the wildcard (different prefix):
    await bus.publish(_event(event_type=EventType.MODEL_REQUEST, thread_id=th_a))

    assert len(recorder.received) == 1


# ---------------------------------------------------------------------------
# TC-BUS-05 — raising handler does not affect others
# ---------------------------------------------------------------------------


async def test_raising_handler_does_not_block_other_subscribers(
    bus: EventBus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raising_calls: list[Event] = []

    async def raising(event: Event) -> None:
        raising_calls.append(event)
        msg = "handler exploded"
        raise RuntimeError(msg)

    good_recorder = _Recorder()

    handle_raising = await bus.subscribe("tool.failed", raising)
    await bus.subscribe("tool.failed", good_recorder)

    with caplog.at_level(logging.WARNING, logger="bus"):
        await bus.publish(_event(event_type=EventType.TOOL_FAILED))

    # The good subscriber received the event.
    assert len(good_recorder.received) == 1
    # The raising subscriber was still invoked.
    assert len(raising_calls) == 1
    # The bus logged the handler error.
    assert any("handler exploded" in r.message for r in caplog.records)

    # The raising subscriber REMAINS subscribed (spec §I4).
    await bus.publish(_event(event_type=EventType.TOOL_FAILED))
    assert len(raising_calls) == 2
    assert len(good_recorder.received) == 2

    await bus.unsubscribe(handle_raising)


# ---------------------------------------------------------------------------
# TC-BUS-06 — backend parity (parametrise fixture; Postgres slot pending)
# ---------------------------------------------------------------------------
# Every test in this module is parametrised via the `bus` fixture's
# `params` list — adding `"redis"` to that list runs the entire suite
# against Redis. The parametrisation IS the parity guarantee.


# ---------------------------------------------------------------------------
# TC-BUS-07 — publish failure does not break execution
# ---------------------------------------------------------------------------


async def test_publish_failure_does_not_raise_into_caller(
    bus: EventBus,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bus's dispatcher itself raises (simulating a broken
    transport), ``publish`` MUST swallow it and log a warning. Spec
    §I7 — publisher decoupling."""
    recorder = _Recorder()
    await bus.subscribe("tool.failed", recorder)

    # Replace the internal dispatcher with one that always raises.
    async def _broken(*_args: object, **_kwargs: object) -> None:
        msg = "transport explosion"
        raise RuntimeError(msg)

    monkeypatch.setattr(bus, "_dispatch", _broken)

    with caplog.at_level(logging.WARNING, logger="bus"):
        # Must not raise into the caller.
        await bus.publish(_event(event_type=EventType.TOOL_FAILED))

    assert recorder.received == []  # broken transport delivered nothing
    assert any("transport explosion" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Spec invariants — additional coverage
# ---------------------------------------------------------------------------


async def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    recorder = _Recorder()
    handle = await bus.subscribe("*", recorder)

    await bus.publish(_event())
    await bus.unsubscribe(handle)
    await bus.publish(_event())

    assert len(recorder.received) == 1


async def test_unsubscribe_unknown_handle_is_noop(bus: EventBus) -> None:
    # Per Module API Reference §4.1: unsubscribe is idempotent / never
    # raises.
    fake = "sub_does_not_exist"
    await bus.unsubscribe(fake)


async def test_multiple_subscribers_to_same_topic_all_invoked(
    bus: EventBus,
) -> None:
    a = _Recorder()
    b = _Recorder()
    c = _Recorder()

    for r in (a, b, c):
        await bus.subscribe("tool.failed", r)

    await bus.publish(_event(event_type=EventType.TOOL_FAILED))

    assert len(a.received) == 1
    assert len(b.received) == 1
    assert len(c.received) == 1


async def test_concurrent_publish_delivers_all_events(bus: EventBus) -> None:
    recorder = _Recorder()
    await bus.subscribe("*", recorder)

    n = 50
    await asyncio.gather(
        *(bus.publish(_event(data={"i": i})) for i in range(n)),
    )
    # Every event is delivered (order is not guaranteed under
    # concurrent publish).
    assert len(recorder.received) == n
    assert len({e.event_id for e in recorder.received}) == n
