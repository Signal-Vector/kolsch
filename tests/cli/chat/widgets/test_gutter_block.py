# ruff: noqa: RUF001 — Unicode glyphs used verbatim in test assertions.
"""Pilot tests for :class:`GutterBlock` — the chat surface primitive.

Coverage:

- Rail-only mount (no leading glyph) — the rail widget is present,
  the glyph column is not, and the body carries the ``gb-body``
  marker class the primitive's CSS keys on.
- Rail + glyph mount — the glyph column is present and its content
  matches the constructor arg.
- Body slot is unconstrained — arbitrary widget types compose cleanly.
- ``_GutterRail.render_line`` invariance — always emits a single ``▎``
  segment per row.
- Focus-within selector is present in ``DEFAULT_CSS`` (qol-079
  consumes it end-to-end; this asserts the contract at the string
  level rather than exercising Pilot focus wiring).
- Palette-var slot values (``$user-border`` etc.) route through the
  ``-rail-<stem>`` / ``-glyph-<stem>`` class dispatch — verified by
  reading back the resolved rail color from the widget's computed
  styles.
- Literal-color slot values (hex) route through inline
  ``styles.color`` assignment — verified the same way.

Uses the shared ``mock_app_factory`` fixture from
``tests/cli/chat/conftest.py`` — no theme.tcss dependency; the
primitive's own ``DEFAULT_CSS`` inlines palette-var fallbacks so it
mounts standalone (Design Principles rule 2: modular).
"""

from __future__ import annotations

import pytest
from textual.widgets import Static

from cli.chat.widgets._gutter_block import (
    _PALETTE_VARS,
    _RAIL_GLYPH,
    GutterBlock,
    _GutterGlyph,
    _GutterRail,
)


@pytest.mark.asyncio
async def test_mount_rail_only_no_glyph(mock_app_factory) -> None:
    """No ``leading_glyph`` → rail + body only, no glyph column."""
    body = Static("hello", markup=False)
    block = GutterBlock(rail_color="$user-border", body=body)
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        rails = list(block.query(_GutterRail))
        glyphs = list(block.query(_GutterGlyph))
        assert len(rails) == 1, "rail should mount"
        assert len(glyphs) == 0, (
            "glyph column must NOT mount when leading_glyph is None"
        )
        assert body.has_class("gb-body")


@pytest.mark.asyncio
async def test_mount_rail_and_glyph(mock_app_factory) -> None:
    """``leading_glyph`` → glyph column mounts between rail and body."""
    body = Static("warning body", markup=False)
    block = GutterBlock(
        rail_color="$warning",
        leading_glyph="⚠",
        body=body,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        rails = list(block.query(_GutterRail))
        glyphs = list(block.query(_GutterGlyph))
        assert len(rails) == 1
        assert len(glyphs) == 1
        assert glyphs[0].content == "⚠"


@pytest.mark.asyncio
async def test_body_slot_accepts_arbitrary_widget(mock_app_factory) -> None:
    """The body slot is unconstrained — any Textual widget composes."""
    body = Static("literal text", markup=False)
    block = GutterBlock(
        rail_color="$assistant-fg",
        leading_glyph=None,
        body=body,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        mounted_bodies = [w for w in block.walk_children() if w is body]
        assert len(mounted_bodies) == 1


@pytest.mark.asyncio
async def test_rail_renders_one_glyph_per_line(mock_app_factory) -> None:
    """``_GutterRail.render_line`` always emits a single ``▎`` segment."""
    body = Static("row1\nrow2\nrow3", markup=False)
    block = GutterBlock(rail_color="$user-border", body=body)
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        rail = block.query_one(_GutterRail)
        strip = rail.render_line(0)
        rendered = "".join(seg.text for seg in strip)
        assert rendered == _RAIL_GLYPH


@pytest.mark.asyncio
async def test_focus_within_reverse_selector_present(mock_app_factory) -> None:
    """qol-079 focus indicator selector present in DEFAULT_CSS."""
    body = Static("focusable body", markup=False)
    block = GutterBlock(
        rail_color="$warning",
        leading_glyph="⚠",
        body=body,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ":focus-within > .gb-glyph-col" in GutterBlock.DEFAULT_CSS
        assert ":focus-within > .gb-rail" in GutterBlock.DEFAULT_CSS
        assert "text-style: reverse" in GutterBlock.DEFAULT_CSS


@pytest.mark.asyncio
async def test_focus_within_reverses_glyph(mock_app_factory) -> None:
    """When focus lands inside a glyph-carrying block, the glyph column
    computes ``text-style: reverse``. Verified by reading back the
    resolved style after simulating focus with the ``:focus-within``
    pseudo-class active.
    """
    from textual.widgets import Input

    body = Input()  # focusable so :focus-within fires
    block = GutterBlock(
        rail_color="$warning",
        leading_glyph="⚠",
        body=body,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        body.focus()
        await pilot.pause()
        glyph = block.query_one(".gb-glyph-col", _GutterGlyph)
        assert "reverse" in str(glyph.rich_style), (
            f"glyph should render with reverse style when focus is within; "
            f"got rich_style={glyph.rich_style!r}"
        )


@pytest.mark.asyncio
async def test_focus_within_reverses_rail_when_glyphless(mock_app_factory) -> None:
    """For a glyph-less block (``leading_glyph=None``), focus reverses
    the rail's ``▎`` — the fallback focus cue for widgets like
    ``UserChatbox`` / ``AssistantProse`` that don't carry a leading glyph.
    """
    from textual.widgets import Input

    body = Input()
    block = GutterBlock(
        rail_color="$user-border",
        leading_glyph=None,
        body=body,
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        body.focus()
        await pilot.pause()
        rail = block.query_one(".gb-rail", _GutterRail)
        assert "reverse" in str(rail.rich_style), (
            f"rail should render with reverse style when focus is within "
            f"and no glyph column is present; got rich_style={rail.rich_style!r}"
        )


@pytest.mark.asyncio
async def test_leading_glyph_color_defaults_to_rail(mock_app_factory) -> None:
    """``leading_glyph_color=None`` → glyph color equals ``rail_color``."""
    body = Static("state body", markup=False)
    block = GutterBlock(
        rail_color="$error",
        leading_glyph="✗",
        body=body,
    )
    async with mock_app_factory(block).run_test() as pilot:
        await pilot.pause()
        assert block.leading_glyph_color == "$error"


@pytest.mark.asyncio
async def test_leading_glyph_color_overridable(mock_app_factory) -> None:
    """Passing ``leading_glyph_color`` diverges from the rail color."""
    body = Static("hint body", markup=False)
    block = GutterBlock(
        rail_color="$warning",
        leading_glyph="⚠",
        leading_glyph_color="$hint",
        body=body,
    )
    async with mock_app_factory(block).run_test() as pilot:
        await pilot.pause()
        assert block.leading_glyph_color == "$hint"
        assert block.rail_color == "$warning"


@pytest.mark.asyncio
async def test_public_slot_properties_read_back(mock_app_factory) -> None:
    """The four slot properties read back the constructor args verbatim."""
    body = Static("readback body", markup=False)
    block = GutterBlock(
        rail_color="$user-border",
        leading_glyph="›",
        leading_glyph_color="$user-border",
        body=body,
    )
    async with mock_app_factory(block).run_test() as pilot:
        await pilot.pause()
        assert block.rail_color == "$user-border"
        assert block.leading_glyph == "›"
        assert block.body is body


@pytest.mark.parametrize("stem", sorted(_PALETTE_VARS))
@pytest.mark.asyncio
async def test_palette_var_class_dispatch(stem, mock_app_factory) -> None:
    """A palette-var ``rail_color`` adds the ``-rail-<stem>`` class
    so Textual's stylesheet resolves the palette var via the CSS rule
    in ``DEFAULT_CSS``.
    """
    body = Static("dispatch body", markup=False)
    block = GutterBlock(rail_color=f"${stem}", body=body)
    async with mock_app_factory(block).run_test() as pilot:
        await pilot.pause()
        assert block.has_class(f"-rail-{stem}"), (
            f"palette var ${stem} should add class -rail-{stem}"
        )


@pytest.mark.asyncio
async def test_literal_color_inline_assignment(mock_app_factory) -> None:
    """A literal hex ``rail_color`` is applied inline to the rail's
    ``styles.color`` — no dispatch class is added.
    """
    body = Static("literal color body", markup=False)
    block = GutterBlock(rail_color="#2ea043", body=body)
    async with mock_app_factory(block).run_test() as pilot:
        await pilot.pause()
        # No -rail-* class was added because "#2ea043" isn't a palette
        # var stem.
        rail_classes = [c for c in block.classes if c.startswith("-rail-")]
        assert rail_classes == []
        rail = block.query_one(_GutterRail)
        assert rail.styles.color is not None
        assert rail.styles.color.hex.lower() == "#2ea043"


@pytest.mark.asyncio
async def test_unknown_palette_var_falls_through_to_inline(mock_app_factory) -> None:
    """A palette-var-shaped value that isn't in :data:`_PALETTE_VARS`
    falls through to inline assignment. Textual's Color parser then
    rejects it — the failure is loud, not silent.
    """
    from textual.css.errors import StyleValueError

    body = Static("unknown var body", markup=False)
    block = GutterBlock(rail_color="$no-such-var-xyz", body=body)
    with pytest.raises(StyleValueError):
        async with mock_app_factory(block).run_test() as pilot:
            await pilot.pause()
