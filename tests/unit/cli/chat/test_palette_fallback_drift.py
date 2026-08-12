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

**spec-132 addition — Python-constant hex drift.** ``model_picker.py``
introduced a second, unrelated shape of hardcoded hex: bare Python
constants named ``_<NAME>_HEX`` (e.g. ``_TOOL_FG_HEX = "#4dd0e1"``), used
because `OptionList.Option.prompt` renders via `rich.Text`/`Visual`
directly — Textual never resolves CSS variables (`"$tool-fg"`) against
option content, so the module needs the literal hex as a Python string,
not a CSS fallback declaration. `_VAR_HEX_RE` above only matches
`$var: #hex` (CSS-shaped) syntax, so it can't see these — a second,
narrower regex + test below closes that gap by parsing `_<NAME>_HEX`
constants, deriving the theme var name via the documented naming
convention (`_TOOL_FG_HEX` -> `$tool-fg`), and comparing.
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

# Matches module-level `_FOO_BAR_HEX = "#rrggbb"` constants — the
# Python-side equivalent of a `$var: #hex;` CSS fallback, used where a
# widget renders content outside Textual's CSS cascade (see the module
# docstring's "Python-constant hex drift" note). The captured name
# (`FOO_BAR`) is mapped to its theme variable via
# `_python_const_to_theme_var` below.
_PY_CONST_HEX_RE = re.compile(
    r"^_([A-Z0-9_]+)_HEX\s*=\s*[\"'](#[0-9a-fA-F]{6})[\"']", re.MULTILINE
)


def _theme_values() -> dict[str, str]:
    """Parse ``$var: #hex;`` declarations from ``theme.tcss``."""
    values: dict[str, str] = {}
    for line in THEME.read_text().splitlines():
        m = _VAR_HEX_RE.match(line.lstrip())
        if m:
            values[m.group(1)] = m.group(2).lower()
    return values


def _python_const_to_theme_var(const_name: str) -> str:
    """Derive the theme var a `_<NAME>_HEX` constant should mirror.

    ``TOOL_FG`` -> ``$tool-fg``, ``HINT`` -> ``$hint``, ``WARNING`` ->
    ``$warning`` — lowercase, underscores to hyphens, `$`-prefixed. This
    is the naming convention `model_picker.py`'s `_TOOL_FG_HEX`/
    `_HINT_HEX`/`_WARNING_HEX` already follow (see that module's
    comment directly above their definitions).
    """
    return "$" + const_name.lower().replace("_", "-")


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


def test_widget_python_hex_constants_match_theme() -> None:
    """Every widget-local ``_<NAME>_HEX`` Python constant matches its theme var.

    Covers the gap the CSS-shaped regex above can't see: widgets (e.g.
    `model_picker.py`) that build `rich.Text` styles directly and so
    duplicate a theme hex as a bare Python string constant rather than a
    `DEFAULT_CSS` fallback declaration. Skips any constant whose derived
    name (via `_python_const_to_theme_var`) isn't an actual theme
    variable — same "skip unrelated widget-local vars" policy as the
    CSS-shaped test above.
    """
    theme = _theme_values()
    assert theme, f"expected theme vars under {THEME}, found none"

    checked = 0
    drifts: list[str] = []
    for py in sorted(WIDGETS.rglob("*.py")):
        text = py.read_text()
        for match in _PY_CONST_HEX_RE.finditer(text):
            const_name, hex_ = match.group(1), match.group(2).lower()
            theme_var = _python_const_to_theme_var(const_name)
            if theme_var not in theme:
                continue
            checked += 1
            if theme[theme_var] != hex_:
                drifts.append(
                    f"{py.relative_to(_REPO_ROOT)}: _{const_name}_HEX = {hex_!r} "
                    f"!= theme {theme_var} {theme[theme_var]!r}"
                )

    assert checked > 0, (
        "expected at least one widget-local `_<NAME>_HEX` constant matching a "
        "theme var (e.g. model_picker.py's _TOOL_FG_HEX) — if this fires, "
        "either the pattern was removed (safe to delete this test) or the "
        "regex/naming convention drifted and needs updating"
    )
    assert not drifts, (
        "widget-local Python hex-constant drift from theme:\n" + "\n".join(drifts)
    )
