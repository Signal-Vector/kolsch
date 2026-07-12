"""InfoBar — persistent bottom status segment strip.

Renders `agent · model [· session] · branch` on the left and
`cwd_basename` on the right. Docked to the bottom of the app region
below `TurnStatus`.

Per the spec (see ``process/spec-textual-chat-ui.md`` → Technical
Context → Status bar → InfoBar), layout collapse under narrow
terminals is CSS-driven (``min-width: 0`` + Textual's overflow
behaviour). We do NOT implement a Python priority-order algorithm.

Design principles (from the spec):
    1. Framework-aligned — a ``Horizontal`` of ``Static`` segments,
       each driven by a reactive attribute; ``dock: bottom`` + CSS.
    2. Modular — one file, one ``DEFAULT_CSS`` block, one Pilot
       test file.
    3. Do not reinvent — no bespoke elision; empty reactives yield
       empty segments.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static


class InfoBar(Horizontal):
    """Persistent one-line status strip docked at the bottom.

    Reactives drive the visible content of one ``Static`` segment each.
    An empty string means the segment renders empty (zero visual weight);
    Textual's CSS layout collapses the row under narrow terminals via
    ``min-width: 0`` on the droppable segments.
    """

    # NOTE: DEFAULT_CSS uses literal hex values (mirroring theme.tcss'
    # concrete palette) so the widget mounts standalone under
    # `mock_app_factory`. When the real app loads theme.tcss the
    # variables of the same names are declared there; the widget
    # itself is self-sufficient and testable in isolation.
    #
    # Layout: each segment is `width: auto` (sizes to its content).
    # The spec permits reactive `display: none` on segments based on
    # `self.size.width` when the framework's CSS-only collapse
    # doesn't hit the target order — that path is taken in
    # `on_resize` below.
    DEFAULT_CSS = """
    InfoBar {
        dock: bottom;
        height: 1;
        background: #1a1d24;
    }
    InfoBar > .segment {
        color: #6e7681;
        margin-right: 1;
        width: auto;
        min-width: 0;
    }
    InfoBar > .identity {
        color: #b0c0d5;
        text-style: bold;
        min-width: 1;
    }
    InfoBar > .cwd {
        dock: right;
    }
    """

    agent = reactive("", layout=True)
    model = reactive("", layout=True)
    session = reactive("", layout=True)
    branch = reactive("", layout=True)
    cwd = reactive("", layout=True)

    # Stable ids for querying in tests / from the parent app.
    _IDENTITY_ID = "info-identity"
    _SESSION_ID = "info-session"
    _BRANCH_ID = "info-branch"
    _CWD_ID = "info-cwd"

    def compose(self) -> ComposeResult:
        # `identity` is agent + model, one segment, bold — the spec calls
        # this out as the leftmost never-drops slot.
        yield Static(
            self._identity_text(),
            id=self._IDENTITY_ID,
            classes="segment identity",
            markup=False,
        )
        yield Static(
            self.session,
            id=self._SESSION_ID,
            classes="segment session",
            markup=False,
        )
        yield Static(
            self.branch,
            id=self._BRANCH_ID,
            classes="segment branch",
            markup=False,
        )
        yield Static(
            self.cwd,
            id=self._CWD_ID,
            classes="segment cwd",
            markup=False,
        )

    def _identity_text(self) -> str:
        """Combine agent + model into the identity slot's text.

        Empty pieces are dropped so we never render a bare separator.
        """
        parts = [p for p in (self.agent, self.model) if p]
        return " · ".join(parts)

    # ── Reactive watchers ────────────────────────────────────────────
    #
    # Each watcher updates the corresponding Static. `if self.is_mounted`
    # gates the update because reactives can fire before `compose` has
    # populated the tree during construction.

    def watch_agent(self, _old: str, _new: str) -> None:
        self._refresh_identity()

    def watch_model(self, _old: str, _new: str) -> None:
        self._refresh_identity()

    def watch_session(self, _old: str, new: str) -> None:
        if self.is_mounted:
            self.query_one(f"#{self._SESSION_ID}", Static).update(new)

    def watch_branch(self, _old: str, new: str) -> None:
        if self.is_mounted:
            self.query_one(f"#{self._BRANCH_ID}", Static).update(new)

    def watch_cwd(self, _old: str, new: str) -> None:
        if self.is_mounted:
            self.query_one(f"#{self._CWD_ID}", Static).update(new)

    def _refresh_identity(self) -> None:
        if self.is_mounted:
            self.query_one(f"#{self._IDENTITY_ID}", Static).update(
                self._identity_text()
            )

    # ── Width-based collapse ─────────────────────────────────────────
    #
    # The spec's Design Principle rule 3 lets us fall back to a small
    # reactive `display: none` toggle if pure-CSS collapse doesn't hit
    # the intended drop order. Priority — first to drop last: cwd,
    # then branch, then session. `identity` (agent + model) never drops.

    _COLLAPSE_WIDTH_CWD = 80
    _COLLAPSE_WIDTH_BRANCH = 60
    _COLLAPSE_WIDTH_SESSION = 40

    def on_resize(self) -> None:  # pragma: no cover - trivial dispatch
        self._apply_collapse(self.size.width)

    def _apply_collapse(self, width: int) -> None:
        if not self.is_mounted:
            return
        cwd_seg = self.query_one(f"#{self._CWD_ID}", Static)
        branch_seg = self.query_one(f"#{self._BRANCH_ID}", Static)
        session_seg = self.query_one(f"#{self._SESSION_ID}", Static)

        cwd_seg.display = width >= self._COLLAPSE_WIDTH_CWD
        branch_seg.display = width >= self._COLLAPSE_WIDTH_BRANCH
        session_seg.display = width >= self._COLLAPSE_WIDTH_SESSION
