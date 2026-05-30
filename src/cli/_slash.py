"""Slash-command framework for ``kolsch chat`` — Phase 2.1 Step 24b.

Single source of truth for command metadata is :data:`COMMAND_REGISTRY`.
The registry drives:

- ``/help`` output (no parallel listing maintained).
- The :class:`SlashCompleter` popup that prompt_toolkit shows when
  the user types ``/``.
- The dynamic footer hint shown in the bottom toolbar while the
  user is typing a slash command.

Adding a future slash command means adding ONE entry to the
registry — the three consumers above pick it up automatically.

This module is a peer of ``src/cli/chat.py``. It's split out
because at ~250 lines (this file) + ~290 lines (chat.py from
24a) + the wiring 24b adds in chat.py, keeping the slash
framework here keeps each module under the readability ceiling
the build doc calls out.

Patch sections:

- §5 — commands table.
- §5.2 — ``/thinking``'s sub-form options.
- §5.3 — completer popup contract.
- §5.4 — footer-hint contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TextIO

from prompt_toolkit.completion import Completer, Completion

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document


# ---------------------------------------------------------------------------
# Control signals returned by slash handlers
# ---------------------------------------------------------------------------


class ControlSignal(StrEnum):
    """What the input loop should do after a slash command returns.

    The dispatch contract: handlers return one of these; the input
    loop branches accordingly. Engine integration (the
    ``user.message`` path) lands in 24c — that's why ``RELOAD``
    exists as a documented value here but isn't returned by any
    24b handler.
    """

    CONTINUE = "continue"
    """Return to the input prompt; keep the chat going."""

    EXIT = "exit"
    """Tear down the chat and exit cleanly with code 0."""

    NEW_SESSION = "new_session"
    """Reserved for ``/new`` (24c+) — close the current session,
    open a fresh one under the same agent + model."""

    RELOAD = "reload"
    """Reserved for ``/session <id>`` (24e+) — load a different
    session's history into the scrollback."""


# ---------------------------------------------------------------------------
# Handler context — what every handler gets at invocation time
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlashContext:
    """The collaborators a slash-command handler may touch.

    Kept narrow on purpose — handlers are chat-local operations
    per patch §5; they MUST NOT reach into runtime state or
    emit events except through the documented channels. The
    24b stub set (``/help``, ``/exit``, ``/clear``) only touches
    ``stdout``; richer fields land as later steps grow.
    """

    stdout: TextIO
    """Where command output writes."""


# ---------------------------------------------------------------------------
# Command metadata — the registry entry shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlashCommand:
    """One row of the registry.

    ``aliases`` lets ``/exit`` and ``/quit`` share a handler without
    two registry entries fighting for the same description in
    ``/help``. ``options`` populates the §5.3 sub-form completer
    popup (only ``/thinking`` uses it in MVP).
    """

    name: str
    """Canonical command name, with the leading slash
    (``"/help"``, ``"/exit"``)."""

    description: str
    """One-line description shown in ``/help`` AND in the completer
    popup's ``display_meta`` column."""

    usage: str
    """Footer-hint shown while the user is mid-typing this
    command. Patch §5.4."""

    handler: Callable[..., Awaitable[ControlSignal]] | None = None
    """Async callable ``(arg_text, ctx) -> ControlSignal``. ``None``
    means "registered but not implemented yet" — the dispatch
    layer turns ``None`` into a polite placeholder message that
    names the step where the handler lands."""

    options: tuple[tuple[str, str], ...] = ()
    """Sub-form options for the completer's ``/cmd <space>``
    popup. Each tuple is ``(option_value, description)``. Empty
    for commands with no discrete sub-options."""

    free_form_arg_hint: str | None = None
    """Placeholder line for the completer's free-form-arg popup
    (e.g., ``/model <type any model ref>``). Mutually exclusive
    with ``options``."""

    aliases: tuple[str, ...] = ()
    """Additional names that resolve to the same handler. Used
    only by ``/quit`` aliasing ``/exit``. Aliases do NOT show up
    in ``/help`` or the completer; they're invisible synonyms."""

    placeholder_step: str | None = None
    """When ``handler is None``, the step where the real handler
    lands (e.g., ``"24c"``, ``"24d"``). Surfaced in the
    placeholder message so the user knows when to expect it."""


# ---------------------------------------------------------------------------
# Handlers — only /help, /exit, /quit, /clear are real in 24b
# ---------------------------------------------------------------------------


async def _handler_help(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/help`` lists all commands; ``/help <command>`` describes one.

    Patch §5 row 1. Reads from :data:`COMMAND_REGISTRY` — no
    parallel listing; adding a future command surfaces in
    ``/help`` automatically.
    """
    arg = arg_text.strip()
    if arg:
        target = arg if arg.startswith("/") else f"/{arg}"
        command = _resolve_command_name(target)
        if command is None:
            ctx.stdout.write(
                f"unknown command: {target}. Type /help for the full list.\n"
            )
            return ControlSignal.CONTINUE
        ctx.stdout.write(f"{command.name:<10}  {command.description}\n")
        ctx.stdout.write(f"            {command.usage}\n")
        return ControlSignal.CONTINUE

    ctx.stdout.write("slash commands:\n")
    for command in _iter_visible_commands():
        ctx.stdout.write(f"  {command.name:<10}  {command.description}\n")
    ctx.stdout.write("\nShift+Enter (or Alt+Enter / Ctrl-J) inserts a newline.\n")
    return ControlSignal.CONTINUE


async def _handler_exit(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/exit`` / ``/quit`` — clean exit, code 0. Patch §5 row 2."""
    _ = arg_text, ctx
    return ControlSignal.EXIT


async def _handler_clear(arg_text: str, ctx: SlashContext) -> ControlSignal:
    """``/clear`` — clear visible scrollback; session + event log untouched.

    Patch §5 row 4 + the §5 spec's load-bearing distinction from
    ``/new``: ``/clear`` is a display-only operation. Subsequent
    ``kolsch sessions events <id>`` against the same session still
    returns every event from before the ``/clear``.

    Implementation: write the ANSI sequence ``\\x1b[H\\x1b[2J\\x1b[3J``
    — cursor home + clear screen + clear scrollback buffer (xterm
    extension supported by every modern terminal we target).
    """
    _ = arg_text
    # Cursor home + erase entire screen + clear scrollback buffer.
    # xterm DEC private mode "3J" is what handles the scrollback;
    # without it some terminals leave the prior output reachable
    # via Page Up. Combined with the "H" home cursor + "2J" screen
    # erase, this produces the §5 "clear visible scrollback"
    # behaviour on macOS Terminal.app, iTerm2, kitty, Alacritty,
    # gnome-terminal.
    ctx.stdout.write("\x1b[H\x1b[2J\x1b[3J")
    ctx.stdout.flush()
    return ControlSignal.CONTINUE


# ---------------------------------------------------------------------------
# COMMAND_REGISTRY — the single source of truth
# ---------------------------------------------------------------------------


COMMAND_REGISTRY: dict[str, SlashCommand] = {
    "/help": SlashCommand(
        name="/help",
        description="show available commands or describe one",
        usage="/help [<command>]   list all commands or print one's hint",
        handler=_handler_help,
    ),
    "/exit": SlashCommand(
        name="/exit",
        description="leave the chat",
        usage="/exit   exit chat (same as Ctrl-D)",
        handler=_handler_exit,
        aliases=("/quit",),
    ),
    "/new": SlashCommand(
        name="/new",
        description="start a fresh session, same agent",
        usage="/new   open a new session under this agent (preserves the old)",
        handler=None,
        placeholder_step="24c",
    ),
    "/clear": SlashCommand(
        name="/clear",
        description="clear visible scrollback (session unchanged)",
        usage="/clear   wipe the screen; session + event log untouched",
        handler=_handler_clear,
    ),
    "/model": SlashCommand(
        name="/model",
        description="switch model for this session",
        usage=("/model <ref>   any ModelSpec input shape (alias or provider/model)"),
        handler=None,
        free_form_arg_hint="<type any model ref>",
        placeholder_step="24d",
    ),
    "/thinking": SlashCommand(
        name="/thinking",
        description="control extended-thinking display and budget",
        usage=(
            "/thinking [on|off|budget <n>|last]   extended-thinking display + budget"
        ),
        handler=None,
        options=(
            ("on", "show thinking inline"),
            ("off", "hide thinking (default)"),
            ("budget <n>", "set max thinking tokens for next exchange"),
            ("last", "show thinking from the most recent reply"),
        ),
        placeholder_step="24c",
    ),
    "/tools": SlashCommand(
        name="/tools",
        description="list tools available to this agent",
        usage="/tools   list this agent's tools + the rules governing each",
        handler=None,
        placeholder_step="24c",
    ),
    "/session": SlashCommand(
        name="/session",
        description="show or switch current session",
        usage="/session [<id>]   print the current session id, or switch",
        handler=None,
        free_form_arg_hint="<recent session id>",
        placeholder_step="24e",
    ),
}
"""Eight visible commands; ``/exit`` carries ``/quit`` as an alias.
Patch §5."""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def is_slash_command(text: str) -> bool:
    """True iff ``text`` is a slash-command invocation.

    The input loop calls this before splitting between "send to
    engine" and "dispatch as slash command". Leading whitespace is
    stripped — a user typing ``  /help`` still gets the command,
    not a user message.
    """
    return text.lstrip().startswith("/")


async def dispatch_slash(text: str, ctx: SlashContext) -> ControlSignal:
    """Parse ``text``, look up the command, run its handler.

    Unknown commands print a single-line error and return
    ``CONTINUE``. Placeholder commands (``handler is None``) print
    a "not yet implemented (Step <NN>)" message and return
    ``CONTINUE``. Real handlers receive ``(arg_text, ctx)``.
    """
    stripped = text.lstrip()
    head, _, tail = stripped.partition(" ")
    arg_text = tail.strip()
    command = _resolve_command_name(head)
    if command is None:
        ctx.stdout.write(f"unknown command: {head}. Type /help for the full list.\n")
        return ControlSignal.CONTINUE
    if command.handler is None:
        step = command.placeholder_step or "a later step"
        ctx.stdout.write(
            f"{command.name} is not yet implemented (lands in Step {step}).\n"
        )
        return ControlSignal.CONTINUE
    return await command.handler(arg_text, ctx)


def _resolve_command_name(head: str) -> SlashCommand | None:
    """Look up a command by name OR alias.

    Walks ``COMMAND_REGISTRY`` once (8 entries — cheap). Returns
    the canonical entry when ``head`` matches its ``name`` or any
    of its ``aliases``.
    """
    for command in COMMAND_REGISTRY.values():
        if head == command.name or head in command.aliases:
            return command
    return None


def _iter_visible_commands() -> Iterable[SlashCommand]:
    """``COMMAND_REGISTRY`` in insertion order — aliases stay hidden.

    The registry is a plain dict; Python 3.7+ preserves insertion
    order, so ``/help`` shows commands in the order they were
    registered (patch §5 row order).
    """
    return COMMAND_REGISTRY.values()


# ---------------------------------------------------------------------------
# Completer
# ---------------------------------------------------------------------------


class SlashCompleter(Completer):
    """Drives the prompt_toolkit popup for slash-command completion.

    Patch §5.3:

    - Buffer starts with ``/`` and has no trailing space → narrow
      registry by prefix; popup lists matching commands with their
      one-line descriptions.
    - Buffer is ``/<command> `` (trailing space) and the command
      has discrete ``options`` → popup lists each option with its
      description.
    - Buffer is ``/<command> `` and the command has a
      ``free_form_arg_hint`` → popup shows the hint as a
      non-selectable placeholder line (best-effort: prompt_toolkit
      requires every Completion to be "insertable", so the hint
      is yielded as a no-op completion whose ``text`` is empty).
    - Anything else → no completions (popup hides).
    """

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        _ = complete_event
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        head, sep, tail = text.partition(" ")
        if not sep:
            # No space yet — narrow command list by prefix.
            for command in _iter_visible_commands():
                if command.name.startswith(head):
                    yield Completion(
                        text=command.name[len(head) :],
                        display=command.name,
                        display_meta=command.description,
                    )
            return
        # Trailing space (or characters after it) — sub-form
        # completions, if the command has any.
        resolved = _resolve_command_name(head)
        if resolved is None:
            return
        partial = tail
        if resolved.options:
            for option_value, option_desc in resolved.options:
                if option_value.startswith(partial):
                    yield Completion(
                        text=option_value[len(partial) :],
                        display=option_value,
                        display_meta=option_desc,
                    )
            return
        if resolved.free_form_arg_hint is not None and not partial:
            # A non-completing hint — yields with empty insertion
            # text so the popup just shows the placeholder line
            # for guidance.
            yield Completion(
                text="",
                display=resolved.free_form_arg_hint,
                display_meta="(any value)",
            )


# ---------------------------------------------------------------------------
# Footer hint generator — patch §5.4
# ---------------------------------------------------------------------------


_DEFAULT_FOOTER_HINT = (
    "Shift+Enter (or Alt+Enter / Ctrl-J) for new line • /help • Ctrl-D to exit"
)
"""Idle hint — shown when the input is empty or doesn't start
with ``/``. Patch §5.4 + §2.1."""


def footer_hint_for_buffer(buffer_text: str) -> str:
    """Pure function: buffer text → footer-hint string.

    - Empty buffer or non-slash text → :data:`_DEFAULT_FOOTER_HINT`.
    - ``/`` typed but no recognisable command yet → default hint.
    - ``/<command>`` (possibly with args) → that command's
      ``usage`` line from the registry.
    - ``/<unknown>`` → default hint (the user hasn't typed
      something the registry recognises).

    Patch §5.4 — also drives the bottom_toolbar in
    ``cli.chat._build_prompt_session``.
    """
    text = buffer_text.lstrip()
    if not text or not text.startswith("/"):
        return _DEFAULT_FOOTER_HINT
    head = text.partition(" ")[0]
    command = _resolve_command_name(head)
    if command is None:
        # Partial command name — find the best matching prefix and
        # show its usage so the hint tracks as the user types.
        for candidate in _iter_visible_commands():
            if candidate.name.startswith(head):
                return candidate.usage
        return _DEFAULT_FOOTER_HINT
    return command.usage
