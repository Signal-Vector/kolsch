# ruff: noqa: RUF002
# Unicode glyphs are the actual characters this primitive documents;
# ruff's confusable-ASCII rewrite would defeat the point.
"""``GutterBlock`` — the chat surface's single-visual-language primitive.

Every widget in the chat *stream* renders through this primitive: a
one-cell left rail (colored per role/state) plus an optional leading
glyph column plus a body slot. Round frames, top/bottom rules, and
per-widget-CSS silhouettes give way to one shared shape. See
``process/spec-078-chat-gutter-block-primitive.md``.

Slots (constructor keyword args):

- ``rail_color``: palette variable name (``"$user-border"``,
  ``"$assistant-fg"``, ``"$warning"``, …) OR a literal color (hex,
  named). Palette vars listed in :data:`_PALETTE_VARS` route through
  CSS class dispatch (see "Color resolution" below); anything else
  is assigned inline to ``styles.color``.
- ``leading_glyph``: single Unicode character rendered in a
  1-cell column between rail and body (``⚠``, ``ℹ``, ``✗``, ``›``,
  state glyph from :data:`cli.chat.state_tokens.STATE_TOKENS`, …).
  ``None`` skips the column entirely — the body sits directly beside
  the rail with a one-column pad.
- ``leading_glyph_color``: same value shape as ``rail_color``. Defaults
  to ``rail_color`` (the common case — matching-color glyph on the rail).
- ``body``: the ``Widget`` mounted into the body slot. Any Textual
  widget subclass; the primitive does not constrain it.

Color resolution (why the two paths):

Textual palette variables (``$foo``) only resolve inside *stylesheet
text* parsed by the CSS engine; direct ``styles.color = "$foo"``
assignment throws ``ColorParseError`` because the inline styles API
runs the raw string through :meth:`textual.color.Color.parse`, which
doesn't know about palette vars. So this primitive resolves the six
kolsch palette vars via per-class CSS rules baked into
:attr:`DEFAULT_CSS` — a caller passing ``rail_color="$user-border"``
gets the ``-rail-user-border`` class on the container, and the CSS
rule ``GutterBlock.-rail-user-border > .gb-rail { color: $user-border }``
lets Textual's CSS engine do the palette resolution the way it
normally does. Literal colors (``"#2ea043"``, ``"red"``, ``"ansi_default"``)
bypass the class table and land on ``styles.color`` directly, which
Textual's parser DOES accept for literal values.

Palette-var fallbacks (``$user-border: #2ea043;`` etc.) are inlined
in :attr:`DEFAULT_CSS` so the primitive renders standalone under
Pilot tests that don't load ``theme.tcss``. Real mounts under
``ChatApp`` use ``theme.tcss`` (or ``mono.tcss`` under ``NO_COLOR``);
those values take precedence because the app's stylesheet cascades
after this widget's defaults.

Focus indicator (qol-079):

Two selectors, both ``text-style: reverse`` on the leftmost column
that column is present:

- ``:focus-within > .gb-glyph-col { text-style: reverse }`` inverts
  the leading glyph — the primary cue on widgets that carry one
  (``NoticeWidget`` ``⚠``, ``ApprovalBlock`` ``⚠``, …).
- ``:focus-within > .gb-rail { text-style: reverse }`` inverts the
  rail's ``▎`` glyph — the fallback cue for glyph-less widgets
  (``UserChatbox``, ``AssistantProse``). On widgets that DO have a
  glyph, both fire — the entire leftmost strip lights up bright,
  reinforcing focus rather than diluting it.

One mechanic, one selector pair, applied uniformly by the primitive
— no per-widget focus CSS. The mechanic survives ``NO_COLOR`` (the
terminal inverts fg/bg trivially) and reuses the same column state
color already occupies, so one column carries two orthogonal
signals (state = hue; focus = inversion).

Non-migrated widgets — the following stay borderless / bare and
intentionally do **not** inherit from or compose ``GutterBlock``:

- ``TurnStatus`` — fixed chrome (single top-of-turn line), not part
  of the scrollable stream.
- ``StatusBar``, ``InfoBar`` — persistent app chrome docked outside
  the transcript region.
- ``SlashPopup`` — transient overlay, not a stream row.
- ``ScrollbackCue`` — one-line marker with its own visual grammar
  (arrow + count), not a message payload.

If you're adding a new widget that renders inside the transcript
stream, inherit from or compose ``GutterBlock``. If you're adding
persistent chrome or a transient overlay, don't.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.segment import Segment
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

_RAIL_GLYPH = "▎"
"""U+258E LEFT ONE EIGHTH BLOCK — the exact glyph the design review calls for.

Not overridable per instance: uniform rail visual is the whole point
of the primitive. If a future design needs a different rail glyph,
change it here (and refresh every gallery capture) — don't add a slot.
"""


_PALETTE_VARS: frozenset[str] = frozenset(
    {
        "user-border",
        "assistant-fg",
        "tool-fg",
        "warning",
        "error",
        "hint",
    }
)
"""kolsch palette variable *stems* (without the leading ``$``).

A ``rail_color`` or ``leading_glyph_color`` that names one of these
routes through per-class CSS rules in :attr:`GutterBlock.DEFAULT_CSS`
so Textual's stylesheet resolves the palette var. Anything else
(literal hex, named color, ``ansi_*``) is assigned inline.

Adding a new palette var to ``theme.tcss``? Also list its stem here
AND add matching ``GutterBlock.-rail-<stem> > .gb-rail`` and
``GutterBlock.-glyph-<stem> > .gb-glyph-col`` rules in ``DEFAULT_CSS``.
The pair is a small, deliberate coupling — the primitive picks up
new theme colors on purpose, not silently.
"""


class _GutterRail(Widget):
    """1-cell-wide column of ``▎`` characters, one per rendered row.

    Textual sizes the widget via CSS (``width: 1``, ``height: 1fr``);
    :meth:`render_line` is called once per visible row and always
    returns the same single-segment ``▎`` styled with the widget's
    current ``rich_style``. The rail auto-fills any allocated height
    without the primitive having to know its body's line count in
    advance — the body Widget's height cascades up the horizontal
    layout, and the rail matches it via ``height: 1fr``.
    """

    # CRITICAL: `height: 100%` (matches sibling height), NOT `height: 1fr`.
    # `1fr` in a `height: auto` container asks for "fill available
    # parent" — which propagates up and makes the GutterBlock consume
    # the entire transcript viewport even for one-line content. The
    # user caught this: every widget composing a GutterBlock rendered
    # at 100% viewport height until this was changed.
    DEFAULT_CSS = """
    _GutterRail {
        width: 1;
        min-width: 1;
        max-width: 1;
        height: 100%;
    }
    """

    can_focus: bool = False  # matches Widget.can_focus (a plain attr, not ClassVar)

    def render_line(self, y: int) -> Strip:
        # y is passed for signature parity; the rail is invariant per
        # row (every line is a lone ``▎``), so we ignore it.
        _ = y
        return Strip([Segment(_RAIL_GLYPH, self.rich_style)], 1)


class _GutterGlyph(Static):
    """1-cell-wide glyph column between the rail and the body.

    Only mounted when the containing :class:`GutterBlock` was
    constructed with a non-``None`` ``leading_glyph``. Rendered as a
    :class:`Static` (not a :class:`_GutterRail`) because the glyph is
    a *single* row at the top of the block — spec-078 §"target render"
    shows the glyph adjacent to the first content line, not repeated
    down the rail. ``content-align: center top`` pins it there.
    """

    # Same `height: 100%` reasoning as `_GutterRail` — `1fr` here
    # would balloon the container to viewport height even for a
    # one-row body. Textual will only paint the glyph on the first
    # row (`content-align: center top`); the rest of the column is
    # empty background, matching the design mockup's "glyph adjacent
    # to the first content line" intent.
    DEFAULT_CSS = """
    _GutterGlyph {
        width: 1;
        min-width: 1;
        max-width: 1;
        height: 100%;
        content-align: center top;
    }
    """

    can_focus: bool = False  # matches Widget.can_focus (a plain attr, not ClassVar)


class GutterBlock(Widget):
    """2-cell left rail + optional leading glyph + body — the chat primitive.

    See module docstring for slot semantics, color resolution, and the
    list of widgets that intentionally opt out.
    """

    # Palette-var fallbacks keep the primitive self-sufficient (Design
    # Principles rule 2: modular — a widget must render standalone
    # without theme.tcss). When theme.tcss / mono.tcss IS loaded these
    # values are overridden because the app stylesheet cascades after.
    DEFAULT_CSS = """
    $user-border: #2ea043;
    $assistant-fg: #b0c0d5;
    $tool-fg:      #4dd0e1;
    $warning:      #ffb454;
    $error:        #ff5555;
    $hint:         #6e7681;

    GutterBlock {
        layout: horizontal;
        height: auto;
        margin: 0 0 1 0;
    }
    GutterBlock > .gb-body {
        width: 1fr;
        padding: 0 0 0 1;
        height: auto;
    }
    GutterBlock:focus-within > .gb-glyph-col {
        text-style: reverse;
    }
    GutterBlock:focus-within > .gb-rail {
        text-style: reverse;
    }

    /* Rail color — one rule per palette var (see _PALETTE_VARS). */
    GutterBlock.-rail-user-border > .gb-rail { color: $user-border; }
    GutterBlock.-rail-assistant-fg > .gb-rail { color: $assistant-fg; }
    GutterBlock.-rail-tool-fg > .gb-rail { color: $tool-fg; }
    GutterBlock.-rail-warning > .gb-rail { color: $warning; }
    GutterBlock.-rail-error > .gb-rail { color: $error; }
    GutterBlock.-rail-hint > .gb-rail { color: $hint; }

    /* Glyph color — same set. */
    GutterBlock.-glyph-user-border > .gb-glyph-col { color: $user-border; }
    GutterBlock.-glyph-assistant-fg > .gb-glyph-col { color: $assistant-fg; }
    GutterBlock.-glyph-tool-fg > .gb-glyph-col { color: $tool-fg; }
    GutterBlock.-glyph-warning > .gb-glyph-col { color: $warning; }
    GutterBlock.-glyph-error > .gb-glyph-col { color: $error; }
    GutterBlock.-glyph-hint > .gb-glyph-col { color: $hint; }
    """

    can_focus: bool = False  # matches Widget.can_focus (a plain attr, not ClassVar)

    def __init__(
        self,
        *,
        rail_color: str,
        leading_glyph: str | None = None,
        leading_glyph_color: str | None = None,
        body: Widget,
        id: str | None = None,  # noqa: A002 — Textual kwarg
        classes: str | None = None,
    ) -> None:
        # Route palette-var colors through class dispatch; literal
        # colors are handled inline at mount time.
        base_classes = classes or ""
        rail_var = _var_stem(rail_color)
        glyph_source = leading_glyph_color or rail_color
        glyph_var = _var_stem(glyph_source) if leading_glyph is not None else None
        added: list[str] = []
        if rail_var is not None:
            added.append(f"-rail-{rail_var}")
        if glyph_var is not None:
            added.append(f"-glyph-{glyph_var}")
        composite_classes = " ".join([base_classes, *added]).strip() or None
        super().__init__(id=id, classes=composite_classes)
        self._rail_color = rail_color
        self._leading_glyph = leading_glyph
        self._leading_glyph_color = leading_glyph_color or rail_color
        self._body = body
        self._body.add_class("gb-body")

    @property
    def rail_color(self) -> str:
        """Palette-var (or literal) color assigned to the rail."""
        return self._rail_color

    @property
    def leading_glyph(self) -> str | None:
        """The leading-glyph character, or ``None`` for no glyph column."""
        return self._leading_glyph

    @property
    def leading_glyph_color(self) -> str | None:
        """Palette-var (or literal) color assigned to the glyph column."""
        return self._leading_glyph_color

    @property
    def body(self) -> Widget:
        """The body widget mounted into the primitive's body slot."""
        return self._body

    def compose(self) -> ComposeResult:
        yield _GutterRail(classes="gb-rail")
        if self._leading_glyph is not None:
            yield _GutterGlyph(self._leading_glyph, classes="gb-glyph-col")
        # Textual's `Vertical` / `Container` / `Horizontal` all default
        # to `height: 1fr` in their built-in DEFAULT_CSS, which cascades
        # ABOVE the `.gb-body { height: auto }` rule below (element
        # selector on the container class beats a descendant-class
        # rule in Textual's practical cascade). Set inline here so
        # every body — regardless of the caller's container choice —
        # shrinks to its content instead of expanding to fill the
        # transcript / app viewport. Without this, an AssistantProse
        # wrapping a two-line markdown block ate 100% of the visible
        # transcript height post spec-078.
        self._body.styles.height = "auto"
        yield self._body

    def on_mount(self) -> None:
        """Apply literal-color slots inline.

        Palette-var slots (values matching a stem in :data:`_PALETTE_VARS`)
        are already handled by the ``-rail-<stem>`` / ``-glyph-<stem>``
        classes added in ``__init__`` — Textual's stylesheet resolves
        them via the class rules in ``DEFAULT_CSS``. Only literal
        values (hex, named, ``ansi_*``) need runtime assignment; those
        Textual's Color parser accepts on the inline-styles API.
        """
        if _var_stem(self._rail_color) is None:
            self.query_one(".gb-rail", _GutterRail).styles.color = self._rail_color
        if (
            self._leading_glyph is not None
            and _var_stem(self._leading_glyph_color) is None
        ):
            self.query_one(
                ".gb-glyph-col",
                _GutterGlyph,
            ).styles.color = self._leading_glyph_color


def _var_stem(color: str | None) -> str | None:
    """Return the palette-var *stem* for a known kolsch palette variable.

    ``"$user-border"`` → ``"user-border"``; ``"#2ea043"`` → ``None``;
    ``"$made-up"`` → ``None``. A ``None`` return means "the caller
    passed a literal color — assign it inline, don't dispatch by class".
    """
    if color is None or not color.startswith("$"):
        return None
    stem = color[1:]
    return stem if stem in _PALETTE_VARS else None


__all__ = ["GutterBlock"]
