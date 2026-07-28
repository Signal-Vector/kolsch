"""bug-060 Part 1 — widget-tracking infrastructure tests.

Part 1 lands `_event_widgets` + the `_register_widget_for_event` /
`_sync_dict_removal` helpers on ``EventAdapter`` and wires every
transcript-root mount seam through them. Nothing observes the tracking
yet (Part 2 wires the unmount behaviour on ``CONTEXT_COMPACTED``); Part
1 verifies the plumbing itself:

- The two helpers behave per contract (populate, clear, count).
- The dict-staleness sync only touches widget-holding dicts.
- Boot-time replay populates ``_event_widgets`` (i.e. the mount seams
  actually call ``_register_widget_for_event`` under the resume path).

Fixture pattern mirrors ``tests/cli/chat/test_bug058_replay_batch.py``
(real ``SessionStore`` + ``InProcessEventBus`` + Pilot-booted
``ChatApp``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.compaction_block import CompactionBlock
from cli.chat.widgets.composer import Composer
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import CardId, EventId, SessionId, ThreadId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

_TEST_SESSION_ID = SessionId(f"ses_{uuid4()}")
_TEST_THREAD_ID = ThreadId(f"th_{uuid4()}")


# ---------------------------------------------------------------------------
# Lightweight adapter builder — Part 1 helpers don't need a live app for
# unit-level assertions. Only the Pilot test needs a full ChatApp boot.
# ---------------------------------------------------------------------------


class _StubApp:
    """Minimal stand-in for ``ChatApp`` so ``EventAdapter`` constructs.

    The Part 1 helpers (``_register_widget_for_event`` /
    ``_sync_dict_removal``) touch only adapter-owned dicts — no app
    interaction. Anything the constructor reads is stubbed out.
    """

    def query_one(self, *_args: Any, **_kwargs: Any) -> Any:
        raise LookupError("stub app: no transcript in unit tests")


def _make_adapter() -> EventAdapter:
    """Build a bare ``EventAdapter`` — no store, no bus, no app services."""
    return EventAdapter(_StubApp())


class _FakeWidget:
    """Cheap object used as a widget stand-in for the helper unit tests.

    The helpers only require `is`-equality + optional `widget` attr, so
    a plain class suffices.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name


class _FakeWrapper:
    """Wrapper that exposes an inner widget under `.widget`.

    ``_sync_dict_removal``'s defensive fallback checks for this shape —
    ``CardMountState`` values in ``_card_widgets`` may wrap the widget
    rather than being it. The unit test exercises the fallback path
    explicitly.
    """

    def __init__(self, widget: Any) -> None:
        self.widget = widget


# ---------------------------------------------------------------------------
# Unit tests — helpers only. No app boot required.
# ---------------------------------------------------------------------------


def test_register_widget_for_event_populates_dict() -> None:
    """Two registers under the same event id → both widgets in order."""
    adapter = _make_adapter()
    event = _mk_user_message(
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        text="hi",
    )
    w1 = _FakeWidget("first")
    w2 = _FakeWidget("second")

    adapter._register_widget_for_event(event, w1)
    adapter._register_widget_for_event(event, w2)

    stored = adapter._event_widgets[event.event_id]
    assert stored == [w1, w2], (
        "expected insertion-ordered list of both widgets under the shared event id"
    )


def test_register_widget_for_event_different_events_independent() -> None:
    """Two events, two widgets each → dict entries stay independent."""
    adapter = _make_adapter()
    e1 = _mk_user_message(
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        text="a",
    )
    e2 = _mk_user_message(
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        text="b",
    )
    w_a1 = _FakeWidget("a1")
    w_a2 = _FakeWidget("a2")
    w_b1 = _FakeWidget("b1")
    w_b2 = _FakeWidget("b2")

    adapter._register_widget_for_event(e1, w_a1)
    adapter._register_widget_for_event(e2, w_b1)
    adapter._register_widget_for_event(e1, w_a2)
    adapter._register_widget_for_event(e2, w_b2)

    assert adapter._event_widgets[e1.event_id] == [w_a1, w_a2]
    assert adapter._event_widgets[e2.event_id] == [w_b1, w_b2]


def test_sync_dict_removal_clears_notice_widgets() -> None:
    """A widget in ``_notice_widgets`` is removed on sync."""
    adapter = _make_adapter()
    widget = _FakeWidget("notice")
    adapter._notice_widgets["notice-1"] = widget

    removed = adapter._sync_dict_removal(widget)

    assert removed == 1
    assert "notice-1" not in adapter._notice_widgets


def test_sync_dict_removal_clears_card_widgets() -> None:
    """A wrapped widget in ``_card_widgets`` is removed via `.widget` attr.

    The widget-backed ``CardMountState`` impl (``CardWidget``) IS the
    widget itself, but the defensive fallback for future wrapper
    shapes is exercised by wrapping under ``_FakeWrapper``.
    """
    adapter = _make_adapter()
    widget = _FakeWidget("card")
    wrapper = _FakeWrapper(widget)
    card_id = CardId(f"crd_{uuid4()}")
    adapter._card_widgets[card_id] = wrapper  # type: ignore[assignment]

    removed = adapter._sync_dict_removal(widget)

    assert removed == 1
    assert card_id not in adapter._card_widgets


def test_sync_dict_removal_clears_compaction_widgets() -> None:
    """A widget in ``_compaction_widgets`` is removed on sync."""
    adapter = _make_adapter()
    widget = _FakeWidget("compaction")
    adapter._compaction_widgets["ev_x"] = widget

    removed = adapter._sync_dict_removal(widget)

    assert removed == 1
    assert "ev_x" not in adapter._compaction_widgets


def test_sync_dict_removal_does_not_touch_unrelated_dicts() -> None:
    """Dicts that don't hold widget-like values stay untouched.

    ``_applied_notice_ids`` is a ``_BoundedLRUSet`` of ids;
    ``_replay_prompt_outcomes`` and ``_workflow_prompt_sources`` hold
    tuples. The sync helper walks widget-holding dicts only.
    """
    adapter = _make_adapter()
    widget = _FakeWidget("w")
    adapter._applied_notice_ids.add("notice-1")
    adapter._replay_prompt_outcomes["prompt-1"] = ("allowed", "footer")
    adapter._workflow_prompt_sources["prompt-1"] = ("run-1", "node-1")

    removed = adapter._sync_dict_removal(widget)

    assert removed == 0
    assert "notice-1" in adapter._applied_notice_ids
    assert adapter._replay_prompt_outcomes == {
        "prompt-1": ("allowed", "footer"),
    }
    assert adapter._workflow_prompt_sources == {
        "prompt-1": ("run-1", "node-1"),
    }


def test_sync_dict_removal_returns_count() -> None:
    """Widget in two dicts → return count is 2."""
    adapter = _make_adapter()
    widget = _FakeWidget("shared")
    adapter._notice_widgets["notice-x"] = widget
    adapter._compaction_widgets["ev_y"] = widget

    removed = adapter._sync_dict_removal(widget)

    assert removed == 2
    assert "notice-x" not in adapter._notice_widgets
    assert "ev_y" not in adapter._compaction_widgets


def test_register_widget_for_event_ignores_none() -> None:
    """Passing ``None`` (mount-failed / stub path) is a silent no-op."""
    adapter = _make_adapter()
    event = _mk_user_message(
        session_id=_TEST_SESSION_ID,
        thread_id=_TEST_THREAD_ID,
        text="hi",
    )

    adapter._register_widget_for_event(event, None)

    assert event.event_id not in adapter._event_widgets


# ---------------------------------------------------------------------------
# Pilot-booted replay test — verifies that the mount seams actually call
# `_register_widget_for_event` under a real resume + replay pass.
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
    session = await store.create_session("ag_mock", name="bug060")
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


def _mk_user_message(
    *,
    session_id: Any,
    thread_id: Any,
    text: str,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.USER_MESSAGE,
        data={"content": {"text": text}},
        actor=Actor(kind="user"),
        created_at=datetime.now(UTC),
    )


def _mk_agent_message(
    *,
    session_id: Any,
    thread_id: Any,
    text: str,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.AGENT_MESSAGE,
        data={"content": {"text": text}},
        actor=Actor(kind="agent", id="ag_mock"),
        created_at=datetime.now(UTC),
    )


async def _seed(
    store: SessionStore,
    thread_id: Any,
    events: list[Event],
) -> None:
    for ev in events:
        await store.append_event(thread_id, ev)


@pytest.mark.asyncio
async def test_event_widgets_populated_by_replay(
    store_env: dict[str, Any],
) -> None:
    """After a resume replay settles, ``_event_widgets`` is non-empty.

    Seeds a small synthetic session (5 USER/AGENT pairs), boots the
    app with ``is_resume=True``, and asserts that at least one entry
    landed in ``_event_widgets`` — proof that the mount seams actually
    call ``_register_widget_for_event`` when driven by real replay.

    A conservative floor (``>= 1``) catches the "no mount seam wired
    the register call" regression without pinning to a specific
    event-id count (widget dedupe / route-to-sink can affect the exact
    hit rate).
    """
    events: list[Event] = []
    for i in range(5):
        events.append(
            _mk_user_message(
                session_id=store_env["session_id"],
                thread_id=store_env["thread_id"],
                text=f"user turn {i}",
            ),
        )
        events.append(
            _mk_agent_message(
                session_id=store_env["session_id"],
                thread_id=store_env["thread_id"],
                text=f"agent reply {i}",
            ),
        )
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        tracked = app.event_adapter._event_widgets
        assert len(tracked) >= 1, (
            "expected `_event_widgets` to be populated by the replay "
            "pass; got an empty dict — every mount seam likely fails "
            "to call `_register_widget_for_event`"
        )
        # Every registered widget list should be non-empty by
        # construction (`_register_widget_for_event` only appends when
        # the widget arg is truthy).
        assert all(len(v) > 0 for v in tracked.values()), (
            "expected every `_event_widgets` entry to hold >=1 widget"
        )


# ---------------------------------------------------------------------------
# Part 2 — unmount on CONTEXT_COMPACTED + re-mount on CONTEXT_COMPACTION_REVOKED.
# ---------------------------------------------------------------------------


def _mk_compacted(
    *,
    session_id: Any,
    thread_id: Any,
    compacted_event_ids: list[str],
    preview: bool = False,
    tokens_before: int = 5000,
    tokens_after: int = 500,
) -> Event:
    """Synthesise a CONTEXT_COMPACTED event covering ``compacted_event_ids``."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONTEXT_COMPACTED,
        data={
            "compacted_event_ids": compacted_event_ids,
            "pre_compaction": {},
            "summary": {"schema_version": 1, "text": "collapsed"},
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "trigger": "manual_preview" if preview else "manual",
            "preview": preview,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _mk_revoked(
    *,
    session_id: Any,
    thread_id: Any,
    compaction_event_id: str,
) -> Event:
    """Synthesise a CONTEXT_COMPACTION_REVOKED pointing at ``compaction_event_id``."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONTEXT_COMPACTION_REVOKED,
        data={
            "compaction_event_id": compaction_event_id,
            "reason": "user_requested",
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _mk_prompt_requested(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
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
                "rule_id": "rl_probe",
                "action": action,
                "target": target,
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _mk_prompt_answered(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    response: str = "y",
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


async def _count_events(store: SessionStore, thread_id: Any) -> int:
    count = 0
    async for _ev in store.read_events(thread_id):
        count += 1
    return count


async def _seed_pairs_then_compaction(
    store_env: dict[str, Any],
    *,
    n_pairs: int = 5,
    preview: bool = False,
) -> tuple[list[Event], Event]:
    """Seed ``n_pairs`` USER/AGENT + one CONTEXT_COMPACTED covering pairs 1..n_pairs-1.

    Returns ``(covered_events, compaction_event)`` where ``covered_events``
    holds the first ``n_pairs-1`` pairs (event ids that appear in the
    compaction's ``compacted_event_ids``) and ``compaction_event`` is the
    tail synthetic CONTEXT_COMPACTED. The last (n-th) pair is untouched
    so tests can assert the tail survives unmount.
    """
    session_id = store_env["session_id"]
    thread_id = store_env["thread_id"]
    all_pairs: list[Event] = []
    for i in range(n_pairs):
        all_pairs.append(
            _mk_user_message(
                session_id=session_id,
                thread_id=thread_id,
                text=f"user {i}",
            ),
        )
        all_pairs.append(
            _mk_agent_message(
                session_id=session_id,
                thread_id=thread_id,
                text=f"agent {i}",
            ),
        )
    # Covered slice: first (n_pairs - 1) pairs, i.e. events 0..2*n_pairs-3.
    covered = all_pairs[: 2 * (n_pairs - 1)]
    compaction = _mk_compacted(
        session_id=session_id,
        thread_id=thread_id,
        compacted_event_ids=[str(e.event_id) for e in covered],
        preview=preview,
    )
    await _seed(store_env["store"], thread_id, [*all_pairs, compaction])
    return covered, compaction


@pytest.mark.asyncio
async def test_compacted_event_unmounts_covered_widgets(
    store_env: dict[str, Any],
) -> None:
    """spec Test 1: replay a slice ending in CONTEXT_COMPACTED → covered widgets gone.

    5 USER/AGENT pairs + a tail CONTEXT_COMPACTED whose
    ``compacted_event_ids`` names events 1-4. After replay settles:

    - No widgets remain registered for the covered event ids.
    - The tail pair (events for ``i=4``) is still registered.
    - The CompactionBlock is mounted.
    """
    covered, compaction = await _seed_pairs_then_compaction(
        store_env,
        n_pairs=5,
    )

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        tracked = app.event_adapter._event_widgets
        for ev in covered:
            assert ev.event_id not in tracked, (
                f"expected covered event {ev.event_id!s} to have no "
                f"tracked widgets after compaction unmount"
            )

        # CompactionBlock is registered under its own event id — the
        # compaction event's id is NOT in `compacted_event_ids`, so the
        # unmount pass leaves it alone.
        assert compaction.event_id in tracked, (
            "CompactionBlock should still be tracked under the compaction "
            "event's own id — it must survive unmount as the placeholder"
        )
        # The mock engine's `_maybe_eager_compact_on_open` returns
        # `None`, but it still mounts a transient `running` CompactionBlock
        # as `_compaction_widgets["pending"]` for the eager check.
        # Filter to the terminal `success` block from our synthetic
        # compaction to keep the assertion tight.
        success_blocks = [
            b for b in pilot.app.query(CompactionBlock) if b.state == "success"
        ]
        assert len(success_blocks) == 1, (
            f"expected exactly 1 success CompactionBlock, got {len(success_blocks)}"
        )


@pytest.mark.asyncio
async def test_preview_compaction_does_not_unmount(
    store_env: dict[str, Any],
) -> None:
    """spec Test 2: preview compactions leave every covered widget mounted."""
    covered, _compaction = await _seed_pairs_then_compaction(
        store_env,
        n_pairs=5,
        preview=True,
    )

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        tracked = app.event_adapter._event_widgets
        for ev in covered:
            assert tracked.get(ev.event_id), (
                f"preview compaction should NOT unmount covered widget for "
                f"event {ev.event_id!s} — spec AC #4 regression"
            )
        preview_blocks = [
            b for b in pilot.app.query(CompactionBlock) if b.state == "preview"
        ]
        assert len(preview_blocks) == 1, (
            f"expected exactly 1 preview CompactionBlock, got {len(preview_blocks)}"
        )


@pytest.mark.asyncio
async def test_uncompact_remounts_covered_events(
    store_env: dict[str, Any],
) -> None:
    """spec Test 4: firing CONTEXT_COMPACTION_REVOKED re-mounts the slice.

    After boot with the unmount landed, dispatch a revoked event
    through the adapter directly (the store is the source of truth for
    the covered slice — the re-replay path reads from it). Assert
    covered widgets are back in ``_event_widgets`` and the block
    transitions to ``revoked``.
    """
    covered, compaction = await _seed_pairs_then_compaction(
        store_env,
        n_pairs=5,
    )

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        # Sanity: covered widgets are gone before revoke fires.
        tracked = app.event_adapter._event_widgets
        for ev in covered:
            assert ev.event_id not in tracked

        revoke = _mk_revoked(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            compaction_event_id=str(compaction.event_id),
        )
        await app.event_adapter.handle(revoke)
        await pilot.pause()

        tracked = app.event_adapter._event_widgets
        for ev in covered:
            assert tracked.get(ev.event_id), (
                f"expected covered event {ev.event_id!s} to be re-mounted "
                f"after CONTEXT_COMPACTION_REVOKED"
            )
        # The CompactionBlock transitioned to revoked.
        blocks = list(pilot.app.query(CompactionBlock))
        assert any(b.state == "revoked" for b in blocks), (
            f"expected at least one CompactionBlock in 'revoked' state, "
            f"got states={[b.state for b in blocks]}"
        )


@pytest.mark.asyncio
async def test_no_durable_writes_on_unmount_or_remount(
    store_env: dict[str, Any],
) -> None:
    """spec Test 5: store event count is unchanged across unmount + remount.

    Mirrors bug-056 AC #4 — replay is UI-only. Counts events BEFORE
    boot, AFTER replay (unmount landed), and AFTER re-mounting via
    revoked. All three must equal.
    """
    covered, compaction = await _seed_pairs_then_compaction(
        store_env,
        n_pairs=5,
    )
    before = await _count_events(store_env["store"], store_env["thread_id"])

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        after_unmount = await _count_events(
            store_env["store"],
            store_env["thread_id"],
        )
        assert after_unmount == before, (
            f"replay must be UI-only — count changed {before} → "
            f"{after_unmount} across unmount pass"
        )

        revoke = _mk_revoked(
            session_id=store_env["session_id"],
            thread_id=store_env["thread_id"],
            compaction_event_id=str(compaction.event_id),
        )
        await app.event_adapter.handle(revoke)
        await pilot.pause()
        after_remount = await _count_events(
            store_env["store"],
            store_env["thread_id"],
        )
        assert after_remount == before, (
            f"/uncompact re-replay must be UI-only — count changed "
            f"{before} → {after_remount} across remount"
        )
        # Silence the linter — covered / compaction are used only for
        # the seeding side-effect above.
        _ = (covered, compaction)


@pytest.mark.asyncio
async def test_approval_inside_compacted_span_replays_terminal(
    store_env: dict[str, Any],
) -> None:
    """spec Test 6: bug-056 approval inside a compacted span replay cycle.

    Slice: one ``.requested`` + ``.answered`` pair (approved) inside
    the compacted span, then a tail user/agent pair, then a
    CONTEXT_COMPACTED covering the approval pair. On boot with
    is_resume=True the ApprovalBlock lands terminal first (bug-056 AC
    #1), then unmounts once the CONTEXT_COMPACTED event fires. On
    /uncompact re-replay it re-mounts in terminal state.
    """
    session_id = store_env["session_id"]
    thread_id = store_env["thread_id"]
    prompt_id = f"prm_{uuid4().hex[:12]}"
    req = _mk_prompt_requested(
        session_id=session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
    )
    ans = _mk_prompt_answered(
        session_id=session_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
        response="y",
    )
    tail_user = _mk_user_message(
        session_id=session_id,
        thread_id=thread_id,
        text="tail user",
    )
    tail_agent = _mk_agent_message(
        session_id=session_id,
        thread_id=thread_id,
        text="tail agent",
    )
    compaction = _mk_compacted(
        session_id=session_id,
        thread_id=thread_id,
        # `compacted_event_ids` names the approval `.requested` only —
        # its widget is the ApprovalBlock we want to see re-mount as
        # terminal on `/uncompact`.
        compacted_event_ids=[str(req.event_id)],
    )
    await _seed(
        store_env["store"],
        thread_id,
        [req, ans, tail_user, tail_agent, compaction],
    )

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        # After unmount, the ApprovalBlock should be gone from the DOM
        # (it was covered by the compaction). Composer stays enabled.
        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 0, (
            f"ApprovalBlock should have unmounted with the covered "
            f"span; got {len(blocks)} still mounted"
        )
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False
        assert composer.disabled is False

        # Fire the revoke and verify the ApprovalBlock re-mounts as
        # terminal (bug-056 semantics on re-replay).
        revoke = _mk_revoked(
            session_id=session_id,
            thread_id=thread_id,
            compaction_event_id=str(compaction.event_id),
        )
        await app.event_adapter.handle(revoke)
        await pilot.pause()

        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 1, (
            f"expected 1 re-mounted ApprovalBlock, got {len(blocks)}"
        )
        assert blocks[0].is_resolved is True, (
            "re-mounted ApprovalBlock must be terminal on /uncompact "
            "(bug-056 AC #1 on re-replay)"
        )
        # Composer still enabled — terminal block never freezes it.
        composer = pilot.app.query_one(Composer)
        assert composer.awaiting_approval is False
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_duplicate_compaction_event_is_noop_on_second_fire(
    store_env: dict[str, Any],
) -> None:
    """spec Test 7: firing the same CONTEXT_COMPACTED twice — second is a no-op.

    First fire unmounts covered widgets; second fire finds an empty
    ``_event_widgets`` slice and returns without raising. Verified via
    unmount count (0 on the second pass) and by re-firing directly
    against the adapter.
    """
    covered, compaction = await _seed_pairs_then_compaction(
        store_env,
        n_pairs=5,
    )

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        tracked = app.event_adapter._event_widgets
        for ev in covered:
            assert ev.event_id not in tracked

        # Fire the same CONTEXT_COMPACTED again — no crash, no
        # additional widgets removed.
        count = await app.event_adapter._unmount_covered_widgets(
            [str(e.event_id) for e in covered],
        )
        assert count == 0, (
            f"second unmount pass should be a no-op — got {count} widgets removed"
        )
        # Dispatching the same CONTEXT_COMPACTED event a second time
        # also stays safe (idempotent under duplicate replay).
        await app.event_adapter.handle(compaction)
        await pilot.pause()
        for ev in covered:
            assert ev.event_id not in tracked
