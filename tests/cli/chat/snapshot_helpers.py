"""Text-snapshot helper for Textual chat tests.

Captures the app's currently-rendered screen as plain text and diffs
against a checked-in golden. Text-only (no ANSI): enough to catch
mount-tree bugs, visibility bugs, and content bugs — the classes
bug-035 shipped past. SVG snapshots would additionally catch
color/style regressions; not scoped here.

Update goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/test_bug036a_snapshot.py

bug-036a introduces this module; bug-036b + bug-036c consume it
for their own snapshot tests (row-title suffixes; header + bash
command visibility).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cli.chat.spinner import SPINNER_FRAMES

if TYPE_CHECKING:
    from textual.app import App

_SNAPSHOTS_ROOT = Path(__file__).resolve().parents[2] / "snapshots"

# Elapsed-seconds tokens the workflow progress header renders
# (``[N/M]  ▓▓▓▓▓░░  <n>s``). These are wall-clock derived and would
# otherwise churn the golden on every run. Replace with a stable
# ``<ELAPSED>s`` token so goldens compare cleanly.
_ELAPSED_RE = re.compile(r"(\]  \S+\s+)\d+s")

# bug-036c: mask the braille spinner used by ``WorkflowNodeRow``
# collapsed-title glyphs. Frame index depends on how many 0.08s ticks
# fired before the compositor rendered — flaky under Pilot. Replace
# the whole frame with a stable ``⠋`` (frame 0) so goldens stay
# comparable. Frame set imported from :mod:`cli.chat.spinner` so the
# alphabet stays in sync with the source of truth (qol-076 unified
# the 10-frame dot-cycle across the chat surface).
_SPINNER_RE = re.compile(f"[{''.join(SPINNER_FRAMES)}]")

# bug-036d: mask ``You · HH:MM:SS`` timestamps in ``UserChatbox``
# borders. The timestamp is wall-clock derived (or fixture-event
# derived, which drifts with fixture re-generation) — replace with a
# stable ``<TS>`` token so goldens compare cleanly.
_USER_TS_RE = re.compile(r"You · \d{2}:\d{2}:\d{2}")

# bug-043: match 2+ spaces followed by a closing box-drawing char.
# Elapsed-seconds width drift changes container widths, which drifts
# the padding before ``│``/``╯``/``╮`` closers. Collapsing to one
# space stabilizes goldens across runs.
_BOX_TRAILING_WS_RE = re.compile(r"  +([│╯╮])")


def _render_app_text(app: App[object]) -> str:
    """Return the current screen as plain text (styling stripped).

    Uses the screen compositor's ``render_strips`` — the same path
    the terminal actually paints — so mounted widget content shows
    up. ``screen.render_lines`` alone returns blank strips under
    ``run_test`` because the widgets haven't been painted through
    the compositor yet. Trailing whitespace on each line is
    stripped so a padding change doesn't churn the golden.
    """
    compositor = app.screen._compositor
    strips = list(compositor.render_strips())
    rendered = [strip.text.rstrip() for strip in strips]
    text = "\n".join(rendered).rstrip() + "\n"
    text = _ELAPSED_RE.sub(r"\1<ELAPSED>s", text)
    text = _SPINNER_RE.sub("⠋", text)
    text = _USER_TS_RE.sub("You · <TS>", text)
    # bug-043: collapse runs of >=2 spaces before a closing box-drawing
    # char (│, ╯, ╮) to a single space. The container's width depends on
    # max child content width, which drifts run-to-run when elapsed-seconds
    # go from `1s` → `12s` (pre-substitution). Normalization stabilizes
    # goldens without touching internal alignment inside box rows.
    return _BOX_TRAILING_WS_RE.sub(r" \1", text)


def assert_snapshot_matches(app: App[object], name: str) -> None:
    """Compare rendered screen text against ``tests/snapshots/<name>.txt``.

    On mismatch, writes the actual output to
    ``tests/snapshots/<name>.actual.txt`` and fails with a diff
    command the reader can paste.
    """
    actual = _render_app_text(app)
    golden = _SNAPSHOTS_ROOT / f"{name}.txt"
    if os.getenv("SNAPSHOT_UPDATE"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual)
        return
    if not golden.exists():
        actual_path = golden.with_suffix(".actual.txt")
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        actual_path.write_text(actual)
        pytest.fail(
            f"No golden at {golden}. Actual written to {actual_path}. "
            f"Create golden: SNAPSHOT_UPDATE=1 pytest <this file>",
        )
    expected = golden.read_text()
    if actual == expected:
        return
    actual_path = golden.with_suffix(".actual.txt")
    actual_path.write_text(actual)
    pytest.fail(
        f"Snapshot mismatch: {name}\n"
        f"  Expected: {golden}\n"
        f"  Actual:   {actual_path}\n"
        f"  Diff:     diff {golden} {actual_path}\n"
        f"  Update if intended: SNAPSHOT_UPDATE=1 pytest <this file>",
    )
