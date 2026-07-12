"""Pilot tests for :class:`ApprovalArgsRow` multi-line detection — qol-048.

qol-047 shipped width-based overflow detection: chevron + `Tab → expand
args` footer hint appear whenever ``len(target)`` exceeds the row's
available width. That check missed targets whose raw length fits but
which carry embedded newlines (``\n``) — heredoc bash commands, pretty-
printed JSON, etc. Textual's compositor renders those across multiple
rows regardless of width, so the row visibly wraps while the discovery
affordance stays hidden.

qol-048 extends ``_recompute_overflow`` to treat ``"\n" in target`` as
overflow. These tests lock in AC #2, #3, #5 from
``process/qol-048-multiline-args-overflow-detection.md``.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _rendered(widget: ApprovalArgsRow) -> str:
    """Return the text currently displayed by the row's child Static."""
    return str(widget.query_one(".args-line", Static).render())


def _footer_text(block: ApprovalBlock) -> str:
    """Return the current text of the block's footer-help Static."""
    return str(block.query_one(".footer-help", Static).render())


HEREDOC_TARGET = "cat <<EOF\nfoo\nbar\nEOF"


# --------------------------------------------------------------------
# AC #5 — three new tests locking in the multi-line detection
# --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [200, 40, 20])
async def test_multiline_target_shows_chevron_at_any_width(
    mock_app_factory: Callable[[Widget], App],
    width: int,
) -> None:
    """Multi-line targets flag as overflowing regardless of terminal width.

    AC #1 + AC #2 — even at 200 cols (well over the target's 24 chars),
    the presence of ``\n`` triggers ``overflowing=True`` and the chevron
    appears on the collapsed row.
    """
    row = ApprovalArgsRow(HEREDOC_TARGET)
    app = mock_app_factory(row)
    async with app.run_test(size=(width, 24)) as pilot:
        # Force a resize event at the parametrized width so ``on_show``
        # / ``on_resize`` land after the row is composed.
        await pilot.resize_terminal(width, 24)
        await pilot.pause()
        assert row.overflowing is True, (
            f"multi-line target must flag as overflowing at width={width}"
        )
        assert row.expanded is False
        text = _rendered(row)
        assert "▸" in text, (
            f"expected collapsed chevron in rendered text at width={width}, "
            f"got {text!r}"
        )
        assert "▾" not in text


@pytest.mark.asyncio
async def test_multiline_target_expanded_renders_all_lines(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Expanding a multi-line target reveals every ``\\n``-separated line.

    AC #3 — press Tab, then confirm the rendered text contains all four
    content lines of the heredoc target (``cat <<EOF``, ``foo``, ``bar``,
    ``EOF``) plus the expanded chevron ``▾`` on the first row.
    """
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=HEREDOC_TARGET,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        args_row = block.query_one(ApprovalArgsRow)
        assert args_row.overflowing is True
        # Tab is bound at the block level; use it so the test mirrors
        # the real interaction path.
        await pilot.press("tab")
        await pilot.pause()
        assert args_row.expanded is True
        text = _rendered(args_row)
        # First line stays ``Args  ▾``.
        assert text.startswith("Args  ▾"), (
            f"expected first line 'Args  ▾', got {text.splitlines()[0]!r}"
        )
        # All four heredoc lines must appear somewhere in the rendered
        # body. Substring check tolerates the widget's char-wrap
        # indent — the spec only requires presence.
        for expected in ("cat <<EOF", "foo", "bar", "EOF"):
            assert expected in text, (
                f"expected {expected!r} in expanded body, got {text!r}"
            )


@pytest.mark.asyncio
async def test_multiline_target_footer_hint_present(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Mounting an ``ApprovalBlock`` with a multi-line target shows the hint.

    AC #2 tail — ``Tab → expand args`` must appear in the block's footer
    without any user interaction. Uses the existing ``Toggled`` plumbing
    that the block already listens for; no new code path.
    """
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=HEREDOC_TARGET,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        # Even at 200 cols the multi-line target flags as overflowing,
        # so the parent's footer suffix should include the Tab hint.
        args_row = block.query_one(ApprovalArgsRow)
        assert args_row.overflowing is True
        assert "Tab → expand args" in _footer_text(block), (
            f"expected 'Tab → expand args' in footer, got "
            f"{_footer_text(block)!r}"
        )


# --------------------------------------------------------------------
# Reviewer-role paranoia
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crlf_target_treated_as_multiline(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """``\\r\\n`` line endings also trigger overflow (``\\n`` is a substring).

    Reviewer role in the spec asks for this check explicitly — the
    ``"\n" in`` guard picks up ``\r\n`` because ``\n`` is a substring
    of ``\r\n``, but the invariant deserves a lock-in test so a future
    refactor to e.g. ``.splitlines()`` doesn't silently regress it.
    """
    row = ApprovalArgsRow("line1\r\nline2\r\nline3")
    app = mock_app_factory(row)
    async with app.run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        assert row.overflowing is True
        assert "▸" in _rendered(row)


@pytest.mark.asyncio
async def test_single_line_target_at_wide_width_still_no_chevron(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Regression: a short single-line target still has no chevron.

    AC #4 — the newline check must not accidentally flip on for the
    control case. Kept as a paranoia guard alongside the qol-047 tests
    since the new OR-branch is easy to over-eagerly trigger.
    """
    row = ApprovalArgsRow("ls -la")
    app = mock_app_factory(row)
    async with app.run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        assert row.overflowing is False
        text = _rendered(row)
        assert "▸" not in text
        assert "▾" not in text
        assert text == "Args  ls -la"
