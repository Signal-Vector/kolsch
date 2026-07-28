"""Test 18 — NO_COLOR code blocks render as plain dim monospace.

Mounts an ``AssistantProse`` widget in isolation with a fenced
Python code block under ``NO_COLOR=1`` and asserts that the
fenced-code block class is the ``_PlainFence`` subclass — the same
substitution that suppresses Pygments highlighting.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets._markdown import MarkdownFence

from cli.chat.widgets.assistant_prose import AssistantProse, _PlainFence


class _HostApp(App[None]):
    def __init__(self, widget: AssistantProse) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_no_color_suppresses_pygments_on_fenced_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_block_class`` returns ``_PlainFence`` for fence under NO_COLOR."""
    monkeypatch.setenv("NO_COLOR", "1")
    widget = AssistantProse()
    app = _HostApp(widget)
    async with app.run_test() as pilot:  # noqa: F841
        # Under NO_COLOR the fence block class swaps to _PlainFence,
        # whose highlight() returns a Content with no spans (i.e. no
        # colour tokens).
        assert widget.get_block_class("fence") is _PlainFence
        assert widget.get_block_class("code_block") is _PlainFence
        content = _PlainFence.highlight("def foo():\n    return 1", "python")
        # `Content` supports iteration over its spans; a NO_COLOR
        # fence has no style spans on any token.
        assert not getattr(content, "spans", None)


@pytest.mark.asyncio
async def test_pygments_active_without_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without NO_COLOR set the fence stays as the coloured MarkdownFence."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    widget = AssistantProse()
    app = _HostApp(widget)
    async with app.run_test() as pilot:  # noqa: F841
        assert widget.get_block_class("fence") is MarkdownFence
