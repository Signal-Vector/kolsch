"""Unit tests for :mod:`cli.chat.metrics` (qol-077).

One format per fact. The formatters return the number ONLY —
framing chrome (parens in TurnStatus, separator dots in the
workflow compact summary, brackets NEVER) is the widget's job.
"""

from __future__ import annotations

from cli.chat.metrics import (
    format_bytes,
    format_counts,
    format_duration,
    format_progress,
)


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


def test_format_duration() -> None:
    """Under 60: ``Ns``. At/above 60: ``Mm SSs`` with zero-padded seconds.

    Edge cases: zero, boundary at 60, mid-minute, big minute value,
    and a negative clamp (defensive guard for callers that derive
    elapsed from clock deltas that briefly go negative).
    """
    assert format_duration(0) == "0s"
    assert format_duration(59) == "59s"
    assert format_duration(60) == "1m 00s"
    assert format_duration(65) == "1m 05s"
    assert format_duration(750) == "12m 30s"
    # Negative clamp: no exception, no `-Ns` string.
    assert format_duration(-1) == "0s"


def test_format_duration_sub_second() -> None:
    """qol-085: ``sub_second_precision`` opt-in applies ONLY below 1s.

    Under 1s + precision>0: decimal format (Option B — matches the
    pre-qol-077 bash-timing site's exact behavior). At/above 1s: the
    parameter is inert so a 2-minute bash still renders ``2m 00s``,
    not ``120.0s`` — the drift qol-085 exists to close.
    """
    # Sub-second with precision: decimal-formatted.
    assert format_duration(0.4, sub_second_precision=1) == "0.4s"
    assert format_duration(0.05, sub_second_precision=1) == "0.1s"
    assert format_duration(0.999, sub_second_precision=1) == "1.0s"
    assert format_duration(0.42, sub_second_precision=2) == "0.42s"

    # Default (precision=0) is qol-077 behavior — whole seconds.
    assert format_duration(0.4) == "0s"

    # 1s threshold: parameter is inert; both branches use whole seconds.
    assert format_duration(1.0, sub_second_precision=1) == "1s"
    assert format_duration(1.9, sub_second_precision=1) == "1s"

    # Minute rollover unchanged — parameter must not leak into ≥60s.
    assert format_duration(90, sub_second_precision=1) == "1m 30s"
    assert format_duration(120, sub_second_precision=1) == "2m 00s"

    # Negative clamp still holds with the opt-in — no `-0.5s` string.
    assert format_duration(-0.5, sub_second_precision=1) == "0s"


# ---------------------------------------------------------------------------
# format_counts
# ---------------------------------------------------------------------------


def test_format_counts() -> None:
    """Bare ``done/total`` — no brackets, no padding.

    The bracketed variant was the workflow-card header's old shape;
    qol-077 dropped it so header + compact summary agree.
    """
    assert format_counts(2, 3) == "2/3"
    assert format_counts(0, 0) == "0/0"
    assert format_counts(10, 100) == "10/100"
    # No brackets anywhere in the output.
    assert "[" not in format_counts(2, 3)
    assert "]" not in format_counts(2, 3)


# ---------------------------------------------------------------------------
# format_progress
# ---------------------------------------------------------------------------


def test_format_progress() -> None:
    """Fixed-width ``▓`` filled + ``░`` remainder.

    ``total <= 0`` renders all-empty (avoids div-by-zero on pre-
    ``RUN_STARTED`` header paints). ``done > total`` and ``done <
    0`` clamp within ``width`` so the bar never overflows.
    """
    assert format_progress(0, 4) == "░░░░░░░░"
    assert format_progress(2, 4) == "▓▓▓▓░░░░"
    assert format_progress(4, 4) == "▓▓▓▓▓▓▓▓"
    # total == 0 is the div-by-zero guard — must not raise.
    assert format_progress(0, 0) == "░░░░░░░░"
    # Custom width is honored (used by future gutter blocks).
    assert format_progress(2, 4, width=4) == "▓▓░░"
    # Clamp on over/under: never renders more than `width` cells.
    assert len(format_progress(99, 4)) == 8
    assert len(format_progress(-3, 4)) == 8


# ---------------------------------------------------------------------------
# format_bytes
# ---------------------------------------------------------------------------


def test_format_bytes() -> None:
    """SI-ish (1024) size with 1-decimal precision under 100 per unit.

    No live caller yet — shipped so the next surface (spec-078
    gutter blocks; token counters) reuses the same format.
    """
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1500000) == "1.4 MB"
    assert format_bytes(2_147_483_648) == "2.0 GB"
