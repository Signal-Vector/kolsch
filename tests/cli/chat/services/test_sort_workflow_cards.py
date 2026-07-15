"""spec-016 Test 1 — `sort_workflow_cards` canonical sort.

Six passes:

1. Three success runs with different `terminal_at` values → sorted
   ASC.
2. One error (unacked) + one success → success first, error at
   boundary.
3. Error acked → moves into terminal group by `terminal_at`.
4. In-progress runs sorted by `started_at` ASC (trigger order).
5. Empty records → empty result.
6. Mixed: 2 success, 1 failed (pinned), 3 in-progress → order:
   successes (ASC terminal_at), failed, in-progress (ASC started_at).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cli.chat.services.workflow_layout import sort_workflow_cards
from cli.chat.services.workflows_impl import WorkflowRunRecord
from tools.services.workflows import WorkflowRunPhase
from workflows.ids import WorkflowRunId

_BASE_TS = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _record(
    *,
    run_id: str,
    phase: WorkflowRunPhase,
    created_offset: int = 0,
    started_offset: int | None = None,
    terminal_offset: int | None = None,
    acked: bool = False,
) -> WorkflowRunRecord:
    """Build a synthetic `WorkflowRunRecord` for sorting assertions."""
    created_at = _BASE_TS + timedelta(seconds=created_offset)
    started_at = (
        _BASE_TS + timedelta(seconds=started_offset)
        if started_offset is not None
        else None
    )
    terminal_at = (
        _BASE_TS + timedelta(seconds=terminal_offset)
        if terminal_offset is not None
        else None
    )
    return WorkflowRunRecord(
        run_id=WorkflowRunId(run_id),
        ref="test/ref",
        phase=phase,
        card=None,
        created_at=created_at,
        started_at=started_at,
        terminal_at=terminal_at,
        acked=acked,
    )


def test_three_successes_sorted_by_terminal_at_asc() -> None:
    a = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000001",
        phase="success",
        terminal_offset=30,
    )
    b = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000002",
        phase="success",
        terminal_offset=10,
    )
    c = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000003",
        phase="success",
        terminal_offset=20,
    )
    result = sort_workflow_cards([a, b, c])
    assert [r.run_id for r in result] == [b.run_id, c.run_id, a.run_id]


def test_error_unacked_pinned_after_successes() -> None:
    success = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000010",
        phase="success",
        terminal_offset=5,
    )
    error = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000011",
        phase="error",
        terminal_offset=10,
    )
    result = sort_workflow_cards([error, success])
    # Success first, then failed pinned at boundary.
    assert [r.run_id for r in result] == [success.run_id, error.run_id]


def test_acked_error_moves_into_terminal_group() -> None:
    success = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000020",
        phase="success",
        terminal_offset=20,
    )
    error_acked = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000021",
        phase="error",
        terminal_offset=5,
        acked=True,
    )
    result = sort_workflow_cards([success, error_acked])
    # Acked error joins terminal group; sort ASC by terminal_at.
    assert [r.run_id for r in result] == [error_acked.run_id, success.run_id]


def test_in_progress_sorted_by_started_at_asc() -> None:
    a = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000030",
        phase="running",
        started_offset=30,
    )
    b = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000031",
        phase="running",
        started_offset=10,
    )
    c = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000032",
        phase="queued",
        started_offset=None,
        created_offset=20,
    )
    result = sort_workflow_cards([a, b, c])
    # b (10s) before a (30s); c queued falls back to created (20s) so
    # order should be: b (10), c (20), a (30).
    assert [r.run_id for r in result] == [b.run_id, c.run_id, a.run_id]


def test_empty_records_returns_empty() -> None:
    assert sort_workflow_cards([]) == []


def test_mixed_layout_ordering() -> None:
    """2 success + 1 failed + 3 in-progress → canonical order."""
    s1 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000040",
        phase="success",
        terminal_offset=15,
    )
    s2 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000041",
        phase="cancelled",
        terminal_offset=5,
    )
    e1 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000042",
        phase="error",
        terminal_offset=20,
    )
    p1 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000043",
        phase="running",
        started_offset=100,
    )
    p2 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000044",
        phase="running",
        started_offset=50,
    )
    p3 = _record(
        run_id="wrk_00000000-0000-0000-0000-000000000045",
        phase="cancelling",
        started_offset=75,
    )
    result = sort_workflow_cards([p1, e1, s1, p3, s2, p2])
    # Expected:
    # - cancelled s2 (terminal_at 5)
    # - success s1 (terminal_at 15)
    # - error e1 (pinned)
    # - running p2 (started 50)
    # - cancelling p3 (started 75)
    # - running p1 (started 100)
    assert [r.run_id for r in result] == [
        s2.run_id,
        s1.run_id,
        e1.run_id,
        p2.run_id,
        p3.run_id,
        p1.run_id,
    ]
