"""TurnStatus — transient docked status line for an in-flight turn.

Renders during an active turn only. When no turn is active (``verb``
is empty), ``display`` is ``none`` and the widget occupies no vertical
space. Docked to the bottom of the app region, above ``InfoBar``.

Design principles (see ``process/spec-textual-chat-ui.md`` → Technical
Context → TurnStatus):

1. **Framework-aligned.** Visibility is a CSS matter (``.-active``,
   ``.-show-elapsed``, ``.-awaiting-approval`` classes toggled from
   reactive watchers). Timers use Textual's ``set_timer`` /
   ``set_interval`` primitives. No hand-rolled clock loop.
2. **Modular.** This widget owns its full DEFAULT_CSS and only touches
   built-in Textual variables (``$panel``, ``$text-muted``,
   ``$warning``) so the mock-app Pilot host can mount it without the
   project theme loaded — the conftest philosophy.
3. **Don't reinvent.** The spinner is a plain frame index into a
   Braille cycle advanced by ``set_interval``. Elapsed formatting is a
   one-liner. No priority-collapse algorithm — ``min-width: 0`` on
   ``#target`` lets Textual truncate under narrow terminals.

Verb sourcing lives in ``event_adapter.py`` (per the spec verb table);
this widget is a dumb view over its reactive attributes.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from cli.chat.metrics import format_duration
from cli.chat.spinner import SPINNER_FRAMES, spinner_frame


class TurnStatus(Horizontal):
    """Docked bottom transient status line for the active turn.

    Reactive contract (event_adapter.py drives these):

    - ``verb`` — non-empty string activates the widget (adds
      ``.-active`` class → CSS toggles ``display: block``). Empty
      string deactivates and cancels timers so idle sessions incur
      zero clock cost.
    - ``target`` — optional context (tool arg summary, filename).
      Rendered dim italic with ``min-width: 0`` so Textual truncates
      under narrow terminals; no Python elision.
    - ``elapsed_seconds`` — updated once per second by the internal
      tick; consumers may also set it directly for testing.
    - ``awaiting_approval`` — toggles the ``.-awaiting-approval``
      class, which in turn reveals the right-docked ``#warning``
      slot via DEFAULT_CSS. The companion ``InfoBar`` reads the same
      state to suppress its cwd segment (spec locks this).

    ``show_elapsed`` is a private reactive gated by a 2-second
    ``set_timer`` — spec ACs require the elapsed segment stays hidden
    for the first two seconds of a turn to avoid flashing on
    sub-2s exchanges.
    """

    DEFAULT_CSS = """
    TurnStatus {
        dock: bottom;
        height: 1;
        background: $panel;
        display: none;
    }
    TurnStatus.-active { display: block; }
    TurnStatus > #spinner-verb { color: $text-muted; }
    TurnStatus > #target {
        color: $text-muted;
        text-style: italic;
        min-width: 0;
        margin-left: 1;
    }
    TurnStatus > #elapsed {
        color: $text-muted;
        margin-left: 1;
        display: none;
    }
    TurnStatus.-show-elapsed > #elapsed { display: block; }
    TurnStatus > #warning {
        color: $warning;
        dock: right;
        display: none;
    }
    TurnStatus.-awaiting-approval > #warning { display: block; }
    """

    verb: reactive[str] = reactive("")
    target: reactive[str] = reactive("")
    elapsed_seconds: reactive[int] = reactive(0)
    show_elapsed: reactive[bool] = reactive(False)
    awaiting_approval: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 — Textual kwarg
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        # Timer handles retained so ``watch_verb`` can cancel them when
        # the turn ends. Textual's timer API returns handles with a
        # ``.stop()`` method.
        self._elapsed_gate_timer = None
        self._tick_timer = None
        self._frame_timer = None
        self._spinner_index = 0

    def compose(self):  # type: ignore[override]
        # IDs match the DEFAULT_CSS selectors; keep them in sync.
        yield Static("", id="spinner-verb", markup=False)
        yield Static("", id="target", markup=False)
        yield Static("", id="elapsed", markup=False)
        # spec-078 "one signal per block": the `⚠ approval required`
        # child was a redundant fourth "you need to approve" cue on
        # top of the ApprovalBlock (rail glyph `⚠`), the Composer's
        # amber rail, and the composer placeholder. Not yielded any
        # more. The `awaiting_approval` reactive + `-awaiting-approval`
        # class + CSS rule stay — they're a no-op with no `#warning`
        # child to reveal, but preserve the reactive-setter contract
        # for `recompute_awaiting_approval` and any downstream
        # consumer that keys off the class.

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_verb(self, verb: str) -> None:
        """Activate / deactivate the widget on verb transitions.

        Empty ``verb`` deactivates: the ``.-active`` class is removed
        (CSS hides the row), timers are stopped, and elapsed state is
        reset so the next turn starts clean.
        """
        if verb:
            self.add_class("-active")
            self._render_spinner_verb()
            self._start_timers()
        else:
            self.remove_class("-active")
            self.remove_class("-show-elapsed")
            self._stop_timers()
            self.elapsed_seconds = 0
            self.show_elapsed = False
            self._spinner_index = 0
            self._render_spinner_verb()

    def watch_target(self, target: str) -> None:
        # Textual's Static.update accepts a plain str; the italic style
        # comes from DEFAULT_CSS.
        try:
            self.query_one("#target", Static).update(target)
        except Exception:
            # During teardown the child may already be gone; a silent
            # skip is fine, we don't hold state here.
            pass

    def watch_elapsed_seconds(self, seconds: int) -> None:
        try:
            # qol-077: the formatter returns the number ONLY; the
            # parens are widget-specific chrome and stay here.
            self.query_one("#elapsed", Static).update(f"({format_duration(seconds)})")
        except Exception:
            pass

    def watch_show_elapsed(self, show: bool) -> None:
        # CSS class carries the visibility; keeping the Python and CSS
        # in one place matches the spec's "framework-aligned" rule.
        if show:
            self.add_class("-show-elapsed")
        else:
            self.remove_class("-show-elapsed")

    def watch_awaiting_approval(self, awaiting: bool) -> None:
        if awaiting:
            self.add_class("-awaiting-approval")
        else:
            self.remove_class("-awaiting-approval")

    # ------------------------------------------------------------------
    # Timer plumbing
    # ------------------------------------------------------------------

    def _start_timers(self) -> None:
        """Kick off the 2s gate, 1Hz tick, and spinner frame timers.

        Called from ``watch_verb`` when a turn begins. Idempotent-ish:
        if a previous run left timers alive, cancel them first.
        """
        self._stop_timers()
        # Elapsed visibility gate: reveal after 2 seconds. Spec locks
        # this so quick sub-2s exchanges don't flash "(0s)".
        self._elapsed_gate_timer = self.set_timer(2.0, self._reveal_elapsed)
        # 1Hz elapsed tick.
        self._tick_timer = self.set_interval(1.0, self._tick_elapsed)
        # Braille spinner: ~12.5 Hz feels right for the frame count.
        self._frame_timer = self.set_interval(0.08, self._advance_spinner)

    def _stop_timers(self) -> None:
        for handle_name in ("_elapsed_gate_timer", "_tick_timer", "_frame_timer"):
            handle = getattr(self, handle_name, None)
            if handle is not None:
                try:
                    handle.stop()
                except Exception:
                    # Textual raises during shutdown; a stopped timer
                    # is the desired end-state regardless.
                    pass
                setattr(self, handle_name, None)

    def _reveal_elapsed(self) -> None:
        self.show_elapsed = True

    def _tick_elapsed(self) -> None:
        self.elapsed_seconds = self.elapsed_seconds + 1

    def _advance_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._render_spinner_verb()

    def _render_spinner_verb(self) -> None:
        """Rewrite ``#spinner-verb`` with the current frame + verb.

        Kept as a single method so both frame advances and verb changes
        route through the same render call — one source of truth for
        the combined string format.
        """
        frame = spinner_frame(self._spinner_index) if self.verb else ""
        text = f"{frame} {self.verb}".strip()
        try:
            self.query_one("#spinner-verb", Static).update(text)
        except Exception:
            # Widget not yet mounted; ``on_mount`` will trigger the
            # first render.
            pass

    def on_mount(self) -> None:
        """Render whatever reactive state was set before mount.

        Reactives assigned before ``on_mount`` still fire their
        watchers, but the child Statics don't exist yet so the queries
        skip. Repaint here to catch up.
        """
        self._render_spinner_verb()
        self.watch_target(self.target)
        self.watch_elapsed_seconds(self.elapsed_seconds)
