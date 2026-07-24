"""UserChatbox widget — the user's turn in the chat stream.

Composes the shared :class:`~cli.chat.widgets._gutter_block.GutterBlock`
primitive: a ``$user-border`` rail, no leading glyph, and a body that
stacks the ``You · HH:MM`` identity header above the raw user text.

Migrated in spec-078 from the round-frame ``Static`` silhouette
(``border: round $user-border`` + ``border_title = "You · HH:MM:SS"``)
onto the gutter primitive. Public API — class name, constructor
signature, "mount and freeze" lifecycle — is unchanged. Body text is
still rendered literally (``markup=False``) so backticks and Rich
markup brackets survive verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets._gutter_block import GutterBlock

if TYPE_CHECKING:
    from textual.app import ComposeResult


class UserChatbox(Widget):
    """A single user turn rendered through :class:`GutterBlock`.

    Rail: ``$user-border``. No leading glyph. Body: identity header
    (``You · HH:MM``) above the raw user text.

    Constructor:
        text: The raw user input string. Rendered literally — backticks,
            brackets, and any other Rich/markdown markup are preserved
            as-is (``markup=False`` on the underlying ``Static``).
        timestamp: The event timestamp used for the identity header.
            The header renders at minute precision (``HH:MM``) —
            seconds are too fine-grained for the chat stream's
            per-row identity chrome.

    Design notes:

    - Once mounted, the widget is frozen — it does not react to
      further events. Matches the "mount and freeze" lifecycle
      shared by :class:`~cli.chat.widgets.assistant_prose.AssistantProse`
      and :class:`~cli.chat.widgets.approval_block.ApprovalBlock`.
    - No custom BINDINGS. The composer / transcript scroll owns focus
      handling; a user chatbox is a passive display.
    - Text preservation: the body ``Static`` is constructed with
      ``markup=False`` so ``[bold]…[/bold]`` in user input renders as
      literal text — this is the same contract the pre-migration
      widget honored.
    """

    # The $user-border fallback keeps the widget self-sufficient
    # (Design Principles rule 2: modular — a widget must render
    # standalone without theme.tcss). The app-level theme.tcss
    # overrides this at runtime because the app stylesheet cascades
    # after DEFAULT_CSS.
    DEFAULT_CSS = """
    $user-border: #2ea043;

    UserChatbox {
        height: auto;
        width: 100%;
    }
    UserChatbox .uc-header {
        color: $user-border;
        text-style: bold;
        height: 1;
        width: 100%;
    }
    UserChatbox .uc-body {
        height: auto;
        width: 100%;
    }
    """

    def __init__(self, text: str, timestamp: datetime) -> None:
        super().__init__()
        self._text = text
        self._timestamp = timestamp

    @property
    def text(self) -> str:
        """The raw user text this widget was constructed with."""
        return self._text

    @property
    def timestamp(self) -> datetime:
        """The event timestamp this widget was constructed with."""
        return self._timestamp

    def compose(self) -> ComposeResult:
        """Compose the identity header + literal body inside a ``GutterBlock``."""
        header = Static(
            f"You · {self._timestamp:%H:%M}",
            classes="uc-header",
            markup=False,
        )
        body_text = Static(self._text, classes="uc-body", markup=False)
        yield GutterBlock(
            rail_color="$user-border",
            body=Vertical(header, body_text),
        )
