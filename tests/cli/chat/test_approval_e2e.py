"""spec-004 Test 6 — end-to-end approval flow (event-sourced path).

Two passes:

1. Wire `InlineApprovalCallback` → `PromptsImpl` → `EventAdapter`
   → mock `mount_approval_block`. Invoke the callback; assert the
   adapter's dispatch of the emitted `WIRE_PROMPT_REQUESTED` from
   the store mounts an `ApprovalBlock` (via the mock's mock mount).
   Simulate the user pressing `y` by dispatching a matching
   `WIRE_PROMPT_ANSWERED`; assert the callback returns
   `AuthzEffect.ALLOW`.
2. Confirm the emitted `WIRE_PROMPT_ANSWERED` event's payload
   round-trips through the session's JSONL (spec-003's
   `logical_seq` allocation stamps a monotonic seq on each
   append).

Note: this test does NOT boot the full `ChatApp` — that's
integration-level work that belongs in a Pilot suite. Instead it
wires the same pieces `ChatApp.on_mount` wires (adapter + impl +
callback) directly, so the load-bearing "who mounts the widget"
handshake is exercised without the Textual runtime.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.approval import InlineApprovalCallback
from cli.chat.event_adapter import EventAdapter
from cli.chat.services.prompts_impl import PromptsImpl
from providers.authz import AuthzEffect
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_REQUESTED,
)
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _MockApp:
    def __init__(self) -> None:
        self.mount_calls: list[tuple[Any, Any, Any]] = []
        self.cleared_calls = 0

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
    ) -> Any:
        self.mount_calls.append((rule_id, action, target))
        return object()

    def clear_awaiting_approval(self) -> None:
        self.cleared_calls += 1


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


async def test_end_to_end_approval_via_event_bus(
    store: SessionStore,
) -> None:
    """Wire up + drive the full event-sourced approval handshake."""
    session = await store.create_session("ag_test", name="e2e")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    app = _MockApp()
    impl = PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    adapter = EventAdapter(app)
    adapter.register_prompts_impl(impl)
    cb = InlineApprovalCallback(app, impl)

    # Fire the callback in a task; poll for the emitted request; dispatch
    # both events through the adapter as if the live bus had done so.
    task = asyncio.create_task(
        cb(rule_id="rl_x", action="bash", target="ls"),
    )

    # Poll the store for the emitted WIRE_PROMPT_REQUESTED event.
    requested_ev = await _poll_for_ui_event(
        store,
        thread_id,
        WIRE_PROMPT_REQUESTED,
    )
    prompt_id = requested_ev.data["payload"]["prompt_id"]

    # Dispatch the request event through the adapter → mounts widget.
    await adapter.handle(requested_ev)
    assert len(app.mount_calls) == 1

    # Simulate the user pressing `y` → append WIRE_PROMPT_ANSWERED.
    answered = await store.append_event(
        thread_id,
        _mk_ui_event(
            session_id=session.session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_ANSWERED,
            payload={"prompt_id": prompt_id, "response": "y"},
        ),
    )
    # Dispatch → resolves the pending future.
    await adapter.handle(answered)

    # The callback returns ALLOW.
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result is AuthzEffect.ALLOW
    assert app.cleared_calls == 1


async def test_answered_event_persists_with_logical_seq(
    store: SessionStore,
) -> None:
    """The emitted `WIRE_PROMPT_ANSWERED` lands in the JSONL with seq."""
    session = await store.create_session("ag_test", name="persist")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    answered = await store.append_event(
        thread_id,
        _mk_ui_event(
            session_id=session.session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_ANSWERED,
            payload={"prompt_id": "prm_test", "response": "y"},
        ),
    )
    # spec-003 stamps `logical_seq` on `data` inside the flock
    # critical section. Non-zero after append.
    seq = answered.data.get("logical_seq")
    assert isinstance(seq, int)
    assert seq > 0

    # And the store round-trips it.
    events = []
    async for ev in store.read_events(thread_id):
        events.append(ev)
    assert any(
        ev.type == EventType.UI_EVENT and ev.data.get("kind") == WIRE_PROMPT_ANSWERED
        for ev in events
    )


# --------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------


def _mk_ui_event(
    *,
    session_id: Any,
    thread_id: Any,
    kind: str,
    payload: dict[str, Any],
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": kind,
            "payload": payload,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _poll_for_ui_event(
    store: SessionStore,
    thread_id: Any,
    kind: str,
    timeout: float = 1.0,  # noqa: ASYNC109 — test polling deadline, not a cancel scope
) -> Event:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async for ev in store.read_events(thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") == kind:
                return ev
        await asyncio.sleep(0.01)
    msg = f"no UI event of kind {kind} within timeout"
    raise AssertionError(msg)
