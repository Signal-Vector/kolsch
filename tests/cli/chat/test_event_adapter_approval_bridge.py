"""bug-021 Test 5 — `EventAdapter` spawns a widget→store bridge task.

Covers AC #7. After `_handle_prompt_requested` awaits
`mount_approval_block(...)`, the adapter spawns a fire-and-forget
task that awaits `ApprovalBlock.resolved()` and appends a
``WIRE_PROMPT_ANSWERED`` UI event to the store with the matching
`prompt_id` + a response-key string derived from the widget's return.

Also asserts:

- Widget cancellation propagates as a debug log, NOT a raise.
- Duplicate widget-resolution observations do not double-emit
  (belt-and-braces with the existing `_resolved_prompt_ids` LRU).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from providers.authz import AuthzEffect
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _MockBlock:
    """Minimal `ApprovalBlock` stand-in that resolves on command."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._result: Any = None
        self._raise: BaseException | None = None
        self.resolved_calls = 0

    def set_result(self, result: Any) -> None:
        self._result = result
        self._event.set()

    def raise_on_resolved(self, exc: BaseException) -> None:
        self._raise = exc
        self._event.set()

    async def resolved(self) -> Any:
        self.resolved_calls += 1
        await self._event.wait()
        if self._raise is not None:
            raise self._raise
        return self._result


class _MockApp:
    """Records mount calls; returns a `_MockBlock` the test drives."""

    def __init__(self, *, block: _MockBlock | None) -> None:
        self._block = block
        self.mount_calls: list[tuple[Any, Any, Any]] = []

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
        *,
        parent_hint: Any = None,
    ) -> Any:
        _ = parent_hint
        self.mount_calls.append((rule_id, action, target))
        return self._block


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


def _mk_requested(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_REQUESTED,
            "payload": {
                "prompt_id": prompt_id,
                "kind": "choice",
                "prompt": "pick",
                "options": ["y", "n"],
                "source": {"kind": "tool", "id": "t1"},
                "rule_id": "rl_x",
                "action": "bash",
                "target": "ls",
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _await_answered_event(
    store: SessionStore,
    thread_id: Any,
    prompt_id: str,
    *,
    timeout: float = 2.0,  # noqa: ASYNC109 — test polling deadline, not a cancel scope
) -> Event:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async for ev in store.read_events(thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") != WIRE_PROMPT_ANSWERED:
                continue
            payload = data.get("payload")
            if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
                return ev
        await asyncio.sleep(0.02)
    msg = f"no WIRE_PROMPT_ANSWERED landed for prompt_id={prompt_id!r}"
    raise AssertionError(msg)


# --------------------------------------------------------------------
# AC #7 — the bridge appends WIRE_PROMPT_ANSWERED after widget resolves.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_task_appends_answered_after_widget_resolves(
    store: SessionStore,
) -> None:
    """Widget → `resolved()` → adapter appends `WIRE_PROMPT_ANSWERED`."""
    session = await store.create_session("ag_test", name="bridge_ok")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )

    prompt_id = f"prm_{uuid4().hex[:8]}"
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    # Dispatch the request; the adapter mounts the block and spawns
    # the bridge task. `mount_approval_block` returns the mock block.
    await adapter.handle(request)
    assert len(app.mount_calls) == 1

    # Simulate the user pressing `y` — the widget's `_resolved_event`
    # sets and `resolved()` returns `AuthzEffect.ALLOW`.
    block.set_result(AuthzEffect.ALLOW)

    answered = await _await_answered_event(store, thread_id, prompt_id)
    payload = answered.data["payload"]
    assert payload["prompt_id"] == prompt_id
    assert payload["response"] == "y"


@pytest.mark.asyncio
async def test_bridge_task_maps_deny_effect_to_n(
    store: SessionStore,
) -> None:
    """Widget `DENY` → adapter appends `response="n"`."""
    session = await store.create_session("ag_test", name="bridge_deny")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    await adapter.handle(request)
    block.set_result(AuthzEffect.DENY)

    answered = await _await_answered_event(store, thread_id, prompt_id)
    assert answered.data["payload"]["response"] == "n"


# --------------------------------------------------------------------
# Cancellation and idempotence.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_task_survives_widget_cancellation(
    store: SessionStore,
) -> None:
    """`resolved()` raising `CancelledError` does not propagate; no append."""
    session = await store.create_session("ag_test", name="bridge_cancel")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    await adapter.handle(request)
    block.raise_on_resolved(asyncio.CancelledError())

    # Give the bridge task a moment to consume the cancellation.
    await asyncio.sleep(0.05)

    # No answered event lands — cancellation is a silent debug log.
    events: list[Event] = []
    async for ev in store.read_events(thread_id):
        if (
            ev.type == EventType.UI_EVENT
            and ev.data.get(
                "kind",
            )
            == WIRE_PROMPT_ANSWERED
        ):
            events.append(ev)
    assert events == []


@pytest.mark.asyncio
async def test_bridge_task_no_double_emit_on_duplicate_dispatch(
    store: SessionStore,
) -> None:
    """A duplicate `WIRE_PROMPT_REQUESTED` dispatch is dedup'd upstream —
    exactly one bridge task spawns → exactly one `WIRE_PROMPT_ANSWERED`
    lands."""
    session = await store.create_session("ag_test", name="bridge_dedup")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    await adapter.handle(request)
    # Second dispatch is a no-op via `_applied_prompt_ids` LRU.
    await adapter.handle(request)
    assert len(app.mount_calls) == 1
    assert block.resolved_calls <= 1  # one bridge task spawned

    block.set_result(AuthzEffect.ALLOW)
    await _await_answered_event(store, thread_id, prompt_id)

    # Count the number of answered events for this prompt_id — MUST be 1.
    count = 0
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") != WIRE_PROMPT_ANSWERED:
            continue
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
            count += 1
    assert count == 1


@pytest.mark.asyncio
async def test_bridge_skipped_when_mount_returns_none(
    store: SessionStore,
) -> None:
    """`mount_approval_block` returning `None` (mock apps) → bridge skipped."""
    session = await store.create_session("ag_test", name="bridge_none")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    app = _MockApp(block=None)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    # Must not raise even though there's no block to await.
    await adapter.handle(request)
    # No answered event ever lands.
    await asyncio.sleep(0.05)
    events: list[Event] = []
    async for ev in store.read_events(thread_id):
        if (
            ev.type == EventType.UI_EVENT
            and ev.data.get(
                "kind",
            )
            == WIRE_PROMPT_ANSWERED
        ):
            events.append(ev)
    assert events == []
