"""ApprovalFromEdit — labelled ``Input`` sub-widget for the approval flow.

Structurally identical to ``ApprovalPatternEdit`` but scopes the
edit to the rule's ``from`` field instead of the pattern. The
``Input`` IS the visible cursor — Textual renders the block cursor
inside any focused ``Input``, so this widget does not implement
cursor visibility.

Public contract:

- Constructor takes ``suggested_from: str`` — used as the ``Input``'s
  initial value.
- Posts ``Confirmed(from_scope: str)`` when the ``Input`` is submitted
  (Enter).
- Posts ``Cancelled()`` when the user presses ``escape``.

The parent ``ApprovalBlock`` container listens for these messages and
swaps children accordingly (see the Approval flow subsection of
``process/spec-textual-chat-ui.md``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult


class ApprovalFromEdit(Vertical):
    """Labelled ``Input`` for editing the approval rule's from-scope."""

    # DEFAULT_CSS is intentionally minimal and free of theme-variable
    # references so the widget mounts standalone in Pilot tests without
    # requiring theme.tcss to be loaded. Colour/emphasis for `.label` is
    # supplied by the loaded theme (theme.tcss / mono.tcss) in the app.
    DEFAULT_CSS = """
    ApprovalFromEdit {
        height: auto;
    }
    ApprovalFromEdit > Input {
        height: 3;
    }
    """

    class Confirmed(Message):
        """Posted when the user presses Enter on the from-scope input."""

        def __init__(self, from_scope: str) -> None:
            self.from_scope = from_scope
            super().__init__()

    class Cancelled(Message):
        """Posted when the user presses Escape."""

    def __init__(self, suggested_from: str) -> None:
        super().__init__()
        self.suggested_from = suggested_from

    def compose(self) -> ComposeResult:
        """Yield the ``From:`` label and its editable scope ``Input``."""
        yield Static("From:", classes="label")
        yield Input(value=self.suggested_from, id="from-scope")

    def on_mount(self) -> None:
        """Focus the scope ``Input`` when the pane mounts."""
        # Framework-native focus: Textual renders the cursor inside the
        # focused Input, no manual cursor management.
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Post ``Confirmed`` with the entered scope on Enter."""
        # Stop the Input.Submitted from bubbling further; parent listens
        # for the Confirmed message instead.
        event.stop()
        self.post_message(self.Confirmed(event.value))

    async def on_key(self, event: events.Key) -> None:
        """Post ``Cancelled`` on Escape."""
        if event.key == "escape":
            event.stop()
            self.post_message(self.Cancelled())
