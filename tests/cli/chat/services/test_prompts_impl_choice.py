"""spec-004 Test 1 — `PromptsImpl.choice` emits + resolves.

Three passes:

1. Fire `choice(...)` in a task; assert a `UI_EVENT` with
   `kind=WIRE_PROMPT_REQUESTED` lands in the store; manually
   dispatch `WIRE_PROMPT_ANSWERED` via `resolve_pending`; assert
   the awaiting task returns the response string.
2. `WIRE_PROMPT_CANCELLED` (via `cancel_pending`) raises
   `asyncio.CancelledError` from the awaiting `choice()`.
3. `PromptSource(kind="tool", id="tc_1")` passed as `source`
   round-trips into the emitted event's payload verbatim.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from cli.chat.services.prompts_impl import PromptsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

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
    timeout: float = 1.0,  # noqa: ASYNC109 — test polling deadline, not a cancel scope
) -> dict:
    """Poll the store for a `WIRE_PROMPT_REQUESTED` payload."""
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
    msg = "no WIRE_PROMPT_REQUESTED event landed in the store within timeout"
    raise AssertionError(msg)


async def test_choice_emits_and_resolves(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_1"}

    task = asyncio.create_task(
        impl.choice("Q?", ["a", "b"], source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)
    assert payload["kind"] == "choice"
    assert payload["prompt"] == "Q?"
    assert payload["options"] == ["a", "b"]
    prompt_id = payload["prompt_id"]

    # Simulate the adapter's `.answered` route.
    impl.resolve_pending(prompt_id, "a")
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == "a"


async def test_choice_cancelled_raises_cancelled_error(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_2"}

    task = asyncio.create_task(
        impl.choice("Cancel?", ["a", "b"], source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)
    prompt_id = payload["prompt_id"]

    impl.cancel_pending(prompt_id)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)


async def test_choice_source_round_trips_verbatim(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {
        "kind": "tool",
        "id": "tc_1",
        "tool_call_id": "tc_1",
    }

    task = asyncio.create_task(
        impl.choice("Q?", ["y", "n"], source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)
    assert payload["source"] == dict(source)

    impl.resolve_pending(payload["prompt_id"], "y")
    await asyncio.wait_for(task, timeout=1.0)


async def test_text_emits_kind_text(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_3"}

    task = asyncio.create_task(
        impl.text("Name?", placeholder="alice", source=source),
    )
    payload = await _wait_for_requested(store, impl._thread_id)
    assert payload["kind"] == "text"
    assert payload["prompt"] == "Name?"
    assert payload["placeholder"] == "alice"

    impl.resolve_pending(payload["prompt_id"], "typed-value")
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == "typed-value"
