"""``StatusBar`` — line 2 workflow strip + Esc-toast surface (feat-019).

Renders the persistent bottom-of-screen status *line 2* — the single-line
strip that summarises live workflow runs when the chat is idle, or the
Esc-Esc toast when the user hits ``Esc`` while both a butler turn and a
live workflow are active.

Design principles (feat-019 §Scope + §Coordinator locks):

1. **Framework-aligned.** Visibility is a CSS matter (``.-visible``
   class toggled from :meth:`set_workflow_strip` / :meth:`set_toast`).
   No hand-rolled clock — the 3s toast auto-dismiss is scheduled by
   the caller (``ChatApp.on_key``) using ``call_later``.
2. **Turn precedence.** When ``TurnStatus`` is active the app owns the
   bottom transient row; :class:`StatusBar` mounts *below*
   ``TurnStatus`` and *above* ``InfoBar`` in the bottom stack, and its
   internal :attr:`turn_active` reactive collapses the workflow strip
   so the turn spinner is the single line-2 signal during an active
   turn. Coordinator Q1 lock.
3. **Best-effort 78-char width.** :meth:`set_workflow_strip` and
   :meth:`set_toast` accept a plain string; no elision performed —
   Textual's CSS ``min-width: 0`` on the child ``Static`` collapses
   under narrow terminals. Coordinator Q2 lock.

The widget owns two pieces of state — the workflow strip text and the
Esc-toast text — which precedence-render as: toast > turn (via
``turn_active`` collapse) > workflow strip. The empty-string default on
both means an idle app with no live workflows and no toast collapses
the row via ``display: none``.

Wired from :class:`cli.chat.services.workflow_layout.WorkflowLayoutManager`
on every ``_flush_reorder`` pass, and from ``ChatApp.on_key`` when the
Esc-Esc toast fires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class StatusBar(Horizontal):
    """Line-2 status strip for the workflow overview + Esc-Esc toast.

    Two reactive text sources plus a turn-precedence flag:

    - :attr:`workflow_strip_text` — the ``▸ ref N/M · Ns`` string built
      by
      :func:`cli.chat.services.workflow_layout.format_live_workflow_strip`.
      Empty string collapses the strip.
    - :attr:`toast_text` — the ``↳ butler cancelled …`` string set by
      ``ChatApp.on_key`` for the 3s Esc-Esc window. Empty string
      collapses the toast.
    - :attr:`turn_active` — bool driven by ``ChatApp`` at
      ``set_turn_verb`` time. True → the strip is suppressed so the
      companion ``TurnStatus`` is the single line-2 signal (Coordinator
      Q1 lock). The toast still wins even while the turn is active
      because the toast is a one-shot dismissable teach.

    Precedence (highest → lowest): ``toast_text`` > ``workflow_strip_text``
    (unless ``turn_active``).
    """

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $panel;
        display: none;
    }
    StatusBar.-visible { display: block; }
    StatusBar > #status-bar-text {
        color: $text-muted;
        min-width: 0;
        margin-left: 0;
    }
    StatusBar.-toast > #status-bar-text {
        color: $warning;
    }
    """

    workflow_strip_text: reactive[str] = reactive("")
    """Workflow-strip text set by :meth:`set_workflow_strip`.

    Empty string clears the strip. When both this and :attr:`toast_text`
    are empty, the widget collapses via ``display: none``.
    """

    toast_text: reactive[str] = reactive("")
    """Esc-toast text set by :meth:`set_toast`.

    Empty string clears the toast. Wins precedence over
    :attr:`workflow_strip_text` when non-empty (see class docstring).
    """

    turn_active: reactive[bool] = reactive(False)
    """When True, the workflow strip is suppressed so ``TurnStatus``
    owns the transient bottom row (Coordinator Q1 lock).

    Toggled by ``ChatApp.set_turn_verb`` — non-empty verb = True; empty
    verb = False.
    """

    _TEXT_ID = "status-bar-text"

    def compose(self) -> ComposeResult:
        yield Static("", id=self._TEXT_ID, classes="status-bar-text", markup=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_workflow_strip(self, text: str | None) -> None:
        """Set the workflow-strip content; ``None`` / empty clears it.

        Called by
        :meth:`cli.chat.services.workflow_layout.WorkflowLayoutManager._flush_reorder`
        with the return of
        :func:`cli.chat.services.workflow_layout.format_live_workflow_strip`.
        Idempotent — the reactive assignment collapses to a no-op when
        the value is unchanged.
        """
        self.workflow_strip_text = text or ""

    def set_toast(self, text: str | None) -> None:
        """Set the Esc-toast content; ``None`` / empty clears it.

        Called by ``ChatApp.on_key`` when the first ``Esc`` cancelled a
        butler turn AND ≥ 1 workflow is live. The 3s auto-clear is
        scheduled by the caller via ``call_later``; this widget stays
        dumb about timing so the state machine lives in one place.
        """
        self.toast_text = text or ""

    def set_turn_active(self, active: bool) -> None:
        """Toggle the turn-precedence flag.

        Called by ``ChatApp.set_turn_verb`` — non-empty verb marks the
        turn active (workflow strip suppressed); empty verb clears it.
        """
        self.turn_active = active

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_workflow_strip_text(self, _old: str, _new: str) -> None:
        self._refresh_visible_text()

    def watch_toast_text(self, _old: str, _new: str) -> None:
        self._refresh_visible_text()

    def watch_turn_active(self, _old: bool, _new: bool) -> None:
        self._refresh_visible_text()

    # ------------------------------------------------------------------
    # Rendering plumbing
    # ------------------------------------------------------------------

    def _refresh_visible_text(self) -> None:
        """Recompute the displayed content per precedence rules.

        Precedence (see class docstring): ``toast_text`` >
        ``workflow_strip_text`` (unless ``turn_active`` suppresses the
        strip). Empty result collapses the row.
        """
        # Toast wins outright.
        if self.toast_text:
            self._paint(self.toast_text, toast=True)
            return
        # Turn active — suppress the strip.
        if self.turn_active:
            self._paint("", toast=False)
            return
        # Strip.
        self._paint(self.workflow_strip_text, toast=False)

    def _paint(self, text: str, *, toast: bool) -> None:
        """Write ``text`` into the child ``Static``; toggle visibility.

        Guards ``is_mounted`` so a reactive fired before ``compose``
        doesn't crash the widget on construction.
        """
        # Update class first so CSS runs before layout.
        if toast:
            self.add_class("-toast")
        else:
            self.remove_class("-toast")
        if text:
            self.add_class("-visible")
        else:
            self.remove_class("-visible")
        if self.is_mounted:
            try:
                self.query_one(f"#{self._TEXT_ID}", Static).update(text)
            except Exception:
                # Textual raises during teardown; a stopped write is
                # the desired end-state.
                pass

    def on_mount(self) -> None:
        """Render whatever reactive state was set before mount.

        Reactives assigned before ``on_mount`` fire their watchers, but
        the child ``Static`` doesn't exist yet so the queries skip.
        Repaint here to catch up.
        """
        self._refresh_visible_text()


__all__ = ["StatusBar"]
