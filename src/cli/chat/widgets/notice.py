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

# qol-080 — severity glyph unconditional; see the redundant-carriers
# callout in `docs/reference/chat-color-guide.md`.
SEVERITY_GLYPHS: dict[str, str] = {
    "info": "ℹ",
    "warning": "⚠",
    "error": "✗",
}


class NoticeWidget(Static):
    """Dim italic notice line. Colour and glyph vary by ``kind``.

    ``kind`` values in v1:

    - ``"info"`` — dim italic, no colour override; ``ℹ`` glyph.
    - ``"warning"`` — amber italic; ``⚠`` glyph.
    - ``"error"`` — red italic; ``✗`` glyph.

    Any unknown kind renders as ``info``.
    """

    DEFAULT_CSS = """
    $warning: #ffb454;
    $error: #ff5555;

    NoticeWidget {
        height: auto;
        padding: 0 1;
        text-style: dim italic;
    }
    NoticeWidget.-warning {
        color: $warning;
        text-style: italic;
    }
    NoticeWidget.-error {
        color: $error;
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
        rendered = self._render_body(text, kind)
        # bug-040: notice text is executor-sourced (e.g.,
        # ``event_adapter._handle_card_notice`` mounts with
        # ``f"exchange failed — {message}"`` where ``message`` is
        # from an executor error payload). Disable Textual markup so
        # bracket content in the message can't trip ``MarkupError``.
        super().__init__(rendered, id=id, markup=False)
        self._body = text
        self._text = rendered
        self._kind = kind
        if kind == "warning":
            self.add_class("-warning")
        elif kind == "error":
            self.add_class("-error")

    @staticmethod
    def _render_body(text: str, kind: str) -> str:
        glyph = SEVERITY_GLYPHS.get(kind, SEVERITY_GLYPHS["info"])
        return f"{glyph} {text}"

    def update_text(self, text: str) -> None:
        """Replace the notice's text in place (used for count updates)."""
        rendered = self._render_body(text, self._kind)
        self._body = text
        self._text = rendered
        self.update(rendered)

    @property
    def text(self) -> str:
        """Return the current rendered text (mostly for tests)."""
        return self._text

    @property
    def kind(self) -> str:
        """Return the notice kind (info / warning / error)."""
        return self._kind


__all__ = ["SEVERITY_GLYPHS", "NoticeWidget"]
