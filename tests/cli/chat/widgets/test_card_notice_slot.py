"""spec-008 Test 5 — ``CardWidget`` NoticeSlot integration.

Three passes:

1. Mount a ``CardWidget``. Call ``.mount_notice("hi", "info")``.
   ``NoticeSlot`` contains one ``NoticeWidget`` with text ``"hi"``.
2. Warning-kind notice: ``.mount_notice("bad", "warning")``. The
   ``⚠`` indicator appears on the ``CardHeader``.
3. Multiple notices stack in the slot in mount order.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.notice_slot import NoticeSlot


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


async def _mount_card(app: _HostApp) -> CardWidget:
    card = CardWidget(card_id=f"crd_{uuid4()}", kind="markdown", title="t")
    transcript = app.query_one("#transcript", VerticalScroll)
    await transcript.mount(card)
    return card


@pytest.mark.asyncio
async def test_mount_notice_adds_widget_to_slot() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        card = await _mount_card(app)
        await pilot.pause()

        card.mount_notice("hi", "info")
        await pilot.pause()

        slot = card.query_one(NoticeSlot)
        widgets = list(slot.query(NoticeWidget))
        assert len(widgets) == 1
        # qol-080: `.text` includes the leading severity glyph.
        assert widgets[0].text == "ℹ hi"
        assert widgets[0].kind == "info"


@pytest.mark.asyncio
async def test_warning_notice_adds_header_indicator() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        card = await _mount_card(app)
        await pilot.pause()

        # Before: no warning indicator.
        assert card._header._warning_indicator is False

        card.mount_notice("bad", "warning")
        await pilot.pause()

        # After: header carries the ⚠ glyph next to the title.
        assert card._header._warning_indicator is True
        # Rendered text includes the indicator. `Static.render`
        # returns the current content — captures whatever was last
        # passed to `update()`.
        rendered = str(card._header.render())
        assert "⚠" in rendered


@pytest.mark.asyncio
async def test_multiple_notices_stack_in_order() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        card = await _mount_card(app)
        await pilot.pause()

        card.mount_notice("first", "info")
        card.mount_notice("second", "warning")
        card.mount_notice("third", "info")
        await pilot.pause()

        slot = card.query_one(NoticeSlot)
        widgets = list(slot.query(NoticeWidget))
        assert len(widgets) == 3
        # Mount order preserved. qol-080: `.text` carries the leading
        # severity glyph (`ℹ` for info, `⚠` for warning).
        assert widgets[0].text == "ℹ first"
        assert widgets[1].text == "⚠ second"
        assert widgets[2].text == "ℹ third"
