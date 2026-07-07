"""ApprovalPatternEdit — labelled `Input` sub-widget for the approval flow.

Framework-native: composes a `Static` label above an `Input`. The `Input`
IS the visible cursor — Textual renders the block cursor inside any
focused `Input`, so this widget does not implement cursor visibility.

Public contract:

- Constructor takes ``suggested_pattern: str`` — used as the ``Input``'s
  initial value.
- Posts ``Confirmed(pattern: str)`` when the ``Input`` is submitted
  (Enter).
- Posts ``Cancelled()`` when the user presses ``escape``.

The parent ``ApprovalBlock`` container listens for these messages and
swaps children accordingly (see the Approval flow subsection of
``process/spec-textual-chat-ui.md``).
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static


class ApprovalPatternEdit(Vertical):
    """Labelled ``Input`` for editing the approval rule's pattern."""

    # DEFAULT_CSS is intentionally minimal and free of theme-variable
    # references so the widget mounts standalone in Pilot tests without
    # requiring theme.tcss to be loaded. Colour/emphasis for `.label` is
    # supplied by the loaded theme (theme.tcss / mono.tcss) in the app.
    DEFAULT_CSS = """
    ApprovalPatternEdit {
        height: auto;
    }
    ApprovalPatternEdit > Input {
        height: 3;
    }
    """

    class Confirmed(Message):
        """Posted when the user presses Enter on the pattern input."""

        def __init__(self, pattern: str) -> None:
            self.pattern = pattern
            super().__init__()

    class Cancelled(Message):
        """Posted when the user presses Escape."""

    def __init__(self, suggested_pattern: str) -> None:
        super().__init__()
        self.suggested_pattern = suggested_pattern

    def compose(self) -> ComposeResult:
        yield Static("Pattern:", classes="label")
        yield Input(value=self.suggested_pattern, id="pattern")

    def on_mount(self) -> None:
        # Framework-native focus: Textual renders the cursor inside the
        # focused Input, no manual cursor management.
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Stop the Input.Submitted from bubbling further; parent listens
        # for the Confirmed message instead.
        event.stop()
        self.post_message(self.Confirmed(event.value))

    async def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.post_message(self.Cancelled())
