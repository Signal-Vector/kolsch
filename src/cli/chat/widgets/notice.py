"""``NoticeWidget`` — dim italic Static line for in-transcript notices.

spec-008 §Cards.notice routes here. Two mount surfaces:

- Transcript root — for ad-hoc notices (previously the
  ``ChatApp.mount_notice`` helper's target). ``EventAdapter._handle_card_notice``
  mounts the widget into ``#transcript`` when the payload carries no
  ``card_id``.
- In-card — mounted inside a
  [`CardWidget`][cli.chat.widgets.card_widget.CardWidget]'s
  [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot] when the
  payload carries ``card_id``.

Repeat dispatches with the same ``notice_id`` update the mounted
widget's text via ``update_text`` rather than mounting a second widget
— this is the drop-notice count-increment path from spec-008 §Drop
rate limit.
"""

from __future__ import annotations

from textual.widgets import Static


class NoticeWidget(Static):
    """Dim italic notice line. Colour and glyph vary by ``kind``.

    ``kind`` values in v1:

    - ``"info"`` — dim italic, no colour override.
    - ``"warning"`` — amber italic.
    - ``"error"`` — red italic.

    Any unknown kind renders as ``info``.
    """

    DEFAULT_CSS = """
    NoticeWidget {
        height: auto;
        padding: 0 1;
        text-style: dim italic;
    }
    NoticeWidget.-warning {
        color: #ffb454;
        text-style: italic;
    }
    NoticeWidget.-error {
        color: #ff5555;
        text-style: italic;
    }
    """

    def __init__(
        self,
        *,
        text: str,
        kind: str = "info",
        id: str | None = None,
    ) -> None:
        super().__init__(text, id=id)
        self._text = text
        self._kind = kind
        if kind == "warning":
            self.add_class("-warning")
        elif kind == "error":
            self.add_class("-error")

    def update_text(self, text: str) -> None:
        """Replace the notice's text in place (used for count updates)."""
        self._text = text
        self.update(text)

    @property
    def text(self) -> str:
        """Return the current rendered text (mostly for tests)."""
        return self._text

    @property
    def kind(self) -> str:
        """Return the notice kind (info / warning / error)."""
        return self._kind


__all__ = ["NoticeWidget"]
