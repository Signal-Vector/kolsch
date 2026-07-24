"""Pilot tests for :class:`AssistantProse` (post spec-078 migration).

Post spec-078 the widget is a :class:`Widget` container composing a
:class:`GutterBlock` (rail = ``$assistant-fg``) around a private
:class:`_ProseMarkdown` child. All prior public API is preserved by
delegating to that child:

- :meth:`append` / :meth:`stream` / :attr:`source` still work — they
  now proxy the internal Markdown widget.
- The ``agent_name`` / ``timestamp`` kwargs continue to render the
  identity header, but the header now lives inside the body slot
  (a ``.ap-header`` Static) instead of on Textual's ``border_title``.
- NO_COLOR fenced-code plainification (``_PlainFence``) is preserved.
- JSON auto-wrapping (``_wrap_json_if_detected``) is preserved on
  the constructor + ``append`` paths.

Uses the ``mock_app_factory`` fixture from
``tests/cli/chat/conftest.py`` so the widget is exercised in
isolation without the full ChatApp.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.widgets import Static

from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.assistant_prose import (
    AssistantProse,
    _PlainFence,
    _wrap_json_if_detected,
)


@pytest.mark.asyncio
async def test_mount_yields_empty_markdown(mock_app_factory) -> None:
    """The widget mounts cleanly and starts empty (no source text)."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.source == ""


@pytest.mark.asyncio
async def test_append_grows_content(mock_app_factory) -> None:
    """`append(text)` writes into the underlying markdown document."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("Hello ")
        await widget.append("world.")
        await pilot.pause()
        assert "Hello" in widget.source
        assert "world" in widget.source


@pytest.mark.asyncio
async def test_stream_context_manager_grows_content(mock_app_factory) -> None:
    """`stream()` yields a `MarkdownStream`; writes accumulate in source."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        async with widget.stream() as stream:
            await stream.write("Token1 ")
            await stream.write("Token2 ")
            await stream.write("Token3")
        await pilot.pause()
        source = widget.source
        assert "Token1" in source
        assert "Token2" in source
        assert "Token3" in source


@pytest.mark.asyncio
async def test_stream_stops_on_context_exit(mock_app_factory) -> None:
    """After `stream()` exits, writing to the stream must raise."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test():
        async with widget.stream() as stream:
            await stream.write("chunk")
        with pytest.raises(RuntimeError):
            await stream.write("after-stop")


@pytest.mark.asyncio
async def test_composes_gutter_block_with_assistant_fg_rail(
    mock_app_factory,
) -> None:
    """The widget composes exactly one GutterBlock with rail=$assistant-fg.

    Replaces the pre-migration DEFAULT_CSS check that asserted
    ``border: round $assistant-fg`` — post-migration the visual
    silhouette is delegated to the GutterBlock primitive.
    """
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        blocks = list(widget.query(GutterBlock))
        assert len(blocks) == 1
        assert blocks[0].rail_color == "$assistant-fg"
        assert blocks[0].leading_glyph is None
        # No round frame anywhere.
        assert "border: round" not in AssistantProse.DEFAULT_CSS


@pytest.mark.asyncio
async def test_fenced_code_block_uses_pygments_under_color(
    mock_app_factory, monkeypatch
) -> None:
    """Under the color theme, fenced code blocks get Pygments spans."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("Here is code:\n\n```python\ndef f():\n    return 1\n```\n")
        await pilot.pause()
        from textual.widgets._markdown import MarkdownFence

        fences = list(widget.query(MarkdownFence))
        assert fences, "Expected a MarkdownFence child for the fenced code block"
        assert not isinstance(fences[0], _PlainFence)
        highlighted = fences[0]._highlighted_code
        assert len(highlighted.spans) > 0, (
            "Pygments-highlighted fence should have style spans under color theme"
        )


@pytest.mark.asyncio
async def test_fenced_code_block_plain_under_no_color(
    mock_app_factory, monkeypatch
) -> None:
    """Under NO_COLOR, fenced code blocks are plain (no Pygments spans)."""
    monkeypatch.setenv("NO_COLOR", "1")
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("```python\ndef f():\n    return 1\n```\n")
        await pilot.pause()
        from textual.widgets._markdown import MarkdownFence

        fences = list(widget.query(MarkdownFence))
        assert fences, "Expected a MarkdownFence child under NO_COLOR too"
        assert isinstance(fences[0], _PlainFence)
        highlighted = fences[0]._highlighted_code
        assert highlighted.spans == [], (
            "Under NO_COLOR the fence must have zero style spans "
            f"(Pygments suppressed); got spans={highlighted.spans!r}"
        )


# ---------------------------------------------------------------------------
# JSON auto-wrapping (post-036d follow-up)
# ---------------------------------------------------------------------------


def test_wrap_json_helper_wraps_valid_json_array() -> None:
    """Raw JSON array wraps in ```json fences with indent."""
    text = '[{"a": 1}, {"b": 2}]'
    wrapped = _wrap_json_if_detected(text)
    assert wrapped.startswith("```json\n")
    assert wrapped.endswith("\n```")
    assert '"a": 1' in wrapped
    assert '"b": 2' in wrapped
    assert "\n" in wrapped[7:-4]


def test_wrap_json_helper_wraps_valid_json_object() -> None:
    """Raw JSON object also wraps."""
    text = '{"score": 10, "rationale": "good"}'
    wrapped = _wrap_json_if_detected(text)
    assert wrapped.startswith("```json\n")


def test_wrap_json_helper_passes_prose_through_unchanged() -> None:
    """Plain prose returns unchanged — no false-positive wrap."""
    text = "Hello world."
    assert _wrap_json_if_detected(text) == text


def test_wrap_json_helper_passes_malformed_json_through_unchanged() -> None:
    """A prefix that looks JSON-ish but doesn't parse returns unchanged."""
    text = "[not really json"
    assert _wrap_json_if_detected(text) == text


def test_wrap_json_helper_handles_empty_string() -> None:
    """Empty / whitespace returns unchanged."""
    assert _wrap_json_if_detected("") == ""
    assert _wrap_json_if_detected("   \n\n") == "   \n\n"


def test_wrap_json_helper_skips_bracket_but_non_json() -> None:
    """Text starting with `[` but not JSON returns unchanged."""
    text = "[note] see attached appendix for details"
    assert _wrap_json_if_detected(text) == text


@pytest.mark.asyncio
async def test_constructor_wraps_json_input(mock_app_factory) -> None:
    """`AssistantProse(json_text)` wraps the initial text as fenced code."""
    text = '[{"a": 1}, {"b": 2}]'
    widget = AssistantProse(text)
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "```json" in widget.source
        assert '"a": 1' in widget.source
        assert '"b": 2' in widget.source


@pytest.mark.asyncio
async def test_constructor_leaves_prose_unchanged(mock_app_factory) -> None:
    """Non-JSON initial text renders as regular Markdown."""
    widget = AssistantProse("Just some prose.")
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "```json" not in widget.source
        assert "Just some prose." in widget.source


@pytest.mark.asyncio
async def test_append_wraps_json_input(mock_app_factory) -> None:
    """`append(json_text)` wraps before appending to the document."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append('[{"score": 10}]')
        await pilot.pause()
        assert "```json" in widget.source
        assert '"score": 10' in widget.source


@pytest.mark.asyncio
async def test_append_leaves_prose_unchanged(mock_app_factory) -> None:
    """`append(prose)` passes through — no false wrap."""
    widget = AssistantProse()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("Hello world.")
        await pilot.pause()
        assert "```json" not in widget.source
        assert "Hello world." in widget.source


# ---------------------------------------------------------------------------
# Identity header (qol-081, now inside body per spec-078)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_header_renders_agent_name_and_time(
    mock_app_factory,
) -> None:
    """With ``agent_name`` + ``timestamp`` the in-body header shows both."""
    widget = AssistantProse(
        agent_name="Claude",
        timestamp=datetime(2026, 7, 15, 10, 32, 14, tzinfo=UTC),
    )
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        header = widget.query_one(".ap-header", Static)
        text = header.content
        assert "Claude" in text
        assert "10:32" in text
        assert "·" in text


@pytest.mark.asyncio
async def test_identity_header_falls_back_to_assistant(mock_app_factory) -> None:
    """When ``agent_name`` is ``None`` the header uses ``Assistant``."""
    widget = AssistantProse(
        agent_name=None,
        timestamp=datetime(2026, 7, 15, 10, 32, 0, tzinfo=UTC),
    )
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        header = widget.query_one(".ap-header", Static)
        assert "Assistant" in header.content
        assert "10:32" in header.content


@pytest.mark.asyncio
async def test_identity_header_strips_seconds(mock_app_factory) -> None:
    """Time renders ``HH:MM`` — seconds must be dropped."""
    widget = AssistantProse(
        agent_name="Claude",
        timestamp=datetime(2026, 7, 15, 10, 32, 59, tzinfo=UTC),
    )
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        header = widget.query_one(".ap-header", Static)
        assert "10:32" in header.content
        assert "10:32:59" not in header.content
        assert ":59" not in header.content


@pytest.mark.asyncio
async def test_identity_header_omitted_when_timestamp_missing(
    mock_app_factory,
) -> None:
    """Callers that don't opt in (no ``timestamp``) keep the bare rail."""
    widget = AssistantProse("Some markdown.")
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        headers = list(widget.query(".ap-header"))
        assert headers == [], "no timestamp → no identity header"


def test_identity_header_css_declares_bold_assistant_fg() -> None:
    """CSS gives the in-body header bold ``$assistant-fg``.

    Post-migration the identity header lives inside the GutterBlock's
    body slot as a ``.ap-header`` Static (was Textual's border_title
    before). This test locks in the color+bold contract so a future
    tweak doesn't silently drop the identity into a default grey.
    """
    css = AssistantProse.DEFAULT_CSS
    assert ".ap-header" in css
    assert "color: $assistant-fg" in css
    assert "text-style: bold" in css
