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

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.event_adapter import EventAdapter
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import CardId, EventId, SessionId, ThreadId
from store import SessionStore, SqliteBackend


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
        session_id=_TEST_SESSION_ID, thread_id=_TEST_THREAD_ID, text="hi",
    )
    w1 = _FakeWidget("first")
    w2 = _FakeWidget("second")

    adapter._register_widget_for_event(event, w1)
    adapter._register_widget_for_event(event, w2)

    stored = adapter._event_widgets[event.event_id]
    assert stored == [w1, w2], (
        "expected insertion-ordered list of both widgets under the "
        "shared event id"
    )


def test_register_widget_for_event_different_events_independent() -> None:
    """Two events, two widgets each → dict entries stay independent."""
    adapter = _make_adapter()
    e1 = _mk_user_message(
        session_id=_TEST_SESSION_ID, thread_id=_TEST_THREAD_ID, text="a",
    )
    e2 = _mk_user_message(
        session_id=_TEST_SESSION_ID, thread_id=_TEST_THREAD_ID, text="b",
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
        session_id=_TEST_SESSION_ID, thread_id=_TEST_THREAD_ID, text="hi",
    )

    adapter._register_widget_for_event(event, None)

    assert event.event_id not in adapter._event_widgets


# ---------------------------------------------------------------------------
# Pilot-booted replay test — verifies that the mount seams actually call
# `_register_widget_for_event` under a real resume + replay pass.
# ---------------------------------------------------------------------------


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


async def _empty_stream(
    *_args: Any, **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _MockEngine:
    """Engine mock — never runs a real turn, no eager-compaction."""

    execute_stream = staticmethod(_empty_stream)

    async def maybe_compact_thread_eagerly(
        self,
        *,
        thread_id: Any,  # noqa: ARG002
        spec: Any,  # noqa: ARG002
        tool_schemas: list[dict[str, Any]] | None = None,  # noqa: ARG002
        trigger: str = "auto_resume",  # noqa: ARG002
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
    *, session_id: Any, thread_id: Any, text: str,
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
    *, session_id: Any, thread_id: Any, text: str,
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
    store: SessionStore, thread_id: Any, events: list[Event],
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
        session=bundle, engine=bundle.runtime.engine, full_history=True,
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
