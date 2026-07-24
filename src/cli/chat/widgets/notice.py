# ruff: noqa: RUF001, RUF002
"""``NoticeWidget`` — in-transcript notice rendered on the GutterBlock rail.

spec-008 §Cards.notice routes here. Two mount surfaces:

- Transcript root — for ad-hoc notices (previously the
  ``ChatApp.mount_notice`` helper's target).
  ``EventAdapter._handle_card_notice`` mounts the widget into
  ``#transcript`` when the payload carries no ``card_id``.
- In-card — mounted inside a
  [`CardWidget`][cli.chat.widgets.card_widget.CardWidget]'s
  [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot] when the
  payload carries ``card_id``.

Repeat dispatches with the same ``notice_id`` update the mounted
widget's body text via :meth:`update_text` rather than mounting a
second widget — the drop-notice count-increment path from spec-008
§Drop rate limit.

Post spec-078: the widget is a :class:`Widget` container composing a
single :class:`GutterBlock` (rail = severity color, glyph = severity
glyph). Pre-migration the glyph was prepended to the body Static's
text via :meth:`_render_body`; post-migration the glyph lives in the
GutterBlock's leading-glyph column and the body Static holds only
the message text. The redundant-carriers principle (qol-080 —
severity carried by hue AND glyph AND text-style, in every mode) is
preserved: the rail color and glyph column always render regardless
of ``NO_COLOR``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets._gutter_block import GutterBlock

if TYPE_CHECKING:
    from textual.app import ComposeResult

# qol-080 — severity glyph unconditional; see the redundant-carriers
# callout in `docs/reference/chat-color-guide.md`.
SEVERITY_GLYPHS: dict[str, str] = {
    "info": "ℹ",
    "warning": "⚠",
    "error": "✗",
}

_SEVERITY_RAIL_COLORS: dict[str, str] = {
    "info": "$hint",
    "warning": "$warning",
    "error": "$error",
}
"""Per-severity rail color (palette var). ``info`` uses ``$hint`` — a
neutral dim that matches the pre-migration ``dim italic`` text
treatment; ``warning`` / ``error`` route to their spec-locked palette
vars.
"""


class NoticeWidget(Widget):
    """Notice row rendered on the shared GutterBlock rail.

    ``kind`` values in v1:

    - ``"info"`` — rail ``$hint``, glyph ``ℹ``, body dim italic.
    - ``"warning"`` — rail ``$warning``, glyph ``⚠``, body italic amber.
    - ``"error"`` — rail ``$error``, glyph ``✗``, body italic red.

    Any unknown kind renders as ``info``.
    """

    DEFAULT_CSS = """
    $warning: #ffb454;
    $error: #ff5555;
    $hint: #6e7681;

    NoticeWidget {
        height: auto;
        width: 100%;
    }
    NoticeWidget .notice-body {
        height: auto;
        width: 100%;
        text-style: dim italic;
    }
    NoticeWidget.-warning .notice-body {
        color: $warning;
        text-style: italic;
    }
    NoticeWidget.-error .notice-body {
        color: $error;
        text-style: italic;
    }
    """

    def __init__(
        self,
        *,
        text: str,
        kind: str = "info",
        id: str | None = None,  # noqa: A002 — Textual kwarg
    ) -> None:
        super().__init__(id=id)
        self._body = text
        self._kind = kind if kind in SEVERITY_GLYPHS else "info"
        if self._kind == "warning":
            self.add_class("-warning")
        elif self._kind == "error":
            self.add_class("-error")
        # bug-040: notice text is executor-sourced (e.g., event adapter
        # mounts with executor error payloads). Disable Textual markup
        # so bracket content in the message can't trip MarkupError.
        self._body_static = Static(
            self._body,
            classes="notice-body",
            markup=False,
        )

    @property
    def text(self) -> str:
        """The notice's body text (glyph is delivered via the rail).

        Post spec-078: pre-migration this returned the glyph-prefixed
        body (``"ℹ session resumed"``); post-migration the glyph lives
        in the GutterBlock leading-glyph column, so this returns just
        the message text. Callers that used ``.text`` for the RAW
        message body (all of them at grep time — no ``src/`` caller
        was depending on the glyph prefix) keep working.
        """
        return self._body

    @property
    def kind(self) -> str:
        """Return the notice kind (``info`` / ``warning`` / ``error``)."""
        return self._kind

    def update_text(self, text: str) -> None:
        """Replace the body text in place (drop-notice count updates).

        Called by ``event_adapter._handle_card_notice`` on repeat
        dispatches with the same ``notice_id``. The rail + glyph column
        are unchanged — only the body Static's content updates.
        """
        self._body = text
        self._body_static.update(text)

    def compose(self) -> ComposeResult:
        """Compose the GutterBlock (severity rail + glyph) around body."""
        glyph = SEVERITY_GLYPHS.get(self._kind, SEVERITY_GLYPHS["info"])
        rail_color = _SEVERITY_RAIL_COLORS.get(
            self._kind,
            _SEVERITY_RAIL_COLORS["info"],
        )
        yield GutterBlock(
            rail_color=rail_color,
            leading_glyph=glyph,
            body=self._body_static,
        )


__all__ = ["SEVERITY_GLYPHS", "NoticeWidget"]
