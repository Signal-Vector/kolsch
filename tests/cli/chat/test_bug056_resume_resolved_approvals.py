"""bug-056 — Resumed sessions render historical approvals as terminal.

Boots a real ``ChatApp`` under Pilot with a session store pre-seeded
with historical ``WIRE_PROMPT_REQUESTED`` + ``WIRE_PROMPT_ANSWERED``
events, then asserts the replay pre-pass renders each approval in its
terminal (resolved / interrupted) state, does NOT freeze the composer,
and appends **zero** new events to the durable log.

Uses the same live-bus fixture shape as
``tests/cli/chat/test_approval_flow_live.py`` (real ``SessionStore`` +
``InProcessEventBus``), and the resume pattern in
``tests/cli/chat/test_chat_eager_resume.py`` (boot with
``is_resume=True``).

Coverage:

- Test 1 (AC #1-#2): answered → resolved-allowed row + composer free.
- Test 2 (AC #3, #7): outcome fidelity for y / n / a+rule / d+rule.
- Test 3 (AC #4): store event count unchanged across the resume.
- Test 4 (AC #6, #8): dangling + mixed slice; all terminal, composer free.
- Test 6 (AC #10): stacked live approvals gate correctly (recompute).
- Test 7 (AC #13): new post-resume prompt is fully interactive.
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
# Fixture — real store + bus, ChatApp not yet booted (caller sets is_resume).
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
    yield  # type: ignore[unreachable]  # pragma: no cover


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
    """Bring up a real store + session; caller writes events, then boots."""
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="bug056")
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


def _mk_bundle(
    env: dict[str, Any],
    *,
    is_resume: bool,
) -> _MockBundle:
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
# Event builders — mirror `test_approval_flow_live`'s payload shapes.
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


def _mk_answered(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    response: str,
    rule: dict[str, Any] | None = None,
) -> Event:
    payload: dict[str, Any] = {
        "prompt_id": prompt_id,
        "response": response,
    }
    if rule is not None:
        payload["rule"] = rule
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_ANSWERED,
            "payload": payload,
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
    """`.cancelled` payload has no response/answer key — see spec."""
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


async def _seed(store: SessionStore, thread_id: Any, events: list[Event]) -> None:
    for ev in events:
        await store.append_event(thread_id, ev)


async def _count_events(store: SessionStore, thread_id: Any) -> int:
    count = 0
    async for _ev in store.read_events(thread_id):
        count += 1
    return count


def _resolved_row(block: ApprovalBlock) -> Any | None:
    """Return the frozen resolved Static (if mounted)."""
    from textual.widgets import Static

    for w in block.query(".resolved").results(Static):
        return w
    return None


# ---------------------------------------------------------------------------
# Test 1 — Answered approvals replay resolved + composer free.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_renders_answered_approval_resolved_and_frees_composer(
    store_env: dict[str, Any],
) -> None:
    prompt_id = f"prm_{uuid4().hex[:12]}"
    events = [
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=prompt_id,
        ),
        _mk_answered(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=prompt_id,
            response="y",
        ),
    ]
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=True)

    async with app.run_test() as pilot:
        await pilot.pause()

        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 1, f"expected 1 mounted block, got {len(blocks)}"
        block = blocks[0]

        # Terminal state — reports as resolved from birth.
        assert block.is_resolved is True

        # Frozen resolved row is mounted with the -allowed class.
        row = _resolved_row(block)
        assert row is not None, "no .resolved child mounted"
        assert "-allowed" in row.classes

        # Composer is enabled (AC #2).
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False
        assert composer.disabled is False


# ---------------------------------------------------------------------------
# Test 2 — Outcome fidelity for y/n/a+rule/d+rule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "rule", "expected_footer", "expected_class"),
    [
        ("y", None, "✓ Allowed once", "-allowed"),
        ("n", None, "✗ Denied once", "-denied"),
        (
            "a",
            {"what": "bash", "pattern": "ls*", "effect": "allow"},
            "✓ Rule saved (allow)",
            "-allowed",
        ),
        (
            "d",
            {"what": "bash", "pattern": "rm*", "effect": "deny"},
            "✗ Rule saved (deny)",
            "-denied",
        ),
    ],
    ids=["allowed-once", "denied-once", "rule-saved-allow", "rule-saved-deny"],
)
async def test_resume_outcome_footers(
    store_env: dict[str, Any],
    response: str,
    rule: dict[str, Any] | None,
    expected_footer: str,
    expected_class: str,
) -> None:
    prompt_id = f"prm_{uuid4().hex[:12]}"
    events = [
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=prompt_id,
        ),
        _mk_answered(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=prompt_id,
            response=response,
            rule=rule,
        ),
    ]
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=True)

    async with app.run_test() as pilot:
        await pilot.pause()

        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 1
        block = blocks[0]
        row = _resolved_row(block)
        assert row is not None
        assert expected_class in row.classes, (
            f"expected class {expected_class!r} on row.classes={row.classes!r}"
        )
        # Static's `content` (Rich renderable) round-trips through `str`.
        assert str(row.content) == expected_footer, (
            f"expected footer {expected_footer!r}, got {row.content!r}"
        )

        # Composer stays enabled through every outcome.
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False


# ---------------------------------------------------------------------------
# Test 3 — Zero durable writes on resume.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_appends_no_new_events(
    store_env: dict[str, Any],
) -> None:
    prompt_ids = [f"prm_{uuid4().hex[:12]}" for _ in range(3)]
    seed: list[Event] = []
    for pid in prompt_ids:
        seed.append(
            _mk_requested(
                session_id=store_env["session_id"],
                thread_id=store_env["thread_id"],
                prompt_id=pid,
            ),
        )
        seed.append(
            _mk_answered(
                session_id=store_env["session_id"],
                thread_id=store_env["thread_id"],
                prompt_id=pid,
                response="y",
            ),
        )
    await _seed(store_env["store"], store_env["thread_id"], seed)
    before = await _count_events(store_env["store"], store_env["thread_id"])

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=True)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Give the pilot enough time that any latent bridge task would
        # have fired an append.
        for _ in range(10):
            await pilot.pause()
            await asyncio.sleep(0.02)

        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == len(prompt_ids)
        for b in blocks:
            assert b.is_resolved is True

        after = await _count_events(store_env["store"], store_env["thread_id"])
        assert after == before, (
            f"resume appended {after - before} new event(s); expected 0"
        )


# ---------------------------------------------------------------------------
# Test 4 — Dangling + mixed session render terminal, composer free.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_dangling_and_mixed(
    store_env: dict[str, Any],
) -> None:
    allowed_pid = f"prm_a_{uuid4().hex[:8]}"
    denied_pid = f"prm_d_{uuid4().hex[:8]}"
    dangling_pid = f"prm_x_{uuid4().hex[:8]}"

    seed: list[Event] = [
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=allowed_pid,
        ),
        _mk_answered(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=allowed_pid,
            response="y",
        ),
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=denied_pid,
        ),
        _mk_answered(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=denied_pid,
            response="n",
        ),
        # Dangling — requested but never answered/cancelled.
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=dangling_pid,
        ),
    ]
    await _seed(store_env["store"], store_env["thread_id"], seed)
    before = await _count_events(store_env["store"], store_env["thread_id"])

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=True)

    async with app.run_test() as pilot:
        await pilot.pause()

        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 3

        # Every block is terminal (non-interactive).
        rows_by_footer: dict[str, Any] = {}
        for b in blocks:
            assert b.is_resolved is True
            row = _resolved_row(b)
            assert row is not None
            rows_by_footer[str(row.content)] = row

        # Allowed, denied, interrupted are all present with correct
        # footers.
        assert "✓ Allowed once" in rows_by_footer
        assert "✗ Denied once" in rows_by_footer
        assert "⊘ Interrupted" in rows_by_footer

        # Composer is free (AC #6, #8).
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False

        # Zero durable writes on the resume — dangling handling is UI-only
        # (the `_scan_dangling_prompts` synth path never fires on resume).
        after = await _count_events(store_env["store"], store_env["thread_id"])
        assert after == before


# ---------------------------------------------------------------------------
# Test 6 — Stacked live approvals gate correctly (recompute, AC #10).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_live_approvals_gate(
    store_env: dict[str, Any],
) -> None:
    """Fresh session — mount two LIVE approvals; resolve one; assert gate."""
    bundle = _mk_bundle(store_env, is_resume=False)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)

    async with app.run_test() as pilot:
        await pilot.pause()

        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        session_id = pilot.app.session.session_id

        # Mount two live requested prompts.
        pid_a = f"prm_a_{uuid4().hex[:8]}"
        pid_b = f"prm_b_{uuid4().hex[:8]}"
        for pid in (pid_a, pid_b):
            ev = _mk_requested(
                session_id=session_id,
                thread_id=thread_id,
                prompt_id=pid,
            )
            await store.append_event(thread_id, ev)
            await bus.publish(ev)

        # Wait for both blocks to mount.
        blocks: list[ApprovalBlock] = []
        for _ in range(40):
            await pilot.pause()
            blocks = list(pilot.app.query(ApprovalBlock))
            if len(blocks) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(blocks) == 2, f"expected 2 mounted blocks, got {len(blocks)}"

        composer = pilot.app.query_one(Composer)
        # Both live → gate is frozen.
        assert composer.awaiting_approval is True

        # Resolve the currently-focused one (whichever it is) with `y`.
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause()
            unresolved = [b for b in blocks if not b.is_resolved]
            if len(unresolved) == 1:
                break
            await asyncio.sleep(0.05)

        # One resolved, one still open — gate MUST remain frozen. The
        # pre-bug-056 single-boolean toggle would have cleared here.
        composer = pilot.app.query_one(Composer)
        unresolved = [b for b in blocks if not b.is_resolved]
        assert len(unresolved) == 1, (
            f"expected exactly one unresolved block, got {len(unresolved)}"
        )
        assert composer.awaiting_approval is True, (
            "composer freed too early — the stacked-approval gate regressed"
        )

        # Resolve the second — gate MUST free.
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause()
            if all(b.is_resolved for b in blocks):
                break
            await asyncio.sleep(0.05)

        assert all(b.is_resolved for b in blocks)
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False, (
            "composer never freed after every live approval resolved"
        )


# ---------------------------------------------------------------------------
# Test 7 — A new approval AFTER resume is fully interactive (AC #13).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_approval_after_resume_is_interactive(
    store_env: dict[str, Any],
) -> None:
    """Historical block terminal; a fresh post-resume request stays live."""
    historical_pid = f"prm_h_{uuid4().hex[:8]}"
    seed = [
        _mk_requested(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=historical_pid,
        ),
        _mk_answered(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            prompt_id=historical_pid,
            response="y",
        ),
    ]
    await _seed(store_env["store"], store_env["thread_id"], seed)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=True)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Historical block is terminal.
        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 1
        historical = blocks[0]
        assert historical.is_resolved is True

        # Composer starts free.
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False

        # Now publish a NEW live requested prompt through the bus (the
        # same seam bug-020 uses in production).
        live_pid = f"prm_live_{uuid4().hex[:8]}"
        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        session_id = pilot.app.session.session_id
        live_req = _mk_requested(
            session_id=session_id,
            thread_id=thread_id,
            prompt_id=live_pid,
        )
        await store.append_event(thread_id, live_req)
        await bus.publish(live_req)

        # The new block mounts; it is INTERACTIVE (not resolved from
        # birth); the composer freezes.
        new_block: ApprovalBlock | None = None
        for _ in range(40):
            await pilot.pause()
            blocks = list(pilot.app.query(ApprovalBlock))
            live_ones = [b for b in blocks if b is not historical]
            if live_ones:
                new_block = live_ones[0]
                break
            await asyncio.sleep(0.05)
        assert new_block is not None, "new post-resume block never mounted"
        assert new_block.is_resolved is False, (
            "new post-resume prompt was misclassified as historical — AC #13 regressed"
        )

        # The composer freezes on the live block.
        composer = pilot.app.query_one(Composer)
        for _ in range(40):
            await pilot.pause()
            if composer.awaiting_approval:
                break
            await asyncio.sleep(0.05)
        assert composer.awaiting_approval is True, (
            "composer did not freeze on the new live approval"
        )

        # Answer the live prompt with `y`; the composer must free.
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause()
            if not composer.awaiting_approval:
                break
            await asyncio.sleep(0.05)
        assert composer.awaiting_approval is False, (
            "composer did not free after the live prompt resolved"
        )
        assert new_block.is_resolved is True
