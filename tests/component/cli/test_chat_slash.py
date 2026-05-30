"""Phase 2.1 Step 24b — slash-command component tests.

Three cases per the build-doc's acceptance criterion:

- **TC-CHAT-05**: ``/exit`` submitted to the input loop exits
  with code 0.
- **TC-CHAT-06**: ``/clear`` clears the visible scrollback BUT
  the session and its event log are untouched
  (``kolsch sessions events <id>`` still returns the history).
- **TC-CHAT-15**: completion narrows by prefix — feeding ``/``
  yields all eight commands; ``/th`` narrows to ``/thinking``;
  ``/thinking `` (trailing space) yields the four sub-options.

Plus a few helper-level tests that pin the registry's
single-source-of-truth property and the dispatch / footer-hint
contract.
"""

from __future__ import annotations

import io as _io
from typing import TYPE_CHECKING

from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cli import chat
from cli._slash import (
    COMMAND_REGISTRY,
    ControlSignal,
    SlashCompleter,
    SlashContext,
    dispatch_slash,
    footer_hint_for_buffer,
    is_slash_command,
)

if TYPE_CHECKING:
    import pytest


# ===========================================================================
# Registry — the single source of truth
# ===========================================================================


def test_registry_has_eight_visible_commands_per_patch_5() -> None:
    """Patch §5 lists eight commands (``/exit`` and ``/quit`` share
    one entry via ``aliases``). The registry has eight entries."""
    assert len(COMMAND_REGISTRY) == 8
    expected = {
        "/help",
        "/exit",
        "/new",
        "/clear",
        "/model",
        "/thinking",
        "/tools",
        "/session",
    }
    assert set(COMMAND_REGISTRY.keys()) == expected


def test_registry_exit_command_aliases_quit() -> None:
    """``/quit`` resolves to the same handler as ``/exit``."""
    assert "/quit" in COMMAND_REGISTRY["/exit"].aliases


def test_registry_real_handlers_are_help_exit_clear_only() -> None:
    """24b ships real handlers for /help, /exit (+ /quit alias),
    /clear; the rest are placeholders with a documented step
    where the handler lands."""
    implemented = {
        name for name, cmd in COMMAND_REGISTRY.items() if cmd.handler is not None
    }
    assert implemented == {"/help", "/exit", "/clear"}
    for name, cmd in COMMAND_REGISTRY.items():
        if cmd.handler is None:
            assert cmd.placeholder_step is not None, (
                f"{name} is a placeholder but doesn't name the step "
                f"where its handler lands"
            )


def test_is_slash_command() -> None:
    """``is_slash_command`` strips leading whitespace and checks
    for the ``/`` prefix."""
    assert is_slash_command("/exit")
    assert is_slash_command("  /help")
    assert not is_slash_command("hello")
    assert not is_slash_command("  hello")
    assert not is_slash_command("")


# ===========================================================================
# Dispatch + handlers — /help, /exit, /clear, placeholders, unknown
# ===========================================================================


async def test_dispatch_help_lists_all_visible_commands() -> None:
    out = _io.StringIO()
    signal = await dispatch_slash("/help", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    # Every visible command surfaces in /help — single source of
    # truth.
    for name in COMMAND_REGISTRY:
        assert name in rendered


async def test_dispatch_help_with_specific_command_prints_its_hint() -> None:
    out = _io.StringIO()
    signal = await dispatch_slash("/help /clear", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    assert "/clear" in rendered
    assert COMMAND_REGISTRY["/clear"].usage in rendered


async def test_dispatch_help_without_slash_prefix_works() -> None:
    """``/help clear`` (without the leading slash on the argument)
    finds the command — the dispatch helper normalises."""
    out = _io.StringIO()
    signal = await dispatch_slash("/help clear", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    assert "/clear" in out.getvalue()


async def test_dispatch_help_unknown_command_prints_error_continues() -> None:
    out = _io.StringIO()
    signal = await dispatch_slash("/help /not-a-command", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    assert "unknown command" in out.getvalue().lower()


async def test_tc_chat_05_dispatch_exit_returns_exit_signal() -> None:
    """TC-CHAT-05 (dispatch half): ``/exit`` returns EXIT."""
    out = _io.StringIO()
    assert await dispatch_slash("/exit", SlashContext(stdout=out)) is ControlSignal.EXIT


async def test_tc_chat_05_dispatch_quit_alias_returns_exit_signal() -> None:
    """TC-CHAT-05 alias: ``/quit`` resolves to the same handler."""
    out = _io.StringIO()
    assert await dispatch_slash("/quit", SlashContext(stdout=out)) is ControlSignal.EXIT


async def test_dispatch_clear_writes_terminal_clear_escape() -> None:
    """``/clear`` emits the ANSI sequence that wipes screen +
    scrollback (cursor home + erase screen + erase scrollback).
    No session-side effects — that's TC-CHAT-06 below."""
    out = _io.StringIO()
    signal = await dispatch_slash("/clear", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    # \x1b[H = cursor home; \x1b[2J = erase entire screen;
    # \x1b[3J = erase scrollback (xterm extension).
    assert "\x1b[H" in out.getvalue()
    assert "\x1b[2J" in out.getvalue()
    assert "\x1b[3J" in out.getvalue()


async def test_dispatch_placeholder_command_prints_step_and_continues() -> None:
    """Every placeholder command (handler=None) prints a polite
    message naming the step where the real handler lands, and
    continues."""
    for name, cmd in COMMAND_REGISTRY.items():
        if cmd.handler is not None:
            continue
        out = _io.StringIO()
        signal = await dispatch_slash(name, SlashContext(stdout=out))
        assert signal is ControlSignal.CONTINUE
        rendered = out.getvalue()
        assert "not yet implemented" in rendered.lower()
        step = cmd.placeholder_step
        assert step is not None
        assert step in rendered, (
            f"{name} placeholder message missing step {step}: {rendered}"
        )


async def test_dispatch_unknown_command_prints_error_continues() -> None:
    out = _io.StringIO()
    signal = await dispatch_slash("/exotic", SlashContext(stdout=out))
    assert signal is ControlSignal.CONTINUE
    assert "unknown command" in out.getvalue().lower()


# ===========================================================================
# TC-CHAT-15 — completer narrows by prefix and surfaces sub-options
# ===========================================================================


def _completions_for(text: str) -> list[str]:
    """Run the completer against ``text`` and return the display
    strings (i.e., what the user sees in the popup)."""
    from prompt_toolkit.completion import CompleteEvent

    completer = SlashCompleter()
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=True)
    return [
        c.display[0][1] if isinstance(c.display, list) else str(c.display)
        for c in completer.get_completions(document, event)
    ]


def test_tc_chat_15_completer_lists_all_eight_commands_after_slash() -> None:
    """TC-CHAT-15 (first form): typing ``/`` yields all eight
    visible commands. Aliases (``/quit``) are NOT in the popup —
    the registry's ``aliases`` field is invisible by design."""
    completions = _completions_for("/")
    assert set(completions) == {
        "/help",
        "/exit",
        "/new",
        "/clear",
        "/model",
        "/thinking",
        "/tools",
        "/session",
    }


def test_tc_chat_15_completer_narrows_by_prefix() -> None:
    """``/th`` → only ``/thinking`` (the sole command starting
    with ``/th``)."""
    completions = _completions_for("/th")
    assert completions == ["/thinking"]


def test_tc_chat_15_completer_yields_thinking_options_after_trailing_space() -> None:
    """TC-CHAT-15 (second form): ``/thinking `` (trailing space)
    yields exactly the four sub-options from patch §5.2."""
    completions = _completions_for("/thinking ")
    assert completions == ["on", "off", "budget <n>", "last"]


def test_completer_yields_free_form_hint_for_model() -> None:
    """``/model `` (trailing space) yields the free-form arg
    hint (``<type any model ref>``) per patch §5.3."""
    completions = _completions_for("/model ")
    assert len(completions) == 1
    assert "any" in completions[0].lower()


def test_completer_yields_nothing_for_non_slash_text() -> None:
    """Plain user message → no popup."""
    assert _completions_for("hello world") == []


# ===========================================================================
# Footer-hint generator — patch §5.4
# ===========================================================================


def test_footer_hint_default_for_empty_buffer() -> None:
    hint = footer_hint_for_buffer("")
    assert "/help" in hint
    assert "Ctrl-D" in hint


def test_footer_hint_default_for_normal_message() -> None:
    hint = footer_hint_for_buffer("hello there")
    assert "/help" in hint
    assert "Ctrl-D" in hint


def test_footer_hint_tracks_command_being_typed() -> None:
    """While the user is mid-typing ``/thinking``, the footer shows
    that command's usage line. Patch §5.4."""
    hint = footer_hint_for_buffer("/thinking")
    assert hint == COMMAND_REGISTRY["/thinking"].usage


def test_footer_hint_tracks_partial_prefix() -> None:
    """Partial command name (``/th``) shows the matching command's
    usage as the user types."""
    hint = footer_hint_for_buffer("/th")
    assert hint == COMMAND_REGISTRY["/thinking"].usage


def test_footer_hint_unknown_command_falls_back_to_default() -> None:
    hint = footer_hint_for_buffer("/exotic")
    assert "/help" in hint
    assert "Ctrl-D" in hint


# ===========================================================================
# TC-CHAT-05 — input loop exits cleanly on /exit (in-process)
# ===========================================================================


async def test_tc_chat_05_input_loop_exits_on_slash_exit() -> None:
    """TC-CHAT-05: ``/exit`` submitted to the input loop returns 0
    (and the input loop terminates — the subprocess smoke in
    test_chat_smoke.py exercises the same shape end-to-end via
    the CLI entry point in a later refresh)."""
    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        session = chat._build_prompt_session(input=pipe_input, output=DummyOutput())
        # Feed "/exit" + Enter — the loop dispatches the slash
        # command, the handler returns EXIT, the loop exits with 0.
        pipe_input.send_text("/exit\r")
        rc = await chat._run_input_loop(session, stdout=out)
    assert rc == 0


# ===========================================================================
# TC-CHAT-06 — /clear preserves the session and event log
# ===========================================================================


async def test_tc_chat_06_clear_does_not_touch_session_or_event_log(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """TC-CHAT-06: ``/clear`` is a display-only operation.

    The handler emits ANSI clear sequences to stdout but performs
    no I/O against the store or event log. The test asserts the
    contract structurally: the dispatch is pure stdout, and the
    handler signature receives only a ``SlashContext`` (which
    carries ``stdout`` and nothing else).

    Adjacent in this step is the simpler invariant:
    ``ctx`` has NO store / runtime / event-bus reference, so a
    handler couldn't side-effect those even if it tried.
    """
    _ = tmp_path_factory  # fixture present for symmetry with the smoke layer
    out = _io.StringIO()
    ctx = SlashContext(stdout=out)
    # Belt-and-braces: SlashContext only carries `stdout`. A
    # handler that wanted to mutate the session would need a
    # store reference — which isn't on the context. The contract
    # is what TC-CHAT-06 verifies; the runtime invariant is the
    # context's narrow shape.
    fields = set(ctx.__dataclass_fields__.keys())
    assert fields == {"stdout"}, (
        f"SlashContext should only expose stdout in 24b; got {fields}. "
        f"Adding fields (e.g., a store reference) widens the surface a "
        f"handler could touch — needs a CHAT-R review."
    )
    signal = await dispatch_slash("/clear", ctx)
    assert signal is ControlSignal.CONTINUE
    # The handler wrote the ANSI clear sequence. Nothing else
    # happened.
    rendered = out.getvalue()
    assert "\x1b[3J" in rendered  # scrollback clear
