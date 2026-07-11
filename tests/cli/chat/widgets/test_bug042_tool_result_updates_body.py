"""bug-042 regression — ``_populate_tool_call_result`` must update the
body ``Static`` on a ``ToolCallBlock``, not the ``CollapsibleTitle``.

The live-app crash: bug-037 introduced ``_populate_tool_call_result``
on ``WorkflowNodeRow``. It used ``block.query(Static)`` and updated
the first hit, which is Textual's internal ``CollapsibleTitle``
(``markup=True`` by default). The intended target was the body
``Static`` (created with ``markup=False`` since bug-040). Tool output
containing ``[...]`` sequences (URL-encoded blobs, Pydantic errors)
crashed ``Content.from_markup`` at render time.

The fix stashes ``self._body_static`` on ``ToolCallBlock`` and adds
``update_body(text)`` that writes directly to it. This test file
locks in that routing.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets._collapsible import CollapsibleTitle

from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_AppFactory = Callable[[Widget], App[None]]


# Reproductions of the two crash payloads observed in the field.
_URL_ENCODED_BRACKET_TAIL = (
    "%2FZRrhp7ghYJ26c1fhbGDYx%2FE4ZaNjXG4vKh3zCC%2B6DlUpDz8"
    "%2BK6SVW2a9RCOZlJ9RQ4sFOjtLnTChoH7j4xEb9aaMQVTrTq58e2vPDrKmxx"
    "YBgAmzESJhu%2FDgArCSby0Bpcq6YZQ%3D%3D\"}]}\n"
)

_PYDANTIC_ERROR_TAIL = (
    "1 validation error for X\nfield\n  Field required "
    "[type=missing, input_value='test', input_type=str]\n"
)


@pytest.mark.asyncio
async def test_update_body_writes_to_body_static_not_title(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3.1 — ``update_body`` writes to the body Static; title unchanged."""
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=example.com",
        body="original body",
    )
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Capture the CollapsibleTitle label before the update. The
        # label carries the visible title string; a wrong update
        # (pre-fix behavior) would overwrite it.
        title_widget = block.query_one(CollapsibleTitle)
        label_before = title_widget.label

        block.update_body("new body content [with brackets]")
        await pilot.pause()

        # The body Static now carries the new content.
        body_static = block._body_static
        assert isinstance(body_static, Static)
        assert body_static.content == "new body content [with brackets]"

        # The title widget is a different object.
        assert body_static is not title_widget

        # The title label is unchanged — the tool result text did NOT
        # land on the title (pre-fix, ``block.query(Static)`` targeted
        # this widget and overwrote it).
        assert title_widget.label == label_before
        assert "new body content" not in str(title_widget.label)


@pytest.mark.asyncio
async def test_update_body_with_bracket_content_does_not_crash(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3.2 — URL-encoded ``[...]}\\n`` payload survives render."""
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=example.com",
        body="initial",
    )
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()

        block.update_body(_URL_ENCODED_BRACKET_TAIL)
        await pilot.pause()

        # Force a render on the body Static — pre-fix this would
        # dispatch through ``visualize(markup=True)`` on the title
        # (wrong widget) and crash. Post-fix it lands on the body
        # Static (markup=False) and returns cleanly.
        rendered = block._body_static.render()
        assert rendered is not None

        # Expand the block to force the body into the mounted DOM.
        block.collapsed = False
        await pilot.pause()

        assert block.is_mounted


@pytest.mark.asyncio
async def test_update_body_with_pydantic_error_string_does_not_crash(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3.3 — Pydantic error ``[type=X, input_value=...]`` survives render."""
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=example.com",
        body="initial",
    )
    app = fake_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()

        block.update_body(_PYDANTIC_ERROR_TAIL)
        await pilot.pause()

        rendered = block._body_static.render()
        assert rendered is not None

        block.collapsed = False
        await pilot.pause()

        assert block.is_mounted


@pytest.mark.asyncio
async def test_populate_tool_call_result_routes_through_update_body(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3.4 — the row's ``_populate_tool_call_result`` hits the body Static.

    Sanity that the fix wires end to end: build a ``WorkflowNodeRow``,
    mount a ``ToolCallBlock`` inside it, call the private helper with a
    bracket-containing body, and check that (a) the body Static was
    updated and (b) the CollapsibleTitle was not touched.
    """
    row = WorkflowNodeRow(node_name="n")
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=example.com",
        body="original",
    )

    class _Host(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield row

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount(block)
        await pilot.pause()

        title_widget = block.query_one(CollapsibleTitle)
        label_before = title_widget.label

        payload = f"result body {_URL_ENCODED_BRACKET_TAIL}"
        row._populate_tool_call_result(block, payload, failed=False)
        await pilot.pause()

        # Body Static took the update.
        assert block._body_static.content == payload

        # Title label did NOT take the update.
        assert title_widget.label == label_before
        assert "result body" not in str(title_widget.label)


@pytest.mark.asyncio
async def test_populate_tool_call_result_failed_adds_class(
    fake_app_factory: _AppFactory,
) -> None:
    """Regression — the ``failed=True`` branch still adds the CSS class."""
    row = WorkflowNodeRow(node_name="n")
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=example.com",
        body="original",
    )

    class _Host(App[None]):
        def compose(self):  # type: ignore[no-untyped-def]
            yield row

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount(block)
        await pilot.pause()

        row._populate_tool_call_result(block, "err body [x]", failed=True)
        await pilot.pause()

        assert block.has_class("-failed")
        assert block._body_static.content == "err body [x]"
