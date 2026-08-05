"""feat-126 Test 3b — the adapter's auto-approve gate (negative cases).

Covers AC #5 at the `_handle_prompt_requested` mount site. The
`kind == "choice"` check there is LOAD-BEARING: the handler mounts an
`ApprovalBlock` for every `WIRE_PROMPT_REQUESTED` regardless of kind,
so the gate is the only thing preventing a future `Prompts.text()`
caller from being force-answered `"y"`.

Asserts, via a mock app whose `mount_approval_block` ACCEPTS the
`auto_approve` kwarg (the bridge-file `_MockApp` does not — its
TypeError fallback would silently drop the flag):

- `kind == "choice"` + app flag ON  → mounted with `auto_approve=True`.
- `kind == "text"`   + app flag ON  → mounted with `auto_approve=False`,
  and NO `WIRE_PROMPT_ANSWERED` is ever appended.
- `kind == "choice"` + app flag OFF → `auto_approve=False`.
- App without the attr (headless/partial fixtures) → `auto_approve=False`.
- Resume/terminal branch (bug-056 outcome snapshot) under flag ON →
  terminal mount, no `auto_approve=True`, no bridge, zero durable writes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _MockBlock:
    """Minimal `ApprovalBlock` stand-in; never resolves on its own."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._result: Any = None

    async def resolved(self) -> Any:
        await self._event.wait()
        return self._result


class _MockApp:
    """Records every mount call INCLUDING the feat-126 kwarg."""

    def __init__(self, *, auto_approve: bool | None = None) -> None:
        # `None` → do not set the attr at all (headless-fixture case).
        if auto_approve is not None:
            self.auto_approve = auto_approve
        self.mount_calls: list[dict[str, Any]] = []
        self._block = _MockBlock()

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
        *,
        parent_hint: Any = None,
        terminal: Any = None,
        auto_approve: bool = False,
    ) -> Any:
        self.mount_calls.append(
            {
                "rule_id": rule_id,
                "action": action,
                "target": target,
                "parent_hint": parent_hint,
                "terminal": terminal,
                "auto_approve": auto_approve,
            }
        )
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
    kind: str = "choice",
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
                "kind": kind,
                "prompt": "pick",
                "options": ["y", "a", "n", "d"],
                "source": {"kind": "tool", "id": "t1"},
                "rule_id": "rl_gate",
                "action": "bash",
                "target": "ls",
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _count_answered(
    store: SessionStore,
    thread_id: Any,
    prompt_id: str,
) -> int:
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
    return count


async def _dispatch(
    store: SessionStore,
    app: _MockApp,
    *,
    kind: str = "choice",
) -> tuple[EventAdapter, Any, str]:
    """Create a session, dispatch one `.requested`, return the handles."""
    session = await store.create_session("ag_test", name="feat126_gate")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
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
        kind=kind,
    )
    await adapter.handle(request)
    return adapter, thread_id, prompt_id


@pytest.mark.asyncio
async def test_choice_kind_with_flag_on_passes_auto_approve_true(
    store: SessionStore,
) -> None:
    """Happy gate: flag ON + `kind=="choice"` → widget gets the flag."""
    app = _MockApp(auto_approve=True)
    _, _, _ = await _dispatch(store, app, kind="choice")
    assert len(app.mount_calls) == 1
    assert app.mount_calls[0]["auto_approve"] is True


@pytest.mark.asyncio
async def test_text_kind_never_auto_approves(
    store: SessionStore,
) -> None:
    """AC #5: a `text()` prompt must NOT be force-answered `"y"`."""
    app = _MockApp(auto_approve=True)
    _, thread_id, prompt_id = await _dispatch(store, app, kind="text")
    assert len(app.mount_calls) == 1
    assert app.mount_calls[0]["auto_approve"] is False
    # Bounded negative wait: no durable answer may appear.
    await asyncio.sleep(0.3)
    assert await _count_answered(store, thread_id, prompt_id) == 0


@pytest.mark.asyncio
async def test_flag_off_passes_auto_approve_false(
    store: SessionStore,
) -> None:
    """Default OFF: `kind=="choice"` alone does not enable the mode."""
    app = _MockApp(auto_approve=False)
    _, _, _ = await _dispatch(store, app, kind="choice")
    assert app.mount_calls[0]["auto_approve"] is False


@pytest.mark.asyncio
async def test_app_without_attr_defaults_off(
    store: SessionStore,
) -> None:
    """Headless/partial fixtures without the attr → getattr guard holds."""
    app = _MockApp(auto_approve=None)  # attr not set at all
    _, _, _ = await _dispatch(store, app, kind="choice")
    assert app.mount_calls[0]["auto_approve"] is False


@pytest.mark.asyncio
async def test_resume_terminal_branch_never_auto_answers(
    store: SessionStore,
) -> None:
    """AC #5 resume gate: a replayed historical prompt writes nothing.

    Seeds the bug-056 replay-outcome snapshot for the prompt_id so the
    dispatch takes the terminal branch — which mounts a frozen row,
    never spawns the bridge, and never passes `auto_approve=True`.
    """
    app = _MockApp(auto_approve=True)
    session = await store.create_session("ag_test", name="feat126_resume")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    # bug-056 snapshot: this prompt already carries its historical
    # outcome in the replayed slice.
    adapter._replay_prompt_outcomes[prompt_id] = ("allowed", "✓ Allowed once")
    request = _mk_requested(
        session_id=session.session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
        kind="choice",
    )
    await adapter.handle(request)

    assert len(app.mount_calls) == 1
    call = app.mount_calls[0]
    # Terminal mount: carries the frozen outcome, never the auto flag.
    assert call["terminal"] == (True, "✓ Allowed once")
    assert call["auto_approve"] is False
    # Zero durable writes — the terminal branch returns before the
    # bridge is ever spawned.
    await asyncio.sleep(0.3)
    assert await _count_answered(store, thread_id, prompt_id) == 0
