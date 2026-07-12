"""bug-020 Test 2 — `CardsImpl._publish_to_bus` fans onto the runtime bus.

Exercises AC #4 (publish to `UI_EVENT_TOPIC`), the topic-constant
match with `EventType.UI_EVENT`, and the best-effort semantics
(bus raise -> impl continues; drain worker doesn't die).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import UI_EVENT_TOPIC, CardsImpl
from schemas import Event
from schemas.events import EventType
from schemas.identifiers import SessionId, ThreadId


class _MockBus:
    """Records `publish` calls; optional `raise_on_publish` for AC #3."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[Event] = []
        self.raise_on_publish = raise_on_publish

    async def publish(self, event: Event) -> None:
        if self.raise_on_publish:
            msg = "simulated bus failure"
            raise RuntimeError(msg)
        self.published.append(event)


async def _boot_impl(
    tmp_path: Path,
    *,
    bus: Any | None = None,
) -> tuple[CardsImpl, Any, Any]:
    """Return `(impl, session_id, thread_id)` wired to a real SQLite store."""
    from store import SessionStore, SqliteBackend

    (tmp_path / ".platform").mkdir(exist_ok=True)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)

    impl = CardsImpl(
        session_store=store,
        session_id=SessionId(str(session.session_id)),
        thread_id=ThreadId(str(threads[0].thread_id)),
        bus=bus,
    )
    return impl, session.session_id, threads[0].thread_id


@pytest.mark.asyncio
async def test_publish_to_bus_publishes_ui_event(tmp_path: Path) -> None:
    """`CardsImpl.open` triggers a bus publish for the `WIRE_CARD_OPENED` event."""
    bus = _MockBus()
    impl, _, _ = await _boot_impl(tmp_path, bus=bus)
    try:
        card = await impl.open(title="test", kind="markdown")
        # Drain worker is async — yield a couple of times.
        for _ in range(10):
            await asyncio.sleep(0)
            if bus.published:
                break

        assert bus.published, "bus.publish never fired"
        first = bus.published[0]
        assert first.type == EventType.UI_EVENT
        assert first.data["kind"] == "kolsch.card.opened"
        assert first.data["payload"]["card_id"] == str(card.id)
    finally:
        await impl.aclose()


def test_topic_constant_matches_subscribe_topic() -> None:
    """`UI_EVENT_TOPIC` equals `EventType.UI_EVENT.value` — bus topic-pattern match."""
    assert UI_EVENT_TOPIC == EventType.UI_EVENT.value


@pytest.mark.asyncio
async def test_publish_survives_bus_backpressure(tmp_path: Path) -> None:
    """A raising bus does not kill the drain worker or the impl (best-effort)."""
    bus = _MockBus(raise_on_publish=True)
    impl, _, _ = await _boot_impl(tmp_path, bus=bus)
    try:
        # `open` -> drain worker -> `_publish_to_bus` -> bus raises.
        # The drain's own try/except logs and continues; a second
        # open must still succeed.
        await impl.open(title="one", kind="markdown")
        await asyncio.sleep(0)
        await impl.open(title="two", kind="markdown")
        # Yield so the drain runs at least once.
        for _ in range(5):
            await asyncio.sleep(0)
        # The impl must remain usable (drain not dead).
        assert impl._drain_task is not None
        assert not impl._drain_task.done()
    finally:
        await impl.aclose()


@pytest.mark.asyncio
async def test_publish_absent_bus_is_silent_noop(tmp_path: Path) -> None:
    """No bus injected — the drain still runs; no publish attempts."""
    impl, _, _ = await _boot_impl(tmp_path, bus=None)
    try:
        await impl.open(title="x", kind="markdown")
        # Drain runs but publish is a no-op — no assertions to make
        # about a mock bus, but confirming the impl doesn't blow up
        # on the drain loop.
        for _ in range(5):
            await asyncio.sleep(0)
        assert impl._drain_task is not None
        assert not impl._drain_task.done()
    finally:
        await impl.aclose()
