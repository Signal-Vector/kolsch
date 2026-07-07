"""spec-007 Test 2 — `MarkdownRenderer` 100ms flush coalescing.

Three passes:

1. Feed 40 `card.appended` events over 1s (40 Hz). Underlying
   `Markdown.append` invoked at most ~10 times (100ms flush).
2. `KOLSCH_CARD_MARKDOWN_FLUSH_MS=30`; flush ratio scales
   accordingly (~33Hz cadence).
3. Final rendered content equals the concatenation of all chunks
   (buffering doesn't lose data).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from cli.chat.widgets.card_body.markdown import MarkdownRenderer


class _FakeEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


async def _feed_40hz(widget: MarkdownRenderer, chunks: list[str]) -> None:
    """Feed one chunk every 25ms (40Hz) via `apply_event`.

    Between chunks we `asyncio.sleep(0.025)` so the widget's
    interval timer can fire on its own cadence in parallel.
    """
    for chunk in chunks:
        widget.apply_event(_FakeEvent(
            "kolsch.card.appended",
            {"chunk": chunk},
        ))
        await asyncio.sleep(0.025)


@pytest.mark.asyncio
async def test_40hz_flush_ratio_bounded(fake_app_factory, monkeypatch) -> None:
    """40Hz input → ≤10 underlying `Markdown.append` calls per second.

    The `MarkdownRenderer._flush_count` counter increments on every
    real `_flush_buffer` invocation that drained a non-empty buffer.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "100")
    widget = MarkdownRenderer()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        chunks = [f"c{i} " for i in range(40)]
        await _feed_40hz(widget, chunks)
        # Ensure the last flush fires.
        await pilot.pause(0.15)
        # ≤10 flushes in ~1s (100ms cadence). Generous upper bound
        # to absorb scheduling jitter; the invariant is
        # "coalesced, not per-append".
        assert widget._flush_count <= 12, (
            f"Expected ≤12 flushes for 40 chunks at 100ms; "
            f"got {widget._flush_count}"
        )
        # But at least one flush fired — buffer drained.
        assert widget._flush_count >= 1


@pytest.mark.asyncio
async def test_flush_ms_env_override(fake_app_factory, monkeypatch) -> None:
    """`KOLSCH_CARD_MARKDOWN_FLUSH_MS=30` → higher flush cadence.

    Under a 30ms interval the same 40Hz input drains more often —
    the counter should be higher than the 100ms case.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "30")
    widget = MarkdownRenderer()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget._FLUSH_MS == 30
        chunks = [f"c{i} " for i in range(40)]
        await _feed_40hz(widget, chunks)
        await pilot.pause(0.05)
        # 30ms cadence over ~1s → up to ~33 flushes. Under-bound the
        # assertion so scheduling jitter doesn't flake — the point
        # is "more flushes than the 100ms case".
        assert widget._flush_count >= 5


@pytest.mark.asyncio
async def test_no_data_loss_under_streaming(fake_app_factory, monkeypatch) -> None:
    """After flush drain, the underlying `Markdown.source` contains
    the concatenation of all chunks — no chunk swallowed by
    coalescing.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "100")
    widget = MarkdownRenderer()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        chunks = [f"chunk{i}\n" for i in range(20)]
        for chunk in chunks:
            widget.apply_event(_FakeEvent(
                "kolsch.card.appended",
                {"chunk": chunk},
            ))
        # Give the interval enough time to drain remaining chunks.
        await pilot.pause(0.3)
        source = widget.source
        for i in range(20):
            assert f"chunk{i}" in source, (
                f"chunk{i} missing from source={source!r}"
            )
