#!/usr/bin/env python3
"""lint_chat_hex.py — palette-bypass guard for chat widgets.

Fails if any widget under ``src/cli/chat/widgets/`` uses a raw ``#hex``
in a color-accepting Textual property value (see ``_COLOR_PROPS`` for
the full list — ``color``, ``background``, ``border*``, ``outline*``,
``tint``, ``hatch``, ``scrollbar-*``, ``link-*``). Widgets MUST
reference a palette variable (see ``docs/reference/chat-color-guide.md``).

Widget-local ``$var: #hex;`` fallback declarations are ALLOWED and
expected — they let widgets render standalone under ``mock_app_factory``
(``tests/cli/chat/conftest.py``), which deliberately does not load
``theme.tcss``. See qol-082 for the convention; the drift-guard
``tests/unit/cli/chat/test_palette_fallback_drift.py`` keeps those
fallbacks honest against the theme.

The regex uses a left-side guard ``(^|[^a-z-])`` so it does NOT match
declaration lines like ``$user-border: #2ea043;`` — where ``-border:``
is a suffix substring, not a property name.

Exit 1 with matches printed on any bypass; exit 0 otherwise.

Usage:
    python -m scripts.lint_chat_hex              # scan widgets/
    python -m scripts.lint_chat_hex <path>       # scan a specific path/file
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TARGET = _REPO_ROOT / "src" / "cli" / "chat" / "widgets"

# Every Textual property that accepts a Color value. Sourced from
# ``textual.css._styles_builder.StylesBuilder.process_*`` methods; verify
# with:
#   uv run python -c "from textual.css._styles_builder import StylesBuilder; \
#     print([m for m in dir(StylesBuilder) if m.startswith('process_')])"
# The ``border[a-z-]*`` shortcut in the regex below covers every
# ``border-*`` variant (including ``border-title-color``,
# ``border-subtitle-background``, etc.), so this tuple only enumerates
# the non-border color-accepting properties.
_COLOR_PROPS: tuple[str, ...] = (
    "color",
    "background",
    "background-tint",
    "tint",
    "outline",
    "outline-top",
    "outline-right",
    "outline-bottom",
    "outline-left",
    "hatch",
    "scrollbar-color",
    "scrollbar-color-hover",
    "scrollbar-color-active",
    "scrollbar-corner-color",
    "scrollbar-background",
    "scrollbar-background-hover",
    "scrollbar-background-active",
    "link-color",
    "link-color-hover",
    "link-background",
    "link-background-hover",
)

# `(?:^|[^a-z-])` guards the left side so `$user-border:` (where
# `-border:` is a suffix substring) is NOT matched — only true
# property-name occurrences are hit. `border[a-z-]*` catches every
# `border` variant (border, border-top, border-title-color, ...).
#
# `(?:[a-z]+\s+)*` between the colon and the hex accepts ANY number of
# optional style keywords — mirrors Textual's parser tolerance. Textual's
# actual border/outline grammar (verified via ``textual.css.parse``)
# does NOT cap the style-token count; forms like
# ``border: heavy dashed thick #ff5555;`` (3 tokens) and
# ``border: heavy dashed thick solid #ff5555;`` (4 tokens) parse valid.
# The `[a-z]+\s+` shape must terminate at `#`, so `*` is safe (no
# runaway). Zero-token allowed, so plain `color: #hex;` still matches.
_BYPASS_RE = re.compile(
    r"(?:^|[^a-z-])(?:"
    + "|".join([re.escape(p) for p in _COLOR_PROPS] + [r"border[a-z-]*"])
    + r")\s*:\s*(?:[a-z]+\s+)*#[0-9a-fA-F]{6}"
)

# Python line comments — a leading `#` (possibly indented) means the
# whole line is a comment; the regex would otherwise fire on
# ``# color: #4dd0e1`` (documentation, not live CSS). Skips Python
# comments; does not attempt to parse CSS `/* */` blocks inside
# DEFAULT_CSS strings — Textual TCSS in this codebase doesn't use them.
_PY_COMMENT_RE = re.compile(r"^\s*#")


def _scan(path: Path) -> list[str]:
    """Return `path:line:content` hits for every property-value hex in `path`."""
    hits: list[str] = []
    if path.is_file():
        files = [path] if path.suffix == ".py" else []
    else:
        files = sorted(path.rglob("*.py"))
    for py in files:
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            if _PY_COMMENT_RE.match(line):
                continue
            if _BYPASS_RE.search(line):
                display: Path = py.resolve()
                with contextlib.suppress(ValueError):
                    display = display.relative_to(_REPO_ROOT)
                hits.append(f"{display}:{lineno}:{line.rstrip()}")
    return hits


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else _DEFAULT_TARGET
    if not target.exists():
        print(f"lint_chat_hex: target not found: {target}", file=sys.stderr)
        return 2

    hits = _scan(target)
    if hits:
        for hit in hits:
            print(hit)
        print(file=sys.stderr)
        print(
            f"lint_chat_hex: property-value hex found under {target}", file=sys.stderr
        )
        print(
            "  Route each literal through a palette variable "
            "($user-border / $assistant-fg / $tool-fg / $error / $warning / $hint).",
            file=sys.stderr,
        )
        print("  See docs/reference/chat-color-guide.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
