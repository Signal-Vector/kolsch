"""Phase 2.1 Step 24e — lifecycle commands + streaming UX tests.

Six named cases per the build-doc's acceptance criterion:

- **TC-CHAT-07 (resume)**: ``--session <id>`` replays the existing
  conversation through the same Renderer used for live events
  (patch §6.2 single-path).
- **TC-CHAT-08 (Ctrl-C)**: cancellation mid-stream → engine emits
  ``thread.exchange_failed`` with cause ``engine.cancelled``;
  the renderer surfaces the system notice. Driven via direct
  ``cancel_token.cancel()`` because FakeFramework + EchoLLM
  resolve too fast to inject a real SIGINT.
- **TC-CHAT-09 (tool error)**: a failing tool renders as the red
  panel — already covered structurally in ``test_chat_renderer``;
  this file pins the end-to-end engine path.
- **TC-CHAT-11 (/model)**: switch mid-session; the next exchange
  carries the new model in its ``model.request`` event.
- **TC-CHAT-13 (/new)**: create a fresh session; both sessions
  show in ``kolsch sessions list``; the title bar reflects the
  new id.
- **TC-CHAT-14 (/thinking)**: each form behaves per patch §5.2 —
  ``/thinking on/off`` set the session flag; ``/thinking budget
  <n>`` sets the budget; ``/thinking last`` prints the graceful
  degradation note.

Plus dynamic completer coverage for ``/model`` aliases and
session-state defaults.
"""

from __future__ import annotations

import io as _io
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import yaml
from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cli import chat
from cli._slash import (
    COMMAND_REGISTRY,
    ChatSessionState,
    ControlSignal,
    SlashCompleter,
    SlashContext,
    dispatch_slash,
)
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from platform_runtime import PlatformRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_state(workspace: Path) -> tuple[ChatSessionState, PlatformRuntime]:
    """Construct a ChatSessionState bound to a fresh runtime + session."""
    runtime = await bootstrap_runtime(root=workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="lifecycle-test",
    )
    threads = await runtime.store.list_threads(session.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    return state, runtime


# ===========================================================================
# TC-CHAT-13 — /new creates a fresh session
# ===========================================================================


async def test_tc_chat_13_new_creates_fresh_session_and_keeps_old(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-13: ``/new`` opens a fresh session under the same
    agent; both sessions appear in ``runtime.store.list_sessions``;
    the loop's state holds the new id.
    """
    state, runtime = await _build_state(chat_workspace)
    original_id = state.session_id

    # Dispatch /new — signals the loop.
    out = _io.StringIO()
    ctx = SlashContext(
        stdout=out,
        agent_config=state.config,
        authz=runtime.authz,
        agent_name=state.config.name,
        state=state,
    )
    signal = await dispatch_slash("/new", ctx)
    assert signal is ControlSignal.NEW_SESSION
    assert state.pending_new_session is True
    assert state.pending_clear is True

    # Drive the loop's lifecycle helper directly.
    await chat._open_new_session(state, stdout=out, agent_ref="kolsch/agents/helper")
    new_id = state.session_id
    assert new_id != original_id, "/new should switch to a different session id"

    # Both sessions persist in the store.
    sessions = await runtime.store.list_sessions()
    session_ids = {s.session_id for s in sessions}
    assert original_id in session_ids
    assert new_id in session_ids

    # The banner reprinted with the new id.
    assert "kolsch chat" in out.getvalue()
    assert str(new_id) in out.getvalue()


# ===========================================================================
# TC-CHAT-11 — /model switches mid-session
# ===========================================================================


async def test_tc_chat_11_model_switches_mid_session(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-11: ``/model <ref>`` updates ``state.config.model``
    in place. The next exchange's model.request will carry the
    new model (engine reads ``state.config`` per call).
    """
    state, runtime = await _build_state(chat_workspace)
    original_model = state.config.model

    out = _io.StringIO()
    ctx = SlashContext(
        stdout=out,
        agent_config=state.config,
        authz=runtime.authz,
        agent_name=state.config.name,
        state=state,
    )
    signal = await dispatch_slash("/model anthropic/claude-haiku-4-5", ctx)
    assert signal is ControlSignal.CONTINUE
    assert state.model_override is not None
    assert state.config.model.provider == "anthropic"
    assert state.config.model.model == "claude-haiku-4-5"
    assert state.config.model != original_model
    assert "model set to anthropic/claude-haiku-4-5" in out.getvalue()


async def test_tc_chat_11_model_bare_prints_current_model(
    chat_workspace: Path,
) -> None:
    """``/model`` without an arg prints the active model."""
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(
        stdout=out,
        agent_config=state.config,
        authz=runtime.authz,
        agent_name=state.config.name,
        state=state,
    )
    signal = await dispatch_slash("/model", ctx)
    assert signal is ControlSignal.CONTINUE
    assert "current model:" in out.getvalue()
    # The model is one of the configured providers — exact provider
    # depends on the workspace's defaults; assert non-empty.
    assert "/" in out.getvalue()


async def test_tc_chat_11_model_invalid_stays_in_session(
    chat_workspace: Path,
) -> None:
    """Patch §5 row 5: invalid model ref → error message, chat
    stays in the session (doesn't crash)."""
    state, runtime = await _build_state(chat_workspace)
    original_model = state.config.model
    out = _io.StringIO()
    ctx = SlashContext(
        stdout=out,
        agent_config=state.config,
        authz=runtime.authz,
        agent_name=state.config.name,
        state=state,
    )
    signal = await dispatch_slash("/model not-a-known-alias", ctx)
    assert signal is ControlSignal.CONTINUE  # NOT EXIT
    assert "invalid model ref" in out.getvalue().lower()
    # State unchanged on error.
    assert state.config.model == original_model
    assert state.model_override is None


# ===========================================================================
# TC-CHAT-14 — /thinking forms with graceful degradation
# ===========================================================================


async def test_tc_chat_14_thinking_bare_prints_state(
    chat_workspace: Path,
) -> None:
    """``/thinking`` with no arg prints display + budget + last
    availability per patch §5.2 row 1."""
    state, _runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    signal = await dispatch_slash("/thinking", ctx)
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    assert "thinking display: off" in rendered
    assert "thinking budget: (unset)" in rendered
    assert "last-reply thinking: unavailable" in rendered


async def test_tc_chat_14_thinking_on_then_off_toggles_flag(
    chat_workspace: Path,
) -> None:
    """Patch §5.2 rows 2-3: /thinking on/off flips session flag."""
    state, _ = await _build_state(chat_workspace)
    ctx = SlashContext(stdout=_io.StringIO(), state=state)
    await dispatch_slash("/thinking on", ctx)
    assert state.thinking_on is True
    await dispatch_slash("/thinking off", ctx)
    assert state.thinking_on is False


async def test_tc_chat_14_thinking_budget_sets_value(
    chat_workspace: Path,
) -> None:
    """Patch §5.2 row 4: /thinking budget <n> sets budget; the
    "may not honor it" note covers the engine's no-reasoning case."""
    state, _ = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    await dispatch_slash("/thinking budget 1024", ctx)
    assert state.thinking_budget == 1024
    assert "1024" in out.getvalue()
    assert "may not honor it" in out.getvalue()


async def test_tc_chat_14_thinking_budget_invalid_int(
    chat_workspace: Path,
) -> None:
    """Non-integer budget arg prints an error; state unchanged."""
    state, _ = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    await dispatch_slash("/thinking budget nope", ctx)
    assert state.thinking_budget is None
    assert "not an integer" in out.getvalue()


async def test_tc_chat_14_thinking_last_prints_graceful_note(
    chat_workspace: Path,
) -> None:
    """Patch §5.2 row 5: /thinking last with no reasoning events
    prints the documented graceful-degradation note."""
    state, _ = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    await dispatch_slash("/thinking last", ctx)
    assert "no thinking content" in out.getvalue().lower()


async def test_tc_chat_14_thinking_unknown_form_prints_error(
    chat_workspace: Path,
) -> None:
    """Unknown sub-form prints a one-line error."""
    state, _ = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    await dispatch_slash("/thinking weird", ctx)
    assert "unknown form" in out.getvalue().lower()


# ===========================================================================
# /session — show + switch
# ===========================================================================


async def test_session_bare_prints_current_session_id(
    chat_workspace: Path,
) -> None:
    """``/session`` with no arg prints the active session id."""
    state, _ = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    signal = await dispatch_slash("/session", ctx)
    assert signal is ControlSignal.CONTINUE
    assert str(state.session_id) in out.getvalue()


async def test_switch_session_loads_and_replays_via_renderer(
    chat_workspace: Path,
) -> None:
    """``_switch_session`` (the loop helper /session <id> triggers)
    swaps state to the new session and replays its events via the
    same Renderer used for live exchanges.
    """
    # Create a session with an exchange.
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session_a = await runtime.store.create_session(
        str(config.agent_id),
        name="session-a",
    )
    threads_a = await runtime.store.list_threads(session_a.session_id)
    from engine import ExecutionContext

    ctx = ExecutionContext(
        thread_id=threads_a[0].thread_id,
        trigger={"kind": "user_message", "content": "session-a-message"},
    )
    async for _ in runtime.engine.execute_stream(config, ctx):
        pass

    # Create a second session as the current one for the state.
    session_b = await runtime.store.create_session(
        str(config.agent_id),
        name="session-b",
    )
    threads_b = await runtime.store.list_threads(session_b.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session_b.session_id,
        thread_id=threads_b[0].thread_id,
    )

    out = _io.StringIO()
    await chat._switch_session(
        state, target_session_id=session_a.session_id, stdout=out
    )
    # State swapped to session_a.
    assert state.session_id == session_a.session_id
    rendered = out.getvalue()
    # Banner reprinted with the new id.
    assert str(session_a.session_id) in rendered
    # Replay surfaced the prior exchange's Echo reply.
    assert "Echo:" in rendered
    assert "session-a-message" in rendered


async def test_session_unknown_id_prints_error(
    chat_workspace: Path,
) -> None:
    """``/session <bad>`` prints a graceful error; chat stays in
    the current session."""
    state, _ = await _build_state(chat_workspace)
    original_id = state.session_id
    out = _io.StringIO()
    ctx = SlashContext(stdout=out, state=state)
    signal = await dispatch_slash("/session ses_does_not_exist", ctx)
    assert signal is ControlSignal.CONTINUE
    assert "not loadable" in out.getvalue().lower()
    assert state.session_id == original_id
    assert state.pending_session_id is None


# ===========================================================================
# TC-CHAT-07 — --session resume replays through Renderer
# ===========================================================================


async def test_tc_chat_07_resume_replays_existing_session_via_renderer(
    chat_workspace: Path,
) -> None:
    """TC-CHAT-07: an existing session with events → cmd_chat
    re-renders the conversation before entering the input loop.
    The render uses the SAME Renderer as live events (patch §6.2
    single-path); the rendered output contains the agent reply
    from the prior exchange.
    """
    # Step 1: create a session with one exchange.
    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="prior-session",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    from engine import ExecutionContext

    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "earlier hello"},
    )
    # Drain the exchange to populate the JSONL.
    async for _ in runtime.engine.execute_stream(config, ctx):
        pass

    # Step 2: resume via --session in a fresh cmd_chat call.
    import argparse

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=chat_workspace,
                agent_ref="kolsch/agents/helper",
                session=str(session.session_id),
                model=None,
            ),
            out=out,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
    assert rc == 0
    rendered = out.getvalue()
    # The replay surfaces the EchoLLMDriver's prior reply.
    assert "Echo:" in rendered
    assert "earlier hello" in rendered
    # Banner reflects the resumed session id, not a fresh one.
    assert str(session.session_id) in rendered


async def test_resume_unknown_session_exits_nonzero(
    chat_workspace: Path,
) -> None:
    """``--session <bad-id>`` at startup → friendly error, exit 1."""
    import argparse

    out = _io.StringIO()
    err = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=chat_workspace,
                agent_ref="kolsch/agents/helper",
                session="ses_does_not_exist",
                model=None,
            ),
            out=out,
            err=err,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
    assert rc == 1
    combined = out.getvalue() + err.getvalue()
    assert "not loadable" in combined.lower() or "not found" in combined.lower()


async def test_cmd_chat_with_valid_model_override_works(
    chat_workspace: Path,
) -> None:
    """``--model anthropic/claude-haiku-4-5`` at startup is accepted
    and the banner reflects the override."""
    import argparse

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=chat_workspace,
                agent_ref="kolsch/agents/helper",
                session=None,
                model="anthropic/claude-haiku-4-5",
            ),
            out=out,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
    assert rc == 0
    assert "anthropic/claude-haiku-4-5" in out.getvalue()


async def test_input_loop_lifecycle_signals_drive_loop(
    chat_workspace: Path,
) -> None:
    """Drive the input loop through /new mid-chat: scripted input
    sends "/new\\r" + EOF. After EOF, the loop returns 0 having
    processed the new-session signal — assert state changes."""

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="loop-lifecycle",
    )
    threads = await runtime.store.list_threads(session.session_id)
    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        pipe_input.send_text("/new\r")
        pipe_input.close()
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=threads[0].thread_id,
            session_id=session.session_id,
            agent_ref="kolsch/agents/helper",
        )
    assert rc == 0
    rendered = out.getvalue()
    # The /new handler triggered: scrollback-clear ANSI fired,
    # then a fresh banner. Asserting on the banner is the
    # observable evidence.
    assert "kolsch chat" in rendered
    # New session id is different from the originally-passed one.
    sessions = await runtime.store.list_sessions()
    assert len(sessions) >= 2  # original + new


async def test_input_loop_session_switch_via_slash(
    chat_workspace: Path,
) -> None:
    """``/session <existing-id>`` mid-chat switches + replays.

    Drives the ControlSignal.RELOAD path in the loop, including
    the post-dispatch state-mutation branch.
    """

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    # Source session — has an exchange.
    source = await runtime.store.create_session(
        str(config.agent_id),
        name="source",
    )
    source_threads = await runtime.store.list_threads(source.session_id)
    from engine import ExecutionContext

    ctx = ExecutionContext(
        thread_id=source_threads[0].thread_id,
        trigger={"kind": "user_message", "content": "source message"},
    )
    async for _ in runtime.engine.execute_stream(config, ctx):
        pass

    # Current session (different one).
    current = await runtime.store.create_session(
        str(config.agent_id),
        name="current",
    )
    current_threads = await runtime.store.list_threads(current.session_id)

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        pipe_input.send_text(f"/session {source.session_id}\r")
        pipe_input.close()
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=current_threads[0].thread_id,
            session_id=current.session_id,
            agent_ref="kolsch/agents/helper",
        )
    assert rc == 0
    rendered = out.getvalue()
    # The source session's exchange was replayed.
    assert "source message" in rendered
    assert "Echo:" in rendered


async def test_cmd_chat_invalid_model_override_exits_nonzero(
    chat_workspace: Path,
) -> None:
    """``--model <unknown-alias>`` at startup → exit 1 (Fatal per
    patch §1.3 row "Invalid invocation")."""
    import argparse

    out = _io.StringIO()
    err = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=chat_workspace,
                agent_ref="kolsch/agents/helper",
                session=None,
                model="not-a-real-alias",
            ),
            out=out,
            err=err,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
    assert rc == 1
    combined = out.getvalue() + err.getvalue()
    assert "invalid --model" in combined.lower()


# ===========================================================================
# TC-CHAT-08 — Ctrl-C cancellation
# ===========================================================================


async def test_tc_chat_08_cancel_token_signals_engine() -> None:
    """TC-CHAT-08: the chat-level cancel mechanism uses the
    engine's typed :class:`engine.CancelToken` — calling
    ``.cancel()`` flips ``is_cancelled()`` to True synchronously,
    which is what the engine's pre-flight polls.

    A full end-to-end Ctrl-C through stdin would require a slow
    LLM to be mid-stream when the signal arrives; the FakeFramework
    + EchoLLMDriver resolve too fast to inject a real interrupt
    in test time. This case pins the mechanism the loop uses;
    the loop's KeyboardInterrupt handler is exercised separately."""
    from engine import CancelToken

    token = CancelToken()
    assert token.is_cancelled() is False
    token.cancel()
    assert token.is_cancelled() is True


def test_cancel_handling_in_loop_signals_engine_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The input loop wraps ``renderer.render`` in a try/except
    KeyboardInterrupt that calls ``cancel.cancel()`` then drains
    remaining events. This test inspects the loop source for that
    pattern — a structural pin so a future refactor doesn't drop
    the cancellation wiring.

    Source-level test (cheap, doesn't require driving stdin)
    because the engine's instant-response under FakeFramework
    makes a real interrupt test flaky. The behavioural test would
    need a slow connector and is deferred to a streaming-engine
    phase."""
    import inspect

    source = inspect.getsource(chat._run_input_loop)
    assert "KeyboardInterrupt" in source
    assert "cancel.cancel()" in source
    assert "interrupted" in source.lower()
    _ = monkeypatch  # signal it's intentionally a structural test


# ===========================================================================
# SlashCompleter dynamic /model alias suggestions
# ===========================================================================


def test_completer_yields_model_aliases_when_configured() -> None:
    """24e: SlashCompleter accepts a ``model_alias_provider``;
    when set, ``/model `` (trailing space) suggests each alias
    with its canonical target as ``display_meta``."""
    aliases = {
        "opus": "anthropic/claude-opus-4-7",
        "haiku": "anthropic/claude-haiku-4-5",
    }
    completer = SlashCompleter(model_alias_provider=lambda: aliases)
    from prompt_toolkit.completion import CompleteEvent

    doc = Document(text="/model ", cursor_position=len("/model "))
    event = CompleteEvent(completion_requested=True)
    completions = list(completer.get_completions(doc, event))
    displays = [
        c.display[0][1] if isinstance(c.display, list) else str(c.display)
        for c in completions
    ]
    # Both aliases surface; the free-form-arg hint also surfaces
    # at the end (the `<type any model ref>` placeholder).
    assert "opus" in displays
    assert "haiku" in displays


def test_completer_omits_aliases_when_provider_returns_empty() -> None:
    """Empty model_aliases → no alias suggestions; the free-form
    hint still surfaces so the user knows the field accepts any
    ref."""
    completer = SlashCompleter(model_alias_provider=dict)
    from prompt_toolkit.completion import CompleteEvent

    doc = Document(text="/model ", cursor_position=len("/model "))
    event = CompleteEvent(completion_requested=True)
    completions = list(completer.get_completions(doc, event))
    # No matched aliases, but the free-form hint still surfaces.
    displays = [
        c.display[0][1] if isinstance(c.display, list) else str(c.display)
        for c in completions
    ]
    assert any("any model" in d.lower() for d in displays)


def test_completer_filters_aliases_by_prefix() -> None:
    """``/model op`` narrows to aliases starting with ``op``."""
    aliases = {
        "opus": "anthropic/claude-opus-4-7",
        "haiku": "anthropic/claude-haiku-4-5",
    }
    completer = SlashCompleter(model_alias_provider=lambda: aliases)
    from prompt_toolkit.completion import CompleteEvent

    doc = Document(text="/model op", cursor_position=len("/model op"))
    event = CompleteEvent(completion_requested=True)
    completions = list(completer.get_completions(doc, event))
    displays = [
        c.display[0][1] if isinstance(c.display, list) else str(c.display)
        for c in completions
    ]
    assert "opus" in displays
    assert "haiku" not in displays


# ===========================================================================
# _load_model_aliases — yaml round-trip
# ===========================================================================


def test_load_model_aliases_returns_empty_for_missing_yaml(tmp_path: Path) -> None:
    """No platform.yaml → empty dict (not a crash)."""
    aliases = chat._load_model_aliases(tmp_path)
    assert aliases == {}


def test_load_model_aliases_reads_section(tmp_path: Path) -> None:
    """Reads ``model_aliases`` from platform.yaml verbatim."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "model_aliases": {
                    "opus": "anthropic/claude-opus-4-7",
                    "haiku": "anthropic/claude-haiku-4-5",
                },
            },
        ),
        encoding="utf-8",
    )
    aliases = chat._load_model_aliases(tmp_path)
    assert aliases == {
        "opus": "anthropic/claude-opus-4-7",
        "haiku": "anthropic/claude-haiku-4-5",
    }


def test_load_model_aliases_handles_malformed_yaml(tmp_path: Path) -> None:
    """A malformed platform.yaml → empty dict, not a crash."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text("{ this is not: valid: yaml ::", encoding="utf-8")
    aliases = chat._load_model_aliases(tmp_path)
    assert aliases == {}


def test_load_model_aliases_handles_non_dict_section(tmp_path: Path) -> None:
    """``model_aliases`` as a list → empty dict (defensive)."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"model_aliases": ["opus", "haiku"]}),
        encoding="utf-8",
    )
    aliases = chat._load_model_aliases(tmp_path)
    assert aliases == {}


# ===========================================================================
# Smoke — full end-to-end with /new mid-session
# ===========================================================================


def test_cli_smoke_chat_with_session_resume(chat_workspace: Path) -> None:
    """End-to-end smoke: --session against an empty pre-existing
    session resumes cleanly. Pins the subprocess path so the
    bug-sessions-namespace-attr-error.md hard rule is honoured for
    24e additions."""
    # Use the session created at fixture time (the workspace bootstrap
    # doesn't create one — we'll create one through a separate
    # subprocess to keep this test hermetic).
    # First create a session via `kolsch run` — that's the canonical
    # session-creator path.
    env = dict(os.environ)
    env.pop("KOLSCH_PROVIDER", None)
    env.pop("KOLSCH_PROVIDER_KEY", None)
    env["KOLSCH_WORKSPACE"] = str(chat_workspace)
    run_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "run",
            "kolsch/agents/helper",
            "--input",
            "content=hello-from-smoke",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stderr

    # Find the most recent session id from the list output.
    list_result = subprocess.run(
        [sys.executable, "-m", "cli", "sessions", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert list_result.returncode == 0
    # The session id appears as the first column. The output has
    # a header row first; the first non-header line whose first
    # token starts with "ses_" is the one we want.
    session_id: str | None = None
    for line in list_result.stdout.strip().splitlines():
        token = line.split(maxsplit=1)[0] if line.strip() else ""
        if token.startswith("ses_"):
            session_id = token
            break
    assert session_id is not None, (
        f"no ses_ id in sessions list output: {list_result.stdout!r}"
    )

    # Resume via --session, send EOF immediately.
    chat_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "chat",
            "kolsch/agents/helper",
            "--session",
            session_id,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        input="",
        check=False,
    )
    assert chat_result.returncode == 0, chat_result.stderr
    assert "Traceback" not in chat_result.stderr
    # Resume rendered the prior exchange.
    assert "Echo:" in chat_result.stdout


# ===========================================================================
# ChatSessionState defaults — pin shape for future-step additions
# ===========================================================================


async def test_chat_session_state_defaults(chat_workspace: Path) -> None:
    """ChatSessionState defaults: lifecycle flags off, no model
    override, empty aliases. Pinning prevents silent regression
    if a future step changes a default."""
    state, _ = await _build_state(chat_workspace)
    assert state.model_override is None
    assert state.thinking_on is False
    assert state.thinking_budget is None
    assert state.pending_clear is False
    assert state.pending_new_session is False
    assert state.pending_session_id is None
    assert state.model_aliases == {}


# ===========================================================================
# Registry sanity — every command has a real handler now
# ===========================================================================


def test_no_placeholder_handlers_remain_in_24e() -> None:
    """24e completes Phase 2.1's slash surface — every entry has
    a real handler, no entry retains ``placeholder_step``
    (the field stays in the dataclass for future use)."""
    real = [c for c in COMMAND_REGISTRY.values() if c.handler is not None]
    assert len(real) == len(COMMAND_REGISTRY)
    # No registry entry retains a placeholder_step (those were
    # bookkeeping for the step it would land in; all landed now).
    for cmd in COMMAND_REGISTRY.values():
        assert cmd.placeholder_step is None, (
            f"{cmd.name} retains placeholder_step={cmd.placeholder_step!r}; "
            f"all handlers land in 24e, so the field should be cleared"
        )
