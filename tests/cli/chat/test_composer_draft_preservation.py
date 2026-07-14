"""spec-014 Test 5 — Composer draft preservation regression guard.

Two passes:

1. Type `"draft"` into the composer. Set `awaiting_approval=True`
   (as happens when a workflow-scoped approval fires). Assert the
   composer's `text` buffer is still `"draft"`.
2. Flip `awaiting_approval=False` (as happens on resolve). Composer
   regains focus; buffer still `"draft"`.

This is a REGRESSION GUARD — existing shipped Textual behaviour
already preserves the `TextArea` buffer through `disabled=True`. No
code fix required; this test locks in the invariant so a future
refactor can't silently drop the draft.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.composer import Composer


@pytest.mark.asyncio
async def test_composer_draft_preserved_through_awaiting_approval(
    mock_app_factory,
) -> None:
    """`awaiting_approval=True` disables the composer WITHOUT clearing text."""
    composer = Composer()
    app = mock_app_factory(composer)
    async with app.run_test() as pilot:
        composer.focus()
        await pilot.pause()
        composer.text = "draft"
        await pilot.pause()
        assert composer.text == "draft"

        # Simulate the approval firing.
        composer.awaiting_approval = True
        await pilot.pause()
        assert composer.disabled is True
        # Buffer STILL intact — Textual doesn't clear disabled TextAreas.
        assert composer.text == "draft"


@pytest.mark.asyncio
async def test_composer_draft_survives_full_approval_cycle(
    mock_app_factory,
) -> None:
    """Type → pause → resolve → text still `"draft"`; composer re-enabled."""
    composer = Composer()
    app = mock_app_factory(composer)
    async with app.run_test() as pilot:
        composer.focus()
        await pilot.pause()
        composer.text = "draft"
        await pilot.pause()

        # Approval fires.
        composer.awaiting_approval = True
        await pilot.pause()
        assert composer.disabled is True
        assert composer.text == "draft"

        # User answers — composer goes back to editable.
        composer.awaiting_approval = False
        await pilot.pause()
        assert composer.disabled is False
        # Draft intact through the full pause/resume cycle.
        assert composer.text == "draft"
