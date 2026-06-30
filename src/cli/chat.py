r"""`kolsch chat` — streaming REPL.

Each user submission becomes an
[`ExecutionContext`][engine.context.ExecutionContext] whose
`execute_stream` events feed the
[`Renderer`][cli._renderer.Renderer]. The renderer is a pure
consumer — every visible block traces back to one event the engine
emitted.

**Architectural commitment.** The chat stays in prompt_toolkit's
non-full-screen idiom. Native terminal scrolling and text selection
only work on output written via normal stdout; full-screen /
alternate-screen mode replaces the scrollback buffer and selection
breaks across every terminal emulator. A guard test enforces this
architecturally — see `tests/component/cli/test_chat_input.py`.

**Key bindings.**

- **Enter** submits the current input.
- **Ctrl-J** inserts a newline. Universal — every terminal sends
  `\n` for Ctrl-J. Shift-Enter on iTerm2 / kitty / Alacritty
  *also* sends Ctrl-J under default settings, so this single
  binding covers the "primary Shift-Enter" requirement on the
  terminals that distinguish it.
- **Alt-Enter** (Esc-Enter sequence) inserts a newline. Universal
  fallback for terminals that don't send a distinct Shift-Enter
  (macOS Terminal.app, stock gnome-terminal). Documented in the
  footer hint.
- **Shift-Enter**: prompt_toolkit 3.0.x doesn't expose a portable
  `s-enter` key token, but every terminal that sends a distinct
  Shift-Enter sequence (iTerm2, kitty, Alacritty in their
  defaults) routes it through Ctrl-J anyway, so the Ctrl-J
  binding above already covers it. Terminals that collapse
  Shift-Enter to plain Enter (macOS Terminal.app,
  stock gnome-terminal) submit — users on those terminals use
  Alt-Enter or Ctrl-J for newline, both documented in the footer
  hint.
- Default prompt_toolkit editing keys cover cursor motion,
  history, kill/yank.

Ctrl-D raises `EOFError` and exits the loop cleanly (returns `0`).
Ctrl-C raises `KeyboardInterrupt` and clears the current input
without exiting.

Chat is interactive-only; the user's submitted line lands in the
scrollback via prompt_toolkit's terminal echo. Non-TTY usage is not
supported (use `kolsch run` for scripted invocation).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING, Any, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from cli._modal import ApprovalModal
from cli._renderer import Renderer
from cli._slash import (
    ChatSessionState,
    ControlSignal,
    SlashCompleter,
    SlashContext,
    _enabled_skill_refs,
    dispatch_slash,
    footer_hint_for_buffer,
    is_slash_command,
)
from cli.permissions import _write_back as _persist_authz_rules
from engine import CancelToken, ExecutionContext
from platform_runtime import bootstrap_runtime
from providers import RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output

    from platform_runtime import PlatformRuntime
    from schemas import ResolvedAgentConfig, SessionId, ThreadId


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


_DEFAULT_AGENT_REF = "kolsch/agents/helper"
"""Fallback when `<agent_ref>` is omitted and
`platform.yaml`'s `defaults.starter_agent` is unset.
3-segment canonical form."""


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


async def cmd_chat(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    prompt_input: Input | None = None,
    prompt_output: Output | None = None,
) -> int:
    """Run the chat REPL.

    Startup flow:

    1. Workspace resolution is already done by the dispatcher in
       `cli.main.main` before `cmd_chat` is called; `args.root` is
       the resolved path.
    2. Bootstrap the runtime.
    3. Resolve `<agent_ref>` and build the agent config.
    4. Apply `--model` override if present.
    5. Create a fresh session named `chat:<agent_ref>`, or reload
       an existing one when `--session` is set.
    6. Print the title banner; enter the input loop.
    """
    stdout = out if out is not None else sys.stdout
    _ = err  # reserved for stderr-bound diagnostics in later steps

    agent_ref = args.agent_ref or _DEFAULT_AGENT_REF
    runtime = await bootstrap_runtime(root=args.root)
    config = await runtime.build_resolved_config(agent_ref)

    model_label = _format_model_label(config.model)
    if args.model:
        # --model startup override resolves through the same
        # ModelSpec parser /model uses; on parse failure exit 1
        # with a clear message rather than entering the chat with
        # a bogus model.
        try:
            override = _parse_startup_model_override(
                args.model,
                runtime,
                args.root,
            )
        except Exception as exc:  # noqa: BLE001 — surface every shape
            err_stream = err if err is not None else sys.stderr
            err_stream.write(f"chat: invalid --model {args.model!r}: {exc}\n")
            return 1
        config = config.model_copy(update={"model": override})
        model_label = f"{override.provider}/{override.model}"

    # --session loads an existing session and replays its events
    # through the same Renderer. Without --session, create a fresh
    # session.
    if getattr(args, "session", None):
        try:
            from schemas import SessionId  # noqa: PLC0415

            session = await runtime.store.get_session(SessionId(str(args.session)))
        except Exception as exc:  # noqa: BLE001
            err_stream = err if err is not None else sys.stderr
            err_stream.write(
                f"chat: --session {args.session!r} not loadable: {exc}\n",
            )
            return 1
    else:
        session = await runtime.store.create_session(
            str(config.agent_id),
            name=f"chat:{agent_ref}",
        )

    _print_banner(
        stdout,
        agent_name=config.name,
        model_label=model_label,
        session_id=str(session.session_id),
    )

    # model_aliases come from platform.yaml; recent_session_ids
    # come from the store. Both feed the SlashCompleter's
    # dynamic-suggestion paths.
    model_aliases = _load_model_aliases(args.root)
    recent_session_ids = await _snapshot_recent_session_ids(runtime)
    enabled_skill_refs = [
        (ref, "force-load before next message") for ref in _enabled_skill_refs(config)
    ]
    prompt_session = _build_prompt_session(
        input=prompt_input,
        output=prompt_output,
        model_aliases=model_aliases,
        recent_session_ids=recent_session_ids,
        enabled_skill_refs=enabled_skill_refs,
    )
    # threads[0] convention — sessions are single-thread today;
    # multi-thread support lands later.
    threads = await runtime.store.list_threads(session.session_id)
    if not threads:
        msg = f"session {session.session_id} has no threads"
        raise RuntimeError(msg)
    thread_id = threads[0].thread_id

    # Replay the loaded session's events before entering the input
    # loop. The replay uses the SAME Renderer as live turns —
    # render-output byte-identical — which is what the renderer's
    # pure-consumer property makes possible.
    if getattr(args, "session", None):
        await _replay_session(
            runtime=runtime,
            thread_id=thread_id,
            stdout=stdout,
            agent_name=config.name,
        )

    # Register the approval modal as the chat-surface prompt
    # callback. Pair the registration with a finally so a
    # subsequent in-process command (test suites, the future
    # Python embedding API) never sees a leaked modal — which would
    # either hang on stdin or, worse, allow silently. The clear is
    # unconditional regardless of how the loop exited.
    # The authz provider's action→tool_ref lookup lets the modal's
    # [a]/[d] write path construct new-shape Rules (qualified
    # ``what`` + glob ``pattern``). When the authz variant doesn't
    # expose lookup_tool_ref (a future custom variant), the modal
    # falls back to legacy regex shape.
    tool_ref_lookup = getattr(runtime.authz, "lookup_tool_ref", None)
    modal = ApprovalModal(
        stdout=stdout,
        tool_ref_lookup=tool_ref_lookup,
    )
    _install_modal_callback(runtime, modal, yaml_path=args.root / "platform.yaml")
    try:
        return await _run_input_loop(
            prompt_session,
            stdout=stdout,
            runtime=runtime,
            config=config,
            thread_id=thread_id,
            session_id=session.session_id,
            agent_ref=agent_ref,
            model_aliases=model_aliases,
            modal=modal,
        )
    finally:
        _clear_modal_callback(runtime)


# ---------------------------------------------------------------------------
# Input loop — separable for tests
# ---------------------------------------------------------------------------


def _build_prompt_session(
    *,
    input: Input | None = None,  # noqa: A002 — prompt_toolkit's own kwarg name
    output: Output | None = None,
    model_aliases: dict[str, str] | None = None,
    recent_session_ids: list[tuple[str, str]] | None = None,
    enabled_skill_refs: list[tuple[str, str]] | None = None,
) -> PromptSession[str]:
    """Construct the prompt_toolkit session with the chat's bindings.

    `input` / `output` default to `None` for production
    (prompt_toolkit auto-detects stdin/stdout); tests inject a
    `create_pipe_input()` + `DummyOutput()` pair so the input loop
    is driven by feeding key sequences from Python code.

    `full_screen` is never set. The architectural guard test
    inspects this module's source and fails loudly if a future
    change introduces full-screen mode.

    `model_aliases` drives the `/model` dynamic completer
    suggestions.
    """
    bindings = _build_key_bindings()
    # The completer + bottom_toolbar both read from the slash
    # registry (single source of truth). /model dynamic suggestions
    # ride a closure over the aliases dict so the completer doesn't
    # import bootstrap-adjacent modules.
    aliases_snapshot = dict(model_aliases or {})
    sessions_snapshot = list(recent_session_ids or [])
    skills_snapshot = list(enabled_skill_refs or [])
    completer = SlashCompleter(
        model_alias_provider=(lambda: aliases_snapshot) if aliases_snapshot else None,
        session_id_provider=(
            (lambda: sessions_snapshot) if sessions_snapshot else None
        ),
        skill_ref_provider=(lambda: skills_snapshot) if skills_snapshot else None,
    )

    def _bottom_toolbar() -> str:
        # prompt_toolkit doesn't expose the current buffer on the
        # bottom-toolbar callback; we peek at the active app's
        # buffer via PromptSession's internals when available, and
        # fall back to the idle hint otherwise. The fallback is
        # the right default: while the chat is idle (no active
        # buffer), the idle hint is what we'd show anyway.
        from prompt_toolkit.application.current import get_app  # noqa: PLC0415

        try:
            buffer_text = get_app().current_buffer.text
        except Exception:  # noqa: BLE001 — app may not be running yet
            buffer_text = ""
        return footer_hint_for_buffer(buffer_text)

    kwargs: dict[str, Any] = {
        "multiline": True,
        "key_bindings": bindings,
        "history": InMemoryHistory(),
        "completer": completer,
        "complete_while_typing": True,
        "bottom_toolbar": _bottom_toolbar,
    }
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    return PromptSession(**kwargs)


def _build_key_bindings() -> KeyBindings:
    """Build the chat's key-binding table.

    `multiline=True` inverts prompt_toolkit's default Enter
    behaviour (which would insert newline). The bindings below
    re-target Enter to submit and route the three documented
    newline keys (Ctrl-J, Alt-Enter, Shift-Enter) to newline
    insertion.
    """
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event: Any) -> None:  # noqa: ANN401
        """Enter submits the current input."""
        event.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _newline_ctrl_j(event: Any) -> None:  # noqa: ANN401
        r"""Ctrl-J inserts a literal newline.

        Universal because every terminal sends `\n` for Ctrl-J.
        On iTerm2 / kitty / Alacritty with default settings,
        Shift-Enter is delivered as Ctrl-J — this binding covers
        the "primary Shift-Enter" requirement on those terminals.
        """
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _newline_alt_enter(event: Any) -> None:  # noqa: ANN401
        """Alt-Enter (Esc-Enter) inserts a newline — universal fallback.

        Cross-terminal fallback for macOS Terminal.app and
        gnome-terminal, which don't distinguish Shift-Enter from
        plain Enter. Every terminal delivers the Esc-Enter sequence
        as a distinct two-key event, so this binding works
        everywhere.
        """
        event.current_buffer.insert_text("\n")

    return kb


async def _run_input_loop(  # noqa: PLR0913, PLR0912, PLR0915 — loop owns all the chat state + per-exchange branching; bundling further hurts readability
    prompt_session: PromptSession[str],
    *,
    stdout: TextIO,
    runtime: PlatformRuntime,
    config: ResolvedAgentConfig,
    thread_id: ThreadId,
    session_id: SessionId | None = None,
    agent_ref: str | None = None,
    model_aliases: dict[str, str] | None = None,
    modal: ApprovalModal | None = None,
) -> int:
    """Drive the prompt_session until EOF, `/exit`, or an unrecoverable error.

    Submitted text starting with `/` dispatches as a slash command;
    anything else becomes an
    [`ExecutionContext`][engine.context.ExecutionContext] fed to the
    engine via
    [`AgentEngine.execute_stream`][engine.AgentEngine.execute_stream]
    and rendered through a fresh `Renderer` per turn — the
    renderer's only state is the current turn's block list.

    Three lifecycle behaviours:

    - `/new` → loop spins up a new session under the same agent,
      clears the visible scrollback, reprints the banner.
    - `/session <id>` → loop loads the named session and replays
      its events through the same renderer (no replay-specific code
      path).
    - Ctrl-C during the renderer's await → signal the engine's
      typed cancellation hook, drain the remaining events (the
      engine emits `thread.exchange_failed` with cause
      `engine.cancelled`), then return to the prompt.

    Ctrl-D / EOF returns exit code 0. Ctrl-C at the prompt clears
    the current input without exiting.
    """
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session_id or _placeholder_session_id(),
        thread_id=thread_id,
        model_aliases=dict(model_aliases or {}),
    )
    # Late-bind state to the modal so the deny path can flip
    # state.active_cancel and the fan-out guard can read it. The
    # modal is constructed during cmd_chat startup (so the authz
    # registration lands before any input is processed); state only
    # exists here.
    if modal is not None:
        modal.bind_state(state)
    slash_ctx = SlashContext(
        stdout=stdout,
        agent_config=config,
        authz=runtime.authz,
        agent_name=config.name,
        state=state,
    )
    while True:
        try:
            text = await prompt_session.prompt_async("You: ")
        except EOFError:
            stdout.write("\n")
            return 0
        except KeyboardInterrupt:
            stdout.write("\n")
            continue
        # When a workflow is paused waiting for a chat reply, a
        # non-slash message is the resume response. A slash command
        # falls through to the dispatcher, which clears the
        # resume-expectation flag for state-changing commands and
        # leaves it armed for info-only ones (`/help`, `/tools`).
        if state.expecting_user_input_response and not is_slash_command(text):
            await _resume_pending_workflow(state, stdout=stdout, response_text=text)
            continue
        if is_slash_command(text):
            signal = await dispatch_slash(text, slash_ctx)
            # Apply pending state transitions BEFORE checking
            # the signal so /clear's ANSI sequence runs in the
            # same iteration as the handler.
            if state.pending_clear:
                stdout.write("\x1b[H\x1b[2J\x1b[3J")
                stdout.flush()
                state.pending_clear = False
            if signal is ControlSignal.EXIT:
                return 0
            if signal is ControlSignal.NEW_SESSION and state.pending_new_session:
                await _open_new_session(
                    state,
                    stdout=stdout,
                    agent_ref=agent_ref or _DEFAULT_AGENT_REF,
                )
                # Reflect new state into the slash_ctx for handlers
                # that read agent_config (e.g., /tools). slash_ctx
                # is frozen so build a fresh one.
                slash_ctx = _refresh_slash_ctx(slash_ctx, state)
                state.pending_new_session = False
            if signal is ControlSignal.RELOAD and state.pending_session_id is not None:
                await _switch_session(
                    state,
                    target_session_id=state.pending_session_id,
                    stdout=stdout,
                )
                slash_ctx = _refresh_slash_ctx(slash_ctx, state)
                state.pending_session_id = None
            continue
        cancel = CancelToken()
        # Share the per-turn cancel handle via ChatSessionState so
        # the modal's deny path and the Esc background watcher can
        # flip the same token Ctrl-C already flips. The finally
        # below clears both fields so the next iteration starts
        # fresh.
        state.active_cancel = cancel
        # Pending /skill refs flip the trigger shape. Consumed
        # (cleared) here so a one-shot `/skill` does not bleed into
        # the next turn.
        trigger = _build_user_trigger(text, state.pending_force_skill)
        state.pending_force_skill.clear()
        ctx = ExecutionContext(
            thread_id=state.thread_id,
            trigger=trigger,
            cancellation=cancel,
        )
        renderer = Renderer(stdout=stdout, agent_name=state.config.name)
        event_stream = state.runtime.engine.execute_stream(state.config, ctx)
        # Spawn the Esc watcher BEFORE entering renderer.render so
        # it's already attached when the first event lands. The
        # watcher is cancelled in the finally so the next
        # prompt_async iteration doesn't fight for stdin.
        esc_task = asyncio.create_task(_watch_esc_during_streaming(state, cancel))
        exit_reason: str | None = None
        try:
            try:
                await renderer.render(event_stream)
            except KeyboardInterrupt:
                # Ctrl-C during streaming: signal the engine's typed
                # cancellation hook + drain the remaining events
                # (the engine emits thread.exchange_failed with
                # cause engine.cancelled) so the JSONL stays honest.
                # The renderer's exchange_failed branch surfaces the
                # cancel as a yellow system notice.
                if state.last_cancel_reason is None:
                    state.last_cancel_reason = "interrupted"
                cancel.cancel()
                stdout.write("\n[interrupted; waiting for engine to cancel...]\n")
                stdout.flush()
                # Best-effort drain so the engine emits its
                # thread.exchange_failed before the loop returns to
                # the prompt. A second KeyboardInterrupt (or any
                # error) is swallowed — the engine's own teardown
                # is the source of truth for the cancellation event.
                with contextlib.suppress(KeyboardInterrupt, Exception):
                    await renderer.render(event_stream)
        finally:
            # Tear down the Esc watcher BEFORE the next prompt's
            # prompt_async grabs stdin — otherwise the watcher's
            # asyncio reader would race the PromptSession's.
            esc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await esc_task
            # Capture the reason BEFORE clearing so the post-render
            # print sees it; the clear keeps the next exchange
            # honest.
            exit_reason = state.last_cancel_reason
            state.active_cancel = None
            state.last_cancel_reason = None
        # Chat-surface print of the distinct cancellation reason.
        # The renderer's generic [cancelled] suffix already fired on
        # the partial agent block (if any); this confirms what
        # happened and which key produced it.
        if exit_reason == "denied":
            stdout.write("[stopped — you denied the tool]\n")
            stdout.flush()
        elif exit_reason == "interrupted":
            stdout.write("[stopped — interrupted]\n")
            stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_trigger(text: str, force_skills: list[str]) -> dict[str, Any]:
    """Construct the per-turn trigger dict.

    Returns the `user_message_with_force_skill` shape when
    `force_skills` is non-empty, otherwise the plain `user_message`
    shape. Pure for test reach — the input loop calls this then
    clears the source list.
    """
    if force_skills:
        return {
            "kind": "user_message_with_force_skill",
            "content": text,
            "force_skills": list(force_skills),
        }
    return {"kind": "user_message", "content": text}


async def _watch_esc_during_streaming(
    state: ChatSessionState,
    cancel: CancelToken,
    *,
    inp_factory: Any = None,  # noqa: ANN401 — Callable[[], prompt_toolkit.Input]; tests inject create_pipe_input
) -> None:
    """Watch stdin for a bare Esc keystroke; flip the cancel token when seen.

    Routed into the same `CancelToken` Ctrl-C uses so the
    framework's iteration-boundary poll observes cancellation and
    raises on the next cycle. `state.last_cancel_reason` is stamped
    to `"interrupted"` BEFORE `cancel.cancel()` so the chat's
    post-render print can distinguish Esc from a modal-driven deny.

    Uses prompt_toolkit's `Input` abstraction so the Vt100 parser
    handles the bare-Esc-vs-`Esc[A`-prefix disambiguation (~0.5s
    flush timer) for us. `inp_factory` is the test seam
    (`create_pipe_input` in unit tests; the default `create_input`
    reads the real terminal).

    Gracefully no-ops if stdin is not a TTY (Input construction,
    `raw_mode`, or `attach` fails). The chat is interactive-only in
    production — non-TTY use is documented as "go use
    `kolsch run`" — but component tests that drive the loop without
    a real terminal don't blow up.
    """
    try:
        inp = (inp_factory or create_input)()
    except Exception:  # noqa: BLE001 — non-TTY surfaces vary by platform
        return

    triggered = asyncio.Event()

    def _on_input_ready() -> None:
        # Drain everything currently buffered and force the parser to
        # flush a lone Esc (otherwise the Vt100 parser would hold the
        # byte waiting for a possible escape-sequence continuation).
        for kp in list(inp.read_keys()) + list(inp.flush_keys()):
            if kp.key == Keys.Escape:
                if state.last_cancel_reason is None:
                    state.last_cancel_reason = "interrupted"
                cancel.cancel()
                triggered.set()
                return

    try:
        with inp.raw_mode(), inp.attach(_on_input_ready):
            await triggered.wait()
    except asyncio.CancelledError:
        # Normal teardown — the per-exchange finally cancelled us.
        raise
    except Exception:  # noqa: BLE001 — raw_mode/attach failures should not crash chat
        return


async def _resume_pending_workflow(
    state: ChatSessionState,
    *,
    stdout: TextIO,
    response_text: str,
) -> None:
    """Resume the chat's pending workflow_run with the operator's reply.

    Loads the run from disk, rebuilds the executor + capsule
    renderer, awaits
    [`IterativeExecutor.resume`][workflows.executor.IterativeExecutor.resume]
    while tailing the JSONL, and re-arms
    `ChatSessionState.expecting_user_input_response` if the workflow
    pauses again on a downstream `user_input`.

    No-op when `pending_workflow_run_id` is unexpectedly absent (the
    input-loop branch already gates on
    `expecting_user_input_response` so this should never fire — the
    guard is defense-in-depth).
    """
    from pathlib import Path  # noqa: PLC0415

    from cli._workflow_chat_runner import (  # noqa: PLC0415
        execute_workflow_with_renderer,
    )
    from workflows.errors import WorkflowError  # noqa: PLC0415
    from workflows.ids import WorkflowRunId  # noqa: PLC0415
    from workflows.parser import load_workflow  # noqa: PLC0415
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    if not state.pending_workflow_run_id:
        # Should never happen — the input-loop branch is gated on the
        # expectation flag. If the flag is True but the pointer is
        # missing, clear the flag so the chat doesn't get stuck.
        state.expecting_user_input_response = False
        return

    runtime = state.runtime
    store = WorkflowRunStore(runtime.root)
    try:
        run = store.read(WorkflowRunId(state.pending_workflow_run_id))
    except (FileNotFoundError, ValueError) as exc:
        stdout.write(f"resume failed: {exc}\n")
        state.expecting_user_input_response = False
        state.pending_workflow_run_id = None
        return

    catalog = runtime.asset_management.catalog()
    entry = catalog.lookup(run.workflow_ref, expected_type="workflow")
    if entry is None:
        stdout.write(f"resume failed: workflow {run.workflow_ref!r} not in catalog.\n")
        state.expecting_user_input_response = False
        return
    try:
        workflow = load_workflow(Path(entry.path))
    except WorkflowError as exc:
        message = getattr(exc, "caller_message", None) or str(exc)
        stdout.write(f"resume failed: {message}\n")
        state.expecting_user_input_response = False
        return

    status = await execute_workflow_with_renderer(
        state=state,
        stdout=stdout,
        store=store,
        workflow=workflow,
        run=run,
        resume_response={"response": response_text},
    )
    state.expecting_user_input_response = status == WorkflowRunStatus.WAITING_FOR_INPUT


async def _open_new_session(
    state: ChatSessionState,
    *,
    stdout: TextIO,
    agent_ref: str,
) -> None:
    """`/new` impl — create a fresh session under the same agent + model.

    The old session is preserved on disk for `kolsch chat <agent>
    --session <old_id>` resume.
    """
    session = await state.runtime.store.create_session(
        str(state.config.agent_id),
        name=f"chat:{agent_ref}",
    )
    threads = await state.runtime.store.list_threads(session.session_id)
    state.session_id = session.session_id
    state.thread_id = threads[0].thread_id
    # ``/new`` is a context shift — a /skill ref queued in the prior
    # session must not silently force-load on the first message of the
    # fresh one. Drop pending refs alongside the session rebind.
    state.pending_force_skill.clear()
    _print_banner(
        stdout,
        agent_name=state.config.name,
        model_label=_format_model_label(state.config.model),
        session_id=str(session.session_id),
    )


async def _switch_session(
    state: ChatSessionState,
    *,
    target_session_id: SessionId,
    stdout: TextIO,
) -> None:
    """`/session <id>` impl — load + replay an existing session.

    Uses the same Renderer as live turns so the rendered output is
    byte-identical to what the original chat showed (modulo
    timestamps / rule IDs).
    """
    threads = await state.runtime.store.list_threads(target_session_id)
    if not threads:
        stdout.write(
            f"/session: session {target_session_id!r} has no threads; "
            "staying in current session.\n",
        )
        return
    state.session_id = target_session_id
    state.thread_id = threads[0].thread_id
    # Same logic as /new — switching sessions is a context shift; a
    # queued /skill ref from the prior conversation has no place in the
    # one we're about to load.
    state.pending_force_skill.clear()
    _print_banner(
        stdout,
        agent_name=state.config.name,
        model_label=_format_model_label(state.config.model),
        session_id=str(target_session_id),
    )
    await _replay_session(
        runtime=state.runtime,
        thread_id=state.thread_id,
        stdout=stdout,
        agent_name=state.config.name,
    )


async def _replay_session(
    *,
    runtime: PlatformRuntime,
    thread_id: ThreadId,
    stdout: TextIO,
    agent_name: str,
) -> None:
    """Pipe a thread's events through a fresh Renderer.

    Single render code path; the renderer's pure-consumer property
    is what makes resume output identical to live output.
    """
    renderer = Renderer(stdout=stdout, agent_name=agent_name)
    await renderer.render(runtime.store.read_path_to_leaf(thread_id))


def _refresh_slash_ctx(
    prior: SlashContext,
    state: ChatSessionState,
) -> SlashContext:
    """Rebuild SlashContext with the post-transition config.

    SlashContext is frozen; lifecycle commands that change the
    agent_config (`/new` doesn't, `/model` does, `/session` doesn't
    change config) need a fresh context so subsequent `/tools` runs
    against the right agent's tool list.
    """
    from dataclasses import replace  # noqa: PLC0415

    return replace(prior, agent_config=state.config)


def _parse_startup_model_override(
    raw: str,
    runtime: PlatformRuntime,
    root: Path,
) -> Any:  # noqa: ANN401 — concrete ModelSpec; Any sidesteps a TYPE_CHECKING import cycle
    """Parse the `--model <ref>` CLI override into a ModelSpec.

    Same alias-resolution path as `/model` so the two surfaces
    behave consistently.
    """
    from assets.assembler import _normalize_model_value  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    _ = runtime  # reserved for runtime-driven validation in a later patch
    aliases = _load_model_aliases(root)
    normalized = _normalize_model_value(raw, aliases)
    if not isinstance(normalized, dict):
        msg = f"unexpected normalization shape: {type(normalized).__name__}"
        raise TypeError(msg)
    return ModelSpec(**normalized)


def _load_model_aliases(root: Path) -> dict[str, str]:
    """Read `platform.yaml`'s `model_aliases` section.

    Returns an empty dict when the section is absent — common for
    fresh workspaces. The `/model` slash command and the `--model`
    startup flag both rely on this for alias resolution.
    """
    from _yamlio import YAMLError, load  # noqa: PLC0415

    yaml_path = root / "platform.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        payload = load(yaml_path.read_text(encoding="utf-8")) or {}
    except YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("model_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


_RECENT_SESSIONS_LIMIT = 10
"""How many recent session ids the /session completer surfaces.
Cap at 10 so the popup stays scannable; users who need older
sessions can paste the id or scroll `kolsch sessions list`."""


async def _snapshot_recent_session_ids(
    runtime: PlatformRuntime,
) -> list[tuple[str, str]]:
    """Snapshot the most recent session ids at chat startup.

    The completer is sync; capture once. `/new` mid-chat
    re-snapshotting isn't wired yet (the user would have to restart
    chat to see a just-created session in the completer popup) —
    small UX gap; the handler-side `/session <id>` accepts any id
    the user types, so it doesn't block correctness.

    Returns a list of `(session_id, display_label)` tuples. The
    label shows the session name + start date so the user can pick
    the right one without leaving chat.
    """
    sessions = await runtime.store.list_sessions(limit=_RECENT_SESSIONS_LIMIT)
    return [
        (
            str(s.session_id),
            (
                f"{s.name or '(unnamed)'} • "
                f"{s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '?'}"
            ),
        )
        for s in sessions
    ]


def _placeholder_session_id() -> SessionId:
    """Synthesize an empty SessionId for in-test isolation paths.

    Production `cmd_chat` always passes a real session_id; tests of
    `_run_input_loop` in isolation use this fallback so the
    dataclass invariant holds.
    """
    from uuid import uuid4  # noqa: PLC0415

    from schemas import SessionId  # noqa: PLC0415

    return SessionId(f"ses_{uuid4()}")


def _install_modal_callback(
    runtime: PlatformRuntime,
    modal: ApprovalModal,
    *,
    yaml_path: Path,
) -> None:
    """Register the modal as the authz provider's prompt callback.

    Only the chat installs the callback; non-chat callers
    (`kolsch run`, server, tests) leave it `None` and the provider's
    `_resolve_prompt` collapses `prompt` to deny.

    Includes the `on_rule_added` hook that atomically rewrites
    `platform.yaml` so a rule added via the modal survives chat
    exit. The hook uses `cli.permissions._write_back` — the same
    helper `kolsch permissions add` uses for atomic rewrites; one
    persistence path, not one-per-callsite.

    Silently no-ops if the runtime's authz isn't the rule-based
    variant (a future custom authz variant might not support the
    callback registration API; the modal stays installable without
    crashing — the per-call `prompt` would just collapse to deny,
    same as a non-chat caller).
    """
    authz = runtime.authz
    if not isinstance(authz, RuleBasedAuthorizationProvider):
        return

    def _on_rule_added(rules: list[Any]) -> None:
        # The hook receives a list[Rule] but is typed list[Any] to
        # keep this module free of an authz Rule import (it's a
        # runtime-only callback, not a typed seam).
        _persist_authz_rules(yaml_path, rules)

    authz.register_prompt_callback(modal, on_rule_added=_on_rule_added)


def _clear_modal_callback(runtime: PlatformRuntime) -> None:
    """Tear down the modal registration on chat exit.

    Load-bearing for tests: a subsequent in-process command against
    the same runtime instance must not see the modal. Without this
    clear, the next `runtime.authz.allow(...)` from a non-chat
    caller would invoke the modal — which would try to read stdin
    (no TTY in tests) and hang or worse.
    """
    authz = runtime.authz
    if isinstance(authz, RuleBasedAuthorizationProvider):
        authz.clear_prompt_callback()


def _print_banner(
    stdout: TextIO,
    *,
    agent_name: str,
    model_label: str,
    session_id: str,
) -> None:
    """Print the title banner.

    Format: `kolsch chat • <agent_name> • <model_label> • <session_id>`.
    Plain stdout — not pinned, not full-screen; scrolls away with
    the conversation per the architectural commitment.
    """
    stdout.write(
        f"kolsch chat • {agent_name} • {model_label} • {session_id}\n",
    )
    stdout.write("\n")
    stdout.flush()


def _format_model_label(model: object) -> str:
    """Render the agent's `model:` field as the banner string.

    Pydantic `ModelSpec` objects carry `provider` + `model`
    attributes; emit `<provider>/<model>`. Strings (aliases) pass
    through. Anything else stringifies to its `repr`.
    """
    provider = getattr(model, "provider", None)
    model_id = getattr(model, "model", None)
    if isinstance(provider, str) and isinstance(model_id, str):
        return f"{provider}/{model_id}"
    if isinstance(model, str):
        return model
    return repr(model)
