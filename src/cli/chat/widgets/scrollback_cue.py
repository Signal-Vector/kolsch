"""ScrollbackCue widget — one-shot dim rule shown at top of resumed transcript.

Mounted at the top of the transcript on `kolsch chat --resume <session>`
when replay is truncated to `--tail` (default 20 turns). Renders a
single line — `── earlier turns in scrollback ↑ ──` — in the `$hint`
colour, dim, centered.

Per the spec `ScrollbackCue lifecycle (locked)` section:

- One-shot: mounted once at the top of the transcript, never
  re-mounted, never sticky.
- Gets baked into terminal scrollback along with everything else once
  new widgets push it out of the inline viewport region.
- No docked positioning — a docked-top cue would fight the inline-mode
  contract by requiring the app to retain viewport ownership of a
  region that would otherwise scroll naturally.

Composition: subclasses Textual's `Static`; all styling lives in the
class-attribute `DEFAULT_CSS`. Nothing else.
"""

from __future__ import annotations

from textual.widgets import Static


class ScrollbackCue(Static):
    """One-shot dim rule cue mounted atop a truncated `--resume` replay."""

    # `$hint` resolves against `themes/theme.tcss` at app runtime. We
    # also declare a fallback in `DEFAULT_CSS` so the widget mounts
    # cleanly under a bare `App` in tests (per conftest's
    # "no theme.tcss loaded" invariant); the app-level stylesheet
    # overrides the fallback when the real theme is loaded.
    DEFAULT_CSS = """
    $hint: #6e7681;
    ScrollbackCue {
        height: 1;
        text-align: center;
        color: $hint;
        text-style: dim;
    }
    """

    #: Rendered text — kept as a class attribute so tests can assert on it
    #: without depending on the spec markdown drifting.
    CUE_TEXT = "── earlier turns in scrollback ↑ ──"

    def __init__(self) -> None:
        super().__init__(self.CUE_TEXT)
