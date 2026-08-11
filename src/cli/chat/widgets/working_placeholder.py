"""``WorkingPlaceholder`` — in-transcript "working" placeholder widget.

spec-130 Step 1: a standalone widget, not yet wired into any event
flow. It mounts inline in ``#transcript`` (later steps) at the exact
spot the assistant's reply will occupy, showing a spinner while the
turn is in flight, then morphs into the real final widget once
content exists. This step only builds the widget itself — the
morph/wiring logic lands in later steps.

Three renderable states:

- **working** — the default/initial state. Cycles through
  :data:`cli.chat.spinner.SPINNER_FRAMES` on the same ~0.08s / 12.5Hz
  interval convention ``TurnStatus`` used, rail ``$hint``.
- **interrupted** (terminal) — set via
  :meth:`WorkingPlaceholder.render_terminal`. Glyph is
  ``state_tokens.py``'s ``"cancelled"`` token (``─``), rail ``$hint``,
  dim text-style.
- **error** (terminal) — set via :meth:`WorkingPlaceholder.render_terminal`.
  Glyph is ``state_tokens.py``'s ``"failed"`` token (``✗``), rail
  ``$error``, bold text-style.

Follows the ``NoticeWidget`` pattern (``src/cli/chat/widgets/notice.py``):
a plain :class:`Widget` container (no base-class constraint) that
composes a single :class:`GutterBlock` in :meth:`compose`. Does not
redeclare ``margin``/``padding`` beyond what's needed for internal
layout — ``GutterBlock`` already supplies the breather/rail geometry.

The interrupted/error glyph choices are deliberate, not arbitrary —
see spec-130's Technical Context "the ``⊘`` glyph collision" note:
``ApprovalBlock``'s ``"⊘ Interrupted"`` and ``state_tokens.py``'s
``"skipped"`` token already both use ``⊘`` for two different
meanings, so this widget reuses the canonical ``"cancelled"`` token
(``─``) instead of adding a third meaning to ``⊘``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar, Literal

from textual.widget import Widget
from textual.widgets import Static

from cli.chat.spinner import SPINNER_FRAMES, spinner_frame
from cli.chat.state_tokens import resolve_state
from cli.chat.widgets._gutter_block import GutterBlock

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer

TerminalKind = Literal["interrupted", "error"]

# spec-130 Technical Context's decided glyph/color table. "working" is
# handled separately (animated spinner, not a static token) — this map
# only covers the two terminal states.
_TERMINAL_STATE_NAMES: dict[TerminalKind, str] = {
    "interrupted": "cancelled",
    "error": "failed",
}
"""Terminal kind -> ``state_tokens.py`` canonical state name.

``"interrupted"`` maps to the ``"cancelled"`` token (``─``, ``$hint``,
dim) rather than ``"skipped"``'s ``⊘`` — see module docstring. ``"error"``
maps to ``"failed"`` (``✗``, ``$error``, bold), matching ``NoticeWidget``'s
error kind and ``ToolCallBlock.-failed`` / ``WorkflowNodeRow.-failed``.
"""


class WorkingPlaceholder(Widget):
    """In-transcript placeholder shown while a turn is in flight.

    Three states:

    - Working (default): spinner cycling through ``SPINNER_FRAMES``,
      rail ``$hint``.
    - Interrupted (terminal, via ``render_terminal("interrupted")``):
      ``─`` glyph, rail ``$hint``, dim text.
    - Error (terminal, via ``render_terminal("error")``): ``✗`` glyph,
      rail ``$error``, bold text.

    Not wired into any event flow yet (spec-130 Step 1) — later steps
    mount/remove/morph this widget from ``app.py`` / ``event_adapter.py``.
    """

    DEFAULT_CSS = """
    $hint: #6e7681;
    $error: #ff5555;

    WorkingPlaceholder {
        height: auto;
        width: 100%;
    }
    WorkingPlaceholder .working-body {
        height: auto;
        width: 100%;
        color: $hint;
    }
    WorkingPlaceholder.-interrupted .working-body {
        color: $hint;
        text-style: dim;
    }
    WorkingPlaceholder.-error .working-body {
        color: $error;
        text-style: bold;
    }
    """

    _VERB = "Thinking…"
    """Fixed verb text for the working state. spec-130 Step 1 scope
    doesn't add per-turn verb vocabulary — that's TurnStatus's existing
    job (deleted in Step 8); this placeholder shows one steady verb.
    """

    _TERMINAL_BODY: ClassVar[dict[TerminalKind, str]] = {
        "interrupted": "Interrupted",
        "error": "Error",
    }
    """Body text shown once ``render_terminal`` flips the placeholder."""

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 — Textual kwarg
    ) -> None:
        super().__init__(id=id)
        self._spinner_index = 0
        self._frame_timer: Timer | None = None
        self._terminal_kind: TerminalKind | None = None
        self._body_static = Static("", classes="working-body", markup=False)

    @property
    def terminal_kind(self) -> TerminalKind | None:
        """The terminal kind this placeholder was flipped to, if any."""
        return self._terminal_kind

    @property
    def is_terminal(self) -> bool:
        """True once ``render_terminal`` has been called."""
        return self._terminal_kind is not None

    def compose(self) -> ComposeResult:
        """Compose the GutterBlock (rail + glyph) around the body Static."""
        yield GutterBlock(
            rail_color="$hint",
            leading_glyph=spinner_frame(self._spinner_index),
            body=self._body_static,
        )

    def on_mount(self) -> None:
        """Render the initial working state and start the spinner timer."""
        self._render_working()
        # ~12.5 Hz — matches TurnStatus's `_frame_timer` cadence
        # (turn_status.py:208).
        self._frame_timer = self.set_interval(0.08, self._advance_spinner)

    def on_unmount(self) -> None:
        """Stop the spinner timer so an unmounted widget incurs no cost."""
        self._stop_timer()

    def render_terminal(self, kind: TerminalKind) -> None:
        """Flip this placeholder to a terminal state (interrupted/error).

        Mirrors the shape of ``ApprovalBlock.render_terminal`` (stop
        the live animation, freeze the row) but uses this widget's own
        glyph/color table via ``state_tokens.py``'s ``"cancelled"`` /
        ``"failed"`` tokens rather than ApprovalBlock's ``⊘``/text-only
        scheme. Idempotent-ish: once terminal, calling again re-renders
        (last call wins) rather than raising — callers on the cancel/
        error paths (later steps) don't need to guard against a
        double-call race.
        """
        self._stop_timer()
        self._terminal_kind = kind
        self.remove_class("-interrupted", "-error")
        self.add_class(f"-{kind}")
        token = resolve_state(_TERMINAL_STATE_NAMES[kind])
        self._update_glyph(token.glyph)
        self._update_rail_color(token.color)
        with contextlib.suppress(Exception):
            self._body_static.update(self._TERMINAL_BODY[kind])

    def _advance_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._render_working()

    def _render_working(self) -> None:
        """Rewrite the glyph column + body for the current spinner frame."""
        self._update_glyph(spinner_frame(self._spinner_index))
        with contextlib.suppress(Exception):
            self._body_static.update(self._VERB)

    def _update_glyph(self, glyph: str) -> None:
        """Rewrite the GutterBlock's leading-glyph column.

        ``GutterBlock`` doesn't expose a reactive glyph — its
        ``leading_glyph`` is fixed at construction. The spinner needs to
        rewrite the glyph column in place on every tick, so this reaches
        into the mounted ``_GutterGlyph`` Static directly (the same
        query-and-update shape ``TurnStatus._render_spinner_verb`` uses
        for its own Static). Widget not yet mounted: silently skip —
        ``on_mount`` triggers the first render once children exist.
        """
        with contextlib.suppress(Exception):
            self.query_one(".gb-glyph-col", Static).update(glyph)

    def _update_rail_color(self, color: str) -> None:
        """Re-dispatch the mounted GutterBlock's rail/glyph CSS class.

        ``GutterBlock`` resolves palette-var rail/glyph colors via
        ``-rail-<stem>`` / ``-glyph-<stem>`` classes assigned once in
        ``GutterBlock.__init__`` (spec-078's class-dispatch design —
        see ``_gutter_block.py``'s "Color resolution" docstring); there
        is no reactive color slot to just reassign. The working/
        interrupted states both use ``$hint`` (the block's constructed
        ``rail_color``), so this only needs to act on the error
        transition — it swaps the ``-rail-hint``/``-glyph-hint`` classes
        for ``-rail-error``/``-glyph-error`` directly on the mounted
        ``GutterBlock`` instance, reusing the exact stem-class names its
        own ``DEFAULT_CSS`` already declares rules for.
        """
        stem = color.removeprefix("$")
        with contextlib.suppress(Exception):
            block = self.query_one(GutterBlock)
            block.remove_class("-rail-hint", "-glyph-hint")
            block.add_class(f"-rail-{stem}", f"-glyph-{stem}")

    def _stop_timer(self) -> None:
        if self._frame_timer is not None:
            with contextlib.suppress(Exception):
                self._frame_timer.stop()
            self._frame_timer = None


__all__ = ["TerminalKind", "WorkingPlaceholder"]
