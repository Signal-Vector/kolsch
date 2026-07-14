"""spec-006 Test 2 — `allowed_ops` gate.

Four passes:

1. `progress` card: `.append("x")` raises `TypeError` BEFORE any
   event fires (store has zero appended events).
2. `progress` card: `.set_progress(1, 10)` succeeds, emits one
   `WIRE_CARD_SET_PROGRESS`.
3. `confirm` card: `.append("x")` raises `TypeError`.
4. `workflow` card: `.set_progress(1, 10)` succeeds (r3 BLOCKER
   fix — `set_progress` in workflow's `allowed_ops`).
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

from cli.chat.services.cards_impl import CardsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_APPENDED, WIRE_CARD_SET_PROGRESS
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


async def _count_ui_events(
    store: SessionStore,
    thread_id: str,
    kind: str,
) -> int:
    n = 0
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == kind:
            n += 1
    return n


async def test_progress_append_raises_type_error_before_emit(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="progress")

        with pytest.raises(TypeError):
            await card.append("x")

        # Zero WIRE_CARD_APPENDED events fired — gate fired before emit.
        assert (
            await _count_ui_events(store, impl._thread_id, WIRE_CARD_APPENDED)
            == 0
        )
    finally:
        await impl.aclose()


async def test_progress_set_progress_succeeds(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="progress")
        await card.set_progress(1, 10)

        assert (
            await _count_ui_events(
                store, impl._thread_id, WIRE_CARD_SET_PROGRESS,
            )
            == 1
        )
    finally:
        await impl.aclose()


async def test_confirm_append_raises_type_error(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="confirm")

        with pytest.raises(TypeError):
            await card.append("x")

        assert (
            await _count_ui_events(store, impl._thread_id, WIRE_CARD_APPENDED)
            == 0
        )
    finally:
        await impl.aclose()


async def test_workflow_set_progress_succeeds(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="workflow")
        await card.set_progress(1, 10)

        assert (
            await _count_ui_events(
                store, impl._thread_id, WIRE_CARD_SET_PROGRESS,
            )
            == 1
        )
    finally:
        await impl.aclose()
