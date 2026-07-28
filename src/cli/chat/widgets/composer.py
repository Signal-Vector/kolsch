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

from typing import TYPE_CHECKING

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import TextArea

if TYPE_CHECKING:
    from textual import events


class Composer(TextArea):
    """Bottom-docked multiline input for the chat surface.

    The composer emits a `Composer.Submitted` message when the user
    presses Enter. Shift+Enter inserts a literal newline. All other
    keys pass through to `TextArea`'s default handling.
    """

    DEFAULT_PLACEHOLDER = (
        "Message… (Enter=send · Alt+Enter=newline · "
        "Ctrl+C=copy · /help · Ctrl+D or 'exit' to quit)"
    )
    # spec-078 one-icon-per-block rule: the amber rail (Composer:disabled
    # → `border-left: outer $warning`) is the primary "you can't type
    # right now" cue. The pre-migration verbose placeholder
    # ("Approval required — respond in the block above") stacked a
    # third redundant "permission required" message on top of the
    # visible ApprovalBlock + the TurnStatus warning row. Kept a
    # short single-line cursor hint that pairs with the rail without
    # repeating the whole sentence.
    AWAITING_PLACEHOLDER = "Awaiting approval…"

    # spec-078: Composer is a `TextArea` subclass. Wrapping in a
    # GutterBlock container (`Widget` composition) would break the
    # widget's ability to accept keyboard input — TextArea methods
    # like `_on_key`, `selection`, `_replace_via_keyboard`, and the
    # reactive `disabled` toggle for `awaiting_approval` are all
    # base-class-owned and drive the actual typing experience. So
    # the rail is delivered via `border-left: outer $user-border`
    # (Textual's `outer` paints `▌` on the left — one glyph wider
    # than spec-078's target `▎` from the GutterBlock primitive but
    # reads the same "colored left rail"). The pre-migration heavy
    # over/under bars are dropped per spec-078 ("drop the heavy
    # over/under bars"); the rail alone anchors the composer against
    # the transcript above and the InfoBar below.
    DEFAULT_CSS = """
    $user-border: #2ea043;
    $warning: #ffb454;

    Composer {
        height: auto;
        min-height: 1;
        max-height: 6;
        border: none;
        border-left: outer $user-border;
        padding: 0 1;
    }
    Composer:focus {
        border: none;
        border-left: outer $user-border;
    }
    /* awaiting-approval state (see `watch_awaiting_approval`): the
     * widget sets `disabled = True` to freeze typing. Swap the rail
     * color to `$warning` so the user's eye lands on the approval
     * block above rather than defaulting to "still typing in the
     * transcript" (Reviewer #2 finding — pre-fix the rail stayed
     * bright green even during approval, colliding with the normal
     * typing signal). */
    Composer:disabled {
        border: none;
        border-left: outer $warning;
    }
    """

    awaiting_approval: reactive[bool] = reactive(False)  # noqa: FBT003 — Textual reactive API

    class Submitted(Message):
        """Posted when the user presses Enter to send the composer content."""

        def __init__(self, content: str) -> None:
            super().__init__()
            self.content = content

    class QuitRequested(Message):
        """Posted when the user asks to quit via an empty-composer Ctrl+D.

        Payloadless — the composer only knows the *intent* ("user wants
        to quit"); it does NOT know whether the app can honour that
        (the app may still be in the middle of a turn). The app-level
        handler is the single decision point (see
        ``ChatApp.on_composer_quit_requested`` — bug-023).
        """

    def __init__(
        self,
        text: str = "",
        *,
        id: str | None = None,  # noqa: A002 — Textual kwarg
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
    _NEWLINE_KEYS = frozenset({"shift+enter", "alt+enter", "ctrl+j"})

    async def _on_key(self, event: events.Key) -> None:
        """Intercept Enter (submit), newline keys, and empty-composer Ctrl+D.

        - ``Enter`` → post ``Submitted``.
        - ``Alt+Enter`` / ``Ctrl+J`` / ``Shift+Enter`` → literal newline.
        - ``Ctrl+D`` on an empty (or whitespace-only) composer → post
          ``QuitRequested``. TextArea's own ``ctrl+d,delete`` binding
          for ``delete_right`` is claimed by the focused widget's
          binding chain BEFORE app-level bindings can run — so unless
          this handler intercepts and stops the event, the app-level
          ``ctrl+d`` binding at ``keybindings.py:32`` never fires
          (bug-023 root cause).
        - ``Ctrl+D`` on a non-empty composer → let it fall through so
          TextArea's ``delete_right`` runs (users mid-edit expect
          forward-delete to work — that's why TextArea's binding
          exists).
        - Any other key delegates to ``TextArea._on_key`` so the
          widget continues to behave like a normal editor.
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
        if key == "ctrl+d" and not self.text.strip():
            # Empty composer: stop the event so TextArea's
            # ``delete_right`` binding does NOT fire, then let the app
            # decide whether to actually quit (it only quits when idle;
            # ``action_quit_if_idle`` is the single quit site).
            event.stop()
            event.prevent_default()
            self.post_message(self.QuitRequested())
            return
        await super()._on_key(event)

    def watch_awaiting_approval(self, awaiting: bool) -> None:  # noqa: FBT001 — Textual reactive-watcher signature
        """Toggle disabled + swap the placeholder when approval is pending.

        Textual hides the cursor on a disabled `TextArea` automatically,
        so no manual cursor management is required here.
        """
        self.disabled = awaiting
        self.placeholder = (
            self.AWAITING_PLACEHOLDER if awaiting else self.DEFAULT_PLACEHOLDER
        )
