"""Textual bindings for the ``ChatApp``.

bug-038 restored Textual's framework-native text-copy convention:

- ``escape`` — cancel the active turn (routes into the
  per-exchange ``CancelToken``; see ``cancel.py``). Primary key.
- ``ctrl+g`` — cancel the active turn (secondary; readline-native
  ``keyboard-quit``). Added in bug-038 as the muscle-memory
  replacement for the legacy ``Ctrl+C`` cancel.
- ``ctrl+d`` — quit the app when the composer is empty; a no-op
  otherwise (matches EOF semantics from the legacy REPL).
- ``ctrl+c`` / ``super+c`` (``Cmd+C``) — copy the current text
  selection to the OS clipboard. NOT bound here: Textual's own
  ``Screen`` class ships a default
  ``Binding("ctrl+c,super+c", "screen.copy_text", ...)`` at
  ``site-packages/textual/screen.py:271``. Before bug-038 the
  app-level ``ctrl+c`` cancel binding shadowed that default so
  neither ``Ctrl+C`` nor ``Cmd+C`` ever reached the copy action;
  removing the override lets the inherited Screen binding fire
  and restores the convention every modern TUI editor uses
  (helix, zellij, kitty configurations, etc.). ``super+c`` is
  best-effort — it only reaches the app when the terminal
  forwards ``Cmd+C`` (Ghostty / Kitty with the keyboard protocol
  enabled do; Terminal.app / iTerm2 with defaults handle
  ``Cmd+C`` themselves, which also copies).
- ``c`` — collapse the focused tool block (owned by the widget's
  own ``BINDINGS``; listed here for spec parity).

Enter (submit) and newline keys (Alt+Enter / Ctrl+J / Shift+Enter)
live on the ``Composer`` widget's ``_on_key`` handler; they are
not global bindings so the composer can intercept them without
competing with app-level bindings.
"""

from __future__ import annotations

from textual.binding import Binding


CHAT_BINDINGS = [
    Binding("escape", "cancel_exchange", "Cancel turn", show=False),
    Binding("ctrl+g", "cancel_exchange", "Cancel turn", show=False),
    Binding("ctrl+d", "quit_if_idle", "Quit", show=False),
    # bug-038: ctrl+c + super+c (Cmd+C) copy is handled by Textual's
    # Screen default at site-packages/textual/screen.py:271
    # (screen.copy_text). The legacy app-level ctrl+c cancel override
    # and the ctrl+shift+c workaround were both removed so the
    # inherited binding reaches the user.
    # Transcript scroll — keeps the composer's own keybindings free.
    # In-app scroll doesn't affect terminal scrollback (Textual owns
    # the inline region); these keys let the user navigate the
    # transcript widget's internal buffer without the mouse.
    Binding("pageup", "scroll_transcript('up')", "Page up", show=False),
    Binding("pagedown", "scroll_transcript('down')", "Page down", show=False),
    Binding("home", "scroll_transcript('home')", "Top", show=False),
    Binding("end", "scroll_transcript('end')", "Bottom", show=False),
]


__all__ = ["CHAT_BINDINGS"]
