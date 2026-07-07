"""spec-004 Test 4 — `PromptsImpl.aclose`.

Three passes:

1. Fire `choice(...)` in a task; call `aclose()` before resolution;
   the awaiting task raises `asyncio.CancelledError`.
2. Second `aclose()` is a no-op (no exception).
3. After `aclose()`, `choice(...)` raises `RuntimeError`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from cli.chat.services.prompts_impl import PromptsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend
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
    msg = "no WIRE_PROMPT_REQUESTED event within timeout"
    raise AssertionError(msg)


async def test_aclose_cancels_in_flight(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_1"}

    task = asyncio.create_task(
        impl.choice("Q?", ["a", "b"], source=source),
    )
    await _wait_for_requested(store, impl._thread_id)

    await impl.aclose()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)


async def test_aclose_second_call_no_op(store: SessionStore) -> None:
    impl = await _mk_impl(store)

    await impl.aclose()
    # Second call must not raise.
    await impl.aclose()


async def test_choice_after_aclose_raises_runtime_error(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    await impl.aclose()

    source: PromptSource = {"kind": "tool", "id": "tc_1"}
    with pytest.raises(RuntimeError):
        await impl.choice("Q?", ["a"], source=source)
    with pytest.raises(RuntimeError):
        await impl.text("Q?", source=source)
