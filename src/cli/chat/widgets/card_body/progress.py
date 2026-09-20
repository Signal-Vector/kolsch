"""`ProgressRenderer` — progress card body renderer.

spec-007 §Body renderers. Renders a message line + a
`ProgressBar`. Consumes
`kolsch.card.set_progress` events; snapshots are a no-op (progress
cards carry no cumulative history).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import Vertical
from textual.widgets import ProgressBar, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ProgressRenderer(Vertical):
    """Static message line + Textual `ProgressBar`.

    `set_progress` payload fields:
      - `current: int` — required.
      - `total: int | None` — falsy → indeterminate bar.
      - `message: str | None` — optional status line.
    """

    DEFAULT_CSS = """
    ProgressRenderer {
        height: auto;
        padding: 0;
    }
    ProgressRenderer > Static {
        height: 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 — matches Textual Widget's `id` kwarg
        super().__init__(id=id)
        self._message = Static("", markup=False)
        self._bar = ProgressBar(total=100, show_eta=False)

    def compose(self) -> ComposeResult:
        """Yield the message ``Static`` and the ``ProgressBar``."""
        yield self._message
        yield self._bar

    def apply_event(self, event_data: Any) -> None:
        """Update message + bar on `kolsch.card.set_progress`."""
        kind = getattr(event_data, "kind", None) or event_data.get("kind")
        if kind != "kolsch.card.set_progress":
            return
        payload = getattr(event_data, "payload", None)
        if payload is None:
            payload = event_data.get("payload") or {}
        message = payload.get("message", "")
        if isinstance(message, str):
            self._message.update(message)
        raw_total = payload.get("total")
        raw_current = payload.get("current", 0)
        # `total=None` or `total=0` → indeterminate: pass `None`
        # to Textual so the bar renders its spinner form.
        if raw_total is None or raw_total == 0:
            self._bar.total = None
        else:
            try:
                self._bar.total = float(raw_total)
            except (TypeError, ValueError):
                self._bar.total = None
        try:
            self._bar.progress = float(raw_current)
        except (TypeError, ValueError):
            self._bar.progress = 0.0

    def apply_snapshot(self, content: str) -> None:
        """Progress cards carry no history — snapshots are a no-op."""
        _ = content


__all__ = ["ProgressRenderer"]
