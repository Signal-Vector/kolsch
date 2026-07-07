"""spec-007 Test 5 — `WorkflowRenderer` stub raises.

Two passes:

1. `apply_event` raises `NotImplementedError` mentioning "spec-013".
2. `apply_snapshot` raises the same.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.card_body.workflow import WorkflowRenderer


def test_apply_event_raises_with_spec_013_hint() -> None:
    renderer = WorkflowRenderer()
    with pytest.raises(NotImplementedError) as excinfo:
        renderer.apply_event({"kind": "kolsch.card.set", "payload": {}})
    assert "spec-013" in str(excinfo.value)


def test_apply_snapshot_raises_with_spec_013_hint() -> None:
    renderer = WorkflowRenderer()
    with pytest.raises(NotImplementedError) as excinfo:
        renderer.apply_snapshot("some content")
    assert "spec-013" in str(excinfo.value)
