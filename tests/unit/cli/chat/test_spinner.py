"""Unit tests for :mod:`cli.chat.spinner` (qol-076).

The canonical `SPINNER_FRAMES` alphabet replaces the two divergent
per-widget spinner tables (`turn_status.py` — 10-frame dot-cycle,
`workflow_node_row.py` — 8-frame rotating-block). One vocabulary
per axis: only the dot-cycle survives.
"""

from __future__ import annotations

import pytest

from cli.chat.spinner import SPINNER_FRAMES, spinner_frame

# ---------------------------------------------------------------------------
# Frame table content
# ---------------------------------------------------------------------------


_EXPECTED_FRAMES: tuple[str, ...] = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def test_spinner_frames_is_the_canonical_10_frame_dot_cycle() -> None:
    """`SPINNER_FRAMES` matches the qol-076 canonical alphabet exactly."""
    assert SPINNER_FRAMES == _EXPECTED_FRAMES


def test_spinner_frames_is_immutable_tuple() -> None:
    """The frame table is a `tuple` so no widget can mutate it in place."""
    assert isinstance(SPINNER_FRAMES, tuple)


def test_spinner_frames_has_ten_frames() -> None:
    """Ten frames — the 8-frame rotating-block variant is gone."""
    assert len(SPINNER_FRAMES) == 10


# ---------------------------------------------------------------------------
# spinner_frame() indexing + wrap
# ---------------------------------------------------------------------------


def test_spinner_frame_zero_returns_first_frame() -> None:
    """`spinner_frame(0)` returns `⠋` — the canonical resting glyph."""
    assert spinner_frame(0) == "⠋"


def test_spinner_frame_wraps_at_frame_count() -> None:
    """`spinner_frame(10)` wraps back to the first frame (modulo 10)."""
    assert spinner_frame(10) == spinner_frame(0) == "⠋"


@pytest.mark.parametrize(
    ("tick", "expected"),
    [(i, _EXPECTED_FRAMES[i]) for i in range(len(_EXPECTED_FRAMES))],
    ids=[f"tick={i}" for i in range(len(_EXPECTED_FRAMES))],
)
def test_spinner_frame_indexes_each_frame_in_order(tick: int, expected: str) -> None:
    """Every in-range tick maps to its positional frame."""
    assert spinner_frame(tick) == expected


def test_spinner_frame_handles_negative_tick_via_python_modulo() -> None:
    """Python `%` returns a non-negative remainder — negative ticks wrap safely."""
    # -1 % 10 == 9 → last frame.
    assert spinner_frame(-1) == SPINNER_FRAMES[-1]
