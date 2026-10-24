"""spec-007 Test 2 — `MarkdownRenderer` 100ms flush coalescing.

The coalescing property is "one underlying `Markdown.append` per
flush tick, however many chunks arrived since the last tick". These
tests drive the ticks by hand (the widget's interval timer is paused
right after mount and `_flush_buffer()` is called explicitly), so the
result never depends on elapsed wall time (qol-164):

1. 40 chunks in 10 batches of 4, one tick per batch → exactly 10
   `append` calls / flushes; an empty-buffer tick is a no-op.
2. `KOLSCH_CARD_MARKDOWN_FLUSH_MS=30` → the timer is installed at a
   30ms interval (the coalescing logic is interval-independent and
   covered by 1).
3. Final rendered content equals the concatenation of all chunks
   (buffering doesn't lose data), plus one live-timer check that the
   real `set_interval` path drains the buffer on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.widgets.card_body.markdown import MarkdownRenderer

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.pilot import Pilot
    from textual.widget import Widget


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


def _feed(widget: MarkdownRenderer, chunks: list[str]) -> None:
    """Feed `chunks` through `apply_event` with no tick in between."""
    for chunk in chunks:
        widget.apply_event(
            _MockEvent(
                "kolsch.card.appended",
                {"chunk": chunk},
            )
        )


def _pause_flush_timer(widget: MarkdownRenderer) -> None:
    """Assert `on_mount` installed the flush timer, then pause it.

    With the timer paused the test owns every tick; nothing drains the
    buffer behind its back however slow the host is.
    """
    handle = widget._interval_handle
    assert handle is not None, "on_mount did not install the flush interval"
    assert handle._interval == widget._FLUSH_MS / 1000, (
        f"flush interval {handle._interval!r} != _FLUSH_MS/1000 "
        f"({widget._FLUSH_MS / 1000!r})"
    )
    handle.pause()


async def _pump_until(
    pilot: Pilot[Any],
    predicate: Callable[[], bool],
    *,
    what: str,
    max_pauses: int = 200,
) -> None:
    """Pump the event loop until `predicate()` holds, or fail naming `what`."""
    for _ in range(max_pauses):
        if predicate():
            return
        await pilot.pause()
    assert predicate(), f"timed out after {max_pauses} pauses waiting for {what}"


@pytest.mark.asyncio
async def test_40hz_flush_ratio_bounded(
    mock_app_factory: Callable[[Widget], App[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One underlying `Markdown.append` per flush tick, whatever the
    chunk count.

    40 chunks arrive in 10 bursts of 4 (the 40Hz-input / 100ms-tick
    ratio); each tick drains its whole burst into exactly one
    `append`. The `MarkdownRenderer._flush_count` counter increments
    on every `_flush_buffer` invocation that drained a non-empty
    buffer.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "100")
    widget = MarkdownRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget._FLUSH_MS / 1000 == 0.1
        _pause_flush_timer(widget)

        appended: list[str] = []
        real_append = widget.append

        def _spy_append(markdown: str) -> Any:
            appended.append(markdown)
            return real_append(markdown)

        monkeypatch.setattr(widget, "append", _spy_append)

        chunks = [f"c{i} " for i in range(40)]
        batches = [chunks[i : i + 4] for i in range(0, 40, 4)]
        for batch in batches:
            _feed(widget, batch)
            widget._flush_buffer()

        assert widget._flush_count == 10, (
            f"Expected exactly 10 flushes for 10 ticks; got {widget._flush_count}"
        )
        assert appended == ["".join(batch) for batch in batches], (
            f"Expected one append per tick carrying its whole burst; got {appended!r}"
        )

        # A tick on an empty buffer is a no-op.
        widget._flush_buffer()
        assert widget._flush_count == 10
        assert len(appended) == 10

        await pilot.pause()
        assert widget.source == "".join(chunks)


@pytest.mark.asyncio
async def test_flush_ms_env_override(
    mock_app_factory: Callable[[Widget], App[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KOLSCH_CARD_MARKDOWN_FLUSH_MS=30` → the flush timer ticks every 30ms.

    The coalescing per tick is interval-independent (see
    `test_40hz_flush_ratio_bounded`); what the env var controls is the
    interval the timer is installed with.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "30")
    widget = MarkdownRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget._FLUSH_MS == 30
        assert widget._interval_handle is not None
        assert widget._interval_handle._interval == 0.03
        _pause_flush_timer(widget)


@pytest.mark.asyncio
async def test_no_data_loss_under_streaming(
    mock_app_factory: Callable[[Widget], App[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After flush drain, the underlying `Markdown.source` contains
    the concatenation of all chunks — no chunk swallowed by
    coalescing.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "100")
    widget = MarkdownRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        _pause_flush_timer(widget)
        chunks = [f"chunk{i}\n" for i in range(20)]
        _feed(widget, chunks)
        widget._flush_buffer()
        await pilot.pause()
        source = widget.source
        for i in range(20):
            assert f"chunk{i}" in source, f"chunk{i} missing from source={source!r}"


@pytest.mark.asyncio
async def test_live_flush_timer_drains_buffer(
    mock_app_factory: Callable[[Widget], App[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `set_interval` path drains the buffer without a manual tick.

    Lower-bound only (`>= 1` flush): host load can delay the tick but
    never prevent it, so the bounded pump cannot be flaked by a slow
    box.
    """
    monkeypatch.setenv("KOLSCH_CARD_MARKDOWN_FLUSH_MS", "100")
    widget = MarkdownRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        _feed(widget, ["live-chunk\n"])
        await _pump_until(
            pilot,
            lambda: widget._flush_count >= 1,
            what="the live flush timer to drain the buffer",
        )
        assert "live-chunk" in widget.source
