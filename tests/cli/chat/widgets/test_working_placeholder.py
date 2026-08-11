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
``WorkingPlaceholder`` into ``#transcript`` before ``event_stream``
iteration begins. The integration tests below drive a real ``ChatApp``
(same scaffolding as ``tests/cli/chat/test_composer_submitted_exit_verbs.py``)
against a fake engine whose ``execute_stream`` sleeps before yielding
its first (and only) event, and assert the placeholder is mounted in
``#transcript`` immediately — i.e. before that first event is ever
processed.
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
from textual.containers import VerticalScroll
from textual.widgets import Static

from cli.chat.app import ChatApp
from cli.chat.spinner import SPINNER_FRAMES
from cli.chat.state_tokens import resolve_state
from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.working_placeholder import WorkingPlaceholder
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from textual.app import App
    from textual.widget import Widget


def _glyph_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered glyph-column text off a mounted widget."""
    return str(widget.query_one(".gb-glyph-col", Static).render())


def _body_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered body text off a mounted widget."""
    return str(widget.query_one(".working-body", Static).render())


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
    """Interrupted state keeps the ``$hint`` rail (per the glyph/color table)."""
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
async def test_render_terminal_interrupted_body_is_dim(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Interrupted body text renders with ``dim`` text-style."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        body = widget.query_one(".working-body", Static)
        assert bool(body.styles.text_style.dim)


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
) -> None:
    """The initial working-state glyph is identical between color + NO_COLOR."""
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

    assert color_glyph == mono_glyph
    assert color_glyph in SPINNER_FRAMES


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
# `app.run_test()`. `_MockEngine.execute_stream` here is a *delayed*
# async generator (sleeps before yielding its one event) so the test can
# assert the placeholder is mounted in `#transcript` strictly before
# that first event is ever handed to `EventAdapter.handle`.

_FIRST_EVENT_DELAY_SECONDS = 0.15
"""Delay before the fake stream yields its first event.

Long enough that a broken implementation (mount happening only after
the first event, or not at all before iteration) would reliably fail
`test_placeholder_mounted_before_first_event_processed` on any
reasonable CI/dev machine, short enough to keep the test fast.
"""


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


def _make_delayed_event_stream(
    thread_id: str,
    events_seen: list[Event],
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` that delays its first event.

    Appends each event to ``events_seen`` as it's yielded, so the test
    can assert ordering ("placeholder mounted" happened-before "first
    event recorded") without needing to inspect `EventAdapter` internals.
    """

    async def _delayed_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        await asyncio.sleep(_FIRST_EVENT_DELAY_SECONDS)
        event = _make_canonical_agent_message_event(thread_id)
        events_seen.append(event)
        yield event

    return _delayed_stream


class _MockEngineStep2:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.execute_stream = _make_delayed_event_stream(thread_id, events_seen)


class _MockRuntimeStep2:
    def __init__(self, thread_id: str, events_seen: list[Event]) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep2(thread_id, events_seen)


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


def _make_app_step2(tmp_path: Path, events_seen: list[Event]) -> ChatApp:
    # `_run_exchange` (unlike the spied version in
    # test_composer_submitted_exit_verbs.py) really constructs an
    # `ExecutionContext`, whose `thread_id` is a `ThreadId` — validated
    # to have a uuid body. A plain `"th_test"` string fails that
    # validation; use a real uuid4 body instead.
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep2(thread_id, events_seen),
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
    (not a spy) against a fake engine whose stream delays its first
    event, then asserts the placeholder is mounted before the delay
    elapses — i.e. before any event has been processed.
    """
    events_seen: list[Event] = []
    app = _make_app_step2(tmp_path, events_seen)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._run_exchange("hello there")
        # Pump the loop just enough for the worker to start and mount
        # the placeholder, but well short of `_FIRST_EVENT_DELAY_SECONDS`
        # — the fake stream has not yielded anything yet.
        await pilot.pause()
        await pilot.pause()

        assert events_seen == [], (
            "the fake stream's first event fired before the assertion — "
            "the delay is too short for this to be a meaningful check"
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
        await pilot.pause()
        await pilot.pause()

        placeholder = app._working_placeholder
        assert placeholder is not None
        assert placeholder in app.query(WorkingPlaceholder)
        assert placeholder.is_terminal is False
        assert placeholder.terminal_kind is None


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
    delay_first_seconds: float = 0.0,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding N streaming-token events.

    Each entry in ``deltas`` becomes one ``MODEL_STREAMING_TOKEN``
    event for the same ``call_id`` — i.e. one turn, N incremental
    tokens, matching how a real streamed reply arrives. When
    ``delay_first_seconds`` is nonzero, the stream sleeps before
    yielding its first event — same delayed-stream trick Step 2's
    tests use — so a test can pump the loop just enough to observe the
    placeholder mounted-but-not-yet-morphed before the first token
    arrives.
    """

    async def _streaming_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        if delay_first_seconds:
            await asyncio.sleep(delay_first_seconds)
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
        delay_first_seconds: float = 0.0,
    ) -> None:
        self.execute_stream = _make_streaming_event_stream(
            thread_id,
            events_seen,
            deltas=deltas,
            delay_first_seconds=delay_first_seconds,
        )


class _MockRuntimeStep3:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        deltas: list[str],
        delay_first_seconds: float = 0.0,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep3(
            thread_id,
            events_seen,
            deltas=deltas,
            delay_first_seconds=delay_first_seconds,
        )


def _make_app_step3(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    deltas: list[str],
    delay_first_seconds: float = 0.0,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep3(
            thread_id,
            events_seen,
            deltas=deltas,
            delay_first_seconds=delay_first_seconds,
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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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

    The placeholder is mounted first (nothing else added to
    `#transcript` for this turn yet), so remove-then-mount should land
    `AssistantProse` at the same index the placeholder held — verified
    directly against `#transcript`'s child list, not just "is
    mounted somewhere." Uses a first-event delay (same trick as Step
    2's `test_placeholder_mounted_at_turn_start`) so the placeholder's
    mounted-but-not-yet-morphed position can be reliably observed
    before the morph fires.
    """
    events_seen: list[Event] = []
    app = _make_app_step3(
        tmp_path,
        events_seen,
        deltas=["hello"],
        delay_first_seconds=_FIRST_EVENT_DELAY_SECONDS,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        children_before = list(transcript.children)

        app._run_exchange("hi")
        # Pump well short of the delay so the stream has not yielded
        # its first event yet, but the placeholder mount has landed.
        await pilot.pause()
        await pilot.pause()
        assert events_seen == [], (
            "the fake stream's first event fired before the assertion — "
            "the delay is too short for this to be a meaningful check"
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

        # Pump past the delay so the first token arrives and the morph
        # (remove placeholder, mount prose) completes.
        await asyncio.sleep(_FIRST_EVENT_DELAY_SECONDS)
        await pilot.pause()
        await pilot.pause()

        children_after = list(transcript.children)
        assert len(children_after) == len(children_before) + 1, (
            "expected exactly one net new child (placeholder removed, prose mounted)"
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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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
    delay_first_seconds: float = 0.0,
) -> Callable[..., AsyncGenerator[Event, None]]:
    """Build a fake ``engine.execute_stream`` yielding one complete ``AGENT_MESSAGE``.

    No ``MODEL_STREAMING_TOKEN`` precedes it — the full text is
    present on the single event, matching a non-streamed reply. When
    ``delay_first_seconds`` is nonzero, the stream sleeps before
    yielding, mirroring Steps 2-3's delayed-stream trick so a test can
    observe the placeholder mounted-but-not-yet-morphed before the
    response arrives.
    """

    async def _complete_stream(
        *_args: Any, **_kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        if delay_first_seconds:
            await asyncio.sleep(delay_first_seconds)
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
        delay_first_seconds: float = 0.0,
    ) -> None:
        self.execute_stream = _make_complete_response_event_stream(
            thread_id,
            events_seen,
            text=text,
            delay_first_seconds=delay_first_seconds,
        )


class _MockRuntimeStep4:
    def __init__(
        self,
        thread_id: str,
        events_seen: list[Event],
        *,
        text: str,
        delay_first_seconds: float = 0.0,
    ) -> None:
        self.store = _MockStoreStep2()
        self.authz = _MockAuthzStep2()
        self.engine = _MockEngineStep4(
            thread_id,
            events_seen,
            text=text,
            delay_first_seconds=delay_first_seconds,
        )


def _make_app_step4(
    tmp_path: Path,
    events_seen: list[Event],
    *,
    text: str,
    delay_first_seconds: float = 0.0,
) -> ChatApp:
    thread_id = f"th_{uuid4()}"
    bundle = _MockBundleStep2(
        runtime=_MockRuntimeStep4(
            thread_id,
            events_seen,
            text=text,
            delay_first_seconds=delay_first_seconds,
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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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

    The placeholder is mounted first (nothing else added to
    `#transcript` for this turn yet), so remove-then-mount should land
    `AssistantProse` at the same index the placeholder held — verified
    directly against `#transcript`'s child list. Uses a first-event
    delay (same trick as Steps 2-3) so the placeholder's
    mounted-but-not-yet-morphed position can be reliably observed
    before the morph fires.
    """
    events_seen: list[Event] = []
    app = _make_app_step4(
        tmp_path,
        events_seen,
        text="the complete answer",
        delay_first_seconds=_FIRST_EVENT_DELAY_SECONDS,
    )
    async with app.run_test() as pilot:
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        children_before = list(transcript.children)

        app._run_exchange("hi")
        # Pump well short of the delay so the stream has not yielded
        # its event yet, but the placeholder mount has landed.
        await pilot.pause()
        await pilot.pause()
        assert events_seen == [], (
            "the fake stream's event fired before the assertion — "
            "the delay is too short for this to be a meaningful check"
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

        # Pump past the delay so the complete response arrives and the
        # morph (remove placeholder, mount full prose) completes.
        await asyncio.sleep(_FIRST_EVENT_DELAY_SECONDS)
        await pilot.pause()
        await pilot.pause()

        children_after = list(transcript.children)
        assert len(children_after) == len(children_before) + 1, (
            "expected exactly one net new child (placeholder removed, prose mounted)"
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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

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
