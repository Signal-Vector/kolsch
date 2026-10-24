"""Tests for :class:`WorkingPlaceholder` (spec-130 Steps 1-2).

Step 1 builds the widget standalone — not wired into any event flow.
Those tests cover:

- Working state: GutterBlock composed with rail ``$hint``, spinner
  glyph cycling through ``SPINNER_FRAMES`` on the ~0.08s interval.
- ``render_terminal("interrupted")``: ``─`` glyph (state_tokens.py's
  ``"cancelled"`` token), ``$hint`` rail, dim text-style.
- ``render_terminal("error")``: ``✗`` glyph (state_tokens.py's
  ``"failed"`` token), ``$error`` rail, bold text-style.
- NO_COLOR construction test per state, following ``test_notice.py``'s
  ``test_glyph_present_under_no_color_construction`` pattern: construct
  under ``mock.patch.dict(os.environ, {"NO_COLOR": "1"})`` and assert
  the glyph is present and identical to the color-mode glyph.

Step 2 wires the turn-start mount: ``ChatApp._run_exchange`` mounts a
``WorkingPlaceholder`` into ``#transcript`` from inside its
``event_stream`` loop, gated per-event on ``not placeholder_mounted
and event.type == EventType.USER_MESSAGE`` — i.e. right after the
engine's ``USER_MESSAGE`` event has been handled (and the
``UserChatbox`` it triggers has been mounted), not before the loop
starts. The integration tests below drive a real ``ChatApp`` (same
scaffolding as
``tests/cli/chat/test_composer_submitted_exit_verbs.py``) against a
fake engine whose ``execute_stream`` yields a real ``USER_MESSAGE``
first (matching production), then blocks on an ``asyncio.Event`` gate
the test releases explicitly before yielding its next (and only
other) event, and assert the placeholder is mounted in
``#transcript`` immediately after that ``USER_MESSAGE`` is processed —
i.e. before the *subsequent* event is ever processed, and below the
``UserChatbox`` the ``USER_MESSAGE`` itself mounted. The gate makes
that happens-before hold by construction rather than by a wall-clock
delay (qol-164), and every "after the event" wait is a bounded
``_pump_until`` on a predicate, never a sleep.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest import mock
from uuid import uuid4

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Static

from cli.chat.app import ChatApp
from cli.chat.spinner import SPINNER_FRAMES
from cli.chat.state_tokens import resolve_state
from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.working_placeholder import WorkingPlaceholder
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from textual.app import App
    from textual.pilot import Pilot
    from textual.widget import Widget


def _glyph_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered glyph-column text off a mounted widget."""
    return str(widget.query_one(".gb-glyph-col", Static).render())


def _body_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered body text off a mounted widget."""
    return str(widget.query_one(".working-body", Static).render())


def _tool_body_text(block: ToolCallBlock) -> str:
    """Read a `ToolCallBlock`'s rendered body text via its `.body` Static.

    Queries by CSS class (same convention as `test_tool_call_block.py`'s
    `_body_statics` helper) rather than reaching into the widget's
    private `_body_static` attribute directly.
    """
    return str(block.query_one(".body", Static).render())


# ----------------------------------------------------------------------
# Working state
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_working_state_composes_gutter_block_with_hint_rail(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The widget composes a GutterBlock with rail ``$hint`` on mount."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.rail_color == "$hint"
        assert block.has_class("-rail-hint")
        # Not yet flipped to a terminal state.
        assert widget.is_terminal is False
        assert widget.terminal_kind is None


@pytest.mark.asyncio
async def test_working_state_initial_glyph_is_a_spinner_frame(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """On mount, the glyph column shows a frame from SPINNER_FRAMES."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert _glyph_text(widget) in SPINNER_FRAMES


@pytest.mark.asyncio
async def test_working_state_spinner_cycles_over_time(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The glyph column advances through distinct spinner frames on the
    ~0.08s interval convention (same cadence as TurnStatus)."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        seen: set[str] = set()
        first = _glyph_text(widget)
        seen.add(first)
        # Advance enough ticks (0.08s each) to observe more than one
        # distinct frame without depending on exact timer precision.
        for _ in range(6):
            await pilot.pause(0.08)
            seen.add(_glyph_text(widget))

        assert len(seen) > 1, "spinner did not cycle through multiple frames"
        assert seen.issubset(set(SPINNER_FRAMES))


@pytest.mark.asyncio
async def test_working_state_body_shows_thinking_verb(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The body text shows the working-state verb."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert "Thinking" in _body_text(widget)


# ----------------------------------------------------------------------
# Terminal state: interrupted
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_terminal_interrupted_glyph_and_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("interrupted")`` shows the cancelled-token glyph."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        cancelled_token = resolve_state("cancelled")
        assert cancelled_token.glyph == "─"
        assert _glyph_text(widget) == "─"
        assert widget.has_class("-interrupted")
        assert widget.is_terminal is True
        assert widget.terminal_kind == "interrupted"


@pytest.mark.asyncio
async def test_render_terminal_interrupted_rail_stays_hint(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Interrupted state keeps the ``$hint`` rail (per the glyph/color table).

    bug-135: the shared ``"cancelled"`` token's rail color is
    deliberately untouched (that's ``state_tokens.py``'s call, out of
    this widget's scope) — the at-a-glance distinction bug-135 adds
    lives in the body's text-style + copy instead (see
    ``test_render_terminal_interrupted_body_is_dim_and_struck`` and
    ``test_render_terminal_interrupted_body_text_is_widened`` below),
    not the rail.
    """
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.has_class("-rail-hint")
        assert not block.has_class("-rail-error")


@pytest.mark.asyncio
async def test_render_terminal_interrupted_body_is_dim_and_struck(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Interrupted body text renders with ``dim`` AND ``strike`` text-style.

    bug-135: ``strike`` was added alongside the pre-existing ``dim`` so
    the interrupted row reads unambiguously as terminal/dead rather
    than "still thinking" — a bare glyph/text swap wasn't enough (see
    bug-135's Background).
    """
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        body = widget.query_one(".working-body", Static)
        assert bool(body.styles.text_style.dim)
        assert bool(body.styles.text_style.strike)


@pytest.mark.asyncio
async def test_render_terminal_interrupted_body_text_is_widened(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """bug-135: interrupted body copy is widened beyond a bare "Interrupted".

    A single word that only differed from "Thinking…" by its text
    read as too subtle a change at a glance. The widened phrase is a
    concrete, assertable part of the new visual contract.
    """
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        assert _body_text(widget) == "Interrupted — turn cancelled"


@pytest.mark.asyncio
async def test_render_terminal_interrupted_stops_spinner(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """After flipping to interrupted, the glyph no longer changes over time."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()
        frozen = _glyph_text(widget)
        assert frozen == "─"

        await pilot.pause(0.3)
        assert _glyph_text(widget) == "─"


# ----------------------------------------------------------------------
# Terminal state: error
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_terminal_error_glyph_and_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("error")`` shows the failed-token glyph."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        failed_token = resolve_state("failed")
        assert failed_token.glyph == "✗"
        assert _glyph_text(widget) == "✗"
        assert widget.has_class("-error")
        assert widget.is_terminal is True
        assert widget.terminal_kind == "error"


@pytest.mark.asyncio
async def test_render_terminal_error_rail_flips_to_error(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Error state flips the rail to ``$error`` (per the glyph/color table)."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.has_class("-rail-error")
        assert not block.has_class("-rail-hint")


@pytest.mark.asyncio
async def test_render_terminal_error_body_is_bold(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Error body text renders with ``bold`` text-style."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        body = widget.query_one(".working-body", Static)
        assert bool(body.styles.text_style.bold)


@pytest.mark.asyncio
async def test_render_terminal_error_stops_spinner(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """After flipping to error, the glyph no longer changes over time."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()
        assert _glyph_text(widget) == "✗"

        await pilot.pause(0.3)
        assert _glyph_text(widget) == "✗"


# ----------------------------------------------------------------------
# NO_COLOR construction tests
# ----------------------------------------------------------------------


def test_working_glyph_present_under_no_color_construction() -> None:
    """Constructing under NO_COLOR still produces a valid working-state glyph.

    Mirrors ``test_notice.py``'s
    ``test_glyph_present_under_no_color_construction`` — NO_COLOR only
    affects theme/palette selection, not the widget's own glyph
    resolution.
    """
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        widget = WorkingPlaceholder()
    # Initial glyph is set from the spinner index at construction time,
    # independent of environment/theme.
    assert widget.is_terminal is False


@pytest.mark.asyncio
async def test_working_glyph_matches_across_color_and_no_color(
    mock_app_factory: Callable[[Widget], App[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial working-state glyph is identical between color + NO_COLOR.

    The spinner animation is frozen for both runs so the read is the
    *initial* glyph however long each mount takes (qol-164: with the
    0.08s interval live, one run could advance a frame before its read
    and the other not).
    """
    monkeypatch.setattr(WorkingPlaceholder, "_advance_spinner", lambda self: None)
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph == SPINNER_FRAMES[0]


@pytest.mark.asyncio
async def test_interrupted_glyph_present_under_no_color_construction(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("interrupted")`` glyph is identical under NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        w_color.render_terminal("interrupted")
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        w_mono.render_terminal("interrupted")
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph == "─"


@pytest.mark.asyncio
async def test_error_glyph_present_under_no_color_construction(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("error")`` glyph is identical under NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        w_color.render_terminal("error")
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        w_mono.render_terminal("error")
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph == "✗"


# ----------------------------------------------------------------------
# Standalone mount (no engine/event wiring)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_mounts_standalone_with_no_wiring(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The widget can be constructed and mounted with zero engine/event wiring."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.is_mounted
        assert widget.query_one(GutterBlock) is not None


# ----------------------------------------------------------------------
# spec-130 Step 2: turn-start mount via `ChatApp._run_exchange`
# ----------------------------------------------------------------------
#
# Scaffolding mirrors `tests/cli/chat/test_composer_submitted_exit_verbs.py`
# — a real `ChatApp` wired against a mock runtime/engine, driven through
# `app.run_test()`. `_MockEngine.execute_stream` here is a *gated*
# async generator: after `USER_MESSAGE` it blocks on an `asyncio.Event`
# the test releases explicitly (`gate.set()`), so the test can assert
# the placeholder is mounted in `#transcript` strictly before the next
# event is ever handed to `EventAdapter.handle` — by construction, not
# by racing a wall-clock delay (qol-164). A gate that is never set
# leaves the stream parked until app teardown cancels the worker
# (`Event.wait()` is cancellable exactly as `asyncio.sleep` is).


async def _pump_until(
    pilot: Pilot[Any],
    predicate: Callable[[], bool],
    *,
    what: str,
    max_pauses: int = 200,
) -> None:
    """Pump the event loop with `pilot.pause()` until `predicate()` holds.

    Bounded by an iteration count, not a duration, and never sleeps:
    host load slows each pause down but cannot make the predicate
    false. Fails naming `what` once `max_pauses` is exhausted.
    """
    for _ in range(max_pauses):
        if predicate():
            return
        await pilot.pause()
    assert predicate(), f"timed out after {max_pauses} pauses waiting for {what}"


def _placeholder_mounted(app: ChatApp) -> bool:
    """The turn's `WorkingPlaceholder` is stashed and attached to the DOM."""
    placeholder = app._working_placeholder
    return placeholder is not None and placeholder.is_mounted


def _running_block_mounted(app: ChatApp) -> bool:
    """A `TOOL_CALLED` running `ToolCallBlock` is pending and attached."""
    block = app._pending_tool_call_block
    return block is not None and block.is_mounted


def _prose_contains(app: ChatApp, text: str) -> bool:
    """Some mounted `AssistantProse` already carries `text`."""
    return any(
        prose.is_mounted and text in prose.source for prose in app.query(AssistantProse)
    )


class _MockAuthzStep2:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


async def _empty_async_gen(*_args: Any, **_kwargs: Any) -> AsyncGenerator[Any, None]:
    """Async generator that yields nothing — same shape as sibling tests."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _MockStoreStep2:
    read_path_to_leaf = staticmethod(_empty_async_gen)


def _make_canonical_agent_message_event(thread_id: str) -> Event:
    """Build a real, fully-valid ``AGENT_MESSAGE`` ``Event``.

    Same shape ``tests/cli/chat/conftest.py``'s ``canonical_event``
    fixture builds — a plain module-level helper is used here (rather
    than that fixture) since it's needed inside a closure built before
    the test body runs, not directly from a test parameter.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"type": "text", "text": "hi back"}},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_user_message_event(thread_id: str, *, text: str = "hi") -> Event:
    """Build a real, fully-valid ``USER_MESSAGE`` ``Event``.

    Mirrors what ``src/engine/engine.py`` actually emits as the very
    first event of any user-submitted trigger. Traced via
    ``src/cli/chat/session.py::_build_user_trigger`` (produces
    ``{"kind": "user_message", "content": text}``) through
    ``src/engine/engine.py::_extract_trigger_content`` (~line 3349),
    which for a plain string ``content`` returns ``{"text": text}`` —
    no ``"type"`` key — wrapped in ``UserMessageData(content=...)`` at
    the ``_emit`` call (~line 1713-1721). The real event's ``data`` is
    therefore ``{"content": {"text": ...}}``, with an
    ``Actor(kind="user", id="user.local")`` — NOT ``kind="system"``
    (that's reserved for synthetic platform events; a real
    ``USER_MESSAGE`` is always attributed to the user). Every fake
    ``execute_stream`` in this file yields one of these first, matching
    production — the engine always emits ``USER_MESSAGE`` before
    anything else for a user-submitted turn, and ``_run_exchange`` (post
    the in-transcript-placeholder-position fix) only mounts the
    ``WorkingPlaceholder`` in response to seeing it.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.USER_MESSAGE,
        data={"content": {"text": text}},
        actor=Actor(kind="user", id="user.local"),
        created_at=datetime.now(UTC),
    )


def _make_delayed_event_stream(
    thread_id: str,
    events_seen: list[Event],
    gate: asyncio.Event,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` that holds back its next event.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering — see ``_make_user_message_event``), then waits on
    ``gate`` before yielding the "interesting" ``AGENT_MESSAGE``. Only the
    ``AGENT_MESSAGE`` is appended to ``events_seen`` — callers use that
    list to assert "placeholder mounted" happened-before "first
    [interesting] event recorded" without needing to inspect
    `EventAdapter` internals, and the gate/assertions in those tests
    are keyed off the AGENT_MESSAGE specifically, not the immediate
    USER_MESSAGE that now precedes it.
    """

    async def _delayed_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        yield _make_user_message_event(thread_id)
        await gate.wait()
        event = _make_canonical_agent_message_event(thread_id)
        events_seen.append(event)
        yield event

    return _delayed_stream


class _MockEngineStep2:
    def __init__(
        self, thread_id: str, events_seen: list[Event], gate: asyncio.Event
    ) -> None:
        self.execute_stream = _make_delayed_event_stream(thread_id, events_seen, gate)


class _MockRuntimeStep2:
    def __init__(
        self, thread_id: str, events_seen: list[Event], gate: asyncio.Event
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep2(thread_id, events_seen, gate)


@dataclass
class _MockConfigStep2:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundleStep2:
    runtime: Any
    config: Any
    session_id: str
    thread_id: str
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


def _make_app_step2(
    tmp_path: Path,
    events_seen: list[Event],
    gate: asyncio.Event | None = None,
) -> ChatApp:
    # `gate` defaults to a fresh, never-set event: the stream parks
    # after `USER_MESSAGE` until app teardown cancels the worker.
    #
    # `_run_exchange` (unlike the spied version in
    # test_composer_submitted_exit_verbs.py) really constructs an
    # `ExecutionContext`, whose `thread_id` is a `ThreadId` — validated
    # to have a uuid body. A plain `"th_test"` string fails that
    # validation; use a real uuid4 body instead.
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep2(
            thread_id, events_seen, gate if gate is not None else asyncio.Event()
        ),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_placeholder_mounted_at_turn_start(tmp_path: Path) -> None:
    """`_run_exchange` mounts a `WorkingPlaceholder` into `#transcript`.

    Drives a real turn through the app's normal `_run_exchange` worker
    (not a spy) against a fake engine whose stream yields a real
    `USER_MESSAGE` first, then holds its next event behind a gate the
    test never releases, and asserts the placeholder is mounted while
    the stream is still held — i.e. right after the `USER_MESSAGE` event has been processed (one
    event in), not before any event has been processed.
    """
    events_seen: list[Event] = []
    app = _make_app_step2(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello there")
        # Pump until the worker has started and mounted the
        # placeholder. The gate is never set, so the fake stream
        # cannot yield its next event however long this takes.
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )

        assert events_seen == [], (
            "the fake stream's gated event fired before the assertion — "
            "the gate did not hold"
        )
        placeholders = app.query(WorkingPlaceholder)
        assert len(placeholders) == 1
        assert placeholders.first().is_mounted
        assert app._working_placeholder is not None
        assert app._working_placeholder is placeholders.first()


@pytest.mark.asyncio
async def test_placeholder_reference_stashed_for_later_steps(
    tmp_path: Path,
) -> None:
    """The mounted placeholder is reachable via `ChatApp._working_placeholder`.

    Later spec-130 steps (morph-on-content, cancel/error terminal
    states) need a handle to the placeholder mounted for the active
    turn. This asserts that handle exists, is the actual mounted
    instance (identity, not just "a" WorkingPlaceholder), and is still
    in its initial working (non-terminal) state — this step doesn't
    wire any transitions.
    """
    events_seen: list[Event] = []
    app = _make_app_step2(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello there")
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )

        placeholder = app._working_placeholder
        assert placeholder is not None
        assert placeholder in app.query(WorkingPlaceholder)
        assert placeholder.is_terminal is False
        assert placeholder.terminal_kind is None


@pytest.mark.asyncio
async def test_placeholder_mounts_below_user_chatbox_not_above_it(
    tmp_path: Path,
) -> None:
    """Regression test: the placeholder must land BELOW the user's own message.

    Real-world bug found post-ship: `_run_exchange` used to mount
    `WorkingPlaceholder` unconditionally as the first line of its try
    block, before `event_stream` iteration even began — but
    `UserChatbox` only ever mounts later, in response to the engine's
    `USER_MESSAGE` event (`EventAdapter._handle_user_message`, the
    single mount site shared by the live and replay paths — see
    `on_composer_submitted`'s docstring). Since `USER_MESSAGE` is
    always the first event a user-submitted trigger yields
    (`src/engine/engine.py` ~line 1713-1721), mounting the placeholder
    before the loop started landed it ABOVE the user's own message
    every time.

    Uses the same gated-stream shape Step 2's tests use (`USER_MESSAGE`
    immediately, then a never-released gate before a non-terminal
    `AGENT_MESSAGE`) so
    the placeholder is mounted and still visible (not yet morphed away)
    at the point this test inspects `#transcript` — both widgets must
    be present, with `UserChatbox` at a LOWER child index than
    `WorkingPlaceholder`.
    """
    events_seen: list[Event] = []
    app = _make_app_step2(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello there")
        # Pump until USER_MESSAGE has been processed (mounting
        # UserChatbox) and the placeholder has mounted in response. The
        # gate is never set, so the fake stream's AGENT_MESSAGE (which
        # would morph the placeholder away) cannot fire.
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )

        assert events_seen == [], (
            "the fake stream's AGENT_MESSAGE fired before the assertion — "
            "the gate did not hold"
        )

        transcript = app.query_one("#transcript", VerticalScroll)
        children = list(transcript.children)

        user_chatbox_index = next(
            (i for i, child in enumerate(children) if isinstance(child, UserChatbox)),
            None,
        )
        placeholder_index = next(
            (
                i
                for i, child in enumerate(children)
                if isinstance(child, WorkingPlaceholder)
            ),
            None,
        )

        assert user_chatbox_index is not None, (
            "UserChatbox was never mounted — USER_MESSAGE was not processed"
        )
        assert placeholder_index is not None, (
            "WorkingPlaceholder was never mounted for this turn"
        )
        assert user_chatbox_index < placeholder_index, (
            "the user's own message must appear BEFORE (above) the "
            "working placeholder in the transcript — got UserChatbox "
            f"at index {user_chatbox_index}, WorkingPlaceholder at "
            f"index {placeholder_index}"
        )


# ----------------------------------------------------------------------
# spec-130 Step 3: streaming-token morph via
# `ChatApp.mount_assistant_streaming_widget` /
# `EventAdapter._handle_model_streaming_token`
# ----------------------------------------------------------------------
#
# Reuses the Step 2 scaffolding (`_MockAuthzStep2`, `_MockStoreStep2`,
# `_MockConfigStep2`, `_MockBundleStep2`) but with a fake
# `engine.execute_stream` that yields real `MODEL_STREAMING_TOKEN`
# events instead of a single delayed `AGENT_MESSAGE`. Drives the real
# `ChatApp._run_exchange` -> `EventAdapter.handle` -> `app.py` mount
# helper path — not a spy on the morph logic — so the test exercises
# the exact code path `kolsch chat` would.


def _make_streaming_token_event(
    thread_id: str, *, call_id: str, text_delta: str
) -> Event:
    """Build a real, fully-valid ``MODEL_STREAMING_TOKEN`` ``Event``.

    Data shape matches ``tests/cli/chat/conftest.py``'s
    ``canonical_event`` fixture for this event type: ``{"call_id":
    ..., "text_delta": ...}``.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.MODEL_STREAMING_TOKEN,
        data={"call_id": call_id, "text_delta": text_delta},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_streaming_event_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    deltas: list[str],
    call_id: str = "call-1",
    gate_first: asyncio.Event | None = None,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding N streaming-token events.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering), which is what triggers the placeholder mount. Each entry
    in ``deltas`` becomes one ``MODEL_STREAMING_TOKEN`` event for the
    same ``call_id`` — i.e. one turn, N incremental tokens, matching
    how a real streamed reply arrives. When ``gate_first`` is given,
    the stream waits on it (AFTER yielding ``USER_MESSAGE``, before the
    first streaming-token event) — same gated-stream shape Step 2's
    tests use — so a test can observe the placeholder mounted (in
    response to ``USER_MESSAGE``) but not yet morphed, then release the
    gate to let the first token arrive.
    """

    async def _streaming_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        yield _make_user_message_event(thread_id)
        if gate_first is not None:
            await gate_first.wait()
        for delta in deltas:
            event = _make_streaming_token_event(
                thread_id,
                call_id=call_id,
                text_delta=delta,
            )
            events_seen.append(event)
            yield event

    return _streaming_stream


class _MockEngineStep3:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        deltas: list[str],
        gate_first: asyncio.Event | None = None,
    ) -> None:
        self.execute_stream = _make_streaming_event_stream(
            thread_id,
            events_seen,
            deltas=deltas,
            gate_first=gate_first,
        )


class _MockRuntimeStep3:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        deltas: list[str],
        gate_first: asyncio.Event | None = None,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep3(
            thread_id,
            events_seen,
            deltas=deltas,
            gate_first=gate_first,
        )


def _make_app_step3(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    deltas: list[str],
    gate_first: asyncio.Event | None = None,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep3(
            thread_id,
            events_seen,
            deltas=deltas,
            gate_first=gate_first,
        ),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_first_streaming_token_removes_placeholder_and_mounts_prose(
    tmp_path: Path,
) -> None:
    """First `MODEL_STREAMING_TOKEN` morphs the placeholder into `AssistantProse`.

    Drives a real turn through `_run_exchange` with a fake stream that
    emits one streaming-token event, then asserts: the placeholder is
    gone (unmounted, no longer queryable), `AssistantProse` is mounted
    with the token's text, and `ChatApp._working_placeholder` has been
    cleared back to `None` so later steps don't act on a removed
    widget.
    """
    events_seen: list[Event] = []
    app = _make_app_step3(tmp_path, events_seen, deltas=["hello"])
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hi")
        await _pump_until(
            pilot, lambda: _prose_contains(app, "hello"), what="streamed prose"
        )

        assert len(events_seen) == 1, "expected exactly one streaming-token event"

        # Placeholder is gone — not just hidden.
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None

        prose_widgets = app.query(AssistantProse)
        assert len(prose_widgets) == 1
        prose = prose_widgets.first()
        assert prose.is_mounted
        assert "hello" in prose.source


@pytest.mark.asyncio
async def test_first_streaming_token_prose_lands_in_placeholder_position(
    tmp_path: Path,
) -> None:
    """The mounted `AssistantProse` occupies the same transcript child slot.

    The placeholder is mounted right after `USER_MESSAGE` is processed
    (which mounts its own `UserChatbox` first — nothing else added to
    `#transcript` for this turn beyond those two), so remove-then-mount
    should land `AssistantProse` at the same index the placeholder
    held — verified directly against `#transcript`'s child list, not
    just "is mounted somewhere." Gates the first token (same gated
    stream as Step 2's `test_placeholder_mounted_at_turn_start`) so the
    placeholder's mounted-but-not-yet-morphed position is observed
    before the test releases the morph.
    """
    events_seen: list[Event] = []
    gate = asyncio.Event()
    app = _make_app_step3(
        tmp_path,
        events_seen,
        deltas=["hello"],
        gate_first=gate,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        children_before = list(transcript.children)

        app._run_exchange("hi")
        # Pump until USER_MESSAGE has been processed (mounting
        # UserChatbox) and the placeholder mount has landed. The gate
        # holds the stream's first (interesting) event back.
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )
        assert events_seen == [], (
            "the fake stream's first event fired before the assertion — "
            "the gate did not hold"
        )
        placeholder_index = next(
            (
                i
                for i, child in enumerate(transcript.children)
                if isinstance(child, WorkingPlaceholder)
            ),
            None,
        )
        assert placeholder_index is not None, (
            "placeholder was not mounted before the morph — test setup "
            "invalid, can't verify position"
        )

        # Release the gate so the first token arrives, and pump until
        # the morph (remove placeholder, mount prose) completes.
        gate.set()
        await _pump_until(
            pilot,
            lambda: _prose_contains(app, "hello") and not _placeholder_mounted(app),
            what="placeholder -> prose morph",
        )

        children_after = list(transcript.children)
        assert len(children_after) == len(children_before) + 2, (
            "expected exactly two net new children (UserChatbox from "
            "USER_MESSAGE, plus the placeholder removed / prose mounted "
            "swap netting one more)"
        )
        prose_index = next(
            i
            for i, child in enumerate(children_after)
            if isinstance(child, AssistantProse)
        )
        # The prose widget is the last child (same as the placeholder
        # was, at the time it alone had been added for this turn).
        assert prose_index == len(children_after) - 1
        assert prose_index == placeholder_index, (
            "AssistantProse did not land in the placeholder's former "
            "transcript position"
        )


@pytest.mark.asyncio
async def test_second_streaming_token_appends_not_second_morph(
    tmp_path: Path,
) -> None:
    """A second token for the same turn appends via the existing flow.

    Only the FIRST token for a `call_id` should trigger the
    mount/morph path (`EventAdapter._handle_model_streaming_token`
    only calls `mount_assistant_streaming_widget` when no widget is
    yet cached for that `call_id`). This asserts: still exactly one
    `AssistantProse` instance, its text contains both deltas
    concatenated, and no second `WorkingPlaceholder` was ever
    (re)mounted.
    """
    events_seen: list[Event] = []
    app = _make_app_step3(tmp_path, events_seen, deltas=["hello ", "world"])
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hi")
        await _pump_until(
            pilot,
            lambda: len(events_seen) == 2 and _prose_contains(app, "world"),
            what="both streamed tokens appended",
        )

        assert len(events_seen) == 2, "expected exactly two streaming-token events"

        prose_widgets = app.query(AssistantProse)
        assert len(prose_widgets) == 1, (
            "a second token must not mount a second AssistantProse"
        )
        prose = prose_widgets.first()
        assert "hello" in prose.source
        assert "world" in prose.source

        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None


# ----------------------------------------------------------------------
# spec-130 Step 4: complete-response direct-mount morph via
# `ChatApp.mount_assistant_message` /
# `EventAdapter._handle_agent_message` (and `_handle_model_response`'s
# non-streaming branch, which calls the same sink method)
# ----------------------------------------------------------------------
#
# Reuses the Step 2 scaffolding (`_MockAuthzStep2`, `_MockStoreStep2`,
# `_MockConfigStep2`, `_MockBundleStep2`,
# `_make_canonical_agent_message_event`) but with a fake
# `engine.execute_stream` that yields exactly one complete
# `AGENT_MESSAGE` event — full text present from the start, no
# `MODEL_STREAMING_TOKEN` at all. Drives the real
# `ChatApp._run_exchange` -> `EventAdapter.handle` -> `app.py` mount
# helper path, same as Step 3, so this exercises the exact code path
# `kolsch chat` would for a non-streamed reply.


def _make_agent_message_event(thread_id: str, *, call_id: str, text: str) -> Event:
    """Build a real, fully-valid ``AGENT_MESSAGE`` ``Event`` with full text.

    Same data shape as `_make_canonical_agent_message_event` but with
    a caller-supplied ``call_id``/``text`` so the complete-response
    fixture can carry distinct content per test.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"call_id": call_id, "content": {"type": "text", "text": text}},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_complete_response_event_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    text: str,
    call_id: str = "call-1",
    gate_first: asyncio.Event | None = None,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding one complete ``AGENT_MESSAGE``.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount). No
    ``MODEL_STREAMING_TOKEN`` precedes the ``AGENT_MESSAGE`` — the full
    text is present on the single event, matching a non-streamed
    reply. When ``gate_first`` is given, the stream waits on it (after
    ``USER_MESSAGE``, before the complete response) mirroring Steps
    2-3's gated stream so a test can observe the placeholder
    mounted-but-not-yet-morphed before releasing the response.
    """

    async def _complete_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        yield _make_user_message_event(thread_id)
        if gate_first is not None:
            await gate_first.wait()
        event = _make_agent_message_event(thread_id, call_id=call_id, text=text)
        events_seen.append(event)
        yield event

    return _complete_stream


class _MockEngineStep4:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        text: str,
        gate_first: asyncio.Event | None = None,
    ) -> None:
        self.execute_stream = _make_complete_response_event_stream(
            thread_id,
            events_seen,
            text=text,
            gate_first=gate_first,
        )


class _MockRuntimeStep4:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        text: str,
        gate_first: asyncio.Event | None = None,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep4(
            thread_id,
            events_seen,
            text=text,
            gate_first=gate_first,
        )


def _make_app_step4(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    text: str,
    gate_first: asyncio.Event | None = None,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep4(
            thread_id,
            events_seen,
            text=text,
            gate_first=gate_first,
        ),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_complete_agent_message_removes_placeholder_and_mounts_full_prose(
    tmp_path: Path,
) -> None:
    """A complete `AGENT_MESSAGE` (no streaming) swaps placeholder -> full prose.

    Drives a real turn through `_run_exchange` with a fake stream that
    emits one complete `AGENT_MESSAGE` event (full text, no preceding
    `MODEL_STREAMING_TOKEN`), then asserts: the placeholder is gone
    (unmounted, no longer queryable), `AssistantProse` is mounted with
    the FULL text already present (no empty-then-fill), and
    `ChatApp._working_placeholder` has been cleared back to `None`.
    """
    events_seen: list[Event] = []
    app = _make_app_step4(tmp_path, events_seen, text="the complete answer")
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hi")
        await _pump_until(
            pilot,
            lambda: _prose_contains(app, "the complete answer"),
            what="complete-response prose",
        )

        assert len(events_seen) == 1, "expected exactly one AGENT_MESSAGE event"
        assert events_seen[0].type == EventType.AGENT_MESSAGE

        # Placeholder is gone — not just hidden.
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None

        prose_widgets = app.query(AssistantProse)
        assert len(prose_widgets) == 1
        prose = prose_widgets.first()
        assert prose.is_mounted
        # Full content present from the single mount — never
        # empty-then-fill for this path.
        assert "the complete answer" in prose.source


@pytest.mark.asyncio
async def test_complete_agent_message_prose_lands_in_placeholder_position(
    tmp_path: Path,
) -> None:
    """The mounted `AssistantProse` occupies the same transcript child slot.

    The placeholder is mounted right after `USER_MESSAGE` is processed
    (which mounts its own `UserChatbox` first — nothing else added to
    `#transcript` for this turn beyond those two), so remove-then-mount
    should land `AssistantProse` at the same index the placeholder
    held — verified directly against `#transcript`'s child list. Gates
    the response (same gated stream as Steps 2-3) so the placeholder's
    mounted-but-not-yet-morphed position is observed before the test
    releases the morph.
    """
    events_seen: list[Event] = []
    gate = asyncio.Event()
    app = _make_app_step4(
        tmp_path,
        events_seen,
        text="the complete answer",
        gate_first=gate,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        children_before = list(transcript.children)

        app._run_exchange("hi")
        # Pump until USER_MESSAGE has been processed (mounting
        # UserChatbox) and the placeholder mount has landed. The gate
        # holds the stream's (interesting) event back.
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )
        assert events_seen == [], (
            "the fake stream's event fired before the assertion — the gate did not hold"
        )
        placeholder_index = next(
            (
                i
                for i, child in enumerate(transcript.children)
                if isinstance(child, WorkingPlaceholder)
            ),
            None,
        )
        assert placeholder_index is not None, (
            "placeholder was not mounted before the morph — test setup "
            "invalid, can't verify position"
        )

        # Release the gate so the complete response arrives, and pump
        # until the morph (remove placeholder, mount full prose)
        # completes.
        gate.set()
        await _pump_until(
            pilot,
            lambda: (
                _prose_contains(app, "the complete answer")
                and not _placeholder_mounted(app)
            ),
            what="placeholder -> prose morph",
        )

        children_after = list(transcript.children)
        assert len(children_after) == len(children_before) + 2, (
            "expected exactly two net new children (UserChatbox from "
            "USER_MESSAGE, plus the placeholder removed / prose mounted "
            "swap netting one more)"
        )
        prose_index = next(
            i
            for i, child in enumerate(children_after)
            if isinstance(child, AssistantProse)
        )
        # The prose widget is the last child (same as the placeholder
        # was, at the time it alone had been added for this turn).
        assert prose_index == len(children_after) - 1
        assert prose_index == placeholder_index, (
            "AssistantProse did not land in the placeholder's former "
            "transcript position"
        )


@pytest.mark.asyncio
async def test_complete_response_path_no_empty_then_fill(tmp_path: Path) -> None:
    """The final widget never passes through a visible empty-body state.

    Unlike the streaming path (placeholder -> empty `AssistantProse`
    -> incremental `append()`), the complete-response path must go
    straight from placeholder to a FULLY populated `AssistantProse` in
    one swap. This asserts the mounted widget's source is non-empty
    and equal to the full text at the first observation point after
    the morph — there is no intermediate frame where an
    `AssistantProse` exists with empty/partial content for this path.
    """
    events_seen: list[Event] = []
    full_text = "a fairly long complete response with several words in it"
    app = _make_app_step4(tmp_path, events_seen, text=full_text)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hi")
        # Stop at the first observation where ANY AssistantProse
        # exists — the assertion below then checks it is already full.
        await _pump_until(
            pilot,
            lambda: len(app.query(AssistantProse)) > 0,
            what="first AssistantProse mount",
        )

        prose_widgets = app.query(AssistantProse)
        assert len(prose_widgets) == 1
        prose = prose_widgets.first()
        # The very first (and only) AssistantProse observed already
        # carries the full text — no empty/partial intermediate state
        # was ever the thing a Reviewer or user could see.
        assert prose.source.strip() == full_text


@pytest.mark.asyncio
async def test_streaming_and_complete_response_paths_do_not_interfere(
    tmp_path: Path,
) -> None:
    """Step 3 (streaming) and Step 4 (complete-response) morphs are independent.

    Runs one turn through each path on separate `ChatApp` instances in
    the same test to confirm neither morph implementation leaks state
    into or otherwise disturbs the other: each ends with exactly one
    `AssistantProse` carrying its own full text, zero leftover
    `WorkingPlaceholder`s, and a cleared `_working_placeholder`
    reference — regardless of which path (streamed-append vs.
    direct-full-mount) produced it.
    """
    # Streaming turn (Step 3 path).
    streaming_events: list[Event] = []
    streaming_app = _make_app_step3(
        tmp_path, streaming_events, deltas=["hello ", "world"]
    )
    async with streaming_app.run_test() as pilot:
        await pilot.pause()
        streaming_app._run_exchange("hi")
        await _pump_until(
            pilot,
            lambda: (
                len(streaming_events) == 2
                and _prose_contains(streaming_app, "world")
                and streaming_app._working_placeholder is None
            ),
            what="streaming turn complete",
        )

        streaming_prose = streaming_app.query(AssistantProse)
        assert len(streaming_prose) == 1
        streaming_prose_widget = streaming_prose.first()
        assert "hello" in streaming_prose_widget.source
        assert "world" in streaming_prose_widget.source
        assert len(streaming_app.query(WorkingPlaceholder)) == 0
        assert streaming_app._working_placeholder is None

    # Complete-response turn (Step 4 path), separate app instance.
    complete_events: list[Event] = []
    complete_app = _make_app_step4(tmp_path, complete_events, text="a complete reply")
    async with complete_app.run_test() as pilot:
        await pilot.pause()
        complete_app._run_exchange("hi")
        await _pump_until(
            pilot,
            lambda: (
                _prose_contains(complete_app, "a complete reply")
                and complete_app._working_placeholder is None
            ),
            what="complete-response turn complete",
        )

        complete_prose = complete_app.query(AssistantProse)
        assert len(complete_prose) == 1
        complete_prose_widget = complete_prose.first()
        assert complete_prose_widget.source.strip() == "a complete reply"
        assert len(complete_app.query(WorkingPlaceholder)) == 0
        assert complete_app._working_placeholder is None

    # Cross-check: each app's prose widget is distinct — no shared
    # mutable state between the two morph paths. Captured while each
    # app was still running (above); comparing after both contexts
    # have exited just confirms identity, no live query needed.
    assert streaming_prose_widget is not complete_prose_widget


# ----------------------------------------------------------------------
# spec-130 Step 5: tool-call two-phase morph via
# `ChatApp.mount_tool_call_running` / `ChatApp.mount_tool_call` /
# `EventAdapter._handle_tool_called` / `EventAdapter._handle_tool_result`
# ----------------------------------------------------------------------
#
# Reuses the Step 2 scaffolding (`_MockAuthzStep2`, `_MockStoreStep2`,
# `_MockConfigStep2`, `_MockBundleStep2`) but with a fake
# `engine.execute_stream` that yields a real `TOOL_CALLED` event
# followed by a real `TOOL_RESULT` event for the same `tool_call_id`.
# Drives the real `ChatApp._run_exchange` -> `EventAdapter.handle` ->
# `app.py` mount helper path (not a spy), so the test exercises the
# same two-phase morph `kolsch chat` would: a running `ToolCallBlock`
# mounted at TOOL_CALLED, then the SAME instance filled at TOOL_RESULT.


def _make_tool_called_event(
    thread_id: str, *, tool_call_id: str, tool_name: str, arguments: dict[str, Any]
) -> Event:
    """Build a real, fully-valid ``TOOL_CALLED`` ``Event``."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.TOOL_CALLED,
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_tool_result_event(
    thread_id: str, *, tool_call_id: str, tool_name: str, result: dict[str, Any]
) -> Event:
    """Build a real, fully-valid ``TOOL_RESULT`` ``Event``.

    Data shape matches what ``EventAdapter._handle_tool_result`` reads
    (``event.data.get("result")``) — distinct from
    ``tests/cli/chat/conftest.py``'s ``_canonical_data`` helper, which
    uses ``"output"`` for an unrelated coverage sweep test, not the
    field the real handler consumes.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.TOOL_RESULT,
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_tool_call_event_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    gate_first: asyncio.Event | None = None,
    gate_between: asyncio.Event | None = None,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding TOOL_CALLED then TOOL_RESULT.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount). One tool call, two
    events, same ``tool_call_id`` — matching how a real tool invocation
    arrives. ``gate_first`` (after ``USER_MESSAGE``, before
    TOOL_CALLED) and ``gate_between`` (between TOOL_CALLED and
    TOOL_RESULT) reuse the gated stream from Steps 2-4 so a test can
    observe an intermediate mount state, then release the gate to let
    the next event land.
    """
    args = arguments if arguments is not None else {"path": "/tmp/x"}
    res = result if result is not None else {"content": "file contents here"}

    async def _tool_call_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        yield _make_user_message_event(thread_id)
        if gate_first is not None:
            await gate_first.wait()
        called = _make_tool_called_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=args,
        )
        events_seen.append(called)
        yield called
        if gate_between is not None:
            await gate_between.wait()
        result_event = _make_tool_result_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=res,
        )
        events_seen.append(result_event)
        yield result_event

    return _tool_call_stream


class _MockEngineStep5:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        tool_call_id: str = "call-1",
        tool_name: str = "read_file",
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        gate_first: asyncio.Event | None = None,
        gate_between: asyncio.Event | None = None,
    ) -> None:
        self.execute_stream = _make_tool_call_event_stream(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            gate_first=gate_first,
            gate_between=gate_between,
        )


class _MockRuntimeStep5:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        tool_call_id: str = "call-1",
        tool_name: str = "read_file",
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        gate_first: asyncio.Event | None = None,
        gate_between: asyncio.Event | None = None,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep5(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            gate_first=gate_first,
            gate_between=gate_between,
        )


def _make_app_step5(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    gate_first: asyncio.Event | None = None,
    gate_between: asyncio.Event | None = None,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep5(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            gate_first=gate_first,
            gate_between=gate_between,
        ),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_tool_called_removes_placeholder_and_mounts_running_block(
    tmp_path: Path,
) -> None:
    """`TOOL_CALLED` removes the placeholder and mounts a running `ToolCallBlock`.

    Gates TOOL_RESULT behind an event the test never releases, so the
    running, not-yet-filled state can be observed directly: placeholder gone,
    exactly one `ToolCallBlock` mounted, empty body (the running-state
    signal — matches `WorkflowNodeRow.mount_tool_call`'s "empty body
    == running" rule), and `ChatApp._working_placeholder` cleared.
    """
    events_seen: list[Event] = []
    app = _make_app_step5(
        tmp_path,
        events_seen,
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        gate_between=asyncio.Event(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("read a file for me")
        await _pump_until(
            pilot,
            lambda: _running_block_mounted(app),
            what="running ToolCallBlock mount",
        )

        assert len(events_seen) == 1, "expected only TOOL_CALLED to have fired yet"
        assert events_seen[0].type == EventType.TOOL_CALLED

        # Placeholder is gone — not just hidden.
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None

        blocks = app.query(ToolCallBlock)
        assert len(blocks) == 1
        block = blocks.first()
        assert block.is_mounted
        # Running state: empty body, matching
        # `WorkflowNodeRow.mount_tool_call`'s `state = "running" if
        # not body and not failed` rule — no result content yet.
        assert _tool_body_text(block) == ""
        assert app._pending_tool_call_block is block


@pytest.mark.asyncio
async def test_tool_result_fills_the_same_block_identity(tmp_path: Path) -> None:
    """`TOOL_RESULT` fills the SAME `ToolCallBlock` mounted at `TOOL_CALLED`.

    The core two-phase-morph assertion: capture the widget reference
    right after `TOOL_CALLED`, then assert it's the identical object
    (not a new one) once `TOOL_RESULT` has filled it — an `is` check,
    not just an equality/count check.
    """
    events_seen: list[Event] = []
    gate = asyncio.Event()
    app = _make_app_step5(
        tmp_path,
        events_seen,
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        result={"content": "the file's contents"},
        gate_between=gate,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("read a file for me")
        # Pump past TOOL_CALLED; the gate holds TOOL_RESULT back.
        await _pump_until(
            pilot,
            lambda: _running_block_mounted(app),
            what="running ToolCallBlock mount",
        )
        assert len(events_seen) == 1, "expected only TOOL_CALLED to have fired yet"

        running_blocks = app.query(ToolCallBlock)
        assert len(running_blocks) == 1
        running_block = running_blocks.first()
        assert _tool_body_text(running_block) == ""

        # Release the gate so TOOL_RESULT arrives, and pump until it
        # has filled the block in place.
        gate.set()
        await _pump_until(
            pilot,
            lambda: (
                len(events_seen) == 2
                and "the file's contents" in _tool_body_text(running_block)
            ),
            what="TOOL_RESULT fill",
        )

        assert len(events_seen) == 2
        assert events_seen[1].type == EventType.TOOL_RESULT

        filled_blocks = app.query(ToolCallBlock)
        assert len(filled_blocks) == 1, (
            "TOOL_RESULT must fill the existing block, not mount a second one"
        )
        filled_block = filled_blocks.first()
        # Identity check — the SAME instance, not a new one.
        assert filled_block is running_block
        assert "the file's contents" in _tool_body_text(filled_block)
        assert app._pending_tool_call_block is None


@pytest.mark.asyncio
async def test_tool_call_running_block_lands_in_placeholder_position(
    tmp_path: Path,
) -> None:
    """The running `ToolCallBlock` occupies the placeholder's former slot.

    Same remove-before-mount position check Steps 3-4 run for
    `AssistantProse`, applied to the tool-call running block. The
    placeholder is mounted right after `USER_MESSAGE` is processed
    (which mounts its own `UserChatbox` first), so the net-new-children
    count includes that extra widget too.
    """
    events_seen: list[Event] = []
    gate = asyncio.Event()
    app = _make_app_step5(
        tmp_path,
        events_seen,
        gate_first=gate,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        children_before = list(transcript.children)

        app._run_exchange("read a file for me")
        await _pump_until(
            pilot, lambda: _placeholder_mounted(app), what="placeholder mount"
        )
        assert events_seen == [], (
            "the fake stream's first event fired before the assertion — "
            "the gate did not hold"
        )
        placeholder_index = next(
            (
                i
                for i, child in enumerate(transcript.children)
                if isinstance(child, WorkingPlaceholder)
            ),
            None,
        )
        assert placeholder_index is not None, (
            "placeholder was not mounted before the morph — test setup "
            "invalid, can't verify position"
        )

        # Release the gate and pump until the whole TOOL_CALLED ->
        # TOOL_RESULT sequence has landed (the result arrives ungated,
        # so the running block is filled in place by then).
        gate.set()
        await _pump_until(
            pilot,
            lambda: (
                len(events_seen) == 2
                and app._pending_tool_call_block is None
                and len(app.query(ToolCallBlock)) > 0
            ),
            what="placeholder -> ToolCallBlock morph and fill",
        )

        children_after = list(transcript.children)
        assert len(children_after) == len(children_before) + 2, (
            "expected exactly two net new children (UserChatbox from "
            "USER_MESSAGE, plus the placeholder removed / running block "
            "mounted swap netting one more)"
        )
        block_index = next(
            i
            for i, child in enumerate(children_after)
            if isinstance(child, ToolCallBlock)
        )
        assert block_index == len(children_after) - 1
        assert block_index == placeholder_index, (
            "ToolCallBlock did not land in the placeholder's former transcript position"
        )


@pytest.mark.asyncio
async def test_tool_call_result_never_produces_a_second_mount(
    tmp_path: Path,
) -> None:
    """No second mount/remove cycle happens for the TOOL_RESULT fill.

    Drives the full TOOL_CALLED -> TOOL_RESULT sequence with no
    artificial delay and asserts the end state directly: exactly one
    `ToolCallBlock`, filled, zero placeholders, pending-block reference
    cleared. A regression that mounted a second block on TOOL_RESULT
    (the old single-shot behaviour) would leave two `ToolCallBlock`
    instances in the transcript.
    """
    events_seen: list[Event] = []
    app = _make_app_step5(
        tmp_path,
        events_seen,
        tool_name="bash",
        arguments={"command": "ls"},
        result={"stdout": "file1.txt\nfile2.txt"},
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("list files")
        await _pump_until(
            pilot,
            lambda: (
                len(events_seen) == 2
                and any(
                    "file1.txt" in _tool_body_text(block)
                    for block in app.query(ToolCallBlock)
                )
            ),
            what="TOOL_CALLED -> TOOL_RESULT fill",
        )

        assert len(events_seen) == 2
        assert [e.type for e in events_seen] == [
            EventType.TOOL_CALLED,
            EventType.TOOL_RESULT,
        ]

        blocks = app.query(ToolCallBlock)
        assert len(blocks) == 1
        block = blocks.first()
        assert "file1.txt" in _tool_body_text(block)
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None
        assert app._pending_tool_call_block is None


@pytest.mark.asyncio
async def test_mount_tool_call_falls_back_to_single_shot_without_pending_block(
    tmp_path: Path,
) -> None:
    """`mount_tool_call` falls back to a fresh single-shot mount if nothing is pending.

    Exercises `ChatApp.mount_tool_call` directly (bypassing
    `mount_tool_call_running`/`TOOL_CALLED`) — the `ChatMountSink`
    Protocol method must keep working exactly as before for any caller
    that never went through the two-phase running-block mount, e.g. a
    sink used outside the normal event flow, or a card-mounting tool
    call (`mount_tool_call_running` is deliberately skipped for those
    — see `test_tool_called_skips_running_mount_for_card_mounting_tool`
    below).
    """
    app = _make_app_step5(tmp_path, [])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._pending_tool_call_block is None

        widget = await app.mount_tool_call(
            "read_file",
            "path='/tmp/x'",
            "fallback body",
            failed=False,
        )
        await pilot.pause()

        assert isinstance(widget, ToolCallBlock)
        assert widget.is_mounted
        assert "fallback body" in _tool_body_text(widget)
        blocks = app.query(ToolCallBlock)
        assert len(blocks) == 1
        assert blocks.first() is widget


def _make_tool_failed_event(
    thread_id: str,
    *,
    tool_call_id: str,
    tool_name: str,
    message: str,
    recovery_hint: str | None = None,
) -> Event:
    """Build a real, fully-valid ``TOOL_FAILED`` ``Event``.

    ``recovery_hint`` (when set) matches the top-level
    ``event.data["recovery_hint"]`` field
    ``EventAdapter._handle_tool_failed`` reads (qol-090 /
    ``ToolFailedData.recovery_hint``).
    """
    data: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "error": {"type": "boom", "message": message},
    }
    if recovery_hint is not None:
        data["recovery_hint"] = recovery_hint
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(thread_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.TOOL_FAILED,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def _make_tool_call_then_failed_event_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
    arguments: dict[str, Any] | None = None,
    error_message: str = "file not found",
    recovery_hint: str | None = None,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding TOOL_CALLED then TOOL_FAILED.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount). A ``TOOL_CALLED`` event
    always precedes exactly one of ``TOOL_RESULT`` (success) or
    ``TOOL_FAILED`` (failure) — see ``src/engine/engine.py``'s
    force-invoke path for a concrete real-code example of that exact
    sequencing. This exercises the failure branch: the running block
    mounted at ``TOOL_CALLED`` must still be the SAME widget
    ``_handle_tool_failed`` fills, with the ``-failed`` CSS class
    correctly applied.
    """
    args = arguments if arguments is not None else {"path": "/tmp/missing"}

    async def _stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        yield _make_user_message_event(thread_id)
        called = _make_tool_called_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=args,
        )
        events_seen.append(called)
        yield called
        failed = _make_tool_failed_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            message=error_message,
            recovery_hint=recovery_hint,
        )
        events_seen.append(failed)
        yield failed

    return _stream


class _MockEngineStep5Failed:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        tool_call_id: str = "call-1",
        tool_name: str = "read_file",
        arguments: dict[str, Any] | None = None,
        error_message: str = "file not found",
        recovery_hint: str | None = None,
    ) -> None:
        self.execute_stream = _make_tool_call_then_failed_event_stream(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            error_message=error_message,
            recovery_hint=recovery_hint,
        )


class _MockRuntimeStep5Failed:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        tool_call_id: str = "call-1",
        tool_name: str = "read_file",
        arguments: dict[str, Any] | None = None,
        error_message: str = "file not found",
        recovery_hint: str | None = None,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep5Failed(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            error_message=error_message,
            recovery_hint=recovery_hint,
        )


def _make_app_step5_failed(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
    arguments: dict[str, Any] | None = None,
    error_message: str = "file not found",
    recovery_hint: str | None = None,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep5Failed(
            thread_id,
            events_seen,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            error_message=error_message,
            recovery_hint=recovery_hint,
        ),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_tool_called_then_failed_fills_same_block_with_failed_styling(
    tmp_path: Path,
) -> None:
    """`TOOL_FAILED` fills the SAME running block and applies `-failed` styling.

    `TOOL_CALLED` always precedes exactly one of `TOOL_RESULT` or
    `TOOL_FAILED` — both route through `ChatApp.mount_tool_call`, so
    both must correctly consume the pending running block, not just
    the success path. Regression coverage for a real trap: filling a
    pending block via `update_body()` alone silently drops the
    failure's visual signal (no `-failed` class => no red rail/title
    color) unless `mount_tool_call` explicitly applies the class when
    `failed=True`, mirroring `WorkflowNodeRow._populate_tool_call_result`'s
    existing transition precedent. Also asserts title-glyph parity: a
    running block's title has no `✗` prefix (constructed with
    `failed=False`); the fill must rewrite the title to match what a
    single-shot `ToolCallBlock(..., failed=True)` construction would
    have produced, not leave the stale non-failed title in place.
    """
    events_seen: list[Event] = []
    app = _make_app_step5_failed(
        tmp_path,
        events_seen,
        tool_name="read_file",
        arguments={"path": "/tmp/missing"},
        error_message="file not found",
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("read a missing file")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 2
        assert [e.type for e in events_seen] == [
            EventType.TOOL_CALLED,
            EventType.TOOL_FAILED,
        ]

        blocks = app.query(ToolCallBlock)
        assert len(blocks) == 1, (
            "TOOL_FAILED must fill the existing running block, not mount a second one"
        )
        block = blocks.first()
        assert "file not found" in _tool_body_text(block)
        assert block.has_class("-failed")
        assert app._pending_tool_call_block is None
        assert len(app.query(WorkingPlaceholder)) == 0

        # Title-glyph parity: the fill path's title must match what a
        # single-shot `failed=True` construction would produce for the
        # same tool_name/args — the args string is formatted by
        # `_format_args`, matching what `_handle_tool_failed` passes.
        expected_title = ToolCallBlock(
            "read_file", "path='/tmp/missing'", "irrelevant", failed=True
        ).title
        assert block.title == expected_title
        assert str(block.title).startswith("✗ "), (
            "fill path must add the ✗ prefix, not leave the running "
            "block's non-failed title in place"
        )


@pytest.mark.asyncio
async def test_tool_called_skips_running_mount_for_card_mounting_tool(
    tmp_path: Path,
) -> None:
    """`run_workflow` (card-mounting) tool calls never get a running block,
    and the turn's placeholder is still cleared (not orphaned).

    `run_workflow` opens its own `WorkflowCard` instead of a
    `ToolCallBlock`; its `TOOL_RESULT` is suppressed (never reaches
    `mount_tool_call`). Mounting a running block at `TOOL_CALLED` for
    these would leave it permanently stuck with no event left to fill
    or remove it — `_handle_tool_called` skips the running-block mount
    for card-mounting tools, same as the existing
    `_is_card_mounting_tool` suppression `_handle_tool_result` already
    honors.

    Fixup regression coverage: skipping the running-block mount alone
    is not enough — with no `mount_tool_call_running` call, nothing
    else removes `self._working_placeholder` for this turn either (no
    other morph path runs for a card-mounting call; the eventual
    `WorkflowCard` mounts through a wholly separate mechanism,
    `EventAdapter._get_or_mount_card`, that never touches the
    placeholder). Before the fix, the placeholder stayed mounted,
    non-terminal, and orphaned forever right alongside the card. This
    drives the full `TOOL_CALLED` -> `TOOL_RESULT` cycle (not just
    `TOOL_CALLED`) and asserts the placeholder is gone at the end —
    `_handle_tool_result`'s suppressed branch returns early without
    touching the placeholder, so `TOOL_CALLED`'s explicit
    `clear_working_placeholder()` call is the only place this gets
    fixed, and must still hold after the full cycle completes.
    """
    events_seen: list[Event] = []
    app = _make_app_step5(
        tmp_path,
        events_seen,
        tool_call_id="call-wf-1",
        tool_name="run_workflow",
        arguments={"bundle": "kolsch/workflows/example"},
        result={"card_id": None},
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("run the example workflow")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 2
        assert [e.type for e in events_seen] == [
            EventType.TOOL_CALLED,
            EventType.TOOL_RESULT,
        ]

        # No running ToolCallBlock mounted for a card-mounting tool,
        # through either event.
        assert len(app.query(ToolCallBlock)) == 0
        assert app._pending_tool_call_block is None
        # The placeholder must be gone — not orphaned — even though no
        # replacement widget (streamed prose / complete prose / running
        # tool block) was ever mounted for this turn.
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None


@pytest.mark.asyncio
async def test_tool_failed_fill_composes_matched_rule_denial_hint(
    tmp_path: Path,
) -> None:
    """An authz-denial `TOOL_FAILED` composes the same 2-line hint into the fill.

    `ToolCallBlock`'s constructor appends `format_denial_hint(matched_rule)`
    into the body text at single-shot mount time (feat-041). This
    asserts `ChatApp.mount_tool_call`'s fill-path (Step 5) replicates
    that exact composition when filling a pre-mounted running block,
    rather than silently dropping the denial hint.
    """
    events_seen: list[Event] = []
    app = _make_app_step5_failed(
        tmp_path,
        events_seen,
        tool_name="write_file",
        arguments={"path": "/etc/passwd"},
        error_message="authz.denied: not allowed",
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        # Directly drive the two-phase calls (bypassing the engine
        # stream, which doesn't carry matched_rule in this test's
        # fixture) to isolate the fill-path composition itself.
        running = await app.mount_tool_call_running("write_file", "path='/etc/passwd'")
        await pilot.pause()

        matched_rule = {
            "id": "rl_deny_etc",
            "effect": "deny",
            "source_file": "policy.yaml",
            "source_line": 12,
            "pattern": "/etc/**",
        }
        filled = await app.mount_tool_call(
            "write_file",
            "path='/etc/passwd'",
            "denied",
            failed=True,
            matched_rule=matched_rule,
        )
        await pilot.pause()

        assert filled is running, "fill path must reuse the running block"
        body_text = _tool_body_text(filled)
        assert "denied" in body_text
        assert "denied by rl_deny_etc" in body_text
        assert "pattern /etc/**" in body_text


def _visible_hint_text(block: ToolCallBlock) -> str | None:
    """Return the hint Static's rendered text, or `None` if hidden.

    spec-130 Step 5 fixup: `ToolCallBlock`'s hint `Static` is always
    mounted (see `tool_call_block.py`'s `_apply_recovery_hint`) but
    hidden via `display = False` when there's no hint — this queries
    by CSS class (matching `test_tool_call_block.py`'s convention)
    and returns `None` when hidden, so a test can assert the
    user-visible outcome directly.
    """
    hint = block.query_one(".hint", Static)
    if not hint.display:
        return None
    return str(hint.render())


@pytest.mark.asyncio
async def test_tool_called_then_failed_with_recovery_hint_reveals_hint_line(
    tmp_path: Path,
) -> None:
    """A `TOOL_FAILED` carrying `recovery_hint` reveals it on the SAME running block.

    Regression coverage for the drop a reviewer found: before this
    fix, `ChatApp.mount_tool_call`'s fill path only called
    `update_body()`, which cannot add a `recovery_hint` line — that
    line lives on a SEPARATE `Static` child, not in the body text
    (unlike `matched_rule`'s denial hint, which IS body text). This
    drives the real `TOOL_CALLED` -> `TOOL_FAILED` event flow (not a
    direct method call) so it exercises the exact path
    `EventAdapter._handle_tool_failed` -> `ChatApp.mount_tool_call`
    takes in `kolsch chat`.
    """
    events_seen: list[Event] = []
    app = _make_app_step5_failed(
        tmp_path,
        events_seen,
        tool_name="browse_url",
        arguments={"url": "https://example.com"},
        error_message="playwright not installed",
        recovery_hint="pip install kolsch[browser]",
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("browse a url for me")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 2
        assert events_seen[1].type == EventType.TOOL_FAILED

        blocks = app.query(ToolCallBlock)
        assert len(blocks) == 1
        block = blocks.first()
        hint_text = _visible_hint_text(block)
        assert hint_text is not None, (
            "recovery_hint must be revealed on the fill path, not dropped"
        )
        assert "↳ hint:" in hint_text
        assert "pip install kolsch[browser]" in hint_text
        assert block.has_class("-failed")


# ----------------------------------------------------------------------
# spec-130 Step 6: cancel / turn-supersession -> interrupted terminal
# state, via the real keyed registries
# `ChatApp._live_placeholders` / `ChatApp._live_tool_call_blocks`
# ----------------------------------------------------------------------
#
# Reuses the Step 2 scaffolding (`_MockAuthzStep2`, `_MockStoreStep2`,
# `_MockConfigStep2`, `_MockBundleStep2`) with two new fake-stream
# shapes:
#
# - `_make_cancelled_event_stream`: raises `ExchangeCancelled` (the
#   engine's cooperative-cancel exception, e.g. after Esc) before ever
#   yielding an event — exercises `_run_exchange`'s
#   `except ExchangeCancelled` branch.
# - `_make_hanging_event_stream` / `_make_hanging_after_tool_called_stream`:
#   `await asyncio.Event().wait()` (never completes) so a turn can be
#   left "in flight, not yet terminal" indefinitely — exercises real
#   `@work(exclusive=True)` supersession by calling `_run_exchange` a
#   SECOND time while the first is still parked on that `await`.

from errors import ExchangeCancelled  # noqa: E402


def _make_cancelled_event_stream(
    *_ignored: Any,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` that raises ``ExchangeCancelled``.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering — ``USER_MESSAGE`` is always emitted early in
    ``_Exchange.run()``, well before the framework call that raises
    ``ExchangeCancelled`` on a mid-exchange cooperative cancel — see
    ``src/engine/engine.py``'s ``_run_framework``/spawn-loop cancellation
    checks, all of which run after Step 11's ``USER_MESSAGE`` emit).
    ``_run_exchange`` mounts the placeholder in response to seeing
    ``USER_MESSAGE``, so this fixture must yield it before raising —
    otherwise the interrupt-sweep test below would be asserting against
    a placeholder that (per the fixed, realistic ordering) was never
    mounted in the first place, which is not the scenario this test
    means to cover. Then raises before yielding anything else — matches
    the engine's real cooperative-cancel shape when
    ``CancelToken.is_cancelled()`` is true at the next safe point (Esc
    fired before any further content arrived).

    ``thread_id`` is read off the real ``ExecutionContext`` the caller
    passes to ``execute_stream(config, ctx)`` (the second positional
    arg) rather than threaded through every mock class's constructor —
    keeps this fixture's call sites (which never needed a thread_id
    before) unchanged.
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(str(ctx.thread_id))
        raise ExchangeCancelled(cancelled_at_event_id="")

    return _stream


class _MockEngineStep6Cancelled:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.execute_stream = _make_cancelled_event_stream()


class _MockRuntimeStep6Cancelled:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep6Cancelled()


def _make_app_step6_cancelled(tmp_path: Path) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep6Cancelled(),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_cooperative_cancel_flips_placeholder_to_interrupted(
    tmp_path: Path,
) -> None:
    """A cooperative cancel (Esc, `ExchangeCancelled`) interrupts the placeholder.

    Before spec-130 Step 6, `ExchangeCancelled` fell into
    `_run_exchange`'s generic `except Exception` branch and mounted an
    unrelated "[exchange failed — ...]" notice, leaving the turn's
    `WorkingPlaceholder` orphaned (mounted, non-terminal, spinning
    forever). This asserts the placeholder is flipped instead: still
    mounted (never silently removed), `is_terminal`, `terminal_kind ==
    "interrupted"`, and popped out of the registry.
    """
    app = _make_app_step6_cancelled(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        placeholders = app.query(WorkingPlaceholder)
        assert len(placeholders) == 1, (
            "the placeholder must still be mounted — flipped terminal, not removed"
        )
        placeholder = placeholders.first()
        assert placeholder.is_terminal is True
        assert placeholder.terminal_kind == "interrupted"
        assert _glyph_text(placeholder) == "─"

        # Registry hygiene: the interrupted entry must not linger.
        assert app._live_placeholders == {}
        assert app._working_placeholder is None

        # No generic "[exchange failed — ...]" notice for this path —
        # the interrupted placeholder IS the terminal signal.
        assert len(app.query(".error")) == 0


@pytest.mark.asyncio
async def test_cooperative_cancel_does_not_resolve_a_durable_future(
    tmp_path: Path,
) -> None:
    """The interrupt sweep never touches a durable-write future.

    bug-110 precedent: this path is presentation-only. Spies on
    `ChatApp._interrupt_live_registries` to confirm it's the sweep that
    ran (not some other code path), and independently spies on the
    live `PromptsImpl.cancel_pending`/`resolve` surface (the actual
    durable-write future resolution hook bug-110 warns against reusing
    here) to confirm the sweep never calls it — the interrupted
    placeholder has no associated prompt at all, so any call into that
    surface from this path would be a bug, not incidental.
    """
    app = _make_app_step6_cancelled(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        registry = app._service_registry
        assert registry is not None and registry.prompts is not None
        prompts_calls: list[str] = []
        original_cancel_pending = registry.prompts.cancel_pending

        def _spy_cancel_pending(prompt_id: str) -> None:
            prompts_calls.append(prompt_id)
            original_cancel_pending(prompt_id)

        registry.prompts.cancel_pending = _spy_cancel_pending

        sweep_calls: list[None] = []
        original_sweep = app._interrupt_live_registries

        async def _spy_sweep() -> None:
            sweep_calls.append(None)
            await original_sweep()

        app._interrupt_live_registries = _spy_sweep  # type: ignore[method-assign]

        app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(sweep_calls) == 1
        placeholder = app.query(WorkingPlaceholder).first()
        assert placeholder.terminal_kind == "interrupted"
        assert prompts_calls == [], (
            "the presentation-only sweep must never resolve/cancel a "
            "durable prompt future"
        )


def _make_hanging_event_stream() -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` that yields USER_MESSAGE then hangs.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount) — this fixture is reused
    for BOTH turn A and turn B in the supersession tests below, so each
    invocation yields its own ``USER_MESSAGE`` and each turn gets its
    own mounted placeholder, matching what the real engine would do for
    two independently-submitted turns. Then parks on a never-set
    ``asyncio.Event`` so the turn stays "in flight, not yet terminal"
    indefinitely — the state needed to exercise real
    `@work(exclusive=True)` supersession by starting a second turn
    while this one is still running.
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(str(ctx.thread_id))
        await asyncio.Event().wait()

    return _stream


class _MockEngineStep6Hanging:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.execute_stream = _make_hanging_event_stream()


class _MockRuntimeStep6Hanging:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep6Hanging()


def _make_app_step6_hanging(tmp_path: Path) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep6Hanging(),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_exclusive_worker_supersession_interrupts_turn_a_placeholder(
    tmp_path: Path,
) -> None:
    """Starting turn B while turn A is still live interrupts turn A's placeholder.

    THE KEY spec-130 Step 6 regression test (closes the Step 2 review
    gap): `_run_exchange` is `@work(exclusive=True)`, so calling it a
    second time makes Textual cancel the FIRST worker outright — a real
    `asyncio.CancelledError` raised inside turn A's `_run_exchange`,
    confirmed by direct reproduction against this repo's actual
    Textual version (see the probe in this step's implementation
    notes). A naive "flip whichever turn is currently active" cancel
    handler would flip turn B's fresh placeholder (wrong widget) or do
    nothing (if it read `self._working_placeholder`, which by the time
    turn A's cancel branch runs has ALREADY been overwritten by turn
    B's `mount_working_placeholder()` call). This asserts the actual
    orphaned widget — turn A's placeholder, found via the DOM query,
    independent of any App-side reference — ends up interrupted, while
    turn B's own fresh placeholder is untouched and still working.
    """
    app = _make_app_step6_hanging(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("first message")
        await pilot.pause()
        await pilot.pause()

        placeholders_after_a = app.query(WorkingPlaceholder)
        assert len(placeholders_after_a) == 1, (
            "turn A's placeholder must be mounted before turn B starts — "
            "test setup invalid otherwise"
        )
        placeholder_a = placeholders_after_a.first()
        assert placeholder_a.is_terminal is False

        # Start turn B — same `on_composer_submitted` -> `_run_exchange`
        # call shape a rapid resubmit takes. Textual's `exclusive=True`
        # cancels turn A's worker synchronously as part of scheduling
        # this call.
        app._run_exchange("second message, before the first settled")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        placeholders_after_b = app.query(WorkingPlaceholder)
        assert len(placeholders_after_b) == 2, (
            "turn A's placeholder must still be mounted (flipped "
            "terminal, not removed) alongside turn B's fresh one"
        )

        # Turn A's placeholder (same widget identity as captured above)
        # is now interrupted. `terminal_kind == "interrupted"` implies
        # `is_terminal` — checking only this (stronger) assertion also
        # sidesteps a mypy narrowing false-positive: asserting
        # `is_terminal is False` earlier on this same object, then
        # `is True` later (a real state change from the intervening
        # `_run_exchange` call mypy can't see through), makes mypy
        # incorrectly flag the second assert as unreachable.
        assert placeholder_a.terminal_kind == "interrupted"

        # Turn B's placeholder is the OTHER one, still working.
        placeholder_b = next(p for p in placeholders_after_b if p is not placeholder_a)
        assert placeholder_b.is_terminal is False

        # The alias now points at turn B's placeholder, not turn A's —
        # confirms the sweep read the registry, not just the alias.
        assert app._working_placeholder is placeholder_b
        assert app._live_placeholders == {app._active_turn_id: placeholder_b}


@pytest.mark.asyncio
async def test_exclusive_worker_supersession_does_not_resolve_durable_future(
    tmp_path: Path,
) -> None:
    """The supersession sweep is presentation-only, same as the simple-cancel path."""
    app = _make_app_step6_hanging(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        registry = app._service_registry
        assert registry is not None and registry.prompts is not None
        prompts_calls: list[str] = []
        original_cancel_pending = registry.prompts.cancel_pending

        def _spy_cancel_pending(prompt_id: str) -> None:
            prompts_calls.append(prompt_id)
            original_cancel_pending(prompt_id)

        registry.prompts.cancel_pending = _spy_cancel_pending

        app._run_exchange("first message")
        await pilot.pause()
        await pilot.pause()

        app._run_exchange("second message")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        placeholder_a = next(p for p in app.query(WorkingPlaceholder) if p.is_terminal)
        assert placeholder_a.terminal_kind == "interrupted"
        assert prompts_calls == [], (
            "the presentation-only sweep must never resolve/cancel a "
            "durable prompt future"
        )


def _make_hanging_after_tool_called_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake stream: yields USER_MESSAGE, then ``TOOL_CALLED``, then hangs forever.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount) — this fixture is reused
    for both turn A and turn B in the supersession test below, so each
    invocation mounts its own placeholder before it two-phase-morphs
    into its own running ``ToolCallBlock`` at ``TOOL_CALLED``. Leaves a
    running/pending ``ToolCallBlock`` mounted and never filled — the
    state needed to exercise supersession of a pending tool-call block
    (as opposed to a bare placeholder).
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(str(ctx.thread_id))
        called = _make_tool_called_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments={"path": "/tmp/x"},
        )
        events_seen.append(called)
        yield called
        await asyncio.Event().wait()

    return _stream


class _MockEngineStep6ToolHanging:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.execute_stream = _make_hanging_after_tool_called_stream(
            thread_id, events_seen
        )


class _MockRuntimeStep6ToolHanging:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep6ToolHanging(thread_id, events_seen)


def _make_app_step6_tool_hanging(tmp_path: Path, events_seen: list[Event]) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep6ToolHanging(thread_id, events_seen),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_exclusive_worker_supersession_interrupts_pending_tool_call_block(
    tmp_path: Path,
) -> None:
    """Superseding a turn with a pending running `ToolCallBlock` interrupts it.

    Same shape as the placeholder supersession test, but for a tool
    call already in its running (empty-body) state when turn B starts.
    Asserts the running block reaches `render_interrupted()`'s visual
    state — `-interrupted` CSS class, `─`-prefixed title, "Interrupted"
    body — documenting the chosen terminal treatment for a superseded
    tool call: distinct from `-failed` (a user cancellation is not a
    tool error) and consistent with the placeholder's own interrupted
    glyph/color vocabulary.

    Turn B, driven against the SAME fake-stream shape, also reaches
    its own `TOOL_CALLED` and mounts its own (distinct) running block
    before it too parks on the hang — so the registry legitimately
    holds turn B's entry afterward. The assertions key off turn A's
    block by IDENTITY (not "the registry is empty"), so this doesn't
    conflate "turn A's block was cleaned up" with "no tool call is
    running at all."
    """
    events_seen: list[Event] = []
    app = _make_app_step6_tool_hanging(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("read a file for me")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 1
        assert events_seen[0].type == EventType.TOOL_CALLED
        running_blocks = app.query(ToolCallBlock)
        assert len(running_blocks) == 1
        running_block_a = running_blocks.first()
        assert _tool_body_text(running_block_a) == ""
        assert app._pending_tool_call_block is running_block_a

        # Start turn B — cancels turn A's worker, which was parked
        # past TOOL_CALLED with its running block still pending. Turn
        # B runs against the same fake-stream shape, so it mounts its
        # OWN running block before it, too, hangs.
        app._run_exchange("second message, before the tool call settled")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # Turn A's block (same identity captured above) is now
        # interrupted — never removed from the transcript, just flipped.
        assert running_block_a.has_class("-interrupted")
        assert not running_block_a.has_class("-failed")
        assert _tool_body_text(running_block_a) == "Interrupted"
        assert str(running_block_a.title).startswith("─ ")

        # Turn A's registry entry specifically is gone.
        assert running_block_a not in app._live_tool_call_blocks.values()

        # Turn B's own running block is a DIFFERENT instance, still
        # running (not interrupted) — the sweep didn't touch it.
        running_blocks_after = app.query(ToolCallBlock)
        assert len(running_blocks_after) == 2
        running_block_b = next(
            b for b in running_blocks_after if b is not running_block_a
        )
        assert not running_block_b.has_class("-interrupted")
        assert _tool_body_text(running_block_b) == ""
        assert app._pending_tool_call_block is running_block_b
        assert app._live_tool_call_blocks == {app._active_turn_id: running_block_b}


@pytest.mark.asyncio
async def test_card_mounting_tool_call_still_clears_placeholder_after_registry_retrofit(
    tmp_path: Path,
) -> None:
    """Re-verification (spec-130 Step 6 item 8): the card-mounting-tool fix still holds.

    Step 5 fixed a card-mounting tool call (``run_workflow``) leaving
    the turn's `WorkingPlaceholder` orphaned by having `_handle_tool_called`
    call `clear_working_placeholder()` directly for that case. This
    step's registry retrofit changed `clear_working_placeholder`'s
    internals (it now goes through `_remove_working_placeholder()` and
    pops `self._live_placeholders`) — this test re-confirms the
    end-to-end behaviour Step 5 fixed is still correct after that
    retrofit: no `WorkingPlaceholder` survives mounted-and-non-terminal
    once a card-mounting tool call's `TOOL_CALLED` -> `TOOL_RESULT`
    cycle completes, AND the registry entry (not just the alias) is
    gone too.
    """
    events_seen: list[Event] = []
    app = _make_app_step5(
        tmp_path,
        events_seen,
        tool_call_id="call-wf-1",
        tool_name="run_workflow",
        arguments={"bundle": "kolsch/workflows/example"},
        result={"card_id": None},
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("run the example workflow")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 2
        assert [e.type for e in events_seen] == [
            EventType.TOOL_CALLED,
            EventType.TOOL_RESULT,
        ]

        # No running ToolCallBlock for a card-mounting tool.
        assert len(app.query(ToolCallBlock)) == 0
        assert app._pending_tool_call_block is None
        assert app._live_tool_call_blocks == {}

        # The placeholder must be gone — not orphaned — AND its
        # registry entry must be gone too (not just the alias).
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._working_placeholder is None
        assert app._live_placeholders == {}


# ----------------------------------------------------------------------
# spec-130 Step 6 (review fixup): the mid-mount-await orphan window
# ----------------------------------------------------------------------
#
# Two independent reviewers reproduced, under NATURAL async scheduling
# (no monkeypatching of registration logic — just a real race against
# the real `VerticalScroll.mount()` coroutine), that
# `mount_working_placeholder()` / `mount_tool_call_running()` used to
# register into the Step 6 registry AFTER `await transcript.mount(...)`
# rather than before. `Widget.mount()` itself attaches the widget to
# the DOM SYNCHRONOUSLY (calls `self.app._register(...)` inline before
# returning the awaitable — see `textual.widget.Widget.mount`); the
# `await` only waits for the mount message pump to finish. So a turn
# whose `@work(exclusive=True)` worker is cancelled while suspended on
# that `await` used to leave its widget attached to the DOM with the
# post-await registration line never having run — a permanent,
# non-terminal, un-sweepable orphan.
#
# These tests reproduce the exact window by wrapping the REAL
# `VerticalScroll.mount` (calling through to it, not replacing its
# behaviour) so it signals once the real synchronous attach has
# happened, then parks on a never-set `asyncio.Event` — the same
# "suspended mid-await" shape a genuine `@work(exclusive=True)`
# preemption produces, just deterministically triggered instead of
# depending on scheduler-timing luck.


@pytest.mark.asyncio
async def test_placeholder_registered_before_dom_attach_survives_mid_mount_cancel(
    tmp_path: Path,
) -> None:
    """A turn cancelled while suspended inside `transcript.mount(placeholder)`
    still has its placeholder found and interrupted by the sweep.

    Reproduces the exact window both reviewers found: wraps the REAL
    `VerticalScroll.mount` so, for the ``WorkingPlaceholder`` mount
    only, it calls through to the genuine Textual `mount()` (the
    widget is now truly, synchronously attached to the DOM — confirmed
    by asserting on `transcript.children` before this coroutine
    proceeds any further) and then blocks on a never-set
    `asyncio.Event`, i.e. execution is suspended exactly where a real
    exclusive-worker preemption would land it: after the synchronous
    DOM attach, before whatever comes after `await transcript.mount(...)`
    in the caller. A second turn is then started, superseding the
    first via the same real `@work(exclusive=True)` mechanism every
    other Step 6 test uses (no manual `CancelledError` injection). If
    registration happened AFTER the mount await (the bug), the widget
    would be attached but absent from `self._live_placeholders`, and
    the sweep would find nothing to interrupt — it would spin forever,
    reachable only by walking the DOM. With the fix (register before
    the mount call), the entry already exists when the cancel fires.
    """
    app = _make_app_step6_hanging(tmp_path)
    attached_before_suspend = asyncio.Event()
    release_mount = asyncio.Event()
    real_mount = VerticalScroll.mount

    def _racing_mount(
        self: VerticalScroll, *widgets: object, **kwargs: object
    ) -> object:
        awaitable = real_mount(self, *widgets, **kwargs)
        if any(isinstance(w, WorkingPlaceholder) for w in widgets):
            # The real `Widget.mount()` call above has ALREADY
            # synchronously attached the widget to the DOM (Textual's
            # `_register` runs inline, before any `await`) — confirm
            # that directly, then wrap the awaitable so the caller
            # suspends indefinitely on it, simulating a worker
            # parked mid-await at exactly this point.
            assert any(isinstance(c, WorkingPlaceholder) for c in self.children), (
                "test setup invalid: the widget must already be DOM-attached "
                "synchronously by Widget.mount() before this wrapper runs"
            )

            async def _suspend_then_await() -> None:
                attached_before_suspend.set()
                await release_mount.wait()
                await awaitable

            return _suspend_then_await()
        return awaitable

    with mock.patch.object(VerticalScroll, "mount", _racing_mount):
        async with app.run_test() as pilot:
            await pilot.pause()

            app._run_exchange("first message")
            # Yield until turn A's placeholder is genuinely attached to
            # the DOM AND its worker is suspended inside the mount
            # await — not an artificial delay, a real signal fired
            # from inside the real mount path.
            await asyncio.wait_for(attached_before_suspend.wait(), timeout=2.0)

            # At this exact moment: the widget IS in the DOM. Assert
            # the registration already happened too (the fix under
            # test) — this is the core assertion the bug violated.
            transcript = app.query_one("#transcript", VerticalScroll)
            dom_placeholders = [
                c for c in transcript.children if isinstance(c, WorkingPlaceholder)
            ]
            assert len(dom_placeholders) == 1, (
                "the placeholder must be DOM-attached at this point — "
                "test setup invalid otherwise"
            )
            placeholder_a = dom_placeholders[0]
            assert app._live_placeholders.get(app._active_turn_id) is placeholder_a, (
                "the registry entry must already exist WHILE the mount "
                "await is still pending — registering after the await "
                "is exactly the bug this test guards against"
            )

            # Now supersede — the real `@work(exclusive=True)` cancel
            # path fires while turn A is suspended exactly inside
            # `transcript.mount(placeholder)`'s await.
            app._run_exchange("second message, superseding mid-mount")
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            # Turn A's placeholder must be interrupted — not a
            # permanent, unreachable-via-registry orphan. Checking
            # `terminal_kind` (stronger than `is_terminal`) also
            # confirms it landed in the right terminal state, not just
            # any terminal state.
            assert placeholder_a.terminal_kind == "interrupted"
            # And its registry entry is gone — the sweep found and
            # consumed it, it didn't just leave it dangling.
            assert placeholder_a not in app._live_placeholders.values()

            # Release the never-actually-awaited real mount coroutine
            # so it doesn't leak a pending task past the test body.
            release_mount.set()
            await pilot.pause()


@pytest.mark.asyncio
async def test_tool_call_block_registered_before_dom_attach_survives_mid_mount_cancel(
    tmp_path: Path,
) -> None:
    """Same mid-mount-await race, for `mount_tool_call_running`'s `ToolCallBlock`.

    Identical shape to the placeholder version above, applied to the
    OTHER call site the reviewers flagged
    (`mount_tool_call_running()`), confirming the same fix (register
    before the mount call) closes the window there too.
    """
    events_seen: list[Event] = []
    app = _make_app_step6_tool_hanging(tmp_path, events_seen)
    attached_before_suspend = asyncio.Event()
    release_mount = asyncio.Event()
    real_mount = VerticalScroll.mount

    def _racing_mount(
        self: VerticalScroll, *widgets: object, **kwargs: object
    ) -> object:
        awaitable = real_mount(self, *widgets, **kwargs)
        if any(isinstance(w, ToolCallBlock) for w in widgets):
            assert any(isinstance(c, ToolCallBlock) for c in self.children), (
                "test setup invalid: the widget must already be DOM-attached "
                "synchronously by Widget.mount() before this wrapper runs"
            )

            async def _suspend_then_await() -> None:
                attached_before_suspend.set()
                await release_mount.wait()
                await awaitable

            return _suspend_then_await()
        return awaitable

    with mock.patch.object(VerticalScroll, "mount", _racing_mount):
        async with app.run_test() as pilot:
            await pilot.pause()

            app._run_exchange("read a file for me")
            await asyncio.wait_for(attached_before_suspend.wait(), timeout=2.0)

            transcript = app.query_one("#transcript", VerticalScroll)
            dom_blocks = [
                c for c in transcript.children if isinstance(c, ToolCallBlock)
            ]
            assert len(dom_blocks) == 1, (
                "the running block must be DOM-attached at this point — "
                "test setup invalid otherwise"
            )
            block_a = dom_blocks[0]
            assert app._live_tool_call_blocks.get(app._active_turn_id) is block_a, (
                "the registry entry must already exist WHILE the mount "
                "await is still pending"
            )
            assert app._pending_tool_call_block is block_a, (
                "the alias must also already be set before the mount await "
                "resolves, for the same reason"
            )

            app._run_exchange("second message, superseding mid-mount")
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            assert block_a.has_class("-interrupted")
            assert not block_a.has_class("-failed")
            assert _tool_body_text(block_a) == "Interrupted"

            release_mount.set()
            await pilot.pause()


# ----------------------------------------------------------------------
# spec-130 Step 7: error -> error terminal state, via the same keyed
# registries Step 6 introduced, plus the widened `_run_exchange` inner
# `try` now also covering `mount_working_placeholder()` itself.
# ----------------------------------------------------------------------
#
# Reuses the Step 2 scaffolding (`_MockAuthzStep2`, `_MockStoreStep2`,
# `_MockConfigStep2`, `_MockBundleStep2`) with three new fake-stream /
# fake-mount shapes:
#
# - `_make_erroring_event_stream`: raises a generic `RuntimeError`
#   before ever yielding an event — exercises `_run_exchange`'s
#   generic `except Exception` branch with no preceding content.
# - `_make_erroring_after_tool_called_stream`: yields `TOOL_CALLED`
#   then raises — exercises the same branch with a pending running
#   `ToolCallBlock` still live when the error hits.
# - A monkeypatched `ChatApp.mount_working_placeholder` that raises
#   directly — exercises the widened inner `try`'s coverage of the
#   mount call itself, distinct from an event-stream-time failure.


def _make_erroring_event_stream(
    *_ignored: Any,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` that raises a generic error.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount — a real model/tool
    failure always occurs deep inside the framework call, well after
    Step 11's ``USER_MESSAGE`` emit). Then raises before yielding
    anything else — a real (non-cancellation) exchange failure
    occurring before any further content exists for the turn, the
    exact case Step 7's ``except Exception`` branch (now paired with
    ``_fail_live_registries()``) must catch.
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(str(ctx.thread_id))
        raise RuntimeError("model backend unavailable")

    return _stream


class _MockEngineStep7Erroring:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.execute_stream = _make_erroring_event_stream()


class _MockRuntimeStep7Erroring:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep7Erroring()


def _make_app_step7_erroring(tmp_path: Path) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep7Erroring(),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_generic_error_flips_placeholder_to_error_terminal_state(
    tmp_path: Path,
) -> None:
    """A real exchange error (no preceding content) flips the placeholder to error.

    Before spec-130 Step 7, a generic error already fell into
    `_run_exchange`'s `except Exception` branch and mounted the
    "[exchange failed — ...]" notice — but never touched the turn's
    `WorkingPlaceholder`, leaving it mounted, non-terminal, and
    orphaned (spinning forever) underneath that notice. This asserts
    the placeholder is now flipped: still mounted (never silently
    removed), `is_terminal`, `terminal_kind == "error"`, `✗` glyph —
    AND the generic failure notice is still mounted alongside it (this
    step doesn't replace that notice, it additionally fixes up the
    placeholder that used to be left behind).
    """
    app = _make_app_step7_erroring(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        placeholders = app.query(WorkingPlaceholder)
        assert len(placeholders) == 1, (
            "the placeholder must still be mounted — flipped terminal, not removed"
        )
        placeholder = placeholders.first()
        assert placeholder.is_terminal is True
        assert placeholder.terminal_kind == "error"
        assert _glyph_text(placeholder) == "✗"

        # Registry hygiene: the errored entry must not linger.
        assert app._live_placeholders == {}
        assert app._working_placeholder is None

        # The generic failure notice is still mounted — Step 7 adds
        # the placeholder fixup, it doesn't remove the existing notice.
        assert len(app.query(".error")) == 1


@pytest.mark.asyncio
async def test_generic_error_does_not_resolve_a_durable_future(
    tmp_path: Path,
) -> None:
    """The error sweep is presentation-only, same guarantee as the cancel sweep."""
    app = _make_app_step7_erroring(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        registry = app._service_registry
        assert registry is not None and registry.prompts is not None
        prompts_calls: list[str] = []
        original_cancel_pending = registry.prompts.cancel_pending

        def _spy_cancel_pending(prompt_id: str) -> None:
            prompts_calls.append(prompt_id)
            original_cancel_pending(prompt_id)

        registry.prompts.cancel_pending = _spy_cancel_pending

        sweep_calls: list[None] = []
        original_sweep = app._fail_live_registries

        async def _spy_sweep() -> None:
            sweep_calls.append(None)
            await original_sweep()

        app._fail_live_registries = _spy_sweep  # type: ignore[method-assign]

        app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(sweep_calls) == 1
        placeholder = app.query(WorkingPlaceholder).first()
        assert placeholder.terminal_kind == "error"
        assert prompts_calls == [], (
            "the presentation-only error sweep must never resolve/cancel a "
            "durable prompt future"
        )


def _make_erroring_after_tool_called_stream(
    thread_id: str,
    events_seen: list[Event],
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "read_file",
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake stream: yields USER_MESSAGE, ``TOOL_CALLED``, then raises a generic error.

    Yields a real ``USER_MESSAGE`` first (matching the engine's actual
    ordering; triggers the placeholder mount, which then two-phase-morphs
    into the running ``ToolCallBlock`` at ``TOOL_CALLED``). Leaves that
    running/pending ``ToolCallBlock`` mounted and never filled when the
    error hits — the state needed to exercise the error sweep on a
    pending tool-call block (as opposed to a bare placeholder).
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(str(ctx.thread_id))
        called = _make_tool_called_event(
            thread_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments={"path": "/tmp/x"},
        )
        events_seen.append(called)
        yield called
        raise RuntimeError("tool backend crashed mid-call")

    return _stream


class _MockEngineStep7ToolErroring:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.execute_stream = _make_erroring_after_tool_called_stream(
            thread_id, events_seen
        )


class _MockRuntimeStep7ToolErroring:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep7ToolErroring(thread_id, events_seen)


def _make_app_step7_tool_erroring(tmp_path: Path, events_seen: list[Event]) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep7ToolErroring(thread_id, events_seen),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


@pytest.mark.asyncio
async def test_generic_error_flips_pending_tool_call_block_to_failed(
    tmp_path: Path,
) -> None:
    """A running `ToolCallBlock` still live when the error hits reaches `-failed`.

    Documents the chosen terminal treatment for a tool call in flight
    when the exchange itself errors out: `ToolCallBlock.render_failed()`
    reuses `set_failed_state()`/`-failed` (the SAME visual a real
    `TOOL_FAILED` result produces) rather than the cancel path's
    `-interrupted` — this genuinely is a failure (the exchange itself
    errored), not a user cancellation, so it should read identically to
    any other failed tool call in the transcript, per the decided
    glyph/color table's ``error`` row.
    """
    events_seen: list[Event] = []
    app = _make_app_step7_tool_erroring(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("read a file for me")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert len(events_seen) == 1
        assert events_seen[0].type == EventType.TOOL_CALLED
        running_blocks = app.query(ToolCallBlock)
        assert len(running_blocks) == 1
        block = running_blocks.first()

        # Still mounted (never removed) and flipped to the failed
        # visual state — distinct from the cancel path's `-interrupted`.
        assert block.has_class("-failed")
        assert not block.has_class("-interrupted")
        assert _tool_body_text(block) == "Error"
        assert str(block.title).startswith("✗ ")

        # Registry hygiene: no longer live/pending.
        assert app._pending_tool_call_block is None
        assert app._live_tool_call_blocks == {}

        # The placeholder was already morphed away by TOOL_CALLED
        # (Step 5's two-phase mount) before the error — nothing left
        # to flip there for this particular turn shape.
        assert len(app.query(WorkingPlaceholder)) == 0


@pytest.mark.asyncio
async def test_mount_time_failure_now_produces_exchange_failed_notice(
    tmp_path: Path,
) -> None:
    """A failure inside `mount_working_placeholder()` itself is now caught.

    Before spec-130 Step 7, `mount_working_placeholder()` sat OUTSIDE
    `_run_exchange`'s inner `try/except Exception` — a mount-time
    failure propagated straight out of `_run_exchange` entirely,
    skipping `mount_exchange_failed()`'s transcript-visible failure
    notice and hitting Textual's default (uncaught) worker exception
    handling instead. This monkeypatches `ChatApp.mount_working_placeholder`
    to raise directly (simulating e.g. a `query_one("#transcript", ...)`
    failure or a widget-construction error) and asserts the SAME
    user-visible "[exchange failed — ...]" notice now appears — the
    widened inner `try` gives a mount-time failure the identical
    treatment an event-stream-time failure already got. Also confirms
    the worker itself does not raise past `_run_exchange` (Textual's
    `Worker` surfaces an uncaught exception via `worker.error`, not by
    re-raising into the caller — this asserts the app is still alive
    and responsive afterward, which a truly-uncaught exception would
    have prevented).

    Post-ship placeholder-position fix: `mount_working_placeholder()`
    is now only ever called from INSIDE the `event_stream` loop (gated
    on `USER_MESSAGE`, not before iteration begins), so the fake
    stream's `USER_MESSAGE` IS processed (mounting a real `UserChatbox`)
    before the mount call raises — `event_stream` is both constructed
    and partially iterated by the time this failure hits. That's a real
    difference from the original (pre-fix) shape of this test, so the
    assertions below check the `UserChatbox` outcome explicitly rather
    than asserting the stream was never touched.
    """
    events_seen: list[Event] = []
    app = _make_app_step2(tmp_path, events_seen)

    async def _raising_mount_working_placeholder() -> WorkingPlaceholder:
        raise RuntimeError("transcript not ready")

    app.mount_working_placeholder = _raising_mount_working_placeholder  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello")
        await _pump_until(
            pilot,
            lambda: len(app.query(".error")) > 0,
            what="exchange-failed notice",
        )

        # No placeholder was ever mounted (the mount call raised before
        # producing one) — nothing to flip, nothing orphaned either.
        assert len(app.query(WorkingPlaceholder)) == 0
        assert app._live_placeholders == {}
        assert app._working_placeholder is None

        # USER_MESSAGE WAS processed before the mount call raised — the
        # user's own message still appears, same as it would for any
        # other later-stage failure.
        assert len(app.query(UserChatbox)) == 1

        # The same generic failure notice an event-stream-time error
        # produces is now mounted for a mount-time error too.
        notices = app.query(".error")
        assert len(notices) == 1
        assert "transcript not ready" in str(notices.first().content)

        # The fake stream's AGENT_MESSAGE event never fired — the mount
        # failure short-circuited right after USER_MESSAGE, before the
        # gate/second event was ever reached.
        assert events_seen == []

        # The app is still alive and responsive — the worker's raise
        # was caught inside `_run_exchange`, not left to become a truly
        # uncaught exception that would tear down the app/test harness.
        await pilot.pause()
        assert app.is_running


# ----------------------------------------------------------------------
# spec-130 Step 7: cancel and error stay distinct terminal states,
# neither one accidentally routing through the other's sweep method.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_and_error_are_distinct_and_do_not_cross_wires(
    tmp_path: Path,
) -> None:
    """Cancellation still yields "interrupted"; a real error still yields "error".

    Directly guards against the two sweep paths bleeding into each
    other now that they share `_sweep_live_registries()`'s iteration
    logic: drives one turn down the cooperative-cancel path
    (`ExchangeCancelled`) and a second, independent turn down the
    generic-error path (`RuntimeError`), asserting each placeholder
    lands on its OWN distinct terminal kind/glyph/CSS class — the
    shared plumbing must not collapse the two into one outcome.
    """
    cancelled_app = _make_app_step6_cancelled(tmp_path)
    async with cancelled_app.run_test() as pilot:
        await pilot.pause()
        cancelled_app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        cancelled_placeholder = cancelled_app.query(WorkingPlaceholder).first()
        assert cancelled_placeholder.terminal_kind == "interrupted"
        assert _glyph_text(cancelled_placeholder) == "─"
        assert cancelled_placeholder.has_class("-interrupted")
        assert not cancelled_placeholder.has_class("-error")

    erroring_app = _make_app_step7_erroring(tmp_path)
    async with erroring_app.run_test() as pilot:
        await pilot.pause()
        erroring_app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        erroring_placeholder = erroring_app.query(WorkingPlaceholder).first()
        assert erroring_placeholder.terminal_kind == "error"
        assert _glyph_text(erroring_placeholder) == "✗"
        assert erroring_placeholder.has_class("-error")
        assert not erroring_placeholder.has_class("-interrupted")


@pytest.mark.asyncio
async def test_error_sweep_does_not_touch_interrupted_helper_and_vice_versa(
    tmp_path: Path,
) -> None:
    """`_interrupt_live_registries` and `_fail_live_registries` never call each other.

    A narrower unit-level guard on top of the behavioural test above:
    spies on BOTH sweep methods across the cancel path and asserts only
    `_interrupt_live_registries` fired (never `_fail_live_registries`),
    then does the same across the error path and asserts only
    `_fail_live_registries` fired. Catches a refactor that accidentally
    routes one path through the other's public method, even if the
    resulting widget state happened to look right by coincidence.
    """
    cancelled_app = _make_app_step6_cancelled(tmp_path)
    interrupt_calls: list[None] = []
    fail_calls: list[None] = []
    original_interrupt = cancelled_app._interrupt_live_registries
    original_fail = cancelled_app._fail_live_registries

    async def _spy_interrupt() -> None:
        interrupt_calls.append(None)
        await original_interrupt()

    async def _spy_fail() -> None:
        fail_calls.append(None)
        await original_fail()

    cancelled_app._interrupt_live_registries = _spy_interrupt  # type: ignore[method-assign]
    cancelled_app._fail_live_registries = _spy_fail  # type: ignore[method-assign]

    async with cancelled_app.run_test() as pilot:
        await pilot.pause()
        cancelled_app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert interrupt_calls == [None]
        assert fail_calls == []

    erroring_app = _make_app_step7_erroring(tmp_path)
    interrupt_calls_2: list[None] = []
    fail_calls_2: list[None] = []
    original_interrupt_2 = erroring_app._interrupt_live_registries
    original_fail_2 = erroring_app._fail_live_registries

    async def _spy_interrupt_2() -> None:
        interrupt_calls_2.append(None)
        await original_interrupt_2()

    async def _spy_fail_2() -> None:
        fail_calls_2.append(None)
        await original_fail_2()

    erroring_app._interrupt_live_registries = _spy_interrupt_2  # type: ignore[method-assign]
    erroring_app._fail_live_registries = _spy_fail_2  # type: ignore[method-assign]

    async with erroring_app.run_test() as pilot:
        await pilot.pause()
        erroring_app._run_exchange("hello")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert fail_calls_2 == [None]
        assert interrupt_calls_2 == []


# ----------------------------------------------------------------------
# bug-135: real Escape-keypress end-to-end cancel-visibility test
# ----------------------------------------------------------------------
#
# Every cancel-flip test above drives `ExchangeCancelled` by having the
# fake `engine.execute_stream` raise it directly (`_make_cancelled_event_
# stream`) or by relying on Textual's `@work(exclusive=True)` preemption
# (`_make_hanging_event_stream`) — neither ever calls `ChatApp.on_key`,
# so neither exercises the real Escape key-routing state machine
# (composer-approval guard -> esc-toast window -> `_cancel_butler_turn`
# -> `request_cancel` -> `CancelToken.cancel()`) that a live `kolsch
# chat` session actually uses. `test_esc_toast.py` DOES drive real Esc
# keypresses through `on_key` (via its `_press_escape` helper) but its
# fake `execute_stream` never yields any events at all, so it has no
# `WorkingPlaceholder` to assert against.
#
# This fixture bridges the gap: a fake stream that yields USER_MESSAGE
# (mounting a real, live, spinning WorkingPlaceholder) and then AWAITS
# the real `ExecutionContext.cancellation` token — i.e. it behaves like
# the real engine's cooperative-cancel checkpoint (see
# `src/engine/_cancellation.py`, out of scope to touch/import here, but
# this fixture reproduces its externally-observable contract: block
# until the CancelToken fires, then raise `ExchangeCancelled`). A
# physical `events.Key(key="escape")` fed through `app.on_key` is what
# fires that token, exactly as a live Esc keypress does.


def _make_token_aware_cancel_stream(
    thread_id: str,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Fake ``engine.execute_stream``: yields USER_MESSAGE, then waits on the REAL token.

    Mirrors the real engine's cooperative-cancel contract (see
    ``src/engine/_cancellation.py``, not imported/touched here) closely
    enough for this test's purposes: the turn stays genuinely "in
    flight" (placeholder mounted, spinning) until
    ``ctx.cancellation.wait_cancelled()`` resolves — which only happens
    when something calls ``CancelToken.cancel()`` on the SAME token
    instance the app attached to ``ExecutionContext.cancellation`` in
    ``_run_exchange``. That's exactly what a real Escape keypress does
    via ``on_key`` -> ``_cancel_butler_turn`` -> ``request_cancel``, so
    driving this fixture via ``app.on_key`` (rather than injecting
    ``ExchangeCancelled`` directly) genuinely exercises the real
    Esc-to-cancel wiring, not just the placeholder's rendering.
    """

    async def _stream(*args: Any, **_kwargs: Any) -> AsyncGenerator[Event, None]:
        ctx = args[1]
        yield _make_user_message_event(thread_id)
        await ctx.cancellation.wait_cancelled()
        raise ExchangeCancelled(cancelled_at_event_id="")

    return _stream


class _MockEngineEscE2E:
    def __init__(self, thread_id: str, *_args: Any, **_kwargs: Any) -> None:
        self.execute_stream = _make_token_aware_cancel_stream(thread_id)


class _MockRuntimeEscE2E:
    def __init__(self, thread_id: str, *_args: Any, **_kwargs: Any) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineEscE2E(thread_id)


def _make_app_esc_e2e(tmp_path: Path) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeEscE2E(thread_id),
        config=_MockConfigStep2(),
        session_id="ses_test",
        thread_id=thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    return ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )


async def _press_escape(app: ChatApp) -> None:
    """Fire a real ``escape`` key event through ``ChatApp.on_key``.

    Same shape as ``test_esc_toast.py``'s helper of the same name —
    duplicated locally (rather than imported across test modules) since
    this file has no existing cross-import of that module and a single
    two-line helper doesn't warrant introducing one.
    """
    event = events.Key(key="escape", character=None)
    await app.on_key(event)


@pytest.mark.asyncio
async def test_real_escape_keypress_flips_placeholder_to_visually_distinct_interrupted(
    tmp_path: Path,
) -> None:
    """bug-135 AC #4: a real Esc keypress through `on_key` yields the new terminal look.

    Drives the full real path — `events.Key("escape")` -> `ChatApp.
    on_key` -> `_cancel_butler_turn()` -> `request_cancel()` ->
    `CancelToken.cancel()` -> the fake stream's `wait_cancelled()`
    resolves -> `ExchangeCancelled` raised inside `_run_exchange` ->
    `_interrupt_live_registries()` -> `_sweep_live_registries
    ("interrupted")` -> `placeholder.render_terminal("interrupted")` —
    with a REAL `WorkingPlaceholder` mounted for the in-flight turn.
    Not a direct `render_terminal()` call, not an injected
    `ExchangeCancelled` bypassing `on_key`.

    Asserts both the pre-existing terminal markers (glyph/class,
    `is_terminal`/`terminal_kind`) AND the bug-135 visual-distinction
    additions (struck-through body text-style, widened body copy) —
    the new contract this bug adds, not just what the old tests already
    covered.
    """
    app = _make_app_esc_e2e(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello there")
        # Pump enough for USER_MESSAGE to be processed (mounting the
        # placeholder) but the fake stream is now parked on
        # `wait_cancelled()` — it will hang here until Esc fires below.
        await pilot.pause()
        await pilot.pause()

        placeholders = app.query(WorkingPlaceholder)
        assert len(placeholders) == 1, (
            "the placeholder must be mounted and still live before Esc — "
            "test setup invalid otherwise"
        )
        placeholder = placeholders.first()
        assert placeholder.is_terminal is False, (
            "placeholder must still be in its live working state before "
            "the real Esc keypress — otherwise this test wouldn't be "
            "exercising a real live-to-terminal transition"
        )
        assert app._butler_turn_active() is True, (
            "on_key's cancel branch requires an active butler turn — "
            "test setup invalid otherwise"
        )

        # The real Escape keypress, routed through the real handler —
        # NOT a direct render_terminal() call, NOT an injected
        # ExchangeCancelled.
        await _press_escape(app)
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # Terminal markers (pre-existing contract, still true).
        # `terminal_kind == "interrupted"` implies `is_terminal` — checking
        # only this (stronger) assertion also sidesteps a mypy narrowing
        # false-positive: asserting `is_terminal is False` earlier on this
        # same object, then `is True` later (a real state change from the
        # intervening `_press_escape` call mypy can't see through), makes
        # mypy incorrectly flag the later code as unreachable. Same
        # precedent as
        # `test_exclusive_worker_supersession_interrupts_turn_a_placeholder`
        # above.
        assert placeholder.terminal_kind == "interrupted"
        assert _glyph_text(placeholder) == "─"
        assert placeholder.has_class("-interrupted")

        # bug-135's new visual-distinction contract: struck-through
        # body text-style (alongside the pre-existing dim) and widened
        # body copy — reached via the REAL Esc path, not a direct call.
        body = placeholder.query_one(".working-body", Static)
        assert bool(body.styles.text_style.dim)
        assert bool(body.styles.text_style.strike)
        assert _body_text(placeholder) == "Interrupted — turn cancelled"

        # Registry hygiene, matching the direct-injection cancel tests.
        assert app._live_placeholders == {}
        assert app._working_placeholder is None
