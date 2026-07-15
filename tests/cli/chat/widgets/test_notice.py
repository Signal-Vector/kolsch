"""qol-080: `NoticeWidget` always renders a severity glyph.

Under the redundant-carriers principle (see
`docs/reference/chat-color-guide.md` §"NO_COLOR / mono strategy"),
severity is carried by hue AND glyph AND text-style — every mode.
These tests lock in that the glyph is unconditional (both color and
NO_COLOR) and matches the per-severity mapping.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from cli.chat.widgets.notice import SEVERITY_GLYPHS, NoticeWidget


def test_severity_glyphs_locked() -> None:
    """The per-severity glyph mapping is spec-locked."""
    assert SEVERITY_GLYPHS == {"info": "ℹ", "warning": "⚠", "error": "✗"}


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
def test_glyph_leads_rendered_text_per_severity(kind: str, glyph: str) -> None:
    """Each severity emits its glyph as the leading character in `.text`."""
    widget = NoticeWidget(text="Operation completed", kind=kind)
    assert widget.text.startswith(f"{glyph} "), (
        f"kind={kind!r} expected leading {glyph!r}, got {widget.text!r}"
    )
    assert "Operation completed" in widget.text


def test_unknown_kind_falls_back_to_info_glyph() -> None:
    """Any unknown kind renders with the info glyph (see class docstring)."""
    widget = NoticeWidget(text="Something", kind="mystery")
    assert widget.text.startswith("ℹ ")


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
def test_glyph_present_under_no_color(kind: str, glyph: str) -> None:
    """The glyph is emitted under NO_COLOR — the accessibility principle.

    NO_COLOR only affects theme selection (`ansi_default` for palette
    variables); the widget's constructed body text must be identical.
    """
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        widget = NoticeWidget(text="Operation completed", kind=kind)
    assert widget.text.startswith(f"{glyph} ")


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
def test_glyph_matches_across_color_and_no_color(kind: str, glyph: str) -> None:
    """The glyph is identical between color and NO_COLOR modes."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        color = NoticeWidget(text="Body", kind=kind).text
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        mono = NoticeWidget(text="Body", kind=kind).text

    assert color == mono
    assert color.startswith(f"{glyph} ")


def test_update_text_reapplies_glyph() -> None:
    """Repeat dispatches update text in place — the glyph must survive.

    `event_adapter._handle_notice` calls `widget.update_text(new_text)`
    with the raw payload string on repeat notices (drop rate-limit
    count updates in spec-008 §Drop rate limit). If the glyph were only
    prepended in `__init__`, repeats would strip the accessibility cue.
    """
    widget = NoticeWidget(text="Original", kind="warning")
    assert widget.text.startswith("⚠ ")
    widget.update_text("Rate limit approaching (94/100)")
    assert widget.text == "⚠ Rate limit approaching (94/100)"


def test_kind_property_preserved() -> None:
    """`.kind` still exposes the stored severity."""
    for kind in ("info", "warning", "error"):
        assert NoticeWidget(text="x", kind=kind).kind == kind


@pytest.mark.asyncio
async def test_render_under_pilot(mock_app_factory) -> None:  # noqa: ANN001
    """Widget mounts and renders the prefixed body under Pilot."""
    widget = NoticeWidget(text="session resumed", kind="info")
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = str(widget.render())
        assert "ℹ" in rendered
        assert "session resumed" in rendered
