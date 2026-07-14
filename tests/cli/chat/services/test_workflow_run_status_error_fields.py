"""bug-025 Test 3 — ``WorkflowRunStatusView.error_message`` + ``failed_node``.

Covers AC #7. Two shape-level tests:

1. ``WorkflowRunStatusView(...)`` without the new fields defaults
   both to ``None`` — pre-bug-025 fixtures stay byte-identical.
2. ``WorkflowsImpl.status(run_id)`` on a registry record whose
   ``error_message`` + ``failed_node`` are populated (e.g. via the
   bridge drain's ``_maybe_capture_error``) surfaces both back in
   the return view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowsImpl,
)
from tools.services.workflows import WorkflowRunStatusView
from workflows.ids import WorkflowRunId


def _rid() -> WorkflowRunId:
    return WorkflowRunId(f"wrk_{uuid4()}")


def test_status_view_defaults_none() -> None:
    """Construct without new fields → both default to ``None``."""
    view = WorkflowRunStatusView(
        run_id=_rid(),
        phase="running",
    )
    assert view.error_message is None
    assert view.failed_node is None


def test_status_view_carries_error_fields_when_supplied() -> None:
    """Explicit construction with the new fields round-trips."""
    view = WorkflowRunStatusView(
        run_id=_rid(),
        phase="error",
        error_message="node 'fetch_jd' raised RuntimeError",
        failed_node="fetch_jd",
    )
    assert view.error_message == "node 'fetch_jd' raised RuntimeError"
    assert view.failed_node == "fetch_jd"


def test_status_populates_error_from_registry() -> None:
    """``status()`` reads ``error_message`` + ``failed_node`` from the record.

    Bypasses the bridge drain by constructing a
    ``WorkflowRunRecord`` directly in the impl's registry — asserts
    the hot-path ``status()`` surface returns both diagnostic fields.
    """
    # Build a headless impl — production deps are `None` because
    # `status()` is a pure registry read on the hot path.
    impl = WorkflowsImpl.__new__(WorkflowsImpl)
    impl._workflow_registry = {}
    impl._closed = False

    rid = _rid()
    record = WorkflowRunRecord(
        run_id=rid,
        ref="kolsch/workflows/failing",
        phase="error",
        card=None,
        created_at=datetime.now(tz=UTC),
        started_at=datetime.now(tz=UTC),
        error_message="node 'X' raised RuntimeError",
        failed_node="X",
    )
    impl._workflow_registry[rid] = record

    view = impl.status(rid)
    assert view.phase == "error"
    assert view.error_message == "node 'X' raised RuntimeError"
    assert view.failed_node == "X"
