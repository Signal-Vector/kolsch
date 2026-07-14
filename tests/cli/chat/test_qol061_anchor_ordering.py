"""qol-061 — on_mount anchor ordering + native re-engage regression tests.

Verifies:

- Test 1: ``transcript.anchor()`` fires BEFORE ``_maybe_eager_compact_on_open``
  is awaited. The pre-fix bug was the reverse ordering — during a long
  summariser await on a large session, mounts landed without anchor engaged.
- Test 2: A widget mounted DURING a simulated eager-compact hang follows the
  tail (viewport clamps to ``max_scroll_y`` via Textual's compositor).
- Test 3: User scroll-up releases the anchor and preserves position across
  new mounts (Textual native behaviour; guard against future regressions).
- Test 4: Scrolling back to the tail re-engages the anchor via Textual's
  built-in ``_check_anchor`` (``widget.py:792-799``) — no subclass required.

Fixture shape mirrors ``tests/cli/chat/test_bug058_replay_batch.py`` — real
``SessionStore``, ``SqliteBackend``, and a Pilot-booted ``ChatApp``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from textual.containers import VerticalScroll

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.widgets.notice import NoticeWidget
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend


# ---------------------------------------------------------------------------
# Runtime + engine mocks (mirror bug-058's shape).
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
    """Engine mock — never runs a real turn, no eager-compaction by default."""

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
    session = await store.create_session("ag_mock", name="qol061")
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
# Synthesized event builders (identical to bug-058's shape).
# ---------------------------------------------------------------------------


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


def _synthesize_events(
    session_id: Any, thread_id: Any, *, turns: int,
) -> list[Event]:
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
    store: SessionStore, thread_id: Any, events: list[Event],
) -> None:
    for ev in events:
        await store.append_event(thread_id, ev)


# ---------------------------------------------------------------------------
# Test 1 — anchor engages BEFORE the eager-compact await.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anchor_engages_before_eager_compact_await(
    store_env: dict[str, Any],
) -> None:
    """``transcript.anchor()`` fires before ``_maybe_eager_compact_on_open``.

    Regression guard for qol-061: the fix is a hoist of the anchor call from
    AFTER the eager-compact await to BEFORE it. Wraps
    ``_maybe_eager_compact_on_open`` with a probe that records
    ``transcript.is_anchored`` at method entry.
    """
    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=False,
    )

    observed: dict[str, bool | None] = {"is_anchored_at_entry": None}
    original = app._maybe_eager_compact_on_open

    async def _probe() -> None:
        try:
            transcript = app.query_one("#transcript", VerticalScroll)
            observed["is_anchored_at_entry"] = transcript.is_anchored
        except Exception:  # noqa: BLE001
            observed["is_anchored_at_entry"] = None
        await original()

    app._maybe_eager_compact_on_open = _probe  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()

    assert observed["is_anchored_at_entry"] is True, (
        "expected transcript.is_anchored=True at eager-compact entry; "
        f"observed {observed['is_anchored_at_entry']!r}. The anchor() call "
        "still sits BEHIND the eager-compact await — see qol-061."
    )


# ---------------------------------------------------------------------------
# Test 2 — mount during simulated eager-compact hang follows the tail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_during_eager_compact_follows_tail(
    store_env: dict[str, Any],
) -> None:
    """Widget mounted while eager compact hangs still clamps to the tail.

    The pre-fix bug: mounts during the summariser await landed without the
    anchor engaged, so the viewport didn't follow. Post-fix: anchor is
    engaged before the await, so Textual's compositor clamps ``scroll_y``
    on ``virtual_size`` growth even for mounts that land mid-hang.

    Approach: monkeypatch ``_maybe_eager_compact_on_open`` with a body that
    (1) records ``is_anchored`` on entry, (2) performs a real mount into
    ``#transcript`` while the method is still "hanging" (i.e., before it
    returns), (3) returns cleanly so ``on_mount`` completes. After
    ``pilot.pause()``, assert the mounted widget was clamped to the tail.
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=80,
    )
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=True,
    )

    observed: dict[str, Any] = {
        "anchored_on_entry": None,
        "mounted": False,
    }

    async def _hang_and_mount() -> None:
        # Simulates the whale-session eager-compact window: anchor is
        # already engaged (Test 1 asserts this); a widget mount races the
        # summariser awaits. Post-fix, this mount must follow the tail.
        transcript = app.query_one("#transcript", VerticalScroll)
        observed["anchored_on_entry"] = transcript.is_anchored
        # Yield the loop a few times so pending compositor work drains
        # before we mount — mirrors the real summariser's I/O suspend.
        for _ in range(3):
            await asyncio.sleep(0)
        await transcript.mount(NoticeWidget(text="qol-061 mid-hang notice"))
        # Yield again so the mount's layout pass settles before we let
        # on_mount finish.
        for _ in range(3):
            await asyncio.sleep(0)
        observed["mounted"] = True

    app._maybe_eager_compact_on_open = _hang_and_mount  # type: ignore[method-assign]

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        assert observed["mounted"] is True, (
            "the simulated hang's mount did not run"
        )
        assert observed["anchored_on_entry"] is True, (
            "expected anchor engaged BEFORE the eager-compact await; "
            f"observed {observed['anchored_on_entry']!r}"
        )
        # Sanity: virtual_size exceeds container so tail-clamp is meaningful.
        assert transcript.virtual_size.height > transcript.container_size.height, (
            "seed too small — virtual_size does not exceed container_size; "
            f"virtual={transcript.virtual_size.height}, "
            f"container={transcript.container_size.height}"
        )
        assert transcript.is_anchored is True, (
            "anchor should still be engaged after the mid-hang mount"
        )
        assert transcript.is_vertical_scroll_end is True, (
            "expected viewport at tail after mid-hang mount; "
            f"scroll_y={transcript.scroll_y}, "
            f"max_scroll_y={transcript.max_scroll_y}"
        )


# ---------------------------------------------------------------------------
# Test 3 — scroll-up releases anchor; subsequent mount preserves position.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_up_preserves_position_on_new_mount(
    store_env: dict[str, Any],
) -> None:
    """After a user PgUp, mounting a new widget does NOT yank the viewport.

    Textual native behaviour: ``_scroll_to`` sets ``_anchor_released=True`` on
    every user scroll input, so the compositor's tail-clamp is suppressed.
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=80,
    )
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=True,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        assert transcript.virtual_size.height > transcript.container_size.height, (
            "seed too small — virtual_size does not exceed container_size"
        )
        assert transcript.is_anchored is True

        # Focus the transcript so pageup routes to it (not the composer).
        transcript.focus()
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()

        scroll_y_before = transcript.scroll_y
        assert transcript._anchor_released is True, (
            "expected pageup to release the anchor"
        )
        # Sanity: we actually scrolled away from the tail.
        assert scroll_y_before < transcript.max_scroll_y, (
            f"pageup did not move viewport; scroll_y={scroll_y_before}, "
            f"max={transcript.max_scroll_y}"
        )

        # Mount another widget; viewport should NOT jump.
        await transcript.mount(NoticeWidget(text="qol-061 preserved-position"))
        await pilot.pause()

        assert transcript.scroll_y == scroll_y_before, (
            "expected scroll_y unchanged after mount while anchor released; "
            f"before={scroll_y_before}, after={transcript.scroll_y}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Textual's native _check_anchor re-engages on scroll-back-to-tail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_back_to_tail_reengages_anchor_natively(
    store_env: dict[str, Any],
) -> None:
    """Textual's built-in ``_check_anchor`` handles scroll-back-to-tail.

    Load-bearing verification for qol-061: proves we do NOT need a
    ``VerticalScroll`` subclass or a ``watch_scroll_y`` override. Textual's
    ``widget.py:792-799`` flips ``_anchor_released`` back to ``False`` when
    ``scroll_y >= max_scroll_y``, invoked from ``watch_scroll_y``
    (``widget.py:1929-1935``).
    """
    events = _synthesize_events(
        store_env["session_id"],
        store_env["thread_id"],
        turns=80,
    )
    await _seed(store_env["store"], store_env["thread_id"], events)

    bundle = _mk_bundle(store_env, is_resume=True)
    app = ChatApp(
        session=bundle, engine=bundle.runtime.engine, full_history=True,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        assert transcript.virtual_size.height > transcript.container_size.height
        assert transcript.is_anchored is True

        transcript.focus()
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()

        assert transcript._anchor_released is True, (
            "expected pageup to release the anchor"
        )

        # Scroll back to the tail. `end` scrolls to the bottom; pilot.pause
        # lets Textual's watch_scroll_y → _check_anchor fire.
        await pilot.press("end")
        await pilot.pause()

        assert transcript._anchor_released is False, (
            "expected Textual's native _check_anchor to re-engage the anchor "
            "on scroll-back-to-tail; observed _anchor_released still True. "
            "This would indicate a Textual gap — surface as Decision needed."
        )

        # Mount another widget — should follow the tail via the compositor.
        await transcript.mount(
            NoticeWidget(text="qol-061 post-reengage mount"),
        )
        await pilot.pause()

        assert transcript.is_vertical_scroll_end is True, (
            "expected viewport at tail after post-reengage mount; "
            f"scroll_y={transcript.scroll_y}, "
            f"max_scroll_y={transcript.max_scroll_y}"
        )
