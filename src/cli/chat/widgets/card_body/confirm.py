"""`ConfirmRenderer` — confirm card body renderer.

spec-007 §Body renderers. Renders the confirm question as a
`Static` line and reserves a slot for a
[`CardPromptPane`][cli.chat.widgets.card_prompt_pane.CardPromptPane]
mount. The actual pane wiring lands with spec-014 (workflow
approval integration); this spec ships the widget skeleton.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


class ConfirmRenderer(Vertical):
    """Static confirm text + reserved slot for `CardPromptPane`.

    `kolsch.card.set` updates the confirm text (last-writer-wins).
    `kolsch.card.snapshot` replaces content with the snapshot body.
    No prompt-pane mount in this spec — see spec-014.
    """

    DEFAULT_CSS = """
    ConfirmRenderer {
        height: auto;
        padding: 0;
    }
    ConfirmRenderer > Static.confirm-text {
        height: auto;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._text = Static("", classes="confirm-text")

    def compose(self) -> ComposeResult:
        yield self._text
        # `CardPromptPane` mount slot — populated transiently by
        # spec-014 when a workflow approval fires against this card.

    def apply_event(self, event_data: Any) -> None:
        """`kolsch.card.set` updates the confirm text."""
        kind = getattr(event_data, "kind", None) or event_data.get("kind")
        if kind != "kolsch.card.set":
            return
        payload = getattr(event_data, "payload", None)
        if payload is None:
            payload = event_data.get("payload") or {}
        content = payload.get("content")
        if isinstance(content, str):
            self._text.update(content)

    def apply_snapshot(self, content: str) -> None:
        """Replace confirm text with `content` (snapshot replay)."""
        self._text.update(content)


__all__ = ["ConfirmRenderer"]
