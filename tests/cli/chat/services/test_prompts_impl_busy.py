"""spec-004 Test 2 — `PromptBusy` on concurrent calls.

Three passes:

1. Two `choice(...)` calls back-to-back without resolving the first;
   the second raises `PromptBusy` synchronously (before any await
   would suspend).
2. `wait_for_slot(timeout=0.1)` on an unresolved future raises
   `PromptBusy` after ~100ms.
3. Resolve the first via a synthetic `.answered`; `wait_for_slot`
   then returns immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from cli.chat.services.prompts_impl import PromptsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend
from tools.services.exceptions import PromptBusy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from tools.services.prompts import PromptSource


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


async def _mk_impl(store: SessionStore) -> PromptsImpl:
    session = await store.create_session("ag_test", name="p")
    threads = await store.list_threads(session.session_id)
    return PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )


async def _wait_for_requested(
    store: SessionStore,
    thread_id: str,
    timeout: float = 1.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async for ev in store.read_events(thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") == WIRE_PROMPT_REQUESTED:
                payload = data.get("payload") or {}
                if isinstance(payload, dict):
                    return payload
        await asyncio.sleep(0.01)
    msg = "no WIRE_PROMPT_REQUESTED event landed within timeout"
    raise AssertionError(msg)


async def test_second_choice_raises_prompt_busy(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_a"}

    first = asyncio.create_task(
        impl.choice("Q1?", ["a"], source=source),
    )
    # Wait until the impl has actually taken the slot.
    await _wait_for_requested(store, impl._thread_id)

    with pytest.raises(PromptBusy):
        await impl.choice("Q2?", ["b"], source=source)

    # Also verify text() is subject to the same guard.
    with pytest.raises(PromptBusy):
        await impl.text("Q3?", source=source)

    # Clean up: resolve the first so the task can complete.
    payload = await _wait_for_requested(store, impl._thread_id)
    impl.resolve_pending(payload["prompt_id"], "a")
    await asyncio.wait_for(first, timeout=1.0)


async def test_wait_for_slot_times_out(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_b"}

    first = asyncio.create_task(
        impl.choice("Q?", ["a"], source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)

    start = time.monotonic()
    with pytest.raises(PromptBusy):
        await impl.wait_for_slot(source=source, timeout=0.1)
    elapsed = time.monotonic() - start
    # Sanity check: the timeout actually elapsed (not an instant
    # raise from some other guard). Allow generous slack.
    assert elapsed >= 0.09

    # Clean up.
    impl.resolve_pending(payload["prompt_id"], "a")
    await asyncio.wait_for(first, timeout=1.0)


async def test_wait_for_slot_returns_after_resolve(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_c"}

    first = asyncio.create_task(
        impl.choice("Q?", ["a"], source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)

    # Resolve, then `wait_for_slot` should return immediately.
    impl.resolve_pending(payload["prompt_id"], "a")
    await asyncio.wait_for(first, timeout=1.0)

    # No pending → returns immediately.
    await asyncio.wait_for(
        impl.wait_for_slot(source=source, timeout=1.0),
        timeout=0.5,
    )
