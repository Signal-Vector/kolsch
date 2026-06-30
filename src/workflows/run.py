"""Workflow run model plus on-disk sidecar layout.

Three small primitives:

- [`WorkflowRunStatus`][workflows.run.WorkflowRunStatus] — the 7-value
  status landscape. `aborted` and `timed_out` are user- /
  deadline-initiated terminal states; `failed` covers every other
  abort path.
- [`WorkflowRun`][workflows.run.WorkflowRun] — the Pydantic model
  carrying a run's identity, status, and bookkeeping. Stored as the
  meta-sidecar alongside the JSONL.
- [`RunDirectory`][workflows.run.RunDirectory] — a frozen value object
  that computes the sidecar paths (`events.jsonl`, `pid`,
  `cancel.flag`, `meta.json`, `waiting_at`) from a run root. The
  executor, the orphan sweep, and the CLI's `cancel` command agree on
  this layout via the same value object.

NO executor logic in this module — pure data and filesystem plumbing.
The executor consumes `WorkflowRun` and `RunDirectory` via
`workflows.run_store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from schemas.entities import ForkedFrom, ParentRunPointer
from workflows.ids import TraceId, WorkflowRunId

__all__ = [
    "RunDirectory",
    "WorkflowRun",
    "WorkflowRunStatus",
]


class WorkflowRunStatus(StrEnum):
    """The 7-value run-status landscape.

    Lowercase string values match the on-disk JSONL `status` field
    convention; the `StrEnum` base means the runtime values are
    interchangeable with their string literals.
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ABORTED = "aborted"

    @classmethod
    def terminal_set(cls) -> frozenset[WorkflowRunStatus]:
        """Return the four states the executor never transitions out of.

        `WorkflowRunStore.tail` uses this set to know when to stop
        polling.
        """
        return frozenset({cls.SUCCEEDED, cls.FAILED, cls.TIMED_OUT, cls.ABORTED})


class WorkflowRun(BaseModel):
    """One workflow run's bookkeeping record.

    The model is serialised to `runs/<wrk_id>/meta.json` at create
    time; subsequent status transitions are inferred from the JSONL
    tail (per [`WorkflowRunStore.read`][workflows.run_store.WorkflowRunStore.read]).
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=False, extra="forbid")

    run_id: WorkflowRunId
    trace_id: TraceId
    workflow_ref: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    inputs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    pid: int | None = None
    # Populated when status reaches `failed` / `aborted` / `timed_out`
    # so the CLI can render a one-line "what happened" without
    # re-reading the JSONL tail.
    reason: str | None = None
    # Set on child runs spawned by a `kolsch/nodes/workflow` node in a
    # parent. `None` for root runs (the CLI's `workflows run` entry
    # point). Mirrors `schemas.entities.FromWorkflowRun` for agent
    # sessions: the parent-pointer plumbing for cross-JSONL forensics
    # is identical across composition surfaces.
    parent: ParentRunPointer | None = None
    # Set on runs created by `kolsch workflows fork`. Carries the
    # parent run id + the event id at which the fork is rooted.
    # Independent of `parent`: a run can have either, both, or neither
    # (a forked sub-workflow run carries both).
    forked_from: ForkedFrom | None = None


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """Frozen value object computing a run's sidecar paths.

    `root` is the per-run directory (`<workspace>/runs/<wrk_id>`).
    The sidecar paths land at fixed names — every consumer (executor,
    CLI cancel, orphan sweep) goes through this value object so the
    layout has a single source of truth.
    """

    root: Path

    @property
    def events_path(self) -> Path:
        """The JSONL event log for this run."""
        return self.root / "events.jsonl"

    @property
    def pid_path(self) -> Path:
        """The PID sidecar — the executor process for this run."""
        return self.root / "pid"

    @property
    def cancel_flag_path(self) -> Path:
        """The cancel-flag sidecar — touched by `kolsch workflows cancel`."""
        return self.root / "cancel.flag"

    @property
    def meta_path(self) -> Path:
        """The `meta.json` sidecar carrying the run snapshot.

        Written at create time. The JSONL tail is authoritative for
        the live status; the meta sidecar is what
        `WorkflowRunStore.list` reads cheaply without parsing the
        events file.
        """
        return self.root / "meta.json"

    @property
    def waiting_at_path(self) -> Path:
        """The paused-run open-invocation marker.

        Written atomically by the executor's pause handler with the
        open `user_input` node's invocation_id. The presence of this
        sidecar AND a `status=waiting_for_input` meta is the on-disk
        pause contract; the resume entry point reads it to identify
        the open invocation without walking the JSONL backwards.
        Removed at terminal status on resume.
        """
        return self.root / "waiting_at"
