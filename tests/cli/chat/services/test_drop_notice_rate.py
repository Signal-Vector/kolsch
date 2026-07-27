"""spec-008 Test 6 — Drop-notice rate limit.

Three passes:

1. Fill the bounded queue on a live card (256 slots) then attempt
   another 30 appends. Exactly 3 ``Cards.notice`` calls fire (at
   drop 10, 20, 30) — not 30.
2. Each drop-notice on the same card shares the same ``notice_id``.
3. The rendered notice text updates to the current count
   (``"dropped 30 events"`` supersedes ``"dropped 10 events"``).

We spy on ``CardsImpl.notice`` to record every call — the notice
itself is fire-and-forget on the critical lane, and asserting on
its landing in the store would deadlock against the intentionally
paused drain. Recording at the ``notice`` seam captures WHAT the
rate-limit path fires without waiting for it to reach the store.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.cards_impl import CardsImpl
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from schemas.events import Event


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk_impl_paused(
    store: SessionStore,
) -> tuple[CardsImpl, list[dict[str, Any]]]:
    """Build a CardsImpl whose drain never advances + a notice log.

    The paused drain forces streaming appends to drop. Every drop
    tests ``_maybe_fire_drop_notice``; the notice sink lets us
    inspect exactly which ``notice`` calls the rate limiter made.
    """
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    impl = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    park = asyncio.Event()

    async def _park(event: Event) -> None:
        _ = event
        await park.wait()

    impl._publish_to_bus = _park  # type: ignore[assignment]

    notice_log: list[dict[str, Any]] = []
    original_notice = impl.notice

    def _spy_notice(
        text: str,
        *,
        kind: str = "info",
        card: Any = None,
    ) -> None:
        notice_log.append(
            {
                "text": text,
                "kind": kind,
                "card_id": str(card.id) if card is not None else None,
            },
        )
        # Do NOT delegate — the real notice would deadlock against
        # the paused drain (it schedules a critical-lane emit which
        # blocks on the full queue).
        _ = original_notice

    impl.notice = _spy_notice  # type: ignore[assignment]
    return impl, notice_log


async def _pump(_impl: CardsImpl) -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_rate_limit_fires_every_10_drops(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    """Fill the queue then push 30 more appends. 3 notices fire, not 30."""
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    impl, notice_log = await _mk_impl_paused(store)
    try:
        card = await impl.open("t", kind="markdown")
        # Saturate the queue via streaming appends. ~257 events go
        # through before drops start.
        for i in range(300):
            await card.append(f"a{i}")
        # Reset per-card counters so we can measure exactly 30 drops.
        impl._dropped_events_per_card[card.id] = 0
        impl._notice_ids_per_card.pop(card.id, None)
        notice_log.clear()

        for i in range(30):
            await card.append(f"post{i}")
        await _pump(impl)

        # Exactly 3 notice calls fired — at drop 10, 20, 30 — not 30.
        assert len(notice_log) == 3
        assert impl._dropped_events_per_card[card.id] == 30
        # Each notice is warning-kind and targeted at the card.
        for n in notice_log:
            assert n["kind"] == "warning"
            assert n["card_id"] == str(card.id)
            assert n["text"].startswith("dropped")
    finally:
        await impl.aclose()


async def test_drop_notices_share_notice_id(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    """Repeat drop-notices on the same card reuse the per-card ``notice_id``.

    We assert this on ``CardsImpl._notice_ids_per_card`` — the
    setdefault-based dedup in ``notice()`` is what makes
    ``EventAdapter._handle_card_notice`` update in place instead of
    mounting a second widget.
    """
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    # This test needs the REAL notice implementation to exercise the
    # per-card notice_id dedup path — spy it but forward to the
    # original so the notice_id gets registered.
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    impl = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )

    # Call the real notice with drop-notice text twice and verify the
    # per-card notice_id is reused. This is the seam the rate limiter
    # relies on for update-in-place behaviour.
    try:
        card = await impl.open("t", kind="markdown")
        impl.notice("dropped 10 events", kind="warning", card=card)
        first_id = impl._notice_ids_per_card[card.id]
        impl.notice("dropped 20 events", kind="warning", card=card)
        second_id = impl._notice_ids_per_card[card.id]
        assert first_id == second_id
    finally:
        await impl.aclose()


async def test_drop_notice_text_updates_count(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    """Successive drop-notices carry monotonically increasing counts."""
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    impl, notice_log = await _mk_impl_paused(store)
    try:
        card = await impl.open("t", kind="markdown")
        for i in range(300):
            await card.append(f"a{i}")
        impl._dropped_events_per_card[card.id] = 0
        impl._notice_ids_per_card.pop(card.id, None)
        notice_log.clear()

        for i in range(30):
            await card.append(f"post{i}")
        await _pump(impl)

        texts = [n["text"] for n in notice_log]
        assert texts == [
            "dropped 10 events",
            "dropped 20 events",
            "dropped 30 events",
        ]
    finally:
        await impl.aclose()
