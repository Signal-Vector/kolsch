"""Workflows service — start / status / cancel a nested workflow run.

Two shapes:

- [`Workflows`][tools.services.workflows.Workflows] is the
  author-facing Protocol tools annotate against.
- [`AbstractWorkflows`][tools.services.workflows.AbstractWorkflows]
  is a plain ABC whose methods raise `NotImplementedError`; tests
  subclass it, and the spec-012 concrete impl overrides the stubs.

The ABC does NOT inherit from the Protocol. `@runtime_checkable` is
deliberately NOT set (default `False`).

`WorkflowRunId` is re-exported from `workflows.ids` where it lives
as the shipped `wrk_<uuid>` prefixed id.

spec-012 additions (r-review r1):

- `WorkflowRunPhase` / `WorkflowRunStatusPhase` Literal aliases
  covering the services-owned phase vocabulary. Distinct from the
  executor-owned `WorkflowRunStatus(StrEnum)` (see
  `src/workflows/run.py:43`).
- `NodeCounts` dataclass — running/completed/failed/etc counters
  populated as node events flow through the bridge.
- `WorkflowRunStatusView` dataclass — return type of
  `Workflows.status()`. Renamed from `WorkflowStatus`/`WorkflowRunStatus`
  to avoid collision with the shipped executor StrEnum.
- `EXECUTOR_STATUS_TO_PHASE` + `executor_status_to_phase()` — one-
  way translation from the executor's 7-value enum to the services'
  6-value phase Literal.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from workflows.ids import WorkflowRunId
from workflows.run import WorkflowRunStatus

if TYPE_CHECKING:
    from tools.services.cards import Card


__all__ = [
    "EXECUTOR_STATUS_TO_PHASE",
    "AbstractWorkflows",
    "NodeCounts",
    "WorkflowRunId",
    "WorkflowRunPhase",
    "WorkflowRunStatusPhase",
    "WorkflowRunStatusView",
    "Workflows",
    "executor_status_to_phase",
]


WorkflowRunPhase = Literal[
    "queued",
    "running",
    "cancelling",
    "success",
    "error",
    "cancelled",
]
"""Services-owned run phase vocabulary.

Six values covering the WorkflowsImpl-visible lifecycle. Note the
`cancelling` intermediate — services-only, set by
`WorkflowsImpl.cancel()` on the record directly. The executor never
emits `cancelling`; from its perspective the run stays `RUNNING`
until it unwinds to `ABORTED`.
"""


WorkflowRunStatusPhase = Literal[
    "queued",
    "running",
    "cancelling",
    "success",
    "error",
    "cancelled",
    "unknown",
]
"""Return-value phase — adds `unknown` for the cold-read miss case.

`WorkflowRunStatusView.phase` uses this widened alias; the value
`unknown` fires when both the in-memory registry AND the on-disk
store lack a record for the queried run id.
"""


@dataclass
class NodeCounts:
    """Per-run node counters populated as the bridge drains events.

    Mutated in-place on the enclosing `WorkflowRunRecord` — the
    fields carry the running tally of node states seen through the
    executor observer seam. spec-013's renderer reads these to paint
    the header progress bar.
    """

    total: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0


@dataclass
class WorkflowRunStatusView:
    """Public return type of `Workflows.status()`.

    r-review r1: renamed from `WorkflowRunStatus` to avoid collision
    with the shipped executor-owned `WorkflowRunStatus(StrEnum)` at
    `src/workflows/run.py:43`. The two vocabularies are related but
    distinct — see :data:`EXECUTOR_STATUS_TO_PHASE`.

    Fields:
        run_id: The queried run's id.
        phase: The current phase — one of the seven values in
            :data:`WorkflowRunStatusPhase`.
        node_counts: When the run is in registry, the live counters;
            None otherwise (cold-read paths don't reconstruct this
            in the current shape).
        elapsed_seconds: Wall-time since `started_at` (or
            `created_at` for still-queued runs); None when the run
            is only known via cold-read.
        last_event_ts: Timestamp of the most recent observed
            executor event; None for queued or cold-read runs.
        ref: The workflow's 3-seg ref (``bundle/workflows/name``)
            when known — populated from the registry record on the
            in-memory path and from ``meta.json`` on the cold-read
            path. ``None`` when the ``run_id`` is fully unknown
            (``phase="unknown"``). Added by feat-018 so butler-facing
            tools (``workflow_status``) can echo the ref back in
            their ``_chat_anchor`` payload without a second lookup.
        error_message: Terminal-error diagnostic. Populated when
            ``phase == "error"`` (or a cancel that carried a reason);
            ``None`` on every other phase and on cold-read paths that
            couldn't reconstruct the reason. Butler-facing tools
            (``workflow_status``) surface this back to the model so a
            retry loop isn't the model's only option after an
            immediate-failure workflow — added by bug-025 to fix the
            "workflow errors silently, model retries same args"
            symptom from session ses_629b626e.
        failed_node: Name of the node whose failure produced the
            terminal error. ``None`` for construction-time failures
            (no node ran), successful runs, and cold-read paths that
            can't attribute the failure. Added by bug-025 alongside
            ``error_message``.
        outputs: Terminal-outputs dict from
            ``workflow.run.completed.payload.outputs``. Populated on
            the hot path when the ``RUN_COMPLETED`` event lands on the
            bridge, and on the cold-read path when the JSONL tail
            carries a completed row. ``None`` on every non-terminal
            phase, on failures/aborts (which don't carry an outputs
            dict), and on cold-read misses. Added by bug-031 so the
            blocking ``run_workflow`` tool can return the outputs in
            the same turn — closing the "workflow completes, model
            silent 12 min" symptom from session ses_af5980ab.
        on_disk_run_id: The **canonical on-disk identifier** — the
            ``wrk_<uuid>`` that keys ``runs/<on_disk_run_id>/`` (owns
            ``meta.json`` / ``events.jsonl`` / ``pid`` /
            ``cancel.flag``). Distinct from ``run_id`` because the
            chat's ``WorkflowsImpl`` registry mints its own
            queue-time id at ``start()`` (before the executor's
            ``WorkflowRunStore.create`` mints the on-disk id). Both
            share the ``wrk_<uuid>`` shape so a prefix grep cannot
            disambiguate them — operators must use this field to
            correlate a status view with its on-disk artefacts.
            ``None`` when the run is still queued (executor not
            constructed yet, no on-disk dir) or on cold-read /
            registry-unknown paths where the lookup key was already
            the on-disk id (in which case ``run_id`` and
            ``on_disk_run_id`` coincide — populated to the same
            value for uniformity). Added by bug-066 to close the
            "workflow_status echoes an id that isn't in ``runs/``"
            correlation defect.
    """

    run_id: WorkflowRunId
    phase: WorkflowRunStatusPhase
    node_counts: NodeCounts | None = None
    elapsed_seconds: float | None = None
    last_event_ts: datetime | None = None
    ref: str | None = None
    # bug-025: terminal-error diagnostic. See dataclass docstring.
    error_message: str | None = None
    failed_node: str | None = None
    # bug-031: terminal-outputs dict — see dataclass docstring.
    outputs: dict[str, Any] | None = None
    # bug-066: canonical on-disk identifier — see dataclass docstring.
    on_disk_run_id: WorkflowRunId | None = None


# ---------------------------------------------------------------------------
# Executor-status ↔ services-phase translation (r-review r1).
#
# The executor emits `WorkflowRunStatus(StrEnum)` values (see
# `src/workflows/run.py:43`): the 7-value {pending, running,
# waiting_for_input, succeeded, failed, timed_out, aborted} set.
# Services expose `WorkflowRunPhase`: the 6-value {queued, running,
# cancelling, success, error, cancelled} Literal.
#
# `cancelling` is services-only — set by `WorkflowsImpl.cancel()`
# directly on the record. It is NOT emitted by the executor and does
# NOT appear in this translation table. Cold-read never returns
# `cancelling`.
# ---------------------------------------------------------------------------


EXECUTOR_STATUS_TO_PHASE: dict[WorkflowRunStatus, WorkflowRunPhase] = {
    WorkflowRunStatus.PENDING: "queued",
    WorkflowRunStatus.RUNNING: "running",
    # Paused runs are still "running" from the services phase POV —
    # the UI surfaces the pause via a `paused` sub-event.
    WorkflowRunStatus.WAITING_FOR_INPUT: "running",
    WorkflowRunStatus.SUCCEEDED: "success",
    WorkflowRunStatus.FAILED: "error",
    WorkflowRunStatus.TIMED_OUT: "error",
    WorkflowRunStatus.ABORTED: "cancelled",
}


def executor_status_to_phase(
    status: WorkflowRunStatus,
) -> WorkflowRunPhase:
    """Translate the executor's status to the services phase.

    `cancelling` is a services-only intermediate state — it is set
    by `WorkflowsImpl.cancel()` on the `WorkflowRunRecord.phase`
    directly. It is NOT emitted by the executor and does NOT appear
    in the translation.

    Args:
        status: The executor's `WorkflowRunStatus` enum value.

    Returns:
        The matching services `WorkflowRunPhase` literal.
    """
    return EXECUTOR_STATUS_TO_PHASE[status]


class Workflows(Protocol):
    """Tools' author-facing view of the workflows service.

    spec-012 expanded from the spec-001 stub to carry the full
    `start(ref, inputs, *, card=)` / `status(run_id)` /
    `cancel(run_id, *, cascade=)` / `aclose()` surface.
    """

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: Card | None = None,
    ) -> WorkflowRunId:
        """Queue a nested workflow run; return its id immediately.

        Non-blocking — the run enters the impl's FIFO queue and the
        drain worker picks it up. If `card` is passed the impl marks
        `card._owner_transferred=True` so the tool that opened the
        card does not auto-close it on tool return — the workflow
        service owns the terminal transition.
        """
        ...

    def status(self, run_id: WorkflowRunId) -> WorkflowRunStatusView:
        """Return the current phase view for a run.

        Registry-first: an in-flight run reads from the impl's
        in-memory `_workflow_registry`. On a miss, cold-reads the
        run's `meta.json` + a bounded JSONL tail. Only both-miss
        returns `phase="unknown"`.
        """
        ...

    async def cancel(
        self,
        run_id: WorkflowRunId,
        *,
        cascade: bool = False,
    ) -> bool:
        """Cancel a running workflow run.

        Dual-path: fires the in-process `CancelToken` AND writes the
        run's `cancel_flag_path`. `cascade=True` is accepted for
        forward compatibility with spec-017; this spec only cancels
        the single run regardless of the flag.

        Returns True if the token was fired, False if the run is
        unknown / already terminal.
        """
        ...

    async def aclose(self) -> None:
        """Cancel drain worker, per-run drains, and all live tokens."""
        ...

    @property
    def default_timeout_seconds(self) -> int:
        """Default blocking timeout for `run_workflow` (and virtual variants).

        Sourced from `platform.yaml:workflows.default_timeout_seconds`
        at runtime bootstrap. Tools read this to size their long-poll
        deadline when the caller doesn't pass an explicit timeout.
        """
        ...


class AbstractWorkflows(ABC):
    """Platform + test base for `Workflows` impls. Methods raise by default."""

    @property
    def default_timeout_seconds(self) -> int:
        """Default stub — subclasses override. 300s preserves bug-031 default."""
        return 300

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: Card | None = None,
    ) -> WorkflowRunId:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        _ = ref, inputs, card
        raise NotImplementedError

    def status(self, run_id: WorkflowRunId) -> WorkflowRunStatusView:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        _ = run_id
        raise NotImplementedError

    async def cancel(
        self,
        run_id: WorkflowRunId,
        *,
        cascade: bool = False,
    ) -> bool:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        _ = run_id, cascade
        raise NotImplementedError

    async def aclose(self) -> None:
        """Called from ChatApp.on_unmount. Cancels drain tasks, snapshot timers, cancel tokens. Idempotent. Post-aclose, subsequent mutation raises RuntimeError.

        The ABC default is a no-op — test mocks and the placeholder
        `ChatServiceRegistry` fields (all `None` today) don't need real
        teardown. Concrete impls in spec-012 override with real
        teardown logic.
        """
        return
