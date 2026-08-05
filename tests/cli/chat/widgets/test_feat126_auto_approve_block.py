"""feat-126 Test 2 — `ApprovalBlock(auto_approve=True)` self-resolves.

Covers AC #2/#3 at the widget layer, plus the resume guard half of
AC #5:

- With `auto_approve=True` and NO key sent, the mounted block
  resolves itself to a bare `AuthzEffect.ALLOW` (allow-once — not a
  `(effect, Rule)` tuple, so nothing can reach the rule-persistence
  hook), `is_resolved` flips True, and the frozen footer reads
  `✓ Auto-approved (once)` with the `-allowed` class.
- Control: `auto_approve=False` (the default) does NOT resolve
  without a keypress.
- A terminal/replayed block (`terminal_state=` set, bug-056) is never
  auto-resolved even when constructed with `auto_approve=True` —
  `on_mount`'s terminal early-return wins.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from cli.chat.widgets.approval_block import ApprovalBlock
from providers.authz import AuthzEffect

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


def _make_block(
    *,
    auto_approve: bool = False,
    terminal_state: tuple[bool | None, str] | None = None,
) -> ApprovalBlock:
    """Construct a canonical `ApprovalBlock` for these tests."""
    return ApprovalBlock(
        rule_id="rl_feat126",
        action="kolsch/tools/bash",
        target="ls -la",
        auto_approve=auto_approve,
        terminal_state=terminal_state,
    )


@pytest.mark.asyncio
async def test_auto_approve_block_self_resolves_allow_once(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """No keypress → bare `AuthzEffect.ALLOW` + `✓ Auto-approved (once)`."""
    block = _make_block(auto_approve=True)
    app = mock_app_factory(block)
    async with app.run_test():
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        # Bare effect — allow-ONCE. A tuple would mean a persisted Rule
        # travelled back through the callback (AC #3 violation).
        assert result is AuthzEffect.ALLOW
        assert not isinstance(result, tuple)
        assert block.is_resolved is True
        footer = block.body_children[-1]
        assert "✓ Auto-approved (once)" in str(footer.render())
        assert footer.has_class("-allowed")


@pytest.mark.asyncio
async def test_default_block_does_not_auto_resolve(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Control (AC #7): the default path still waits for a keypress."""
    block = _make_block()
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        # A few refresh frames — plenty for a scheduled auto-resolve
        # to have fired if the default path were contaminated.
        for _ in range(3):
            await pilot.pause()
        assert block.is_resolved is False
        assert not block._resolved_event.is_set()


@pytest.mark.asyncio
async def test_terminal_block_never_auto_resolves(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Resume guard (AC #5): terminal_state wins over auto_approve.

    A replayed/historical block must never produce a fresh durable
    answer. Even if a caller passed `auto_approve=True`, `on_mount`'s
    bug-056 terminal early-return runs first, so `_resolved_event` is
    never set (the adapter never spawns a bridge for terminal blocks
    either — this is defence-in-depth).
    """
    block = _make_block(
        terminal_state=(True, "✓ Allowed once"),
        auto_approve=True,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        for _ in range(3):
            await pilot.pause()
        assert not block._resolved_event.is_set()
        # `is_resolved` reports True for terminal blocks (they don't
        # gate the composer) — the invariant here is that the WAITER
        # channel (`_resolved_event`) stays unset so no bridge write
        # could ever fire.
        assert block.is_resolved is True
