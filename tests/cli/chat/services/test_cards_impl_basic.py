"""spec-006 Test 1 — `CardsImpl.open` + `CardImpl.append/close`.

Five passes:

1. `await impl.open("t", kind="markdown")` returns a `CardImpl` with
   fresh `id`, `kind="markdown"`, `title="t"`. Store has one
   `WIRE_CARD_OPENED` event.
2. `await card.append("hi")` emits one `WIRE_CARD_APPENDED` with
   `payload["chunk"] == "hi"`.
3. `await card.close()` emits one `WIRE_CARD_CLOSED`. Second
   `close()` silently no-ops (no second event).
4. After close, `card.append("x")` raises `CardClosed`.
5. `impl.open("t", kind="unknown")` raises `UnknownCardKind`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cli.chat.services.cards_impl import CardImpl, CardsImpl
from schemas.events import EventType
from schemas.wire_kinds import (
    WIRE_CARD_APPENDED,
    WIRE_CARD_CLOSED,
    WIRE_CARD_OPENED,
)
from store import SessionStore, SqliteBackend
from tools.services.exceptions import CardClosed, UnknownCardKind


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


async def _collect_ui_events(
    store: SessionStore,
    thread_id: str,
    kind: str,
) -> list[dict]:
    payloads: list[dict] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == kind:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


async def test_open_returns_card_and_emits_opened(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        assert isinstance(card, CardImpl)
        assert card.kind == "markdown"
        assert card.title == "t"
        assert str(card.id).startswith("crd_")

        opened = await _collect_ui_events(
            store,
            impl._thread_id,
            WIRE_CARD_OPENED,
        )
        assert len(opened) == 1
        assert opened[0]["card_id"] == str(card.id)
        assert opened[0]["title"] == "t"
        assert opened[0]["kind"] == "markdown"
    finally:
        await impl.aclose()


async def test_append_emits_appended(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.append("hi")

        appended = await _collect_ui_events(
            store,
            impl._thread_id,
            WIRE_CARD_APPENDED,
        )
        assert len(appended) == 1
        assert appended[0]["chunk"] == "hi"
        assert appended[0]["card_id"] == str(card.id)
    finally:
        await impl.aclose()


async def test_close_emits_once_and_is_idempotent(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.close()
        # Second close is a no-op.
        await card.close()

        closed = await _collect_ui_events(
            store,
            impl._thread_id,
            WIRE_CARD_CLOSED,
        )
        assert len(closed) == 1
        assert card.is_closed is True
    finally:
        await impl.aclose()


async def test_append_after_close_raises_card_closed(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.close()

        with pytest.raises(CardClosed):
            await card.append("x")
    finally:
        await impl.aclose()


async def test_unknown_kind_raises(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        with pytest.raises(UnknownCardKind):
            await impl.open("t", kind="unknown")  # type: ignore[arg-type]
    finally:
        await impl.aclose()
