"""Shared metric formatters for the chat surface (qol-077).

One format per fact:

- ``format_duration`` — whole seconds under 60, ``Mm SSs`` at or above.
  Used everywhere the chat renders a wall-clock elapsed (turn status
  line, workflow-card header, workflow compact summary, terminal-run
  footer, live-workflow strip). qol-085 adds a keyword-only
  ``sub_second_precision`` opt-in that applies ONLY below 1s so
  fast-bash timings can retain a ``0.4s`` signal; the ≥1s branches
  are unchanged.
- ``format_counts`` — bare ``N/M``, no brackets. The workflow-card
  header was the last holdout on bracketed counts; qol-077 dropped
  the brackets so header + compact summary + strip agree.
- ``format_progress`` — fixed-width ``▓``-filled + ``░``-remainder
  bar. Default width matches the pre-existing
  ``WorkflowProgressHeader._BAR_WIDTH`` (8 cells).
- ``format_bytes`` — SI-ish binary size (1024). No live caller in
  this pass, but shipped now so the next widget that surfaces a
  file/token size (spec-078 gutter blocks) reuses the same format
  instead of starting a new drift.

Framing chrome (parens around the turn-status elapsed, separator
dots in the workflow compact summary) stays widget-specific — the
helpers return the number ONLY.
"""

from __future__ import annotations


def format_duration(seconds: float, *, sub_second_precision: int = 0) -> str:
    """Render ``seconds`` as ``Ns`` under 60, ``Mm SSs`` at or above.

    Negative inputs clamp to ``0s`` — a defensive guard for callers
    that derive elapsed from clock deltas that can briefly go
    negative on re-anchoring.

    qol-085: when ``sub_second_precision > 0`` AND ``seconds < 1``,
    render as ``{seconds:.{p}f}s`` (e.g. ``0.4s``) so bash-node
    timings keep their fast-execution signal. The ≥1s branches are
    unaffected — a 2-minute bash still renders ``2m 00s`` regardless
    of ``sub_second_precision``.
    """
    if sub_second_precision > 0 and 0 <= seconds < 1:
        return f"{seconds:.{sub_second_precision}f}s"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s"


def format_counts(done: int, total: int) -> str:
    """Render a bare ``done/total`` — no brackets, no padding."""
    return f"{done}/{total}"


def format_progress(done: int, total: int, width: int = 8) -> str:
    """Render a fixed-``width`` bar of ``▓`` filled + ``░`` remainder.

    ``total <= 0`` renders as all-empty (avoids div-by-zero on
    pre-``RUN_STARTED`` header paints). ``done > total`` and
    ``done < 0`` are clamped so the bar stays within ``width``.
    """
    if total <= 0:
        return "░" * width
    filled = min(width, max(0, round(width * done / total)))
    return "▓" * filled + "░" * (width - filled)


def format_bytes(n: int) -> str:
    """Render ``n`` bytes as a compact SI-ish (1024-based) size.

    Under 1 KB: ``N B`` (no fractional). Otherwise the smallest unit
    that keeps the value under 1024 is picked, with 1-decimal
    precision under 100 and 0-decimal at or above so the string
    stays compact.
    """
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024:
            # 1 decimal for anything with a fraction, none otherwise
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


__all__ = [
    "format_bytes",
    "format_counts",
    "format_duration",
    "format_progress",
]
