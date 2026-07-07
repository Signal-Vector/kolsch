"""`WorkflowRenderer` — STUB body renderer for the `workflow` kind.

spec-007 §Body renderers. Registered in
[`KIND_TO_RENDERER`][cli.chat.widgets.card_body.KIND_TO_RENDERER]
so `Cards.open(kind="workflow")` doesn't raise `KeyError` at widget
dispatch time. Full impl lands with **spec-013** (workflow card
renderer). Both `apply_event` and `apply_snapshot` raise
`NotImplementedError` — a workflow card being fed content before
spec-013 is a caller bug, not a silent fallback.
"""

from __future__ import annotations

from typing import Any

from textual.widgets import Static


_STUB_MSG = "WorkflowRenderer stub; full impl in spec-013"


class WorkflowRenderer(Static):
    """STUB — full impl in spec-013.

    Registered under `"workflow"` in `KIND_TO_RENDERER`. Any
    content-bearing call raises `NotImplementedError` pointing at
    the spec that will replace this class.
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)

    def apply_event(self, event_data: Any) -> None:
        """Stub — raises. Full dispatch lands with spec-013."""
        _ = event_data
        raise NotImplementedError(_STUB_MSG)

    def apply_snapshot(self, content: str) -> None:
        """Stub — raises. Full dispatch lands with spec-013."""
        _ = content
        raise NotImplementedError(_STUB_MSG)


__all__ = ["WorkflowRenderer"]
