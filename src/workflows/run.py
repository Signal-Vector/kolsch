"""Workflow run model + sidecar layout — Phase 3.1 Step 0.

Three small primitives:

- :class:`WorkflowRunStatus` — the 7-value status landscape per
  master spec §8 + §8.4. ``aborted`` and ``timed_out`` are user-/
  deadline-initiated terminal states; ``failed`` covers every other
  abort path.
- :class:`WorkflowRun` — the Pydantic model carrying a run's
  identity, status, and bookkeeping. Stored as the meta-sidecar
  alongside the JSONL.
- :class:`RunDirectory` — a frozen value object that computes the
  three sidecar paths (``events.jsonl``, ``pid``, ``cancel.flag``)
  from a run root. The executor (Step 1+), the orphan sweep
  (Step 4), and the CLI's ``cancel`` command (Phase 3.0 Step 6)
  agree on this layout via the same value object.

NO executor logic in this module — pure data + filesystem
plumbing. The executor consumes ``WorkflowRun`` + ``RunDirectory``
via :mod:`workflows.run_store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from schemas.entities import ParentRunPointer
from workflows.ids import TraceId, WorkflowRunId

__all__ = [
    "RunDirectory",
    "WorkflowRun",
    "WorkflowRunStatus",
]


class WorkflowRunStatus(StrEnum):
    """The 7-value run-status landscape — master spec §8 + §8.4.

    Lowercase string values match the on-disk JSONL ``status`` field
    convention; the :class:`StrEnum` base means the runtime values
    are interchangeable with their string literals.
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
        """The four states the executor never transitions out of.

        :meth:`WorkflowRunStore.tail` uses this set to know when to
        stop polling.
        """
        return frozenset(
            {cls.SUCCEEDED, cls.FAILED, cls.TIMED_OUT, cls.ABORTED}
        )


class WorkflowRun(BaseModel):
    """One workflow run's bookkeeping record.

    Phase 3.1 Step 0 — minimum-viable shape. The model is serialised
    to ``runs/<wrk_id>/meta.json`` at create time; subsequent status
    transitions are inferred from the JSONL tail (per
    :meth:`WorkflowRunStore.read`).
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=False, extra="forbid")

    run_id: WorkflowRunId
    trace_id: TraceId
    workflow_ref: str
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    inputs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    pid: int | None = None
    # Populated when status reaches ``failed`` / ``aborted`` /
    # ``timed_out`` so the CLI can render a one-line "what happened"
    # without re-reading the JSONL tail.
    reason: str | None = None
    # Phase 3.7 — set on child runs spawned by a ``kolsch/nodes/workflow``
    # node in a parent. ``None`` for root runs (the CLI's ``workflows
    # run`` entry point). Mirrors :class:`schemas.entities.FromWorkflowRun`
    # for agent sessions: the parent-pointer plumbing for cross-JSONL
    # forensics is identical across composition surfaces.
    parent: ParentRunPointer | None = None


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """Frozen value object computing a run's sidecar paths.

    ``root`` is the per-run directory (``<workspace>/runs/<wrk_id>``).
    The three sidecar paths land at fixed names — every consumer
    (executor, CLI cancel, orphan sweep) goes through this value
    object so the layout has a single source of truth.
    """

    root: Path

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def pid_path(self) -> Path:
        return self.root / "pid"

    @property
    def cancel_flag_path(self) -> Path:
        return self.root / "cancel.flag"

    @property
    def meta_path(self) -> Path:
        """``meta.json`` carries the :class:`WorkflowRun` snapshot at
        create time. The JSONL tail is authoritative for the live
        status (Step 1 onward); the meta sidecar is what
        :meth:`WorkflowRunStore.list` reads cheaply without parsing
        the events file."""
        return self.root / "meta.json"

    @property
    def waiting_at_path(self) -> Path:
        """Phase 3.4 — the paused-run open invocation marker.

        Written atomically by the executor's pause handler with the
        open ``user_input`` node's invocation_id. The presence of
        this sidecar AND a ``status=waiting_for_input`` meta is the
        on-disk pause contract; Step 1's resume entry point reads it
        to identify the open invocation without walking the JSONL
        backwards. Removed at terminal status on resume (Step 1).
        """
        return self.root / "waiting_at"
