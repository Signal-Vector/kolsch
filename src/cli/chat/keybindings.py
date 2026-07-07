"""Textual bindings for the ``ChatApp``.

Mirrors the legacy REPL's key mapping, plus a few Textual-native
affordances the legacy prompt_toolkit surface couldn't do:

- ``escape`` — cancel the active turn (routes into the
  per-exchange ``CancelToken``; see ``cancel.py``).
- ``ctrl+c`` — cancel the active turn.
- ``ctrl+d`` — quit the app when the composer is empty; a no-op
  otherwise (matches EOF semantics from the legacy REPL).
- ``ctrl+shift+c`` — copy the current text selection (drag to
  select in the transcript) to the OS clipboard via
  ``App.copy_to_clipboard``. Distinct from ``ctrl+c`` — the
  legacy Ctrl+C stays reserved for cancellation.
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
    Binding("ctrl+c", "cancel_exchange", "Cancel turn", show=False),
    Binding("ctrl+d", "quit_if_idle", "Quit", show=False),
    Binding("ctrl+shift+c", "screen.copy_text", "Copy selected text", show=False),
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
