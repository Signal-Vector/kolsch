"""Composer widget — the bottom-docked multiline text input.

Subclass of Textual's `TextArea`. Docked to the bottom of the app;
grows to content up to `max-height: 6`, then scrolls internally.
`Enter` submits (posts a `Submitted` message); `Shift+Enter` inserts a
newline. When `awaiting_approval` is set the composer disables itself
and swaps its placeholder to a hint pointing the user at the
`ApprovalBlock` above.

Per the spec's Design Principles (framework-aligned, don't reinvent),
the composer contains no custom cursor management, no per-character
tinting, and no Python-side max-row clamp — CSS + Textual's own
`disabled` + focus system do all the work.
"""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import TextArea


class Composer(TextArea):
    """Bottom-docked multiline input for the chat surface.

    The composer emits a `Composer.Submitted` message when the user
    presses Enter. Shift+Enter inserts a literal newline. All other
    keys pass through to `TextArea`'s default handling.
    """

    DEFAULT_PLACEHOLDER = (
        "Message… (Enter=send · Alt+Enter=newline · "
        "Ctrl+Shift+C=copy · /help)"
    )
    AWAITING_PLACEHOLDER = "Approval required — respond in the block above"

    DEFAULT_CSS = """
    $user-border: #2ea043;
    /* Dividers above and below the input — no box border around it.
     * The composer sits between the transcript (above) and the
     * InfoBar (below); the top and bottom rules frame the input
     * area cleanly. Idle and focused share the same borders — no
     * visual jump when focus lands. */
    Composer {
        height: auto;
        min-height: 1;
        max-height: 6;
        border: none;
        border-top: heavy $user-border;
        border-bottom: heavy $user-border;
        padding: 0 1;
    }
    Composer:focus {
        border: none;
        border-top: heavy $user-border;
        border-bottom: heavy $user-border;
    }
    """

    awaiting_approval: reactive[bool] = reactive(False)

    class Submitted(Message):
        """Posted when the user presses Enter to send the composer content."""

        def __init__(self, content: str) -> None:
            super().__init__()
            self.content = content

    def __init__(
        self,
        text: str = "",
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            text,
            id=id,
            classes=classes,
            soft_wrap=True,
            placeholder=self.DEFAULT_PLACEHOLDER,
        )

    # Alt+Enter, Ctrl+J, and Shift+Enter all insert a newline.
    # Terminal support varies: iTerm2 and Terminal.app frequently
    # collapse Shift+Enter → plain Enter unless the user enables a
    # specific key mapping; Alt+Enter and Ctrl+J are more widely
    # respected. Accept all three so users don't have to know which
    # their terminal supports.
    _NEWLINE_KEYS = frozenset(
        {"shift+enter", "alt+enter", "ctrl+j"}
    )

    async def _on_key(self, event: events.Key) -> None:
        """Intercept Enter (submit) and newline keys.

        Any other key is delegated to `TextArea._on_key` so the widget
        continues to behave like a normal editor.
        """
        if self.disabled:
            return
        key = event.key
        if key in self._NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        await super()._on_key(event)

    def watch_awaiting_approval(self, awaiting: bool) -> None:
        """Toggle disabled + swap the placeholder when approval is pending.

        Textual hides the cursor on a disabled `TextArea` automatically,
        so no manual cursor management is required here.
        """
        self.disabled = awaiting
        self.placeholder = (
            self.AWAITING_PLACEHOLDER if awaiting else self.DEFAULT_PLACEHOLDER
        )
