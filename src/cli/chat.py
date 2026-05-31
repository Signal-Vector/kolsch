"""``kolsch chat`` — Phase 2.1 streaming REPL.

Step 24a scaffolded the input loop + the no-full-screen guard.
Step 24b added the slash-command framework. Step 24c (this layer)
replaces the echo stub with **real engine integration**: each user
submission becomes an ``ExecutionContext`` whose ``execute_stream``
events feed the :class:`cli._renderer.Renderer`. The renderer is a
pure consumer (CHAT-R9) — every visible block traces back to one
event the engine emitted.

Architectural commitment (patch §2.4): the chat MUST stay in
prompt_toolkit's non-full-screen idiom. Native terminal scrolling
and text selection only work on output written via normal stdout;
full-screen / alternate-screen mode replaces the scrollback
buffer and selection breaks across every terminal emulator.
TC-CHAT-18 enforces this architecturally — see
``tests/component/cli/test_chat_input.py``.

Key bindings (patch §2.5):

- **Enter** submits the current input.
- **Ctrl-J** inserts a newline. (Universal — every terminal sends
  ``\\n`` for Ctrl-J. Shift-Enter on iTerm2 / kitty / Alacritty
  *also* sends Ctrl-J under default settings, so this single
  binding covers the patch's "primary Shift-Enter" requirement
  on the terminals that distinguish it.)
- **Alt-Enter** (Esc-Enter sequence) inserts a newline. Universal
  fallback for terminals that don't send a distinct Shift-Enter
  (macOS Terminal.app, stock gnome-terminal). Documented in the
  footer hint.
- **Shift-Enter**: prompt_toolkit 3.0.x doesn't expose a portable
  ``s-enter`` key token, but every terminal that sends a distinct
  Shift-Enter sequence (iTerm2, kitty, Alacritty in their
  defaults) routes it through Ctrl-J anyway, so the Ctrl-J
  binding above already covers it. Terminals that collapse
  Shift-Enter to plain Enter (macOS Terminal.app,
  stock gnome-terminal) submit — users on those terminals use
  Alt-Enter or Ctrl-J for newline, both documented in the footer
  hint.
- Default prompt_toolkit editing keys cover everything else in
  the §2.5 table (cursor motion, history, kill/yank).

Ctrl-D raises ``EOFError`` and exits the loop cleanly (returns
``0``). Ctrl-C raises ``KeyboardInterrupt`` and clears the
current input without exiting (a hint nudges the user toward
Ctrl-D / ``/exit`` in later steps; for 24a the input simply
clears).

Chat is interactive-only; the user's submitted line lands in
the scrollback via prompt_toolkit's terminal echo. Non-TTY
usage is not supported (use ``kolsch run`` for scripted
invocation).
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from cli._modal import ApprovalModal
from cli._renderer import Renderer
from cli._slash import (
    ChatSessionState,
    ControlSignal,
    SlashCompleter,
    SlashContext,
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
# Defaults — patch §1.1
# ---------------------------------------------------------------------------


_DEFAULT_AGENT_REF = "kolsch/agents/helper"
"""Fallback when ``<agent_ref>`` is omitted and
``platform.yaml``'s ``defaults.starter_agent`` is unset.
Phase 2.1 patch §1.1."""


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

    Phase 2.1 patch §1.2 startup flow:

    1. Resolve the workspace via Phase 1.2's ``resolve_workspace``
       (already done by the dispatcher in ``cli.main.main`` before
       ``cmd_chat`` is called; ``args.root`` is the resolved path).
    2. Bootstrap the runtime.
    3. Resolve ``<agent_ref>`` and build the agent config.
    4. Apply ``--model`` override if present (recorded; full
       per-spec normalisation lands in 24d).
    5. Create a fresh session named ``chat:<agent_ref>``. Resume
       support (``--session``) lands in 24e.
    6. Print the title banner; enter the input loop.

    For 24a the input-loop body is a stub — each submitted line
    echoes as ``you said: <text>``. The renderer replaces this in
    24c.
    """
    stdout = out if out is not None else sys.stdout
    _ = err  # reserved for stderr-bound diagnostics in later steps

    agent_ref = args.agent_ref or _DEFAULT_AGENT_REF
    runtime = await bootstrap_runtime(root=args.root)
    config = await runtime.build_resolved_config(agent_ref)

    model_label = _format_model_label(config.model)
    if args.model:
        # 24e: --model startup override resolves through the same
        # ModelSpec parser /model uses; on parse failure we exit
        # 1 with a clear message rather than entering the chat
        # with a bogus model. Patch §1.1 + §1.3 row "Invalid
        # invocation".
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

    # Phase 2.1 / 24e — --session loads an existing session and
    # replays its events through the same Renderer (patch §6.2).
    # Without --session, create a fresh session.
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

    # Phase 2.1 / 24e — model_aliases come from platform.yaml;
    # recent_session_ids come from the store. Both feed the
    # SlashCompleter's dynamic-suggestion paths per patch §5.3.
    model_aliases = _load_model_aliases(args.root)
    recent_session_ids = await _snapshot_recent_session_ids(runtime)
    prompt_session = _build_prompt_session(
        input=prompt_input,
        output=prompt_output,
        model_aliases=model_aliases,
        recent_session_ids=recent_session_ids,
    )
    # threads[0] convention — Phase 1 sessions are single-thread;
    # multi-thread support lands when the Workflow Engine arrives.
    threads = await runtime.store.list_threads(session.session_id)
    if not threads:
        msg = f"session {session.session_id} has no threads"
        raise RuntimeError(msg)
    thread_id = threads[0].thread_id

    # 24e — replay the loaded session's events before entering the
    # input loop. The replay uses the SAME Renderer as live
    # exchanges (patch §6.2 "render-output byte-identical"),
    # which is what the 24c renderer's pure-consumer property
    # makes possible.
    if getattr(args, "session", None):
        await _replay_session(
            runtime=runtime,
            thread_id=thread_id,
            stdout=stdout,
            agent_name=config.name,
        )

    # Phase 2.1 / 24d — register the approval modal as the
    # chat-surface prompt callback. Pair the registration with a
    # finally so a subsequent in-process command (test suites,
    # the future Python embedding API) never sees a leaked
    # modal — which would either hang on stdin or, worse, allow
    # silently. The clear is unconditional regardless of how the
    # loop exited.
    modal = ApprovalModal(stdout=stdout)
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
) -> PromptSession[str]:
    """Construct the prompt_toolkit session with the patch §2.5 bindings.

    ``input`` / ``output`` default to ``None`` for production
    (prompt_toolkit auto-detects stdin/stdout); tests inject a
    ``create_pipe_input()`` + ``DummyOutput()`` pair so the input
    loop is driven by feeding key sequences from Python code.

    Patch §2.4: ``full_screen`` is NEVER set. The architectural
    guard test (TC-CHAT-18) inspects this module's source and
    fails loudly if a future change introduces full-screen mode.

    24e: ``model_aliases`` drives the ``/model`` dynamic
    completer suggestions per patch §5.3 row "/model".
    """
    bindings = _build_key_bindings()
    # The completer + bottom_toolbar both read from the slash
    # registry (single source of truth — patch §5.3 + §5.4).
    # 24e adds /model dynamic suggestions via a closure over the
    # aliases dict so the completer doesn't import bootstrap-
    # adjacent modules.
    aliases_snapshot = dict(model_aliases or {})
    sessions_snapshot = list(recent_session_ids or [])
    completer = SlashCompleter(
        model_alias_provider=(lambda: aliases_snapshot) if aliases_snapshot else None,
        session_id_provider=(
            (lambda: sessions_snapshot) if sessions_snapshot else None
        ),
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
    """The patch §2.5 binding table.

    ``multiline=True`` inverts prompt_toolkit's default Enter
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
        """Ctrl-J inserts a literal newline.

        Universal because every terminal sends ``\\n`` for Ctrl-J.
        On iTerm2 / kitty / Alacritty with default settings,
        Shift-Enter is delivered as Ctrl-J — this binding covers
        the patch's "primary Shift-Enter" requirement on those
        terminals.
        """
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _newline_alt_enter(event: Any) -> None:  # noqa: ANN401
        """Alt-Enter (Esc-Enter) inserts a newline — universal fallback.

        Documented in the patch §2.5 as the cross-terminal fallback
        for macOS Terminal.app and gnome-terminal which don't
        distinguish Shift-Enter from plain Enter. Every terminal
        delivers the Esc-Enter sequence as a distinct two-key
        event, so this binding works everywhere.
        """
        event.current_buffer.insert_text("\n")

    return kb


async def _run_input_loop(  # noqa: PLR0913 — loop owns all the chat state; bundling further hurts readability
    prompt_session: PromptSession[str],
    *,
    stdout: TextIO,
    runtime: PlatformRuntime,
    config: ResolvedAgentConfig,
    thread_id: ThreadId,
    session_id: SessionId | None = None,
    agent_ref: str | None = None,
    model_aliases: dict[str, str] | None = None,
) -> int:
    """Drive the prompt_session until EOF, ``/exit``, or an unrecoverable error.

    Submitted text starting with ``/`` dispatches as a slash
    command; anything else becomes an :class:`ExecutionContext`
    fed to the engine via :meth:`AgentEngine.execute_stream` and
    rendered through a fresh :class:`Renderer` per exchange
    (patch §3 — "the renderer's only state is the current
    exchange's block list").

    24e adds three lifecycle behaviours per patch §5 + §6:

    - ``/new`` → loop spins up a new session under the same
      agent, clears the visible scrollback, reprints the banner.
    - ``/session <id>`` → loop loads the named session and
      replays its events through the SAME renderer (no
      replay-specific code path — patch §6.2).
    - Ctrl-C during the renderer's await → signal the engine's
      typed cancellation hook, drain the remaining events
      (the engine emits ``thread.exchange_failed`` with cause
      ``engine.cancelled``), then return to the prompt
      (patch §6.1).

    Ctrl-D / EOF returns exit code 0. Ctrl-C at the prompt
    clears the current input without exiting (the hint-line
    advice).
    """
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session_id or _placeholder_session_id(),
        thread_id=thread_id,
        model_aliases=dict(model_aliases or {}),
    )
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
        ctx = ExecutionContext(
            thread_id=state.thread_id,
            trigger={"kind": "user_message", "content": text},
            cancellation=cancel,
        )
        renderer = Renderer(stdout=stdout, agent_name=state.config.name)
        event_stream = state.runtime.engine.execute_stream(state.config, ctx)
        try:
            await renderer.render(event_stream)
        except KeyboardInterrupt:
            # Patch §6.1 — Ctrl-C during streaming: signal the
            # engine's typed cancellation hook + drain the
            # remaining events (the engine emits
            # thread.exchange_failed with cause engine.cancelled)
            # so the JSONL stays honest. The renderer's
            # exchange_failed branch surfaces the cancel as a
            # yellow system notice.
            cancel.cancel()
            stdout.write("\n[interrupted; waiting for engine to cancel...]\n")
            stdout.flush()
            # Best-effort drain so the engine emits its
            # thread.exchange_failed before the loop returns to
            # the prompt. A second KeyboardInterrupt (or any
            # error) is swallowed — the engine's own teardown
            # is the source of truth for the cancellation event.
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(KeyboardInterrupt, Exception):
                await renderer.render(event_stream)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_new_session(
    state: ChatSessionState,
    *,
    stdout: TextIO,
    agent_ref: str,
) -> None:
    """``/new`` impl — create a fresh session under the same agent + model.

    Patch §5 row 3 — old session is preserved on disk for
    ``kolsch chat <agent> --session <old_id>`` resume.
    """
    session = await state.runtime.store.create_session(
        str(state.config.agent_id),
        name=f"chat:{agent_ref}",
    )
    threads = await state.runtime.store.list_threads(session.session_id)
    state.session_id = session.session_id
    state.thread_id = threads[0].thread_id
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
    """``/session <id>`` impl — load + replay an existing session.

    Patch §6.2 — uses the SAME Renderer as live exchanges so the
    rendered output is byte-identical to what the original chat
    showed (modulo timestamps / rule IDs).
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

    Patch §6.2 — single render code path; the renderer's
    pure-consumer property (CHAT-R9 + 24c) is what makes resume
    output identical to live output.
    """
    renderer = Renderer(stdout=stdout, agent_name=agent_name)
    await renderer.render(runtime.store.read_path_to_leaf(thread_id))


def _refresh_slash_ctx(
    prior: SlashContext,
    state: ChatSessionState,
) -> SlashContext:
    """Rebuild SlashContext with the post-transition config.

    SlashContext is frozen; lifecycle commands that change the
    agent_config (``/new`` doesn't, ``/model`` does, ``/session``
    doesn't change config) need a fresh context so subsequent
    ``/tools`` runs against the right agent's tool list.
    """
    from dataclasses import replace  # noqa: PLC0415

    return replace(prior, agent_config=state.config)


def _parse_startup_model_override(
    raw: str,
    runtime: PlatformRuntime,
    root: Path,
) -> Any:  # noqa: ANN401 — concrete ModelSpec; Any sidesteps a TYPE_CHECKING import cycle
    """Parse the ``--model <ref>`` CLI override into a ModelSpec.

    Same alias-resolution path as ``/model`` so the two surfaces
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
    """Read ``platform.yaml``'s ``model_aliases`` section.

    Returns an empty dict when the section is absent — common for
    fresh workspaces. The ``/model`` slash command and the
    ``--model`` startup flag both rely on this for alias
    resolution.
    """
    import yaml  # noqa: PLC0415

    yaml_path = root / "platform.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
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
sessions can paste the id or scroll ``kolsch sessions list``."""


async def _snapshot_recent_session_ids(
    runtime: PlatformRuntime,
) -> list[tuple[str, str]]:
    """Snapshot the most recent session ids at chat startup.

    The completer is sync; we capture once. ``/new`` mid-chat
    re-snapshotting isn't wired yet (the user would have to
    restart chat to see a just-created session in the completer
    popup) — small UX gap noted as a polish-phase item; the
    handler-side ``/session <id>`` accepts any id the user
    types, so it doesn't block correctness.

    Returns a list of ``(session_id, display_label)`` tuples.
    The label shows the session name + start date so the user
    can pick the right one without leaving chat.
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
    """Synthesize an empty SessionId for the rare in-test path that
    bypasses cmd_chat's session-creation logic. Production cmd_chat
    always passes a real session_id; tests of ``_run_input_loop``
    in isolation use this fallback so the dataclass invariant
    holds.
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

    Phase 2.1 / 24d: only the chat installs the callback; non-chat
    callers (``kolsch run``, server, tests) leave it None and the
    provider's ``_resolve_prompt`` collapses ``prompt`` to deny
    per Phase 2.0 §2.2.

    Includes the ``on_rule_added`` hook that atomically rewrites
    ``platform.yaml`` so a rule added via the modal survives chat
    exit. The hook uses ``cli.permissions._write_back`` — the
    SAME helper ``kolsch permissions add`` uses for atomic
    rewrites; one persistence path, not one-per-callsite.

    Silently no-ops if the runtime's authz isn't the rule-based
    variant (a future custom authz variant might not support the
    callback registration API; the modal stays installable
    without crashing — the per-call ``prompt`` would just collapse
    to deny, same as a non-chat caller).
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

    Load-bearing for tests: a subsequent in-process command
    against the same runtime instance MUST NOT see the modal.
    Without this clear, the next ``runtime.authz.allow(...)`` from
    a non-chat caller would invoke the modal — which would try to
    read stdin (no TTY in tests) and hang or worse.
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
    """Print the title banner per patch §2.1 sketch.

    Format: ``kolsch chat • <agent_name> • <model_label> • <session_id>``.
    Plain stdout — not pinned, not full-screen; scrolls away with
    the conversation per the §2.4 architectural commitment.
    """
    stdout.write(
        f"kolsch chat • {agent_name} • {model_label} • {session_id}\n",
    )
    stdout.write("\n")
    stdout.flush()


def _format_model_label(model: object) -> str:
    """Render the agent's ``model:`` field as the banner string.

    Pydantic ``ModelSpec`` objects carry ``provider`` + ``model``
    attributes; emit ``<provider>/<model>``. Strings (aliases) pass
    through. Anything else stringifies to its ``repr``.
    """
    provider = getattr(model, "provider", None)
    model_id = getattr(model, "model", None)
    if isinstance(provider, str) and isinstance(model_id, str):
        return f"{provider}/{model_id}"
    if isinstance(model, str):
        return model
    return repr(model)
