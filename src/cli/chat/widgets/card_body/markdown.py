"""`MarkdownRenderer` — buffered `Markdown` body renderer.

spec-007 §Body renderers. Wraps Textual's built-in
[`Markdown`][textual.widgets.Markdown] with a 100ms flush interval
so a 40 Hz `card.appended` stream doesn't repaint the terminal 40
times per second (see spec-007 AC #7 — no visible flicker under
high-frequency streaming).

Every `kolsch.card.appended` chunk lands in an in-memory buffer;
a Textual `set_interval` timer (installed on `on_mount`) drains the
buffer into a single `Markdown.append` call. The flush cadence is
governed by `KOLSCH_CARD_MARKDOWN_FLUSH_MS` (env, default 100).

`kolsch.card.set` and `kolsch.card.snapshot` bypass the buffer —
they replace the whole document via `Markdown.update`. The buffer
is cleared on replace to avoid re-appending post-snapshot content.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from textual.widgets import Markdown

if TYPE_CHECKING:
    from textual.timer import Timer

_DEFAULT_FLUSH_MS = 100
"""Default flush interval (ms). spec-007 §Live markdown coalescing."""

_ENV_FLUSH_MS = "KOLSCH_CARD_MARKDOWN_FLUSH_MS"
"""Override the flush interval at runtime. Test 2 exercises the
override with `KOLSCH_CARD_MARKDOWN_FLUSH_MS=30`.
"""


class MarkdownRenderer(Markdown):
    """Markdown body renderer with 100ms flush coalescing.

    Buffers `card.appended` chunks in memory; drains via
    `set_interval` at `_FLUSH_MS` cadence. `apply_event` /
    `apply_snapshot` are the two entrypoints called by
    `CardWidget.apply_event`.
    """

    DEFAULT_CSS = """
    MarkdownRenderer {
        height: auto;
        background: transparent;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._buffer: list[str] = []
        self._FLUSH_MS = int(
            os.environ.get(_ENV_FLUSH_MS, str(_DEFAULT_FLUSH_MS)),
        )
        self._interval_handle: Timer | None = None
        # Test hook: increments on every `_flush_buffer` call that
        # results in an underlying `Markdown.append` invocation. Test
        # 2 asserts the 40Hz→≤10Hz ratio via this counter.
        self._flush_count: int = 0

    def on_mount(self) -> None:  # type: ignore[override]
        """Install the flush interval on mount.

        The interval fires every `_FLUSH_MS / 1000` seconds; each
        tick drains the buffer into a single `Markdown.append`.
        """
        self._interval_handle = self.set_interval(
            self._FLUSH_MS / 1000.0,
            self._flush_buffer,
        )

    def apply_event(self, event_data: Any) -> None:
        """Route a UI-event data payload to the correct branch.

        `card.appended` → buffer; `card.set` → replace.
        Progress / status / close never route here.
        """
        kind = getattr(event_data, "kind", None) or event_data.get("kind")
        payload = getattr(event_data, "payload", None)
        if payload is None:
            payload = event_data.get("payload") or {}
        if kind == "kolsch.card.appended":
            chunk = payload.get("chunk")
            if isinstance(chunk, str):
                self._buffer.append(chunk)
        elif kind == "kolsch.card.set":
            content = payload.get("content")
            if isinstance(content, str):
                self._buffer = []
                # `Markdown.update` returns an `AwaitComplete` we do
                # not await — the widget's own scheduler mounts the
                # new blocks on the next tick.
                self.update(content)

    def apply_snapshot(self, content: str) -> None:
        """Replace document with `content`; drop any buffered chunks."""
        self._buffer = []
        self.update(content)

    def _flush_buffer(self) -> None:
        """Drain the buffer into `Markdown.append`.

        Called by the `set_interval` timer. No-op when the buffer is
        empty so idle cards don't wake the render pipeline.
        """
        if not self._buffer:
            return
        chunks = "".join(self._buffer)
        self._buffer = []
        self._flush_count += 1
        # Textual's `Markdown.append` returns an `AwaitComplete` we
        # discard — the append mounts on the next render tick.
        self.append(chunks)


__all__ = ["MarkdownRenderer"]
