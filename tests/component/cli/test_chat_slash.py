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
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ===========================================================================
# Registry — the single source of truth
# ===========================================================================


def test_registry_has_twelve_visible_commands_per_patch_5() -> None:
    """Patch §5 + skills-build + qol-permission-system-human-readable.md
    Step 3 add ``/skill`` then ``/perm``; Phase 3.9 Step 1 adds
    ``/workflow`` and Step 3 adds ``/fork``. ``/exit`` and ``/quit``
    still share one entry via ``aliases``."""
    assert len(COMMAND_REGISTRY) == 12
    expected = {
        "/help",
        "/exit",
        "/new",
        "/clear",
        "/model",
        "/thinking",
        "/tools",
        "/session",
        "/skill",
        "/perm",
        "/workflow",
        "/fork",
    }
    assert set(COMMAND_REGISTRY.keys()) == expected


def test_registry_exit_command_aliases_quit() -> None:
    """``/quit`` resolves to the same handler as ``/exit``."""
    assert "/quit" in COMMAND_REGISTRY["/exit"].aliases


def test_registry_real_handlers_match_phase_2_1_step_24e_set() -> None:
    """24e completes the Phase 2.1 chat surface — every registry
    entry now has a real handler. No placeholders remain;
    ``/quit`` continues to share ``/exit``'s handler via the
    aliases tuple (it has no entry of its own). Phase 2.4
    skills-build adds ``/skill``; Phase 3.9 Step 1 adds ``/workflow``
    and Step 3 adds ``/fork``."""
    implemented = {
        name for name, cmd in COMMAND_REGISTRY.items() if cmd.handler is not None
    }
    assert implemented == {
        "/help",
        "/exit",
        "/clear",
        "/tools",
        "/new",
        "/model",
        "/thinking",
        "/session",
        "/skill",
        "/perm",
        "/workflow",
        "/fork",
    }
    # No placeholder rows remain.
    placeholders = {
        name for name, cmd in COMMAND_REGISTRY.items() if cmd.handler is None
    }
    assert placeholders == set(), (
        f"24e expected all handlers real; placeholders remain: {placeholders}"
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


async def test_dispatch_handlers_without_state_print_clear_error() -> None:
    """Lifecycle handlers (``/new``, ``/model``, ``/thinking``,
    ``/session``) require ``ctx.state`` per 24e; when absent
    they print a polite "no chat runtime" message and continue
    rather than crash. Tests that construct SlashContext by
    hand without a runtime get this graceful path."""
    runtime_dependent = {"/new", "/model", "/thinking", "/session"}
    for name in runtime_dependent:
        out = _io.StringIO()
        signal = await dispatch_slash(name, SlashContext(stdout=out))
        assert signal is ControlSignal.CONTINUE
        rendered = out.getvalue()
        assert "requires a chat runtime" in rendered.lower(), (
            f"{name} should print a 'no runtime' note; got {rendered!r}"
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


def test_tc_chat_15_completer_lists_all_twelve_commands_after_slash() -> None:
    """TC-CHAT-15 (first form): typing ``/`` yields every visible
    command (eight Step 24e + ``/skill`` from phase 2.4 + ``/perm``
    from qol-permission-system-human-readable.md Step 3 + ``/workflow``
    from Phase 3.9 Step 1 + ``/fork`` from Phase 3.9 Step 3). Aliases
    (``/quit``) are NOT in the popup — the registry's ``aliases``
    field is invisible by design."""
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
        "/skill",
        "/perm",
        "/workflow",
        "/fork",
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


async def test_tc_chat_05_input_loop_exits_on_slash_exit(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-05: ``/exit`` submitted to the input loop returns 0
    (and the input loop terminates — the subprocess smoke in
    test_chat_smoke.py exercises the same shape end-to-end via
    the CLI entry point).

    24c: ``_run_input_loop`` now requires runtime, config, and
    thread_id (the engine wiring lands here). Bootstrap a real
    runtime against the session-scoped chat_workspace fixture so
    the input loop has something to wire to. The engine never
    runs in this test because the user input is a slash command,
    not a message.
    """
    out = _io.StringIO()
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="tc-chat-05",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        # Feed "/exit" + Enter — the loop dispatches the slash
        # command, the handler returns EXIT, the loop exits with 0.
        pipe_input.send_text("/exit\r")
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=thread_id,
        )
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
    handler signature receives only a ``SlashContext`` whose
    fields are READ-ONLY lookups — no store, no event bus, no
    engine reference.

    24c widens the field set (stdout / agent_config / authz /
    agent_name) for ``/tools``; the read-only invariant still
    holds. A handler that wanted to mutate the session would need
    a store reference — which isn't on the context.
    """
    _ = tmp_path_factory  # fixture present for symmetry with the smoke layer
    out = _io.StringIO()
    ctx = SlashContext(stdout=out)
    # SlashContext exposes ONLY read-only lookups in 24c. The
    # field set is what we lock here; adding a write seam (store,
    # event bus, engine) would widen the surface a handler can
    # touch and needs a CHAT-R review.
    fields = set(ctx.__dataclass_fields__.keys())
    assert fields == {"stdout", "agent_config", "authz", "agent_name", "state"}, (
        f"SlashContext shape changed unexpectedly; got {fields}. "
        f"24e adds the mutable `state` for lifecycle commands; "
        f"every field is still a chat-internal lookup. No store / "
        f"engine / event-bus reference — adding one needs a "
        f"CHAT-R review."
    )
    # Defaulted fields stay None when the caller doesn't supply
    # them (the simple handlers shouldn't depend on them).
    assert ctx.agent_config is None
    assert ctx.authz is None
    assert ctx.agent_name is None
    assert ctx.state is None
    signal = await dispatch_slash("/clear", ctx)
    assert signal is ControlSignal.CONTINUE
    # The handler wrote the ANSI clear sequence. Nothing else
    # happened.
    rendered = out.getvalue()
    assert "\x1b[3J" in rendered  # scrollback clear


# ===========================================================================
# TC-CHAT-12 — /tools lists exactly the agent's tools (and no others)
# ===========================================================================


async def test_tc_chat_12_tools_lists_helper_agents_four_declared_tools(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-12: ``/tools`` lists exactly the tools the agent has
    access to. The shipped ``kolsch/agents/helper`` declares four
    refs in its ``subsystems.tools.config.declared`` block
    (``read_file``, ``write_file``, ``list_files``, ``bash``); the
    output names all four and contains none beyond.

    The rule chain column shows the rule_id that governs each
    tool. The helper agent in a freshly-initialised workspace
    runs against the default ``rl_default_deny`` tail — the
    column says "default deny" because no rule earlier in the
    chain matches ``tool.<name>`` for the helper. (A real
    deployment that ran ``kolsch permissions add`` would surface
    that rule's id instead — the lookup mirrors authz's
    first-match-wins.)
    """
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    out = _io.StringIO()
    ctx = SlashContext(
        stdout=out,
        agent_config=config,
        authz=runtime.authz,
        agent_name=config.name,
    )
    signal = await dispatch_slash("/tools", ctx)
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    # All four declared tools are listed.
    assert "read_file" in rendered
    assert "write_file" in rendered
    assert "list_files" in rendered
    assert "bash" in rendered
    # And nothing else: a builtin like "search" that the agent
    # does NOT declare must not appear.
    assert "search" not in rendered
    # The output identifies itself as a tools listing.
    assert "tools available" in rendered.lower()


async def test_tools_handler_without_agent_config_prints_polite_error() -> None:
    """When ``ctx.agent_config`` is None (test-context construction
    without runtime), ``/tools`` prints a polite error rather than
    crashing. Defensive — the chat itself always populates the
    field, but keeping the defensive path tested protects against
    future signature drift."""
    out = _io.StringIO()
    ctx = SlashContext(stdout=out)
    signal = await dispatch_slash("/tools", ctx)
    assert signal is ControlSignal.CONTINUE
    assert "no agent context" in out.getvalue().lower()


async def test_tools_handler_with_empty_subsystems_prints_empty_note() -> None:
    """An agent with no ``tools`` subsystem prints a one-line
    "no tools configured" note rather than an empty listing.
    Exercises the early-return branch in the handler.
    """
    from schemas import (
        AgentId,
        IOSpec,
        ModelSpec,
        ResolvedAgentConfig,
    )

    bare = ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_bare"),
        name="bare",
        description="agent with no tools",
        model=ModelSpec(provider="fake", model="fake-model"),
        prompt="...",
        io=IOSpec(mode="conversational"),
    )
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, agent_config=bare)
    signal = await dispatch_slash("/tools", ctx)
    assert signal is ControlSignal.CONTINUE
    assert "no tools" in out.getvalue().lower()


async def test_tools_handler_surfaces_matching_rule_id(
    chat_workspace: Path,
) -> None:
    """When an authz rule's ``(who, what)`` matches the agent's
    ``(principal, tool.<name>)`` pair, ``/tools`` shows that
    rule's id and effect. Mirrors authz first-match-wins.
    """
    from providers import RuleBasedAuthorizationProvider

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    out = _io.StringIO()
    # Custom authz with a rule that matches every agent + every
    # tool — first match wins, so /tools surfaces this rule's id.
    # qol-action-namespace-bundle-scoped.md Step 2 — tool actions
    # are now 3-seg `<bundle>/tools/<name>` (was 1-seg `tool.<name>`);
    # this regex matches the new shape.
    custom_authz = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {
                    "who": "agent\\..*",
                    "what": ".+/tools/.+",
                    "effect": "allow",
                },
                {"who": ".*", "what": ".*", "effect": "deny"},
            ],
        },
    )
    rules = await custom_authz.list_rules()
    matching_rule = next(r for r in rules if r.effect == "allow")
    ctx = SlashContext(
        stdout=out,
        agent_config=config,
        authz=custom_authz,
        agent_name=config.name,
    )
    signal = await dispatch_slash("/tools", ctx)
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    # Every tool row carries the same rule (it's the first match).
    assert matching_rule.id in rendered
    assert "(allow)" in rendered
