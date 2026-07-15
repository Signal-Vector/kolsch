"""spec-008 Test 3 — ``Cards.notice(card=...)`` payload.

Three passes:

1. ``impl.notice("hello", kind="info")`` (no card kwarg) — payload
   carries no ``card_id``.
2. ``impl.notice("hello", card=card1)`` — payload carries
   ``card_id=str(card1.id)``.
3. Two notice calls with ``text.startswith("dropped")`` on the
   same card share the same ``notice_id`` (dedup for drop-notice
   updates).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cli.chat.services.cards_impl import CardsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_NOTICE
from store import SessionStore, SqliteBackend


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


async def _mk_impl(store: SessionStore) -> CardsImpl:
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    return CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )


async def _drain(impl: CardsImpl) -> None:
    # Wait for spawned fire-and-forget notice tasks to finish. The
    # store's `append_event` is where the durable write lands; the
    # task has to reach completion for the notice to be readable.
    for _ in range(20):
        await asyncio.sleep(0)
    pending = [t for t in impl._pending_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _collect_notice_payloads(
    store: SessionStore,
    thread_id: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_CARD_NOTICE:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


async def test_notice_without_card_omits_card_id(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        # Open a card first so the drain task is running.
        await impl.open("t", kind="markdown")
        impl.notice("hello", kind="info")
        await _drain(impl)

        payloads = await _collect_notice_payloads(store, impl._thread_id)
        assert len(payloads) == 1
        assert payloads[0]["text"] == "hello"
        assert payloads[0]["kind"] == "info"
        assert "card_id" not in payloads[0]
    finally:
        await impl.aclose()


async def test_notice_with_card_includes_card_id(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        impl.notice("hello", kind="info", card=card)
        await _drain(impl)

        payloads = await _collect_notice_payloads(store, impl._thread_id)
        assert len(payloads) == 1
        assert payloads[0]["text"] == "hello"
        assert payloads[0]["card_id"] == str(card.id)
    finally:
        await impl.aclose()


async def test_drop_notice_shares_notice_id_per_card(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        impl.notice("dropped 10 events", kind="warning", card=card)
        impl.notice("dropped 20 events", kind="warning", card=card)
        await _drain(impl)

        payloads = await _collect_notice_payloads(store, impl._thread_id)
        assert len(payloads) == 2
        # Same notice_id on both — drop-notice text prefix triggers
        # the per-card dedup path.
        assert payloads[0]["notice_id"] == payloads[1]["notice_id"]
        # Both carry card_id.
        assert payloads[0]["card_id"] == str(card.id)
        assert payloads[1]["card_id"] == str(card.id)
        # Text differs (count increments).
        assert payloads[0]["text"] != payloads[1]["text"]
    finally:
        await impl.aclose()


async def test_ad_hoc_notices_get_fresh_notice_id(store: SessionStore) -> None:
    """Non-drop-notice repeat text still gets fresh notice_ids."""
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        impl.notice("hi", kind="info", card=card)
        impl.notice("hi", kind="info", card=card)
        await _drain(impl)

        payloads = await _collect_notice_payloads(store, impl._thread_id)
        assert len(payloads) == 2
        assert payloads[0]["notice_id"] != payloads[1]["notice_id"]
    finally:
        await impl.aclose()
