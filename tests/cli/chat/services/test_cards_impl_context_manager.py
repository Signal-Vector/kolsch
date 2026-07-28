"""spec-006 Test 3 — `__aenter__` / `__aexit__` context manager.

Four passes:

1. Normal exit: `async with await impl.open(...)` closes with
   `final_status="success"`.
2. Exception exit: raise inside the `async with`; card closes with
   `final_status="error"`, `error_message=str(exc)[:200]`.
3. `CancelledError` exit: card closes with `final_status="cancelled"`.
4. `async with card:` on an already-closed card raises `CardClosed`
   from `__aenter__`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.cards_impl import CardsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_CLOSED
from store import SessionStore, SqliteBackend
from tools.services.exceptions import CardClosed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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


async def _last_closed_payload(
    store: SessionStore,
    thread_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_CARD_CLOSED:
            got = data.get("payload") or {}
            if isinstance(got, dict):
                payload = got
    return payload


async def test_aexit_success(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        async with card:
            await card.append("hello")

        assert card.is_closed is True
        payload = await _last_closed_payload(store, impl._thread_id)
        assert payload["final_status"] == "success"
    finally:
        await impl.aclose()


async def test_aexit_error(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")

        with pytest.raises(ValueError):
            async with card:
                raise ValueError("boom")

        assert card.is_closed is True
        payload = await _last_closed_payload(store, impl._thread_id)
        assert payload["final_status"] == "error"
        assert payload["error_message"] == "boom"
    finally:
        await impl.aclose()


async def test_aexit_cancelled(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")

        with pytest.raises(asyncio.CancelledError):
            async with card:
                raise asyncio.CancelledError

        assert card.is_closed is True
        payload = await _last_closed_payload(store, impl._thread_id)
        assert payload["final_status"] == "cancelled"
    finally:
        await impl.aclose()


async def test_aenter_on_closed_raises_card_closed(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.close()

        with pytest.raises(CardClosed):
            async with card:
                pass
    finally:
        await impl.aclose()


async def test_aexit_error_message_truncated_to_200(
    store: SessionStore,
) -> None:
    """Long exception messages truncate to 200 chars per Protocol.

    Bonus check on the truncation invariant — spec-006 AC #7
    references `str(exc_val)[:200]`.
    """
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        long_msg = "x" * 500

        with pytest.raises(RuntimeError):
            async with card:
                raise RuntimeError(long_msg)

        payload = await _last_closed_payload(store, impl._thread_id)
        assert payload["error_message"] == long_msg[:200]
        assert len(payload["error_message"]) == 200
    finally:
        await impl.aclose()
