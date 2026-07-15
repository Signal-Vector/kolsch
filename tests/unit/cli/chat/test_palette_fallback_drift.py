"""Drift-guard: widget-local `$var: #hex;` fallbacks must match theme.tcss.

Per qol-082's palette-fallback convention: every widget that references a
palette variable in its ``DEFAULT_CSS`` also declares a fallback for that
variable (e.g. ``$user-border: #2ea043;``) so the widget renders standalone
under ``mock_app_factory`` (see ``tests/cli/chat/conftest.py``), which
deliberately does *not* load ``theme.tcss``.

The fallback must mirror the value declared in ``theme.tcss`` — a drift
means an app-run and an isolated-test render the widget in different hues.
This test walks every widget-local ``$var: #hex;`` declaration under
``src/cli/chat/widgets/`` and verifies each hex matches the theme.

Widget-local variables that are **not** declared in ``theme.tcss`` (e.g.
``$compact-fg`` in ``compaction_block.py``, ``$card-fg`` in
``card_widget.py``) are skipped here — they're a separate concern (dropped
in qol-074), not fallback drift.
"""

from __future__ import annotations

import re
from pathlib import Path

# Anchor to the repo root via this file's location so the test is
# CWD-independent (tests may be invoked from anywhere).
_REPO_ROOT = Path(__file__).resolve().parents[4]
WIDGETS = _REPO_ROOT / "src" / "cli" / "chat" / "widgets"
THEME = _REPO_ROOT / "src" / "cli" / "chat" / "themes" / "theme.tcss"

_VAR_HEX_RE = re.compile(r"(\$[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})")


def _theme_values() -> dict[str, str]:
    """Parse ``$var: #hex;`` declarations from ``theme.tcss``."""
    values: dict[str, str] = {}
    for line in THEME.read_text().splitlines():
        m = _VAR_HEX_RE.match(line.lstrip())
        if m:
            values[m.group(1)] = m.group(2).lower()
    return values


def test_widget_fallbacks_match_theme() -> None:
    """Every widget-local ``$var: #hex;`` matching a theme var uses the same hex."""
    theme = _theme_values()
    assert theme, f"expected theme vars under {THEME}, found none"

    drifts: list[str] = []
    for py in sorted(WIDGETS.rglob("*.py")):
        text = py.read_text()
        for match in _VAR_HEX_RE.finditer(text):
            var, hex_ = match.group(1), match.group(2).lower()
            if var in theme and theme[var] != hex_:
                drifts.append(
                    f"{py.relative_to(_REPO_ROOT)}: {var} fallback {hex_} "
                    f"!= theme {theme[var]}"
                )

    assert not drifts, "widget-local fallback drift from theme:\n" + "\n".join(drifts)
