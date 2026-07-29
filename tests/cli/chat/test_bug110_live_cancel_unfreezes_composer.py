"""bug-110 — Live-cancelled approval flips the mounted widget terminal.

Boots a real ``ChatApp`` under Pilot on a fresh (non-resume) session,
publishes a live ``WIRE_PROMPT_REQUESTED`` through the bus so the
interactive ``ApprovalBlock`` mounts and the composer freezes, then
publishes a ``WIRE_PROMPT_CANCELLED`` for the same ``prompt_id`` and
asserts the widget converged on the same terminal-resolved state the
resume path produces on boot — closing the freeze that pinned the
composer on "Awaiting approval…" forever (diagnosed from live session
``ses_0f542cb2-cea2-4dd1-bb12-ed80d3295ecd``).

Coverage:

- Test 1 (AC-1/2/3): live-cancel → block terminal + composer unfrozen +
  interrupted-styled row.
- Test 2 (AC-5): stacked live approvals — cancelling one keeps the
  composer frozen until the second resolves.
- Test 3 (AC-7): idempotent — duplicate ``.cancelled`` or ``.cancelled``
  after ``.answered`` is a no-op.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.composer import Composer
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixture — real store + bus, ChatApp booted fresh (no resume).
# Mirrors `test_bug056_resume_resolved_approvals`'s fixture shape.
# ---------------------------------------------------------------------------


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


async def _empty_stream(
    *_args: Any,
    **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    return
    yield  # pragma: no cover


class _MockEngine:
    """Engine mock — never runs a real turn, no eager-compaction."""

    execute_stream = staticmethod(_empty_stream)

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: Any,
        spec: Any,
        tool_schemas: list[dict[str, Any]] | None = None,
        trigger: str = "auto_resume",
    ) -> Any:
        return None


class _MockConfig:
    name = "TestAgent"
    agent_id = "ag_mock"
    model = None


class _MockBundle:
    def __init__(
        self,
        *,
        runtime: Any,
        config: Any,
        session_id: Any,
        thread_id: Any,
        agent_ref: str,
        root: Path,
        model_label: str,
        is_resume: bool,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.session_id = session_id
        self.thread_id = thread_id
        self.agent_ref = agent_ref
        self.root = root
        self.model_label = model_label
        self.is_resume = is_resume


class _Runtime:
    def __init__(self, store: SessionStore, root: Path) -> None:
        self.store = store
        self.authz = _MockAuthz()
        self.engine = _MockEngine()
        self.root = root
        self.bus = InProcessEventBus()


@pytest.fixture
async def store_env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="bug110")
    threads = await store.list_threads(session.session_id)
    try:
        yield {
            "backend": backend,
            "store": store,
            "session_id": session.session_id,
            "thread_id": threads[0].thread_id,
            "root": tmp_path,
        }
    finally:
        await backend.close()


def _mk_bundle(env: dict[str, Any], *, is_resume: bool) -> _MockBundle:
    return _MockBundle(
        runtime=_Runtime(env["store"], env["root"]),
        config=_MockConfig(),
        session_id=env["session_id"],
        thread_id=env["thread_id"],
        agent_ref="kolsch/agents/mock",
        root=env["root"],
        model_label="mock/test",
        is_resume=is_resume,
    )


# ---------------------------------------------------------------------------
# Event builders.
# ---------------------------------------------------------------------------


def _mk_requested(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    rule_id: str = "rl_probe",
    action: str = "bash",
    target: str = "ls /tmp",
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
                "prompt": f"Tool approval — {action} {target}",
                "options": ["y", "a", "n", "d"],
                "source": {"kind": "tool", "id": "t1"},
                "rule_id": rule_id,
                "action": action,
                "target": target,
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _mk_cancelled(
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
            "kind": WIRE_PROMPT_CANCELLED,
            "payload": {
                "prompt_id": prompt_id,
                "reason": "user_cancelled",
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _mk_answered(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    response: str,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_ANSWERED,
            "payload": {
                "prompt_id": prompt_id,
                "response": response,
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _resolved_row(block: ApprovalBlock) -> Any | None:
    from textual.widgets import Static

    for w in block.query(".resolved").results(Static):
        return w
    return None


async def _publish(bus: InProcessEventBus, store: SessionStore, thread_id: Any, ev: Event) -> None:
    await store.append_event(thread_id, ev)
    await bus.publish(ev)


async def _wait_for_block(pilot: Any, prompt_id: str) -> ApprovalBlock:
    """Wait for the live ApprovalBlock for `prompt_id` to mount."""
    for _ in range(60):
        await pilot.pause()
        blocks = list(pilot.app.query(ApprovalBlock))
        if blocks:
            return blocks[-1]
        await asyncio.sleep(0.02)
    msg = f"block never mounted for prompt_id={prompt_id!r}"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Test 1 — AC-1/2/3: live cancel → widget terminal, composer unfrozen,
# interrupted-styled row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_cancel_flips_block_terminal_and_unfreezes_composer(
    store_env: dict[str, Any],
) -> None:
    bundle = _mk_bundle(store_env, is_resume=False)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)

    async with app.run_test() as pilot:
        await pilot.pause()

        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        session_id = pilot.app.session.session_id

        # Publish a live requested prompt → interactive block mounts.
        pid = f"prm_live_{uuid4().hex[:8]}"
        await _publish(
            bus,
            store,
            thread_id,
            _mk_requested(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            ),
        )
        block = await _wait_for_block(pilot, pid)

        # Interactive, not resolved — composer frozen.
        composer = pilot.app.query_one(Composer)
        assert block.is_resolved is False
        for _ in range(40):
            await pilot.pause()
            if composer.awaiting_approval:
                break
            await asyncio.sleep(0.02)
        assert composer.awaiting_approval is True, (
            "composer never froze on the live approval"
        )

        # Now publish a live `.cancelled` for the same prompt_id.
        await _publish(
            bus,
            store,
            thread_id,
            _mk_cancelled(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            ),
        )

        # Wait for the terminal flip to land.
        for _ in range(60):
            await pilot.pause()
            if block.is_resolved:
                break
            await asyncio.sleep(0.02)

        # AC-1: block reports resolved via terminal state (NOT raw
        # `_resolved_event.set()` — that would wake the bridge and
        # append a duplicate WIRE_PROMPT_ANSWERED).
        assert block.is_resolved is True, (
            "live-cancelled block never reported resolved — bug-110 regressed"
        )
        assert block._terminal_state is not None, (
            "terminal flip went through `_resolved_event`, not `render_terminal` — AC-1"
        )
        # Guard: the raw event must remain UN-set so the bridge stays asleep.
        assert block._resolved_event.is_set() is False, (
            "`_resolved_event` was set on a live-cancelled block — would wake "
            "the bridge and append a duplicate WIRE_PROMPT_ANSWERED (AC-4)"
        )

        # AC-3: interrupted-styled row — reuses `-denied` classes and the
        # `⊘ Interrupted` footer (matches the resume path's interrupted branch).
        row = _resolved_row(block)
        assert row is not None, "no .resolved child mounted"
        assert "-denied" in row.classes, (
            f"expected `-denied` (interrupted styling), got {row.classes!r}"
        )
        assert str(row.content) == "⊘ Interrupted", (
            f"expected `⊘ Interrupted` footer, got {row.content!r}"
        )

        # AC-2: composer unfrozen (recompute observed the terminal flip).
        for _ in range(40):
            await pilot.pause()
            if not composer.awaiting_approval:
                break
            await asyncio.sleep(0.02)
        assert composer.awaiting_approval is False, (
            "composer stayed frozen after the live cancel — bug-110 core symptom"
        )


# ---------------------------------------------------------------------------
# Test 2 — AC-5: stacked-approval gate holds when one is cancelled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_cancel_keeps_composer_frozen_when_second_approval_pending(
    store_env: dict[str, Any],
) -> None:
    bundle = _mk_bundle(store_env, is_resume=False)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)

    async with app.run_test() as pilot:
        await pilot.pause()

        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        session_id = pilot.app.session.session_id

        # Mount two live approvals.
        pid_a = f"prm_a_{uuid4().hex[:8]}"
        pid_b = f"prm_b_{uuid4().hex[:8]}"
        for pid in (pid_a, pid_b):
            await _publish(
                bus,
                store,
                thread_id,
                _mk_requested(
                    session_id=session_id,
                    thread_id=thread_id,
                    prompt_id=pid,
                ),
            )
        blocks: list[ApprovalBlock] = []
        for _ in range(60):
            await pilot.pause()
            blocks = list(pilot.app.query(ApprovalBlock))
            if len(blocks) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(blocks) == 2, f"expected 2 blocks, got {len(blocks)}"

        composer = pilot.app.query_one(Composer)
        # Both live → gate frozen.
        assert composer.awaiting_approval is True

        # Cancel the first one via the live cancel path.
        await _publish(
            bus,
            store,
            thread_id,
            _mk_cancelled(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid_a,
            ),
        )
        for _ in range(60):
            await pilot.pause()
            unresolved = [b for b in blocks if not b.is_resolved]
            if len(unresolved) == 1:
                break
            await asyncio.sleep(0.02)

        unresolved = [b for b in blocks if not b.is_resolved]
        assert len(unresolved) == 1, (
            f"expected exactly one unresolved block, got {len(unresolved)}"
        )
        # AC-5: composer STAYS frozen (bug-056 stacked-approval gate intact).
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is True, (
            "composer freed after only ONE of two approvals resolved — "
            "bug-056 stacked-approval gate regressed"
        )


# ---------------------------------------------------------------------------
# Test 3 — AC-7: duplicate/late cancel is a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_and_late_cancel_are_no_ops(
    store_env: dict[str, Any],
) -> None:
    bundle = _mk_bundle(store_env, is_resume=False)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)

    async with app.run_test() as pilot:
        await pilot.pause()

        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        session_id = pilot.app.session.session_id

        pid = f"prm_dupe_{uuid4().hex[:8]}"
        await _publish(
            bus,
            store,
            thread_id,
            _mk_requested(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            ),
        )
        block = await _wait_for_block(pilot, pid)

        # First cancel — flips block terminal.
        await _publish(
            bus,
            store,
            thread_id,
            _mk_cancelled(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            ),
        )
        for _ in range(60):
            await pilot.pause()
            if block.is_resolved:
                break
            await asyncio.sleep(0.02)
        assert block.is_resolved is True

        row = _resolved_row(block)
        assert row is not None
        first_footer = str(row.content)

        # Second (duplicate) cancel — must be a no-op (no crash, no
        # double-render). Same for a `.answered` after the cancel.
        await _publish(
            bus,
            store,
            thread_id,
            _mk_cancelled(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            ),
        )
        await _publish(
            bus,
            store,
            thread_id,
            _mk_answered(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
                response="y",
            ),
        )
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.02)

        # Still one .resolved row, still terminal.
        rows = list(block.query(".resolved"))
        assert len(rows) == 1, (
            f"expected 1 resolved row after duplicate cancel, got {len(rows)}"
        )
        row = _resolved_row(block)
        assert row is not None
        assert str(row.content) == first_footer, (
            f"terminal footer mutated on duplicate cancel: {row.content!r}"
        )
        assert block.is_resolved is True
