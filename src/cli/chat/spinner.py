"""Canonical braille-spinner frame table for the chat surface.

Single source of truth for the "work in progress" animation used by
the chat TUI. Replaces the two divergent per-widget alphabets that
historically shipped side by side — a 10-frame dot-cycle in
``turn_status.py`` and an 8-frame rotating-block in
``workflow_node_row.py``. See qol-076.

The dot-cycle is retained because (a) it already backs the most
prominent chat surface (the turn-status line), (b) it renders more
smoothly at any tick rate that is not a multiple of 8, and (c) it is
more portable across braille-render fidelity.

Widgets keep owning their own tick source (a Textual
``set_interval`` handle each) and index into :data:`SPINNER_FRAMES`
via :func:`spinner_frame`. Spinner lifecycle (start/stop, cadence)
is a widget concern; this module only names the alphabet.
"""

from __future__ import annotations

SPINNER_FRAMES: tuple[str, ...] = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
"""Canonical 10-frame braille dot-cycle used across the chat surface.

Kept as a ``tuple`` so callers get O(1) indexing and can rely on the
sequence being immutable at module load — no widget can mutate a
neighbor's animation by rebinding this attribute.
"""


def spinner_frame(tick: int) -> str:
    """Return the spinner glyph for ``tick`` (wraps modulo frame count).

    ``tick`` is a monotonically increasing integer; the caller owns
    the tick source (typically a Textual ``set_interval`` timer).
    Negative ticks wrap the same way positive ones do — Python's
    ``%`` returns a non-negative remainder — so callers do not need
    to guard against timer under/overflow.
    """
    return SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]


__all__ = ["SPINNER_FRAMES", "spinner_frame"]
