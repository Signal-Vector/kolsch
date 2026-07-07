"""Pilot tests for `AssistantProse`.

Per spec (`process/spec-textual-chat-ui.md` → widget subsection):
- Widget mounts as an inline markdown block with a rounded
  ``$assistant-fg`` border. (Was originally borderless — flipped
  during the first live smoke test so the transcript reads as a
  matched conversation of stacked turn boxes.)
- Streaming: `async with widget.stream() as s: await s.write(chunk)`
  grows the rendered document as chunks arrive.
- Color theme: fenced code blocks render with Pygments spans
  (verified by counting styled spans inside the fence's `Content`).
- NO_COLOR: fenced code blocks render with no style spans
  (Pygments suppressed; plain dim monospace).

Uses the `fake_app_factory` fixture from `tests/cli/chat/conftest.py`
so the widget is exercised in isolation without the full ChatApp.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.assistant_prose import AssistantProse, _PlainFence


@pytest.mark.asyncio
async def test_mount_yields_empty_markdown(fake_app_factory) -> None:
    """The widget mounts cleanly and starts empty (no source text)."""
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Freshly mounted `Markdown` has no source content yet.
        assert widget.source == ""


@pytest.mark.asyncio
async def test_append_grows_content(fake_app_factory) -> None:
    """`append(text)` writes into the underlying markdown document."""
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("Hello ")
        await widget.append("world.")
        await pilot.pause()
        assert "Hello" in widget.source
        assert "world" in widget.source


@pytest.mark.asyncio
async def test_stream_context_manager_grows_content(fake_app_factory) -> None:
    """`stream()` yields a `MarkdownStream`; writes accumulate in source."""
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        async with widget.stream() as stream:
            await stream.write("Token1 ")
            await stream.write("Token2 ")
            await stream.write("Token3")
        # `stream()`'s __aexit__ awaits `stream.stop()`, which flushes
        # any pending fragments into `Markdown.append`.
        await pilot.pause()
        source = widget.source
        assert "Token1" in source
        assert "Token2" in source
        assert "Token3" in source


@pytest.mark.asyncio
async def test_stream_stops_on_context_exit(fake_app_factory) -> None:
    """After `stream()` exits, writing to the stream must raise."""
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test():
        async with widget.stream() as stream:
            await stream.write("chunk")
        with pytest.raises(RuntimeError):
            await stream.write("after-stop")


@pytest.mark.asyncio
async def test_default_css_declares_bordered_box(fake_app_factory) -> None:
    """DEFAULT_CSS boxes the assistant response in ``$assistant-fg``.

    Original spec had the widget borderless; the user asked during
    the first live smoke test for a matched pair with UserChatbox
    so the transcript reads as a symmetric conversation of stacked
    turn boxes.
    """
    assert "border: round $assistant-fg" in AssistantProse.DEFAULT_CSS
    assert "$assistant-fg" in AssistantProse.DEFAULT_CSS
    # Ensure the widget still mounts with its own CSS.
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Textual computes styles at mount; if the CSS were malformed
        # the widget would error before this line.
        assert widget.is_mounted


@pytest.mark.asyncio
async def test_fenced_code_block_uses_pygments_under_color(
    fake_app_factory, monkeypatch
) -> None:
    """Under the color theme, fenced code blocks get Pygments spans.

    We verify by rendering a fenced Python block via `append`, then
    inspecting the mounted `MarkdownFence` child's highlighted content.
    Coloured highlight → at least one styled span. This is the
    baseline the NO_COLOR test contrasts against.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("Here is code:\n\n```python\ndef f():\n    return 1\n```\n")
        await pilot.pause()
        from textual.widgets._markdown import MarkdownFence

        fences = list(widget.query(MarkdownFence))
        assert fences, "Expected a MarkdownFence child for the fenced code block"
        # Under the color theme the fence must NOT be our plain subclass.
        assert not isinstance(fences[0], _PlainFence)
        # Pygments produced styled spans for `def` / `return` etc.
        highlighted = fences[0]._highlighted_code
        assert len(highlighted.spans) > 0, (
            "Pygments-highlighted fence should have style spans under color theme"
        )


@pytest.mark.asyncio
async def test_fenced_code_block_plain_under_no_color(
    fake_app_factory, monkeypatch
) -> None:
    """Under NO_COLOR, fenced code blocks are plain (no Pygments spans)."""
    monkeypatch.setenv("NO_COLOR", "1")
    widget = AssistantProse()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await widget.append("```python\ndef f():\n    return 1\n```\n")
        await pilot.pause()
        from textual.widgets._markdown import MarkdownFence

        fences = list(widget.query(MarkdownFence))
        assert fences, "Expected a MarkdownFence child under NO_COLOR too"
        # Our substitute class extends MarkdownFence so `query` still
        # finds it; identity check confirms the swap took effect.
        assert isinstance(fences[0], _PlainFence)
        highlighted = fences[0]._highlighted_code
        assert highlighted.spans == [], (
            "Under NO_COLOR the fence must have zero style spans "
            "(Pygments suppressed); got spans=%r" % (highlighted.spans,)
        )
