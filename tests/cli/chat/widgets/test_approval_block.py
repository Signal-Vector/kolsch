"""Pilot tests for :class:`ApprovalBlock` — the three-step state machine.

Framework-aligned tests: mount the block via ``fake_app_factory``
(shared fixture in ``tests/cli/chat/conftest.py``), drive keys via
``Pilot.press``, and assert on:

- the identity of the current child pane after each state transition,
- the ``PromptCallbackReturn`` yielded by ``resolved()`` at the end
  of each path,
- the presence and CSS class of the frozen footer once resolution
  fires.

Coverage after the Option-C redesign (the ``from`` scope is fixed and
no longer user-editable, so the flow is 3 steps not 4):

- ``y`` on choice pane resolves to ``AuthzEffect.ALLOW``.
- ``n`` on choice pane resolves to ``AuthzEffect.DENY``.
- ``escape`` on choice pane resolves to ``AuthzEffect.DENY``
  (locked coordinator decision #4).
- ``a`` → pattern edit → confirm → save confirm → ``y`` →
  ``(AuthzEffect.ALLOW, Rule)``.
- ``d`` → pattern edit → confirm → save confirm → ``y`` →
  ``(AuthzEffect.DENY, Rule)``.
- ``a`` → pattern edit → escape → back to choice pane.
- ``a`` → pattern → save confirm → ``n`` → resolves as
  ``AuthzEffect.ALLOW`` fall-back (allow-once).
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget

from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane
from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit
from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm
from providers.authz import AuthzEffect, Rule


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _make_block() -> ApprovalBlock:
    """Construct a canonical ``ApprovalBlock`` for tests."""
    return ApprovalBlock(
        rule_id="rl_test",
        action="kolsch/tools/bash",
        target="ls -la",
    )


async def _current_pane(block: ApprovalBlock) -> Widget | None:
    """Return the current active pane, or ``None`` after resolution."""
    for child in reversed(list(block.children)):
        if isinstance(
            child,
            (ApprovalChoicePane, ApprovalPatternEdit, ApprovalSaveConfirm),
        ):
            return child
    return None


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_shows_choice_pane(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """On mount the current pane is an ``ApprovalChoicePane``.

    The block also renders context ``Static``s (Tool / Args / Rule)
    that carry the identity strings the callback was fired with.
    """
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test():
        current = await _current_pane(block)
        assert isinstance(current, ApprovalChoicePane)
        # Context rows must include the identity strings somewhere. Two
        # kinds of context children after qol-047: the ``.context``
        # Statics (Tool / Rule) and the ``ApprovalArgsRow`` widget
        # (Args) — check both.
        from cli.chat.widgets.approval_args_row import ApprovalArgsRow

        parts: list[str] = []
        for child in block.children:
            if child.has_class("context"):
                parts.append(str(child.render()))
            elif isinstance(child, ApprovalArgsRow):
                # Widget renders through a child Static; walk its own
                # subtree for the visible text.
                for sub in child.query("Static"):
                    parts.append(str(sub.render()))
        context_texts = "\n".join(parts)
        assert "rl_test" in context_texts
        assert "kolsch/tools/bash" in context_texts
        assert "ls -la" in context_texts


@pytest.mark.asyncio
async def test_press_y_resolves_allow_once(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``y`` → immediate ``AuthzEffect.ALLOW``; frozen footer mounted."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("y")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert result is AuthzEffect.ALLOW
        footer = block.children[-1]
        assert "✓ Allowed once" in str(footer.render())
        assert footer.has_class("-allowed")


@pytest.mark.asyncio
async def test_press_n_resolves_deny_once(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``n`` → immediate ``AuthzEffect.DENY``; footer has ``.-denied``."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("n")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert result is AuthzEffect.DENY
        footer = block.children[-1]
        assert "✗ Denied once" in str(footer.render())
        assert footer.has_class("-denied")


@pytest.mark.asyncio
async def test_press_escape_on_choice_pane_resolves_deny_once(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """Escape on the choice pane is deny-once per Coordinator decision #4."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("escape")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert result is AuthzEffect.DENY
        footer = block.children[-1]
        assert "✗ Denied once" in str(footer.render())


@pytest.mark.asyncio
async def test_a_swaps_to_pattern_edit(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``a`` swaps the pane to ``ApprovalPatternEdit`` (no resolution)."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        current = await _current_pane(block)
        assert isinstance(current, ApprovalPatternEdit)
        assert not block._resolved_event.is_set()


@pytest.mark.asyncio
async def test_a_confirm_pattern_swaps_to_save_confirm(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``a`` → confirm pattern → ``ApprovalSaveConfirm`` is now the pane."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        # Enter on the pattern Input fires ``Input.Submitted`` which
        # ApprovalPatternEdit lifts into ``Confirmed``.
        await pilot.press("enter")
        await pilot.pause()
        current = await _current_pane(block)
        assert isinstance(current, ApprovalSaveConfirm)
        assert not block._resolved_event.is_set()


@pytest.mark.asyncio
async def test_a_full_path_resolves_allow_with_rule(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``a`` → pattern confirm → save-confirm ``y`` → ``(ALLOW, Rule)``."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")  # confirm pattern
        await pilot.pause()
        # save-confirm pane should now be focused; press y to save.
        save_pane = await _current_pane(block)
        assert isinstance(save_pane, ApprovalSaveConfirm)
        save_pane.focus()
        await pilot.pause()
        await pilot.press("y")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert isinstance(result, tuple)
        effect, rule = result
        assert effect is AuthzEffect.ALLOW
        assert isinstance(rule, Rule)
        assert rule.what == "kolsch/tools/bash"
        assert rule.pattern == "ls -la"
        assert rule.effect == "allow"
        footer = block.children[-1]
        assert "Rule saved" in str(footer.render())
        assert footer.has_class("-allowed")


@pytest.mark.asyncio
async def test_d_full_path_resolves_deny_with_rule(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``d`` → pattern confirm → save-confirm ``y`` → ``(DENY, Rule)``."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        save_pane = await _current_pane(block)
        assert isinstance(save_pane, ApprovalSaveConfirm)
        save_pane.focus()
        await pilot.pause()
        await pilot.press("y")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert isinstance(result, tuple)
        effect, rule = result
        assert effect is AuthzEffect.DENY
        assert isinstance(rule, Rule)
        assert rule.effect == "deny"
        footer = block.children[-1]
        assert "Rule saved" in str(footer.render())
        assert footer.has_class("-denied")


@pytest.mark.asyncio
async def test_escape_in_pattern_edit_returns_to_choice_pane(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """Escape inside ``ApprovalPatternEdit`` swaps back to the choice pane."""
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(await _current_pane(block), ApprovalPatternEdit)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(await _current_pane(block), ApprovalChoicePane)
        assert not block._resolved_event.is_set()


@pytest.mark.asyncio
async def test_save_confirm_n_falls_back_to_allow_once(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """``a`` path, then ``n`` on save-confirm → allow-once fallback.

    Preserves the user's "I'll allow this one" intent when they change
    their mind about writing the rule.
    """
    block = _make_block()
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        pane = await _current_pane(block)
        assert pane is not None
        pane.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        save_pane = await _current_pane(block)
        assert isinstance(save_pane, ApprovalSaveConfirm)
        save_pane.focus()
        await pilot.pause()
        await pilot.press("n")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert result is AuthzEffect.ALLOW
        footer = block.children[-1]
        assert "✓ Allowed once" in str(footer.render())
