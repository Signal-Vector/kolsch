"""UserChatbox widget — bordered display of the user's turn.

Framework-aligned: subclasses Textual's `Static` and uses the
widget's own `border_title` reactive plus a `DEFAULT_CSS` block
that references the `$user-border` theme variable. The body is
rendered as **literal** text — no markdown, no Rich markup — so
input like backticks or bracket-sequences survives verbatim.

Spec: process/spec-textual-chat-ui.md → widget subsection
"UserChatbox" (see also Coder Prompt step 15.b).
"""

from __future__ import annotations

from datetime import datetime

from textual.widgets import Static


class UserChatbox(Static):
    """A single user turn rendered as a bordered box.

    - Border: round, `$user-border`.
    - Border title: ``You · HH:MM:SS`` (timestamp from the
      originating event's ``ts`` field).
    - Body: literal user text. Backticks, brackets, and any other
      character that Rich markup or Markdown would normally
      interpret are preserved as-is — ``markup=False`` on the
      underlying ``Static``.

    Constructor:
        text: The raw user input string.
        timestamp: The event timestamp used for the border title.

    Design notes:
    - Once mounted, the widget is frozen — it does not react to
      further events. This matches the "mount and freeze"
      lifecycle described in the spec.
    - No custom BINDINGS: the composer / transcript scroll owns
      focus handling; a user chatbox is a passive display.
    """

    # The $user-border fallback keeps the widget self-sufficient
    # (Design Principles rule 2: modular — a widget must render
    # correctly without theme.tcss loaded). The app-level
    # theme.tcss overrides the variable at runtime.
    DEFAULT_CSS = """
    $user-border: #2ea043;
    UserChatbox {
        border: round $user-border;
        padding: 0 1;
        height: auto;
        width: 100%;
    }
    """

    def __init__(self, text: str, timestamp: datetime) -> None:
        # markup=False → the raw input text is preserved verbatim.
        # Rich would otherwise interpret `[bold]…[/]` etc. as markup
        # and swallow the brackets.
        super().__init__(text, markup=False)
        self._text = text
        self._timestamp = timestamp
        self.border_title = f"You · {timestamp:%H:%M:%S}"
