"""``kolsch chat`` — Phase 2.1 streaming REPL.

Step 24a scaffold: the command exists, the input loop works
correctly, and the no-full-screen architectural guard is in
place. No engine integration, no slash commands, no renderer —
those land in 24b, 24c, 24d.

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
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from cli._slash import (
    ControlSignal,
    SlashCompleter,
    SlashContext,
    dispatch_slash,
    footer_hint_for_buffer,
    is_slash_command,
)
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output


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
        # 24a: record the override for the banner; full ModelSpec
        # input-shape parsing + Layer 1 alias resolution lands in
        # 24d alongside the `/model` slash command.
        model_label = args.model

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

    prompt_session = _build_prompt_session(
        input=prompt_input,
        output=prompt_output,
    )
    return await _run_input_loop(prompt_session, stdout=stdout)


# ---------------------------------------------------------------------------
# Input loop — separable for tests
# ---------------------------------------------------------------------------


def _build_prompt_session(
    *,
    input: Input | None = None,  # noqa: A002 — prompt_toolkit's own kwarg name
    output: Output | None = None,
) -> PromptSession[str]:
    """Construct the prompt_toolkit session with the patch §2.5 bindings.

    ``input`` / ``output`` default to ``None`` for production
    (prompt_toolkit auto-detects stdin/stdout); tests inject a
    ``create_pipe_input()`` + ``DummyOutput()`` pair so the input
    loop is driven by feeding key sequences from Python code.

    Patch §2.4: ``full_screen`` is NEVER set. The architectural
    guard test (TC-CHAT-18) inspects this module's source and
    fails loudly if a future change introduces full-screen mode.
    """
    bindings = _build_key_bindings()
    # The completer + bottom_toolbar both read from the slash
    # registry (single source of truth — patch §5.3 + §5.4).
    completer = SlashCompleter()

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


async def _run_input_loop(prompt_session: PromptSession[str], *, stdout: TextIO) -> int:
    """Drive the prompt_session in a loop until EOF or ``/exit``.

    Submitted text starting with ``/`` is dispatched as a slash
    command (24b — see ``cli._slash``). Anything else echoes as
    ``you said: <text>`` for now; the renderer + engine
    integration replace that stub in 24c.

    Ctrl-D / EOF returns exit code 0. Ctrl-C clears the current
    input and continues (24c wires it to stream cancellation once
    streaming exists).
    """
    slash_ctx = SlashContext(stdout=stdout)
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
            if signal is ControlSignal.EXIT:
                return 0
            # CONTINUE / NEW_SESSION / RELOAD all return to the
            # prompt; NEW_SESSION + RELOAD get richer behaviour
            # in 24c / 24e.
            continue
        stdout.write(f"you said: {text}\n")
        stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
