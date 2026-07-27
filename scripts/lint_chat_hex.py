#!/usr/bin/env python3
"""lint_chat_hex.py — palette-bypass guard for chat widgets.

Fails if any widget under ``src/cli/chat/widgets/`` uses a raw ``#hex``
literal inside a Textual TCSS property-value declaration. Widgets MUST
reference a palette variable (see ``docs/reference/chat-color-guide.md``).

Widget-local ``$var: #hex;`` variable declarations are ALLOWED and
expected — they let widgets render standalone under ``mock_app_factory``
(``tests/cli/chat/conftest.py``), which deliberately does not load
``theme.tcss``. See qol-082 for the convention; the drift-guard
``tests/unit/cli/chat/test_palette_fallback_drift.py`` keeps those
fallbacks honest against the theme.

Strategy (qol-083):

The lint delegates TCSS parsing to Textual's own tokenizer
(``textual.css.tokenize.tokenize``) rather than trying to reproduce
its grammar in a regex. Every widget's ``DEFAULT_CSS`` string is
lifted out of the Python source via ``ast``, tokenized, and walked:
a ``color`` token (hex literal) inside a ``declaration_name`` scope
is a bypass; the same token inside a ``variable_name`` scope is the
canonical fallback and passes silently.

The upside vs. the qol-082 regex: whatever Textual's tokenizer
accepts, the lint understands — new color-accepting properties
(``keyline``, future additions), multi-line property values inside
selector blocks, arbitrary style-token counts before the hex
(``border: heavy dashed thick solid #ff5555;``),
``background: #hex 30%`` shorthands, and nested / pseudo / class /
wildcard selectors are all absorbed for free. The regex approach
shipped in qol-082 needed five rounds of adversarial hardening; each
round closed one grammar gap and left the class of issue open.

Textual's tokenizer refuses a few shapes that would otherwise be
plausible (e.g. a multi-line variable declaration ``$var:\\n #hex;``).
When the tokenizer raises :class:`TokenError` on both the raw string
and the wrapped-in-selector retry, the lint emits a one-line stderr
warning and reports no hits for that string. Textual itself would
refuse to load a widget shipping that CSS, so the widget fails
loudly at mount time — the lint doesn't try to be more permissive
than the parser it delegates to.

Exit 1 with matches printed on any bypass; exit 0 otherwise.

Usage:
    python -m scripts.lint_chat_hex              # scan widgets/
    python -m scripts.lint_chat_hex <path>       # scan a specific path/file
"""

from __future__ import annotations

import ast
import contextlib
import sys
from pathlib import Path

from textual.css.tokenize import tokenize
from textual.css.tokenizer import TokenError

_FIXTURE_WRAPPER_SEL = "_LintFixtureScope"
_FIXTURE_WRAPPER_HEADER_LINES = 1  # `_LintFixtureScope {\n` is 1 line
_FIXTURE_INDENT = "    "

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TARGET = _REPO_ROOT / "src" / "cli" / "chat" / "widgets"


def _default_css_strings(path: Path) -> list[tuple[str, int]]:
    """Return every ``DEFAULT_CSS = "..."`` string in ``path``.

    Yields ``(css_source, base_lineno)`` — the string's body and the
    1-indexed line number of the string literal's opening quote in the
    Python source, so bypass hits can be reported against the widget's
    actual .py line.

    Walks the AST looking for assignments where any target is named
    ``DEFAULT_CSS`` (module-level, class-level, or an annotated
    assignment). Non-string values (e.g. a Path pointing at an
    external stylesheet — no widget does this today, but the shape is
    legal) are skipped.
    """
    strings: list[tuple[str, int]] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id == "DEFAULT_CSS":
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    # `value.lineno` is where the string literal starts.
                    strings.append((value.value, value.lineno))
                break
    return strings


def _tokenize_css(css: str, source_name: str) -> tuple[list, int]:
    """Tokenize ``css`` with a synthetic-selector fallback for bare fragments.

    Real widget ``DEFAULT_CSS`` strings are complete stylesheets with
    selectors + optional top-level ``$var: #hex;`` declarations, and
    tokenize cleanly on the first try.

    Test fixtures (see ``tests/unit/cli/chat/test_lint_chat_hex.py``)
    pass bare declaration bodies like ``"border: round $error;"`` to
    keep the test signal tight — those aren't valid TCSS at the top
    level and raise :class:`TokenError` on first tokenize. When that
    happens we wrap the body in a synthetic selector
    (``_LintFixtureScope { ... }``) and retry. The wrapper's header
    adds one line before the caller's line 1, so we return a
    ``line_offset`` the caller applies when reporting hits.

    If the wrapped retry also raises :class:`TokenError` (the input
    is genuinely unparseable — e.g. a multi-line ``$var:\\n #hex;``
    declaration that Textual's own tokenizer refuses regardless of
    wrapping), we emit a one-line stderr warning and return an empty
    token stream. The lint reports no hits for this string; Textual
    will fail loudly at widget-load time for real widgets that ship
    unparseable CSS, so this is not a silent bypass — it's a
    delegation of the "is this valid TCSS?" question to the parser
    that actually loads the widget.
    """
    try:
        return list(tokenize(css, (source_name, source_name))), 0
    except TokenError:
        wrapped = f"{_FIXTURE_WRAPPER_SEL} {{\n{css}\n}}\n"
        try:
            return list(
                tokenize(wrapped, (source_name, source_name))
            ), -_FIXTURE_WRAPPER_HEADER_LINES
        except TokenError as exc:
            print(
                f"lint_chat_hex: {source_name}: unparseable CSS fragment — "
                f"skipping (Textual will surface at widget-load time): {exc}",
                file=sys.stderr,
            )
            return [], 0


def _bypass_hits(css: str, source_name: str) -> list[tuple[int, str]]:
    """Return ``(line_offset, snippet)`` for every property-value hex in ``css``.

    A ``color`` token (Textual's own tokenizer classification for
    ``#rrggbb`` and ``#rgb`` literals) inside a ``declaration_name``
    scope is a bypass. Inside a ``variable_name`` scope — the ``$var:``
    fallback declaration — it is the canonical qol-082 shape and does
    NOT fire.

    ``line_offset`` is 1-indexed against the CSS string's first line.
    """
    hits: list[tuple[int, str]] = []
    scope: str | None = None  # "declaration" | "variable" | None
    property_name: str = ""
    tokens, wrapper_shift = _tokenize_css(css, source_name)
    for token in tokens:
        if token.name == "declaration_name":
            scope = "declaration"
            # `value` is e.g. ``"color:"``; strip the trailing colon.
            property_name = token.value.rstrip(":")
        elif token.name == "variable_name":
            scope = "variable"
            property_name = ""
        elif token.name in ("declaration_end", "variable_value_end"):
            scope = None
            property_name = ""
        elif token.name == "color" and scope == "declaration":
            # Textual's tokenizer emits `color` for hex literals AND for
            # CSS named colors like `red`. Only hex is the bypass we
            # care about (named colors are ansi-safe defaults); guard
            # on the leading `#`.
            if token.value.startswith("#"):
                # `token.start` is (line, col) 1-indexed inside the CSS
                # string. Line 1 of the string corresponds to the .py
                # line where the opening `"""` sits. `wrapper_shift`
                # subtracts the synthetic header line added for bare
                # fixture bodies (see `_tokenize_css`).
                line_in_css = token.start[0] + wrapper_shift
                snippet = f"{property_name}: {token.value}"
                hits.append((line_in_css, snippet))
    return hits


def _scan(path: Path) -> list[str]:
    """Return ``path:line:content`` hits for every property-value hex in ``path``."""
    hits: list[str] = []
    files = (
        [path]
        if path.is_file() and path.suffix == ".py"
        else sorted(path.rglob("*.py") if path.is_dir() else [])
    )
    for py in files:
        for css, base_lineno in _default_css_strings(py):
            for line_in_css, snippet in _bypass_hits(css, str(py)):
                # A DEFAULT_CSS = """\n...""" opens on `base_lineno` and
                # its body begins on the following line — line 1 of the
                # CSS string is base_lineno + 1 IF the string starts
                # with a newline. Otherwise it's `base_lineno`.
                if css.startswith("\n"):
                    lineno = base_lineno + line_in_css
                else:
                    lineno = base_lineno + line_in_css - 1
                display: Path = py.resolve()
                with contextlib.suppress(ValueError):
                    display = display.relative_to(_REPO_ROOT)
                hits.append(f"{display}:{lineno}:{snippet}")
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
