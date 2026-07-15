"""Pilot tests for `ToolCallBlock`.

Modularity rule (spec Design Principles #2): the widget is tested
in isolation via `mock_app_factory` from the package conftest —
no other widget, no theme.tcss, no app-level wiring is required
for the assertions below to hold. The widget's `DEFAULT_CSS`
declares fallback variable values so it renders cleanly against
a bare `App`.

What we assert:

1. Constructor accepts ``(tool_name, args, body, failed=False,
   recovery_hint=None)`` and the widget mounts cleanly.
2. The title is exactly ``"● {tool_name}({args})"``.
3. Default state is **collapsed** (``collapsed=True``). The spec
   originally called for expanded-by-default but was reversed
   after the first live smoke test: expanded bodies dominated the
   transcript and pushed assistant prose off-screen.
4. Non-failed default: no ``-failed`` class present.
5. ``failed=True`` adds the ``-failed`` CSS class. No conditional
   Python drives the colour; it's CSS-only.
6. ``recovery_hint`` mounts an extra ``↳ hint: {text}`` Static
   inside the body. Omitting it mounts no hint Static.
7. Pressing ``c`` while the block has focus collapses it. This is
   the widget's declared ``c`` binding, not the parent
   ``Collapsible``'s default enter/space toggle.
8. ``DEFAULT_CSS`` references the theme variables ``$tool-fg`` /
   ``$error`` / ``$hint`` — swap the theme, colours change with
   no widget churn.
"""

from __future__ import annotations

import pytest
from textual.widgets import Static
from textual.widgets._collapsible import CollapsibleTitle

from cli.chat.widgets.tool_call_block import ToolCallBlock


def _body_statics(widget: ToolCallBlock) -> list[Static]:
    """Return every ``Static`` inside the widget that is not the
    title bar. Order matches mount order.
    """

    return [s for s in widget.query(Static) if not isinstance(s, CollapsibleTitle)]


@pytest.mark.asyncio
async def test_tool_call_block_mounts_collapsed_by_default(mock_app_factory) -> None:
    widget = ToolCallBlock("read_file", "path=/tmp/x", "canonical body")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Default is collapsed (reversed from the spec's original
        # expanded-by-default after live-smoke feedback).
        assert widget.collapsed is True
        # Title format is `● tool_name(args)`.
        assert widget.title == "● read_file(path=/tmp/x)"
        # No -failed class on the happy path.
        assert not widget.has_class("-failed")


@pytest.mark.asyncio
async def test_tool_call_block_failed_adds_css_class(mock_app_factory) -> None:
    """`failed=True` adds the ``-failed`` class. Colours change via
    CSS only — no runtime colour attribute is set."""

    widget = ToolCallBlock("write_file", "path=/tmp/x", "denied by authz", failed=True)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.has_class("-failed")
        # Title is unchanged — silhouette preserved.
        assert widget.title == "● write_file(path=/tmp/x)"


@pytest.mark.asyncio
async def test_tool_call_block_body_static_rendered(mock_app_factory) -> None:
    widget = ToolCallBlock("read_file", "path=/tmp/x", "the body text")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        bodies = _body_statics(widget)
        # Exactly one body Static — no hint Static when
        # recovery_hint is not provided.
        assert len(bodies) == 1
        body = bodies[0]
        assert "body" in body.classes
        assert "the body text" in str(body.render())


@pytest.mark.asyncio
async def test_tool_call_block_recovery_hint_renders_arrow_line(
    mock_app_factory,
) -> None:
    """When ``recovery_hint`` is supplied, an extra ``↳ hint: …``
    Static is mounted inside the body with the ``hint`` CSS class."""

    widget = ToolCallBlock(
        "write_file",
        "path=/tmp/x",
        "permission denied",
        failed=True,
        recovery_hint="try with sudo",
    )
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        bodies = _body_statics(widget)
        assert len(bodies) == 2, (
            "expected one body + one hint Static, "
            f"got {[str(b.render()) for b in bodies]}"
        )
        # Order: body first, hint second.
        body, hint = bodies
        assert "body" in body.classes
        assert "hint" in hint.classes
        rendered_hint = str(hint.render())
        assert "↳ hint:" in rendered_hint
        assert "try with sudo" in rendered_hint


@pytest.mark.asyncio
async def test_tool_call_block_no_hint_when_recovery_omitted(
    mock_app_factory,
) -> None:
    widget = ToolCallBlock("read_file", "path=/tmp/x", "output")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        bodies = _body_statics(widget)
        assert len(bodies) == 1
        assert "hint" not in bodies[0].classes


@pytest.mark.asyncio
async def test_tool_call_block_c_binding_collapses(mock_app_factory) -> None:
    """Pressing ``c`` while the block is focused collapses it. The
    binding is declared on `ToolCallBlock`, not inherited from
    `Collapsible`.

    Start from an explicitly expanded state so the `c` binding has
    something to do (default is now collapsed).
    """

    widget = ToolCallBlock("read_file", "path=/tmp/x", "body")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        widget.collapsed = False
        await pilot.pause()
        widget.focus()
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert widget.collapsed is True


@pytest.mark.asyncio
async def test_tool_call_block_default_css_uses_theme_variables(
    mock_app_factory,
) -> None:
    """The widget's ``DEFAULT_CSS`` references the theme variables
    for the palette. This is the modularity contract: theme.tcss
    swaps colours without widget changes."""

    widget = ToolCallBlock("read_file", "path=/tmp/x", "body")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        css = ToolCallBlock.DEFAULT_CSS
        assert "$tool-fg" in css
        assert "$error" in css
        assert "$hint" in css
        # Focus indicator: border-left on :focus-within.
        assert ":focus-within" in css
        assert "border-left" in css
