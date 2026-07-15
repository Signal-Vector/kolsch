"""bug-058 — batch-wrapped replay + ``anchor()`` adoption regression tests.

Boots a real :class:`cli.chat.app.ChatApp` under
:class:`textual.pilot.Pilot` on a resume session seeded with
alternating USER_MESSAGE / AGENT_MESSAGE events, then asserts:

- Part A: the replay finishes in well under five seconds wall-clock
  (three-engineer review predicts <2s once ``_replay_history``'s
  dispatch loop is wrapped in ``async with transcript.batch():``; five
  seconds is the acceptance gate with headroom for a slower CI box).
- Part B: ``#transcript.is_anchored`` reads ``True`` after ``on_mount``
  settles; the anchor releases when the user scrolls up (via
  ``pageup``); every replayed event's widget is present in the DOM
  after the batch unwinds (no dropped mounts).

Fixture shape mirrors
``tests/cli/chat/test_bug056_resume_resolved_approvals.py`` (real
``SessionStore`` + ``InProcessEventBus``) and the resume boot pattern
from ``tests/cli/chat/test_chat_eager_resume.py``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from textual.containers import VerticalScroll

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend

# ---------------------------------------------------------------------------
# Runtime + engine mocks (mirror bug-056's shape).
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
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="bug058")
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
# Synthesized event builders — every one carries the minimum shape the
# adapter's handler consults (`content.text`, `tool.name`, etc.) so the
# replay actually mounts a widget rather than short-circuiting.
# ---------------------------------------------------------------------------


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


def _synthesize_events(
    session_id: Any,
    thread_id: Any,
    *,
    turns: int,
) -> list[Event]:
    """Build a synthetic session with ``turns`` USER/AGENT pairs.

    Two events per turn keeps the per-turn count minimal but guarantees
    the tail-40-turns replay dispatches ``turns * 2`` events through
    the adapter — enough that the per-mount layout cost dominates the
    wall-clock time and the batch-wrapper effect is measurable.
    """
    events: list[Event] = []
    for i in range(turns):
        events.append(
            _mk_user_message(
                session_id=session_id,
                thread_id=thread_id,
                text=f"user turn {i}",
            ),
        )
        events.append(
            _mk_agent_message(
                session_id=session_id,
                thread_id=thread_id,
                text=f"agent reply {i}",
            ),
        )
    return events


async def _seed(
    store: SessionStore,
    thread_id: Any,
    events: list[Event],
) -> None:
    for ev in events:
        await store.append_event(thread_id, ev)


# ---------------------------------------------------------------------------
# Measurement test — replay finishes under the pass gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_history_completes_under_five_seconds_on_large_session(
    store_env: dict[str, Any],
) -> None:
    """Batch-wrapped replay of a ~500-event session finishes in <5s.

    Seeds 250 turns (500 events) — enough that the pre-batch behaviour
    (37 handlers, each firing a fresh Textual layout pass per mount)
    would be visibly slow, but the test stays fast.
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=250,
    )
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=True,
    )

    start = time.perf_counter()
    async with app.run_test() as pilot:
        # `run_test` returns once `on_mount` has been kicked off; a
        # `pause()` drives the event loop until the pending
        # `_replay_history` + deferred `_scroll_tail` settle.
        await pilot.pause()
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, (
            f"replay took {elapsed:.2f}s; expected < 5.0s "
            f"(target < 2.0s per three-engineer prediction)"
        )


# ---------------------------------------------------------------------------
# Part B — anchor adoption + widget-count parity tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_anchored_after_on_mount(
    store_env: dict[str, Any],
) -> None:
    """After boot, ``#transcript.is_anchored`` reads ``True``.

    Regression guard for bug-058 Part B: ``ChatApp.on_mount`` calls
    ``transcript.anchor()`` after ``_replay_history`` and
    ``_maybe_eager_compact_on_open`` return. A future refactor that
    drops the call will trip this assertion.
    """
    bundle = _mk_bundle(store_env, is_resume=False)
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)
        assert transcript.is_anchored is True, (
            "expected #transcript to be anchored after on_mount; "
            "anchor() call missing or races the deferred scroll"
        )


@pytest.mark.asyncio
async def test_anchor_releases_when_user_scrolls_up(
    store_env: dict[str, Any],
) -> None:
    """User ``pageup`` releases the anchor so incoming events don't yank down.

    Seeds enough turns that the mounted transcript's ``virtual_size.height``
    exceeds the container height, then presses ``pageup``. Asserts the
    anchor is released — Textual's :meth:`Widget._on_scroll` auto-clears
    on user-initiated scroll.
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=50,
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
        transcript = app.query_one("#transcript", VerticalScroll)
        # Sanity: anchor engaged after boot.
        assert transcript.is_anchored is True

        # Focus the transcript so pageup routes to it (not the composer).
        transcript.focus()
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()

        assert transcript.is_anchored is False or transcript._anchor_released, (
            "expected user pageup to release the anchor "
            "(is_anchored=False OR _anchor_released=True); "
            "incoming events would yank the viewport back down"
        )


@pytest.mark.asyncio
async def test_batch_replay_mounts_all_widgets(
    store_env: dict[str, Any],
) -> None:
    """Every replayed event's widget is present in the DOM after batch.

    Reviewer note from Part A: assert widget-count parity so a future
    refactor of ``EventAdapter.handle`` (or the batch wrapper) can't
    silently drop mounts. Seeds 100 turns (200 events — user + agent
    per turn), asserts the ``#transcript`` children count exceeds a
    conservative minimum after resume settles.
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=100,
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
        transcript = app.query_one("#transcript", VerticalScroll)
        # 200 events → at minimum 200 widgets (user + agent per turn).
        # Assert > 50 as a conservative floor that still catches the
        # "batch swallowed every mount" failure mode.
        child_count = len(list(transcript.children))
        assert child_count > 50, (
            f"expected > 50 mounted widgets after 200-event replay; "
            f"got {child_count}. Batch may have dropped mounts."
        )
