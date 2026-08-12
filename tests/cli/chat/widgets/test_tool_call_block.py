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
6. ``recovery_hint`` reveals the ``↳ hint: {text}`` Static inside
   the body. Omitting it leaves that Static mounted but hidden
   (``display = False``) — spec-130 Step 5's fixup: the hint
   Static is now ALWAYS created at construction time (rather than
   only when ``recovery_hint is not None``) so
   ``ToolCallBlock.set_recovery_hint()`` has a stable, already-
   mounted child to reveal/update post-construction, without
   needing to mount a NEW child into an already-live
   ``Collapsible`` (unsafe post-mount). ``_visible_body_statics``
   below filters to what a user would actually see, which is the
   behavior these tests care about — the hidden-but-present Static
   is an implementation detail, not a regression in visible output.
7. Pressing ``c`` while the block has focus collapses it. This is
   the widget's declared ``c`` binding, not the parent
   ``Collapsible``'s default enter/space toggle.
8. ``DEFAULT_CSS`` references the theme variables ``$tool-fg`` /
   ``$error`` / ``$hint`` — swap the theme, colours change with
   no widget churn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static
from textual.widgets._collapsible import CollapsibleTitle

from cli.chat.widgets.tool_call_block import ToolCallBlock

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


def _body_statics(widget: ToolCallBlock) -> list[Static]:
    """Return every ``Static`` inside the widget that is not the
    title bar. Order matches mount order.
    """

    return [s for s in widget.query(Static) if not isinstance(s, CollapsibleTitle)]


def _visible_body_statics(widget: ToolCallBlock) -> list[Static]:
    """Return only the body/hint ``Static``\\ s a user would actually see.

    spec-130 Step 5's fixup: the hint ``Static`` is now always
    mounted (see module docstring point 6) but hidden via
    ``display = False`` when there's no hint yet. Filtering by
    ``display`` here keeps these tests asserting user-visible
    behavior rather than internal child-count bookkeeping.
    """

    return [s for s in _body_statics(widget) if s.display]


@pytest.mark.asyncio
async def test_tool_call_block_mounts_collapsed_by_default(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    widget = ToolCallBlock("read_file", "path=/tmp/x", "canonical body")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Default is collapsed (reversed from the spec's original
        # expanded-by-default after live-smoke feedback).
        assert widget.collapsed is True
        # Title format is `● tool_name(args)`.
        assert widget.title == "read_file(path=/tmp/x)"
        # No -failed class on the happy path.
        assert not widget.has_class("-failed")


@pytest.mark.asyncio
async def test_tool_call_block_failed_adds_css_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """`failed=True` adds the ``-failed`` class. Colours change via
    CSS only — no runtime colour attribute is set."""

    widget = ToolCallBlock("write_file", "path=/tmp/x", "denied by authz", failed=True)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.has_class("-failed")
        # Title is unchanged — silhouette preserved.
        assert widget.title == "✗ write_file(path=/tmp/x)"


@pytest.mark.asyncio
async def test_tool_call_block_body_static_rendered(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    widget = ToolCallBlock("read_file", "path=/tmp/x", "the body text")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Exactly one VISIBLE Static — the hint Static is mounted
        # (spec-130 Step 5 fixup) but hidden when recovery_hint is
        # not provided. See `_visible_body_statics`.
        bodies = _visible_body_statics(widget)
        assert len(bodies) == 1
        body = bodies[0]
        assert "body" in body.classes
        assert "the body text" in str(body.render())


@pytest.mark.asyncio
async def test_tool_call_block_recovery_hint_renders_arrow_line(
    mock_app_factory: Callable[[Widget], App[None]],
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
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """No hint is visible when ``recovery_hint`` is omitted.

    spec-130 Step 5 fixup: the hint ``Static`` is now always mounted
    (see module docstring point 6), so this asserts it's present-but-
    hidden rather than absent — the user-visible outcome (no hint
    line shown) is unchanged.
    """
    widget = ToolCallBlock("read_file", "path=/tmp/x", "output")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        visible = _visible_body_statics(widget)
        assert len(visible) == 1
        assert "hint" not in visible[0].classes

        all_bodies = _body_statics(widget)
        assert len(all_bodies) == 2, (
            "hint Static should be mounted (hidden), not absent"
        )
        hint_static = next(s for s in all_bodies if "hint" in s.classes)
        assert hint_static.display is False


@pytest.mark.asyncio
async def test_tool_call_block_set_recovery_hint_reveals_hidden_static(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``set_recovery_hint`` reveals + fills the hint Static post-mount.

    spec-130 Step 5's two-phase tool-call fill path calls this on an
    already-mounted block (constructed with no ``recovery_hint``) once
    a ``TOOL_FAILED`` result carrying one arrives — this is the
    widget-level contract that fill path depends on.
    """
    widget = ToolCallBlock("write_file", "path=/tmp/x", "output")
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(_visible_body_statics(widget)) == 1

        widget.set_recovery_hint("try with sudo")
        await pilot.pause()

        visible = _visible_body_statics(widget)
        assert len(visible) == 2
        hint = next(s for s in visible if "hint" in s.classes)
        rendered_hint = str(hint.render())
        assert "↳ hint:" in rendered_hint
        assert "try with sudo" in rendered_hint


@pytest.mark.asyncio
async def test_tool_call_block_set_recovery_hint_none_clears_and_hides(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``set_recovery_hint(None)`` re-hides and clears an already-shown hint."""
    widget = ToolCallBlock(
        "write_file", "path=/tmp/x", "output", recovery_hint="try with sudo"
    )
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(_visible_body_statics(widget)) == 2

        widget.set_recovery_hint(None)
        await pilot.pause()

        assert len(_visible_body_statics(widget)) == 1


@pytest.mark.asyncio
async def test_tool_call_block_set_failed_state_rewrites_title_and_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``set_failed_state`` matches a single-shot ``failed=True`` construction.

    spec-130 Step 5's fill path calls this when a running block
    (constructed with ``failed=False``, no ``✗`` prefix) turns out to
    have failed — the title/class must end up identical to what
    constructing fresh with ``failed=True`` would have produced.
    """
    running = ToolCallBlock("write_file", "path=/tmp/x", "")
    reference = ToolCallBlock("write_file", "path=/tmp/x", "denied", failed=True)
    app = mock_app_factory(running)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert running.title == "write_file(path=/tmp/x)"
        assert not running.has_class("-failed")

        running.set_failed_state("write_file", "path=/tmp/x")
        await pilot.pause()

        assert running.title == reference.title
        assert running.has_class("-failed")


@pytest.mark.asyncio
async def test_tool_call_block_c_binding_collapses(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
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
    mock_app_factory: Callable[[Widget], App[None]],
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
        # spec-078: rail is an always-on `border-left: outer $color`
        # per state class (was `:focus-within` thick-border pre-migration).
        assert "border-left: outer" in css
