"""feat-019 Test 1 — `format_live_workflow_strip` cases.

Six passes matching the spec's Test 1 breakdown:

1. Empty records → None.
2. Single ``running`` → ``"▸ ref 3/7 · Ns"``.
3. Two ``running`` → ``"▸ ref1 3/7 · Ns"`` (head only; multiple marker
   via queued count when queued != 0).
4. One ``running`` + one ``queued`` → ``"▸ ref 3/7 · ⋯ 1 queued · Ns"``.
5. One ``running`` with ``awaiting_approval=True`` → strip includes
   ``"⚠ 1 paused"``.
6. All ``success`` → None (no live records).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cli.chat.services.workflow_layout import format_live_workflow_strip
from cli.chat.services.workflows_impl import WorkflowRunRecord
from tools.services.workflows import NodeCounts, WorkflowRunPhase
from workflows.ids import WorkflowRunId

_NOW = datetime.now(tz=UTC)


def _record(
    *,
    suffix: str,
    ref: str,
    phase: WorkflowRunPhase,
    completed: int = 0,
    total: int = 0,
    started_offset: int | None = 0,
    awaiting_approval: bool = False,
) -> WorkflowRunRecord:
    """Build a synthetic ``WorkflowRunRecord`` with node counts + timing."""
    started_at = (
        _NOW - timedelta(seconds=started_offset) if started_offset is not None else None
    )
    return WorkflowRunRecord(
        run_id=WorkflowRunId(f"wrk_00000000-0000-0000-0000-0000000000{suffix}"),
        ref=ref,
        phase=phase,
        card=None,
        created_at=_NOW - timedelta(seconds=60),
        started_at=started_at,
        node_counts=NodeCounts(total=total, completed=completed),
        awaiting_approval=awaiting_approval,
    )


def test_empty_records_returns_none() -> None:
    """No records → strip is None."""
    assert format_live_workflow_strip([]) is None


def test_single_running_full_detail() -> None:
    """One live workflow renders the full ``▸ ref N/M · Ns`` head."""
    r = _record(
        suffix="01",
        ref="test/ref",
        phase="running",
        completed=3,
        total=7,
        started_offset=12,
    )
    result = format_live_workflow_strip([r])
    assert result is not None
    # Head + elapsed marker present.
    assert result.startswith("▸ test/ref 3/7")
    assert "s" in result
    # No ⋯ (no queued), no ⚠ (no paused).
    assert "⋯" not in result
    assert "⚠" not in result


def test_two_running_shows_head_and_elapsed() -> None:
    """Two live workflows still render the head + elapsed (compact)."""
    r1 = _record(
        suffix="02",
        ref="release-candidate",
        phase="running",
        completed=4,
        total=7,
        started_offset=18,
    )
    r2 = _record(
        suffix="03",
        ref="other/ref",
        phase="running",
        completed=1,
        total=3,
        started_offset=5,
    )
    result = format_live_workflow_strip([r1, r2])
    assert result is not None
    # First-listed live workflow leads the strip.
    assert result.startswith("▸ release-candidate 4/7")
    # No queued suffix (no queued records).
    assert "queued" not in result
    # Elapsed marker present.
    assert "s" in result


def test_running_plus_queued_shows_queued_count() -> None:
    """One running + one queued → ``⋯ 1 queued`` suffix."""
    running = _record(
        suffix="04",
        ref="prod/deploy",
        phase="running",
        completed=3,
        total=7,
        started_offset=10,
    )
    queued = _record(
        suffix="05",
        ref="prod/other",
        phase="queued",
        started_offset=None,
    )
    result = format_live_workflow_strip([running, queued])
    assert result is not None
    assert result.startswith("▸ prod/deploy 3/7")
    assert "⋯ 1 queued" in result


def test_running_with_pending_approval_shows_paused() -> None:
    """A running record with ``awaiting_approval=True`` → ``⚠ 1 paused``."""
    r = _record(
        suffix="06",
        ref="stage/plan",
        phase="running",
        completed=2,
        total=5,
        started_offset=8,
        awaiting_approval=True,
    )
    result = format_live_workflow_strip([r])
    assert result is not None
    assert "⚠ 1 paused" in result


def test_all_success_returns_none() -> None:
    """Terminal-only records → no live strip."""
    records = [
        _record(
            suffix=f"1{i}",
            ref="done/ref",
            phase="success",
            started_offset=None,
        )
        for i in range(3)
    ]
    assert format_live_workflow_strip(records) is None
