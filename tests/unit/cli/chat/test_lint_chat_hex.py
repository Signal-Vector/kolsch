"""Fixture tests for ``scripts/lint_chat_hex.py``.

The lint catches raw ``#hex`` literals in Textual color-accepting
property values under ``src/cli/chat/widgets/**``. Since qol-083 it
delegates TCSS parsing to Textual's own ``textual.css.tokenize``
tokenizer, so it inherits the parser's grammar (multi-line values,
arbitrary style-token counts, ``keyline``, ``variable_name`` vs
``declaration_name`` distinction) instead of trying to reproduce it
in a regex.

The historical Round-N comments below (Rounds 2/3/4/5) reference
the qol-082 regex-era shape the fixtures were added to guard
against. They're preserved as documentation of what the tokenizer-
driven lint replaces, and their fixtures still pin real behavior —
each shape must still fire (or pass) after the rewrite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LINT = _REPO_ROOT / "scripts" / "lint_chat_hex.py"


def _run_lint_on(source: str, tmp_path: Path) -> int:
    """Write ``source`` to a fixture .py and run the lint on it, returning exit."""
    fixture = tmp_path / "fixture.py"
    fixture.write_text(source)
    result = subprocess.run(
        [sys.executable, str(_LINT), str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


def test_border_shorthand_hex_fires(tmp_path: Path) -> None:
    """``border: round #ff5555;`` — Textual shorthand with a style token."""
    assert _run_lint_on('DEFAULT_CSS = """border: round #ff5555;"""', tmp_path) == 1


def test_outline_shorthand_hex_fires(tmp_path: Path) -> None:
    """``outline: solid #2ea043;`` — same shape as border shorthand."""
    assert _run_lint_on('DEFAULT_CSS = """outline: solid #2ea043;"""', tmp_path) == 1


def test_hatch_shorthand_hex_fires(tmp_path: Path) -> None:
    """``hatch: cross #ffb454;`` — hatch also takes a style keyword."""
    assert _run_lint_on('DEFAULT_CSS = """hatch: cross #ffb454;"""', tmp_path) == 1


def test_border_shorthand_var_does_not_fire(tmp_path: Path) -> None:
    """``border: round $error;`` — canonical ``$var`` use MUST pass."""
    assert _run_lint_on('DEFAULT_CSS = """border: round $error;"""', tmp_path) == 0


def test_single_token_color_hex_still_fires(tmp_path: Path) -> None:
    """``color: #4dd0e1;`` — regression guard on the plain single-token case."""
    assert _run_lint_on('DEFAULT_CSS = """color: #4dd0e1;"""', tmp_path) == 1


# --- Two-token shorthand (Round 4 fix) ----------------------------------
# Textual accepts a two-token border/outline shorthand
# (`border: heavy dashed #ff5555;`, verified via
# ``textual.css.parse.parse_declarations``). The Round 3 regex allowed
# only ONE optional style token before the hex; Round 4 widens to
# ``{0,2}`` so these forms also fire.


def test_border_two_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``border: heavy dashed #ff5555;`` — Textual two-token shorthand."""
    assert (
        _run_lint_on('DEFAULT_CSS = """border: heavy dashed #ff5555;"""', tmp_path) == 1
    )


def test_outline_two_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``outline: thick dashed #ff5555;`` — outline two-token form."""
    assert (
        _run_lint_on('DEFAULT_CSS = """outline: thick dashed #ff5555;"""', tmp_path)
        == 1
    )


def test_border_top_two_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``border-top: thick solid #ff5555;`` — edge-scoped border, two tokens."""
    assert (
        _run_lint_on('DEFAULT_CSS = """border-top: thick solid #ff5555;"""', tmp_path)
        == 1
    )


# --- N-token shorthand (Round 5 fix) ------------------------------------
# Textual's border/outline grammar does NOT cap style-token count —
# verified empirically via ``textual.css.parse``:
# ``border: heavy dashed thick #ff5555;`` (3 tokens) and
# ``border: heavy dashed thick solid #ff5555;`` (4 tokens) both parse
# valid. Round 4's ``{0,2}`` missed these; Round 5 widens to ``*``
# (unbounded, safe because ``[a-z]+\s+`` must terminate at ``#``).


def test_border_three_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``border: heavy dashed thick #ff5555;`` — Textual absorbs 3 style tokens."""
    assert (
        _run_lint_on(
            'DEFAULT_CSS = """border: heavy dashed thick #ff5555;"""', tmp_path
        )
        == 1
    )


def test_outline_three_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``outline: heavy dashed thick #2ea043;`` — same tolerance on outline."""
    assert (
        _run_lint_on(
            'DEFAULT_CSS = """outline: heavy dashed thick #2ea043;"""', tmp_path
        )
        == 1
    )


def test_border_four_token_shorthand_hex_fires(tmp_path: Path) -> None:
    """``border: heavy dashed thick solid #ff5555;`` — 4 style tokens still fires."""
    assert (
        _run_lint_on(
            'DEFAULT_CSS = """border: heavy dashed thick solid #ff5555;"""',
            tmp_path,
        )
        == 1
    )


def test_border_multi_token_var_does_not_fire(tmp_path: Path) -> None:
    """``border: heavy dashed $error;`` — canonical ``$var``, any-token count."""
    assert (
        _run_lint_on('DEFAULT_CSS = """border: heavy dashed $error;"""', tmp_path) == 0
    )


# --- Python-comment skip (Round 2 fix) ----------------------------------
# The ``_PY_COMMENT_RE`` guard skips lines whose first non-whitespace
# char is ``#`` — documentation lines like ``# color: #4dd0e1`` (in a
# module-level or in-code comment) should NOT trip the lint. Trailing
# comments on a real code line MUST still fire.


def test_python_line_comment_hex_does_not_fire(tmp_path: Path) -> None:
    """``# color: #4dd0e1`` — Python comment, not live CSS."""
    assert _run_lint_on("# color: #4dd0e1", tmp_path) == 0


def test_python_indented_comment_hex_does_not_fire(tmp_path: Path) -> None:
    """``    # color: #4dd0e1`` — indented comment still skipped."""
    assert _run_lint_on("    # color: #4dd0e1", tmp_path) == 0


def test_trailing_comment_on_code_line_still_fires(tmp_path: Path) -> None:
    """``color: #4dd0e1;  # trailing comment`` — real code, comment doesn't shield."""
    assert (
        _run_lint_on(
            'DEFAULT_CSS = """color: #4dd0e1;"""  # trailing comment', tmp_path
        )
        == 1
    )


# --- Extended non-border color-accepting props (Round 2 fix) -----------
# The ``_COLOR_PROPS`` tuple grew in Round 2 to cover every Textual
# property that accepts a Color value (``tint``, ``hatch``,
# ``scrollbar-*``, ``link-*``, ``background-tint``). Fixture-verify each
# newly-covered family so a future trim of ``_COLOR_PROPS`` fails loudly.


def test_tint_hex_fires(tmp_path: Path) -> None:
    """``tint: #4dd0e1;`` — tint is a color-accepting property."""
    assert _run_lint_on('DEFAULT_CSS = """tint: #4dd0e1;"""', tmp_path) == 1


def test_hatch_hex_fires(tmp_path: Path) -> None:
    """``hatch: cross #ffb454;`` — hatch shorthand with style + color."""
    assert _run_lint_on('DEFAULT_CSS = """hatch: cross #ffb454;"""', tmp_path) == 1


def test_scrollbar_color_hex_fires(tmp_path: Path) -> None:
    """``scrollbar-color: #6e7681;`` — one of the ``scrollbar-*`` family."""
    assert _run_lint_on('DEFAULT_CSS = """scrollbar-color: #6e7681;"""', tmp_path) == 1


def test_link_color_hover_hex_fires(tmp_path: Path) -> None:
    """``link-color-hover: #ff5555;`` — ``link-*`` family."""
    assert _run_lint_on('DEFAULT_CSS = """link-color-hover: #ff5555;"""', tmp_path) == 1


def test_background_tint_hex_fires(tmp_path: Path) -> None:
    """``background-tint: #b0c0d5;`` — extended color prop, not just ``background``."""
    assert _run_lint_on('DEFAULT_CSS = """background-tint: #b0c0d5;"""', tmp_path) == 1


# --- Canonical fallback exemption (foundational qol-082 convention) ----
# Widget-local ``$var: #hex;`` fallback declarations are the qol-082
# convention — they let widgets render standalone under
# ``mock_app_factory`` (chat conftest) without ``theme.tcss`` loaded.
# The left-side ``(?:^|[^a-z-])`` guard in the regex ensures these
# declarations (where ``-border:`` / ``-fg:`` are suffix substrings)
# are NOT matched; only true property-name occurrences hit.


def test_user_border_var_fallback_does_not_fire(tmp_path: Path) -> None:
    """``$user-border: #2ea043;`` — widget-local fallback, MUST pass."""
    assert _run_lint_on('DEFAULT_CSS = """$user-border: #2ea043;"""', tmp_path) == 0


def test_assistant_fg_var_fallback_does_not_fire(tmp_path: Path) -> None:
    """``$assistant-fg: #b0c0d5;`` — palette-var fallback, MUST pass."""
    assert _run_lint_on('DEFAULT_CSS = """$assistant-fg: #b0c0d5;"""', tmp_path) == 0


def test_hint_var_fallback_does_not_fire(tmp_path: Path) -> None:
    """``$hint: #6e7681;`` — single-segment palette var, MUST pass."""
    assert _run_lint_on('DEFAULT_CSS = """$hint: #6e7681;"""', tmp_path) == 0


# --- qol-083 gap-closing fixtures --------------------------------------
# qol-082 R5 flagged three residual gaps that the regex approach could
# not fully close: multi-line CSS values (the regex was line-oriented),
# missing `keyline` property (omitted from `_COLOR_PROPS`), and a
# theoretical `$color: #hex;` false-positive (a `$var` named after a
# property tripped the left-side guard). qol-083 rewrote the lint on
# top of Textual's own tokenizer, closing the class of issue — these
# three fixtures pin the specific instances the regex approach missed.


def test_multi_line_border_value_hex_fires(tmp_path: Path) -> None:
    """``border: heavy\\n    #ff5555;`` — CSS value split across lines still fires.

    Textual's parser accepts arbitrary whitespace (including newlines)
    between the style keyword and the color. The qol-082 regex was
    line-oriented and missed this shape; qol-083 delegates to the
    Textual tokenizer, which parses the value regardless of layout.
    """
    css = 'DEFAULT_CSS = """\nWidget {\n    border: heavy\n        #ff5555;\n}\n"""'
    assert _run_lint_on(css, tmp_path) == 1


def test_keyline_hex_fires(tmp_path: Path) -> None:
    """``keyline: thin #ff5555;`` — Textual's ``keyline`` property accepts a color.

    qol-082's ``_COLOR_PROPS`` allowlist omitted ``keyline``. qol-083
    drops the allowlist entirely: the tokenizer emits ``color`` for
    hex literals in ANY declaration scope, so new color-accepting
    properties are absorbed automatically without a maintenance patch.
    """
    css = 'DEFAULT_CSS = """Widget { keyline: thin #ff5555; }"""'
    assert _run_lint_on(css, tmp_path) == 1


# --- Adversarial R1: unparseable CSS must not crash the linter -----------
# qol-083 R1 review probed shapes Textual's own tokenizer refuses.
# The most plausible one — a multi-line variable declaration
# `$var:\n    #hex;` — is invalid TCSS in both bare-top-level and
# selector-wrapped scopes. Textual would refuse to load a widget
# shipping this CSS, so the "should not fire" invariant is really
# "the linter must not crash — Textual will fail loudly at load time".
# Prior behavior: uncaught TokenError propagated to the caller.


def test_bare_multi_line_variable_declaration_does_not_crash(tmp_path: Path) -> None:
    """Bare multi-line ``$var:\\n    #hex;`` — Textual rejects; lint must not crash.

    Verifies the graceful-fallback path in ``_tokenize_css`` when
    both the raw and the wrapped tokenize attempts raise
    ``TokenError``. Exit 0 (no hits reported); Textual would
    surface the parse error at widget-load time.
    """
    css = 'DEFAULT_CSS = """$user-border:\n    #2ea043;"""'
    assert _run_lint_on(css, tmp_path) == 0


def test_wrapped_multi_line_variable_declaration_does_not_crash(tmp_path: Path) -> None:
    """Multi-line ``$var:\\n    #hex;`` inside a selector block — same graceful fallback.

    Textual refuses this shape too; the lint should silently skip it
    rather than crash with an uncaught ``TokenError``.
    """
    css = 'DEFAULT_CSS = """Widget {\n    $x:\n        #2ea043;\n}"""'
    assert _run_lint_on(css, tmp_path) == 0


def test_dollar_color_variable_does_not_fire(tmp_path: Path) -> None:
    """``$color: #ff5555;`` — a var named after a property MUST pass.

    Theoretical false-positive under the qol-082 regex: the left-side
    guard ``(?:^|[^a-z-])`` treated ``$`` as a valid boundary before
    the property-name-lookalike ``color``, so a widget-local var
    literally named ``$color`` would trip the lint. qol-083 relies on
    Textual's ``variable_name`` vs ``declaration_name`` distinction,
    which makes ``$color:`` unambiguously a variable declaration and
    exempts it by construction.
    """
    assert _run_lint_on('DEFAULT_CSS = """$color: #ff5555;"""', tmp_path) == 0
