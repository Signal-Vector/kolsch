"""Pilot tests for :class:`ApprovalArgsRow` — qol-047.

Framework-aligned tests: mount the widget via ``mock_app_factory``
(shared fixture at ``tests/cli/chat/conftest.py``), drive resize +
click + Tab via ``Pilot``, and assert on:

- ``overflowing`` / ``expanded`` state,
- the rendered text in the child ``Static``,
- the presence / absence of the chevron characters (``▸`` / ``▾``),
- ``Toggled`` messages posted on toggle.

The AC #7 coverage matrix, one test per bullet:

1. ``test_short_target_no_chevron``
2. ``test_long_target_shows_chevron_collapsed``
3. ``test_toggle_expands_row``
4. ``test_toggle_collapses_row``
5. ``test_on_resize_updates_overflow``
6. ``test_click_toggles_expansion``
7. ``test_tab_binding_toggles_when_focused_pane_present``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _rendered(widget: ApprovalArgsRow) -> str:
    """Return the text currently displayed by the row's child Static."""
    return str(widget.query_one(".args-line", Static).render())


# --------------------------------------------------------------------
# AC #7 — Unit tests for ``ApprovalArgsRow``
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_target_no_chevron(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A target that fits inside the row width has no chevron."""
    row = ApprovalArgsRow("ls -la")
    app = mock_app_factory(row)
    async with app.run_test(size=(120, 24)):
        assert row.overflowing is False
        text = _rendered(row)
        assert "▸" not in text
        assert "▾" not in text
        assert "…" not in text
        assert text == "Args  ls -la"


@pytest.mark.asyncio
async def test_long_target_shows_chevron_collapsed(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A target too long for the row ends with ``…▸`` when collapsed."""
    # Force overflow at 40 cols: prefix (6) + 100-char target ≫ 40.
    long_target = "x" * 100
    row = ApprovalArgsRow(long_target)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)):
        assert row.overflowing is True
        assert row.expanded is False
        text = _rendered(row)
        assert text.endswith("…▸"), f"expected trailing '…▸', got tail {text[-4:]!r}"
        assert "▾" not in text


@pytest.mark.asyncio
async def test_toggle_expands_row(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``action_toggle_expand`` on an overflowing row reveals the full target."""
    long_target = "abcdefghij" * 20  # 200 chars — well over 40 wide
    row = ApprovalArgsRow(long_target)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)) as pilot:
        assert row.overflowing is True
        row.action_toggle_expand()
        await pilot.pause()
        assert row.expanded is True
        text = _rendered(row)
        # First line is now ``Args  ▾``.
        assert text.startswith("Args  ▾"), (
            f"expected first line 'Args  ▾', got {text.splitlines()[0]!r}"
        )
        # The full target must be present in the wrapped body somewhere.
        body = text[len("Args  ▾\n") :]
        # Strip indent spaces then join to reassemble.
        joined = "".join(line.lstrip() for line in body.splitlines())
        assert joined == long_target


@pytest.mark.asyncio
async def test_toggle_collapses_row(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Two toggles return the row to collapsed state."""
    row = ApprovalArgsRow("x" * 100)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)) as pilot:
        assert row.overflowing is True
        row.action_toggle_expand()
        await pilot.pause()
        assert row.expanded is True
        row.action_toggle_expand()
        await pilot.pause()
        assert row.expanded is False
        # mypy narrows `expanded` to a Literal via the asserts above and
        # can't see the reactive toggle, so it flags the rest unreachable.
        text = _rendered(row)  # type: ignore[unreachable]  # reactive toggled `expanded` between asserts
        assert text.endswith("…▸")
        assert "▾" not in text


@pytest.mark.asyncio
async def test_on_resize_updates_overflow(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Resizing the terminal flips ``overflowing`` in both directions.

    Start narrow — overflow; widen — overflow gone + chevron gone;
    narrow again — overflow + chevron back.
    """
    target = "x" * 80  # Between 40 and 200 cols
    row = ApprovalArgsRow(target)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)) as pilot:
        assert row.overflowing is True
        assert "▸" in _rendered(row)
        # Widen so the target fits — 80 chars + 6 prefix = 86, use 200.
        await pilot.resize_terminal(200, 24)
        await pilot.pause()
        assert row.overflowing is False
        # mypy narrows `overflowing` to a Literal via the asserts above and
        # can't see the resize recompute, so it flags the rest unreachable.
        text = _rendered(row)  # type: ignore[unreachable]  # resize recomputed `overflowing` between asserts
        assert "▸" not in text and "▾" not in text
        # Narrow again — overflow returns.
        await pilot.resize_terminal(30, 24)
        await pilot.pause()
        assert row.overflowing is True
        assert "▸" in _rendered(row)


@pytest.mark.asyncio
async def test_click_toggles_expansion(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A mouse click on the row toggles expansion (AC #4 unit-side)."""
    row = ApprovalArgsRow("x" * 100)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)) as pilot:
        assert row.overflowing is True
        assert row.expanded is False
        await pilot.click(ApprovalArgsRow)
        await pilot.pause()
        assert row.expanded is True
        # A second click collapses again. mypy narrows `expanded` to a
        # Literal above and can't see the click toggle → unreachable.
        await pilot.click(ApprovalArgsRow)  # type: ignore[unreachable]  # click toggled `expanded` between asserts
        await pilot.pause()
        assert row.expanded is False


@pytest.mark.asyncio
async def test_tab_binding_toggles_when_focused_pane_present(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Tab pressed on the focused choice pane toggles the args row.

    Verifies the ``ApprovalBlock`` Tab binding fires when the focused
    descendant does not consume Tab. Uses a target long enough to
    overflow at Textual's default 80-col Pilot viewport.
    """
    long_target = (
        "curl -X POST https://example.com/api/v1/very/long/path"
        "?query=parameter&another=one&yet=more&because=long "
        '-d \'{"json": "body", "with": "lots", "of": "keys"}\''
    )
    block = ApprovalBlock(
        rule_id="rl_test",
        action="kolsch/tools/bash",
        target=long_target,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(60, 24)) as pilot:
        # Give the row a chance to compute overflow after first frame.
        await pilot.pause()
        args_row = block.query_one(ApprovalArgsRow)
        assert args_row.overflowing is True, (
            "target should overflow the 60-col viewport"
        )
        assert args_row.expanded is False
        # Press Tab — should trigger the block's ``action_toggle_args``
        # via the BINDINGS[tab] entry.
        await pilot.press("tab")
        await pilot.pause()
        assert args_row.expanded is True
        # And a second Tab collapses again. mypy narrows `expanded` to a
        # Literal above and can't see the Tab toggle → unreachable.
        await pilot.press("tab")  # type: ignore[unreachable]  # Tab toggled `expanded` between asserts
        await pilot.pause()
        assert args_row.expanded is False


# --------------------------------------------------------------------
# Extra: chevron char discrimination (Reviewer hint).
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chevron_char_flips_between_toggled_states(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Explicit paranoia — ``▸`` and ``▾`` are distinct chars.

    Reviewer requested: the tests should fail loudly if the wrong
    chevron char is used. This test locks each glyph to its state.
    """
    row = ApprovalArgsRow("x" * 100)
    app = mock_app_factory(row)
    async with app.run_test(size=(40, 24)) as pilot:
        collapsed_text = _rendered(row)
        assert "▸" in collapsed_text
        assert "▾" not in collapsed_text
        row.action_toggle_expand()
        await pilot.pause()
        expanded_text = _rendered(row)
        assert "▾" in expanded_text
        assert "▸" not in expanded_text


# --------------------------------------------------------------------
# Round 2 — Reviewer follow-ups
# --------------------------------------------------------------------


def _footer_text(block: ApprovalBlock) -> str:
    """Return the current text of the block's footer-help Static."""
    return str(block.query_one(".footer-help", Static).render())


@pytest.mark.asyncio
async def test_resize_updates_footer_hint(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Round-2 Fix 1: a resize that flips overflow flips the footer too.

    Reviewer Finding #1 — without a ``Toggled`` post on overflow-flag
    flip, the footer got stuck showing ``Tab → expand args`` after the
    terminal widened enough for the target to fit (and vice-versa).
    """
    long_target = "x" * 120  # Fits at 200 cols; overflows at 40.
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=long_target,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        # Narrow: overflow → footer includes ``Tab → expand args``.
        assert "Tab → expand args" in _footer_text(block)
        # Widen so the target fits — the row's chevron disappears AND
        # the parent's footer suffix drops.
        await pilot.resize_terminal(200, 24)
        await pilot.pause()
        row = block.query_one(ApprovalArgsRow)
        assert row.overflowing is False
        assert "Tab → expand" not in _footer_text(block), (
            f"footer should have shed the args-expand hint, got: "
            f"{_footer_text(block)!r}"
        )
        # Narrow again — overflow returns, footer suffix returns. mypy
        # narrowed `overflowing` to Literal[False] above and can't see the
        # resize recompute, so it flags the re-asserted branch unreachable.
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        assert row.overflowing is True
        assert "Tab → expand args" in _footer_text(block)  # type: ignore[unreachable]  # resize recomputed `overflowing` between asserts


@pytest.mark.asyncio
async def test_resize_from_expanded_wide_snaps_back_to_collapsed(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Round-2 Fix 1 corner: expanded row + widen = collapsed + no hint.

    An expanded body with no chevron would be dead UI — nothing left
    to toggle. The widget snaps ``_expanded`` back to False when
    ``_notify_overflow_change`` sees an expanded+non-overflowing row.
    """
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target="x" * 120,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")  # expand
        await pilot.pause()
        row = block.query_one(ApprovalArgsRow)
        assert row.expanded is True
        await pilot.resize_terminal(200, 24)
        await pilot.pause()
        assert row.overflowing is False
        # mypy narrowed `expanded` to Literal[True] above and can't see the
        # overflow-driven snap-back, so it flags the re-asserted branch
        # unreachable.
        assert row.expanded is False, (
            "expanded state must snap back when overflow disappears"
        )
        assert "Tab" not in _footer_text(block)  # type: ignore[unreachable]  # overflow snap-back reset `expanded` between asserts


# --------------------------------------------------------------------
# Round 2 — Fix 3 — AC #3 cross-pane Tab coverage
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_toggles_args_when_pattern_edit_pane_focused(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """AC #3: Tab still toggles the args row when the pattern pane owns focus.

    Round-2 Fix 3 — advance past the choice pane to
    ``ApprovalPatternEdit`` (which mounts an ``Input``), then press
    Tab. ``Input`` does not bind Tab, so the block's Tab binding
    fires via the bubble-up-to-ancestors mechanism.
    """
    from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit

    long_target = "x" * 120
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=long_target,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        args_row = block.query_one(ApprovalArgsRow)
        assert args_row.overflowing is True
        assert args_row.expanded is False
        # Advance to step 2 — ``a`` swaps in ``ApprovalPatternEdit``.
        await pilot.press("a")
        await pilot.pause()
        # Wait for the pattern pane to be the current focused descendant.
        pane = block.query_one(ApprovalPatternEdit)
        assert pane is not None
        # Press Tab — Input doesn't consume it, so the block's binding fires.
        await pilot.press("tab")
        await pilot.pause()
        assert args_row.expanded is True
        # A second Tab collapses again. mypy narrows `expanded` to a Literal
        # above and can't see the Tab toggle → unreachable.
        await pilot.press("tab")  # type: ignore[unreachable]  # Tab toggled `expanded` between asserts
        await pilot.pause()
        assert args_row.expanded is False


@pytest.mark.asyncio
async def test_tab_toggles_args_when_save_confirm_pane_focused(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """AC #3: Tab still toggles the args row when save-confirm owns focus.

    Round-2 Fix 3 — advance through choice → pattern → save-confirm
    (which binds y/n/enter/escape) and press Tab; the block's binding
    fires because save-confirm doesn't bind Tab.
    """
    from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm

    long_target = "x" * 120
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=long_target,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        args_row = block.query_one(ApprovalArgsRow)
        assert args_row.overflowing is True
        assert args_row.expanded is False
        # Advance: ``a`` → pattern; ``enter`` → save-confirm.
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        pane = block.query_one(ApprovalSaveConfirm)
        assert pane is not None
        # Focus the save-confirm pane so its binding table is the
        # keystroke's starting point; then Tab bubbles up.
        pane.focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert args_row.expanded is True
        # A second Tab collapses again. mypy narrows `expanded` to a Literal
        # above and can't see the Tab toggle → unreachable.
        await pilot.press("tab")  # type: ignore[unreachable]  # Tab toggled `expanded` between asserts
        await pilot.pause()
        assert args_row.expanded is False
