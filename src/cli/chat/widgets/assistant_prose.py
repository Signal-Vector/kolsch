"""`AssistantProse` widget — the assistant's streaming markdown response.

Design (see `process/spec-textual-chat-ui.md` → Technical Context →
widget subsection for `AssistantProse`):

- Subclass Textual's `Markdown` widget.
- No border. Foreground colour `$assistant-fg` via `DEFAULT_CSS`.
- Public API:
    * `async with widget.stream() as stream: ...` — context manager
      yielding a `MarkdownStream` for token-by-token append during a
      streaming model response. `Markdown.get_stream` returns a
      `MarkdownStream` started; on exit we call `await stream.stop()`.
    * `async def append(text: str)` — one-shot append. Delegates to
      the underlying `Markdown.append`. Non-streaming providers use
      this on `MODEL_RESPONSE`.
- `NO_COLOR` support: fenced code blocks render as plain dim
  monospace. `MarkdownFence` normally runs Pygments via
  `textual.widgets._markdown.highlight`; we suppress it by
  substituting a `_PlainFence` subclass in the `BLOCKS` mapping when
  `os.environ["NO_COLOR"]` is set. `_PlainFence.highlight` returns a
  `Content` with no spans — the raw code text, no colour.

This widget is framework-aligned (rule 1), self-contained (rule 2),
and delegates streaming + rendering to Textual (rule 3). No custom
markdown parser, no custom stream loop, no per-character styling.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from textual.content import Content
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownFence, MarkdownStream


class _PlainFence(MarkdownFence):
    """Fenced-code block that suppresses Pygments under `NO_COLOR`.

    `Markdown.BLOCKS` maps `"fence"` / `"code_block"` to `MarkdownFence`,
    which calls the module-level `highlight()` (Pygments-driven) inside
    its `highlight` classmethod. We swap the mapping to this subclass
    under `NO_COLOR`; `highlight` here returns a plain `Content` with
    no style spans, so the fenced block renders as monospaced text
    with the widget's ambient `text-style: dim` — no invented colour.
    """

    @classmethod
    def highlight(cls, code: str, language: str) -> Content:  # type: ignore[override]
        # No spans → no colour. Normalise line endings the way the
        # parent does (see `_markdown.highlight`) so line wrapping /
        # width calculations match the coloured variant.
        return Content("\n".join(code.splitlines()))


class AssistantProse(Markdown):
    """Assistant response widget — streaming markdown, no border.

    Reactive attributes: none of our own. Reactive state is fully
    owned by the underlying `Markdown` widget's internal document.
    """

    # Fallback variable defaults so the widget mounts under Pilot
    # without theme.tcss loaded (per the Design Principles' modularity
    # rule: a widget must render standalone). When theme.tcss /
    # mono.tcss ARE loaded by `ChatApp`, their values take precedence
    # because they come later in the cascade.
    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;

    /* Round border in `$assistant-fg` mirrors the `UserChatbox`
     * green box — same silhouette so the transcript reads as a
     * conversation of stacked turn boxes. Padding sits inside the
     * border, giving the markdown a one-column gutter from the
     * frame. `margin: 0 0 1 0` gives a one-row breather between
     * turns. */
    AssistantProse {
        border: round $assistant-fg;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $assistant-fg;
        background: transparent;
        height: auto;
    }
    """

    def get_block_class(self, block_name: str) -> type:  # type: ignore[override]
        """Return the widget class that renders a given markdown block.

        Under `NO_COLOR`, substitute `_PlainFence` for the Pygments-
        highlighted `MarkdownFence` so fenced code blocks render as
        plain monospace with no invented colour. Evaluated per call
        so tests can toggle `NO_COLOR` between runs (Pilot tests are
        function-scoped and may monkeypatch the env).
        """
        if block_name in ("fence", "code_block") and os.environ.get(
            "NO_COLOR"
        ) is not None:
            return _PlainFence
        return Markdown.BLOCKS[block_name]

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[MarkdownStream]:
        """Yield a `MarkdownStream` for token-by-token appends.

        Usage in the event adapter:

            async with prose.stream() as stream:
                async for delta in deltas:
                    await stream.write(delta.text_delta)

        The stream is started by `Markdown.get_stream` and stopped on
        context exit (or on exception). Textual's `MarkdownStream`
        handles the debouncing / batching so that N deltas per second
        collapse to a manageable number of widget refreshes.
        """
        stream = Markdown.get_stream(self)
        try:
            yield stream
        finally:
            await stream.stop()

    async def append(self, text: str) -> None:  # type: ignore[override]
        """Append `text` to the underlying markdown document.

        Thin async wrapper over `Markdown.append` — the base class
        method returns an `AwaitComplete` that we await here so
        callers get a plain coroutine. Non-streaming providers use
        this on `MODEL_RESPONSE`; streaming providers should use
        `stream()` instead so writes are debounced.
        """
        await Markdown.append(self, text)
