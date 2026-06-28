"""Sequential iterative executor — Phase 3.1 Step 1 (skeleton).

Master spec §8.1 §8.2.1 §8.3 + the Phase 3.1 build doc Step 1
prompt. Implements the algorithm and the framing events; defers
real node dispatch to Step 2 (built-in ``bash``/``noop``) + Step 3
(authored Python ``@node``). Cancel-flag polling lands in Step 4;
per-run ``sys.modules`` cleanup lands in Step 3. The TODOs at the
relevant call sites mark the seam.

Public surface:

- :class:`NodeRunner` — Protocol every runner satisfies.
- :class:`RunnerRegistry` — maps a node ``type:`` ref to its runner;
  unmapped built-ins raise :class:`NodeTypeNotImplemented` carrying
  the phase number that ships them.
- :class:`IterativeExecutor` — the algorithm itself; consumes the
  typed :class:`Workflow` directly (never touches raw YAML).

NO recursion. The worklist is iterative; sub-workflow nesting (Phase
3.7) reuses the same iterative shape per-child.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from assets.refs import is_three_segment, parse_three_segment
from workflows._inputs_models import inputs_model_for
from workflows.branching import compute_branch_value
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.context import NodeContext
from workflows._replay import find_event_by_id, replay_into
from workflows.errors import (
    BashNonZeroExit,
    NodeTypeNotImplemented,
    WorkflowBindTypeMismatch,
    WorkflowExtendsDepthExceeded,
    WorkflowInputInvalid,
    WorkflowNodeFailed,
    WorkflowPausedForInput,
    WorkflowResumeRejected,
    WorkflowRunNotFound,
    WorkflowRuntimeError,
)
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeFailedEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunAbortedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunForkedFromEvent,
    WorkflowRunForkedFromPayload,
    WorkflowRunResumedEvent,
    WorkflowRunStartedEvent,
    emit_event,
)
from workflows.ids import InvocationId, WorkflowRunId
from workflows.jinja_env import JINJA_ENV
from workflows.run import WorkflowRunStatus

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog
    from schemas.identifiers import EventId
    from workflows.events import WorkflowEvent
    from workflows.parser import EdgeDef, Workflow
    from workflows.run import RunDirectory, WorkflowRun
    from workflows.run_store import WorkflowRunStore

__all__ = [
    "BUILTIN_PHASE_MAP",
    "IterativeExecutor",
    "NodeRunner",
    "RunnerRegistry",
]


# ---------------------------------------------------------------------------
# Built-in name → phase mapping (per process/phase3.x-roadmap.md §18.2).
# Phase 3.1 (this phase) implements bash + noop; everything else defers
# to a later sub-phase. The map drives RunnerRegistry's
# NodeTypeNotImplemented payload so the error message names the phase
# that ships the runner.
# ---------------------------------------------------------------------------


BUILTIN_PHASE_MAP: Mapping[str, str] = {
    "bash": "3.1",
    "noop": "3.1",
    "agent": "3.2",
    "user_input": "3.4",
    "read_file": "3.5",
    "write_file": "3.5",
    "list_files": "3.5",
    "set_state": "3.5",
    "delete_state": "3.5",
    # Phase 3.6 ships the BRANCH NODE TYPE (alongside filter + compute)
    # per ``process/phase3.x-roadmap.md`` §3.6. Phase 3.3 ships the
    # ORTHOGONAL edge-level ``branch:`` field + bash-exit-code routing;
    # that's a graph-edge construct, not a runtime node type, so it
    # does NOT live in this runner-phase map.
    "branch": "3.6",
    "filter": "3.6",
    "compute": "3.6",
    "map": "3.7",
    "workflow": "3.7",
}


# ---------------------------------------------------------------------------
# NodeRunner protocol + RunnerRegistry.
# ---------------------------------------------------------------------------


class NodeRunner(Protocol):
    """The contract every node-type runner satisfies.

    The executor invokes :meth:`run` per node invocation. Runners
    return the node's captured output as a dict; the executor wraps
    it in the ``workflow.node.completed`` event payload and stores
    it under ``captured[node_name]`` for downstream Jinja references.
    """

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,  # noqa: ANN401 — NodeDefUnion is a Pydantic alias
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        ...


class RunnerRegistry:
    """Maps a node ``type:`` ref to its :class:`NodeRunner`.

    Phase 3.1 Step 1 ships the SKELETON — Steps 2 + 3 register
    ``bash``/``noop`` + the catalog-node runner. Unregistered
    built-ins (every other 14 reserved slot) raise
    :class:`NodeTypeNotImplemented` carrying the phase that ships
    the runner (per :data:`BUILTIN_PHASE_MAP`).

    Authored-Python node refs (any non-``kolsch/nodes/`` 3-seg ref)
    that have no explicit registration also raise — the
    catalog-node runner (Step 3) installs as a generic fallback for
    those.
    """

    def __init__(self) -> None:
        self._runners: dict[str, NodeRunner] = {}
        self._catalog_runner: NodeRunner | None = None

    def register(self, type_ref: str, runner: NodeRunner) -> None:
        """Register a runner for a 3-segment ``type:`` ref."""
        self._runners[type_ref] = runner

    def set_catalog_runner(self, runner: NodeRunner) -> None:
        """Install the generic fallback for non-built-in 3-seg refs.

        Phase 3.1 Step 3 — when set, ``get`` returns this runner for
        any 3-seg ``<bundle>/nodes/<name>`` ref that (a) isn't an
        explicit registration AND (b) isn't a reserved built-in slot
        (those still raise :class:`NodeTypeNotImplemented` via
        :data:`BUILTIN_PHASE_MAP`). The
        :class:`workflows.builtins.CatalogNodeRunner` is the
        canonical caller.
        """
        self._catalog_runner = runner

    @property
    def catalog_runner(self) -> NodeRunner | None:
        """Read-only accessor for the installed fallback runner.

        Phase 3.3 Step 1 — the executor's strict-mode bind-validation
        hook reads it to delegate authored Python ``Inputs(BaseModel)``
        introspection to the catalog runner (master spec §6.4.4).
        ``None`` when no fallback was installed; built-in receivers
        skip the catalog lookup either way.
        """
        return self._catalog_runner

    def get(self, type_ref: str, *, node_name: str) -> NodeRunner:
        """Return the runner; raise :class:`NodeTypeNotImplemented`
        when no runner is mapped.

        ``node_name`` is the workflow's per-invocation node name —
        woven into the raised exception so the failure event names
        the offending node, not just the type ref.
        """
        runner = self._runners.get(type_ref)
        if runner is not None:
            return runner
        # Authored-Python fallback: any 3-seg ref that isn't a
        # reserved built-in slot routes to the catalog runner when
        # one is installed.
        if self._catalog_runner is not None and not _is_builtin_slot(type_ref):
            return self._catalog_runner
        phase = _phase_for_type_ref(type_ref)
        raise NodeTypeNotImplemented(
            node=node_name, type_ref=type_ref, phase=phase
        )


def _is_builtin_slot(type_ref: str) -> bool:
    """``True`` iff ``type_ref`` is a reserved ``kolsch/nodes/<name>`` slot."""
    if not is_three_segment(type_ref):
        return False
    bundle, segment, name = parse_three_segment(type_ref)
    return (
        bundle == "kolsch"
        and segment == "nodes"
        and name in BUILTIN_TYPE_NAMES
    )


def _phase_for_type_ref(type_ref: str) -> str:
    """Best-effort phase lookup for the failure message.

    Built-in reserved slots get their phase from :data:`BUILTIN_PHASE_MAP`.
    Authored Python nodes (any other 3-seg ref) fall through to
    ``"3.1"`` — the Step 3 catalog-node runner is what's missing
    when those refs reach the registry without a default fallback.
    """
    if is_three_segment(type_ref):
        bundle, segment, name = parse_three_segment(type_ref)
        if bundle == "kolsch" and segment == "nodes" and name in BUILTIN_TYPE_NAMES:
            return BUILTIN_PHASE_MAP.get(name, "3.x")
    return "3.1"


@dataclass(slots=True)
class _PendingNode:
    """Phase 3.10 §16.2.1 — one node's pre-dispatch handle.

    Carries the bookkeeping the concurrent worklist needs to resume
    a node's post-dispatch flow (capture, emit completed, cascade
    prune) after the spawned task settles. The ``ctx`` carries the
    per-invocation chain pointer + spawned-session/run handles that
    the success path reads when building the completion payload.
    """

    node_name: str
    node_def: Any
    inv_id: InvocationId
    started_event_id: Any
    bound_inputs: dict[str, Any]
    ctx: NodeContext


# ---------------------------------------------------------------------------
# IterativeExecutor.
# ---------------------------------------------------------------------------


class IterativeExecutor:
    """Sequential worklist-based executor.

    Construction is cheap — the executor records its dependencies and
    waits for :meth:`run` to be invoked. The algorithm is the SEVEN
    steps from master spec §8.1, but Phase 3.1 Step 1 only ships
    framing events + the worklist; real node dispatch lives in
    Steps 2 + 3, cancel polling in Step 4, full event-emission
    contract (caller_chain / trace_id propagation / policy.decision.made)
    in Step 5.

    The constructor takes the registry explicitly so tests can
    inject fake runners without monkey-patching a module-level
    default.
    """

    def __init__(
        self,
        *,
        workflow: Workflow,
        run: WorkflowRun,
        store: WorkflowRunStore,
        registry: RunnerRegistry,
        catalog: AssetCatalog,
        _call_depth: int = 0,
        _call_chain: tuple[str, ...] = (),
    ) -> None:
        # Phase 3.7 — nested-executor depth cap (shared with ``extends:``;
        # same error class, same limit). The check at CONSTRUCTION (not
        # at ``run`` time) is load-bearing: a nested executor that would
        # exceed depth must never begin work, so a Phase 3.7 sub-workflow
        # runner attempting to mint child N+1 fails immediately and the
        # parent's runner can surface a clean failure.
        if _call_depth > 16:
            raise WorkflowExtendsDepthExceeded(
                file=run.workflow_ref,
                line=0,
                depth=_call_depth,
                limit=16,
            )
        self._call_depth = _call_depth
        # Phase 3.7 — the "effective" call chain INCLUDES this run's own
        # workflow ref so :class:`workflows.builtins.workflow_sub.WorkflowSubRunner`
        # catches direct A→A recursion on the first would-be call (an
        # empty chain would only catch it on the second indirection,
        # leaving one extra frame of work executed). When the caller
        # passes a non-empty chain it's a nested executor — the
        # ``WorkflowSubRunner`` already appended the child ref via
        # ``_call_chain=(*ctx.call_chain, child_ref)`` and we honour
        # that verbatim. Root entry points (CLI, tests, scripts)
        # continue to pass nothing and the auto-include kicks in.
        if _call_chain:
            self._call_chain = _call_chain
        else:
            self._call_chain = (run.workflow_ref,)
        self._workflow = workflow
        self._run = run
        self._store = store
        self._registry = registry
        self._catalog = catalog
        # Captured outputs keyed by node name — written under each node
        # invocation's ``workflow.node.completed`` event, read by
        # downstream Jinja ``{{ nodes.<X>.<Y> }}`` references.
        self._captured: dict[str, dict[str, Any]] = {}
        # State dict — initialised from the run's inputs in :meth:`run`.
        self._state: dict[str, Any] = {}
        # Bookkeeping for "completion" per AND-strict rule 1 (skip
        # counts as completion). Used by the worklist to know when a
        # downstream node's predecessors have all reached a terminal
        # decision.
        self._completed_or_skipped: set[str] = set()
        # Phase 3.1 Step 5 — causal-parent chain. The most recent
        # emitted event id becomes the default ``parent_event_id`` for
        # the next event. Per-invocation node.started ids are also
        # stashed so node.completed / node.failed correctly point at
        # the matching node.started even when intermediate events
        # (e.g. workflow.tool.called) reset ``_last_event_id``.
        self._last_event_id: Any = None
        self._node_started_event_id: dict[InvocationId, Any] = {}
        # Phase 3.5 Step 1 — per-invocation "last emitted in-chain
        # event id." Initialised to the node's started event id at
        # node-fire time; advanced by every
        # :meth:`NodeContext.emit_workflow_event` call (currently the
        # state runners emitting ``workflow.state.mutated``). Read by
        # the node's ``workflow.node.completed`` emit so the chain
        # rule per master spec §11.3 holds: a state-mutating node's
        # completion chains to the LAST ``workflow.state.mutated``
        # event, not back to its own started event. Non-state nodes
        # never advance the per-invocation pointer, so completion
        # still chains to started — Phase 3.1's contract is preserved
        # for the existing built-ins (master spec no-regressions
        # promise on byte-identical JSONLs for non-state workflows).
        self._invocation_last_event_id: dict[InvocationId, Any] = {}
        # Phase 3.3 Step 2 — per-node ``workflow.node.completed`` id
        # so the pruning cascade can chain ``workflow.node.pruned``
        # events back to their upstream source's completion event
        # (master spec §11.2 ordering rule).
        self._node_completed_event_id: dict[str, Any] = {}
        # Phase 3.3 Step 1 — edges suppressed by branch dispatch.
        # ``(source, target)`` tuples accumulate as branchable nodes
        # complete: each outgoing edge whose ``branch:`` field doesn't
        # match the source's computed branch value is recorded here so
        # ``_render_bound_inputs`` skips it.
        self._pruned_edges: set[tuple[str, str]] = set()
        # Phase 3.3 Step 2 — pruned node names (reverse-reachability
        # cascade target). A node lands here when EVERY one of its
        # inbound edges is either ``(source, target) in _pruned_edges``
        # OR sourced from a node already in ``_pruned``. Pruned
        # nodes are added to ``_completed_or_skipped`` at the same
        # moment so the existing AND-strict downstream check treats
        # them as decided.
        self._pruned: set[str] = set()
        # Phase 3.7 — replay populates this mapping with the
        # parent-side spawn marker per workflow-node invocation. The
        # resume entry uses it to find the child_run_id for an open
        # workflow-node pause without re-walking the JSONL.
        self._workflow_node_child_runs: dict[str, str] = {}
        # Phase 3.7 — resume-cascade state. When the resume entry
        # detects a paused workflow node (vs the Phase 3.4 user_input
        # case), it records ``{node_name: (open_inv_id, child_run_id)}``
        # so the worklist's per-node dispatch re-fires the
        # WorkflowSubRunner with the ORIGINAL invocation id (instead of
        # minting a fresh one) and stamps ``ctx.is_resume_mode`` so
        # the runner branches into the resume cascade.
        self._resumed_open_workflow_invocations: dict[
            str, tuple[InvocationId, WorkflowRunId]
        ] = {}
        # Phase 3.10 §16.4 — per-run ``sys.modules`` namespace handle.
        # Published via :class:`WorkflowRunContext` at run start so
        # the catalog node runner registers authored Python modules
        # under a run-id-tagged key. Cleanup at run end runs from
        # every ``finally:`` arm alongside the Phase 3.1
        # ``cleanup_bundle_imports`` discipline (both fire during the
        # migration window per master spec §16.4 / build doc AC 2.5).
        from workflows.module_isolation import (  # noqa: PLC0415
            RunModuleNamespace,
        )

        self._module_ns = RunModuleNamespace(run_id=str(run.run_id))
        # Phase 3.10 §16.5 — JSONL emission lock. Acquired around
        # every ``emit_event`` call site that fires under concurrent
        # dispatch so the file's line framing stays intact when two
        # in-flight tasks would otherwise write simultaneously. The
        # sequential path acquires this lock too (via
        # :meth:`_emit_workflow_event`) — the lock is uncontended,
        # JSONL byte output is byte-identical to Phase 3.9.
        self._emit_lock = asyncio.Lock()
        # Phase 3.10 §16.2.1 — state-mutation lock held around any
        # ``set_state`` / ``delete_state`` invocation under
        # ``auto``-mode so the resulting ``workflow.state.mutated``
        # event stream is total-ordered per master spec §11.3. The
        # validator's run-start gate already guarantees no two
        # concurrent siblings touch the same key; this lock keeps the
        # JSONL ordering deterministic across multiple disjoint
        # mutators.
        self._state_mutation_lock = asyncio.Lock()
        # Phase 3.10 §16.9 — high-water mark of concurrent in-flight
        # node invocations observed during the run. Reported on
        # ``workflow.run.completed.payload.peak_in_flight`` and in
        # the CLI envelope. Sequential runs always report ``1``.
        self._peak_in_flight: int = 1

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    async def run(self) -> WorkflowRunStatus:  # noqa: PLR0915, PLR0912 — sequential algorithm; splitting hides the §8.1 ordering
        """Walk the worklist; return the terminal status.

        Per master spec §8.1, this is the seven-step algorithm. The
        Phase 3.1 Step 1 implementation emits framing events,
        evaluates ``when:`` gates, renders ``binds:`` per Round 6
        J5/β, dispatches via the registry, and captures outputs.
        Failures bubble out as terminal ``workflow.run.failed``.
        """
        from workflows.module_isolation import (  # noqa: PLC0415
            cleanup_bundle_imports,
            snapshot_sys_modules,
        )
        from workflows.run_context import (  # noqa: PLC0415
            WorkflowRunContext,
            set_workflow_run_context,
        )

        run_id = self._run.run_id
        run_dir = self._store.directory(run_id)
        # Phase 3.1 Step 4 — write the executor's PID atomically so
        # the engine-startup orphan sweep can decide whether a
        # ``status=running`` JSONL was left behind by a crashed run.
        # ``tmp + rename`` guarantees a partial write never surfaces.
        run_dir.root.mkdir(parents=True, exist_ok=True)
        tmp_pid = run_dir.pid_path.with_suffix(".tmp")
        tmp_pid.write_text(str(os.getpid()), encoding="utf-8")
        tmp_pid.replace(run_dir.pid_path)
        # Step 1 of the algorithm — run-start gate. The Phase 3.0
        # validator is the gate; the executor never enters the
        # worklist with an invalid workflow.
        from workflows.validator import (  # noqa: PLC0415
            validate_workflow_or_raise,
        )

        validate_workflow_or_raise(
            self._workflow, self._catalog, file=self._run.workflow_ref
        )

        # Phase 3.1 Step 3 — capture the pre-run ``sys.modules`` key
        # set so the cleanup step at run end only removes entries
        # that loaded WHILE this run was active AND lie under the
        # workspace's ``bundles/`` tree. Third-party packages are
        # already in the snapshot, so they survive the cleanup
        # filter even if a node module re-imports them.
        sys_modules_snapshot = snapshot_sys_modules()

        # Phase 3.1 Step 5 — publish the run context so sub-systems
        # (authz's ``policy.decision.made`` emitter, the tool
        # dispatcher's ``workflow.tool.called`` emitter) can append to
        # this run's JSONL without threading the run id through their
        # public APIs. The token is reset in the ``finally:`` block so
        # a failed run doesn't leak its handle. Phase 3.10 §16.4 —
        # ``module_ns`` rides along on the context so
        # :class:`CatalogNodeRunner` registers authored Python imports
        # under this run's prefix.
        run_context_token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run_id,
                trace_id=self._run.trace_id,
                store=self._store,
                module_ns=self._module_ns,
            )
        )

        # Step 2 — initialise the state dict from workflow inputs.
        self._state = dict(self._run.inputs)

        # Step 3 — compute the topological worklist + per-node
        # predecessor closure (graph reachability).
        topo, predecessors = self._compute_topology()

        # Phase 3.1 Step 5 — emit ``workflow.run.started`` via the
        # typed envelope. The envelope hoists ``workflow_run_id`` +
        # ``trace_id`` to the top level (master spec §11.3); the
        # ``payload`` keeps the legacy keys for backward compat with
        # Step 1-4 tests. Phase 3.10 §16.9 — ``concurrency_mode`` is
        # added to the payload ONLY when the workflow opts into
        # ``auto`` so sequential JSONL stays byte-identical to Phase
        # 3.9; the CLI envelope defaults to ``sequential`` when the
        # field is absent.
        started_payload: dict[str, Any] = {
            "workflow_ref": self._workflow_ref(),
            "inputs": dict(self._run.inputs),
            "trace_id": str(self._run.trace_id),
        }
        if self._workflow.concurrency == "auto":
            started_payload["concurrency_mode"] = "auto"
        self._last_event_id = emit_event(
            self._store,
            WorkflowRunStartedEvent(
                workflow_run_id=run_id,
                trace_id=self._run.trace_id,
                parent_event_id=None,
                payload=started_payload,
            ),
        )

        terminal_status: WorkflowRunStatus
        try:
            # Phase 3.1 Step 4 — pre-walk cancel poll. An operator who
            # writes ``cancel.flag`` BEFORE the first node fires sees
            # the run abort cleanly without dispatching anything.
            if run_dir.cancel_flag_path.exists():
                self._emit_aborted(run_id)
                return WorkflowRunStatus.ABORTED
            # Phase 3.4 Step 1 — the worklist body lives in
            # :meth:`_continue_worklist` so both initial-run and
            # resume share the SAME dispatch loop (the resume path
            # populates ``_captured`` / ``_pruned`` / ``_completed_or_skipped``
            # from JSONL replay, emits the user_input completion,
            # then calls into this same method which skips already-
            # decided nodes via the early-continue check at the top
            # of the loop).
            terminal_status = await self._continue_worklist(
                run_id=run_id,
                run_dir=run_dir,
                topo=topo,
                predecessors=predecessors,
            )
        finally:
            # Phase 3.10 §16.4 — per-run prefix cleanup. Removes only
            # this run's prefix-tagged entries; two concurrent runs
            # see distinct cleanups.
            self._module_ns.cleanup_for_run()
            # Phase 3.1 Step 3 — module-isolation cleanup runs on
            # EVERY terminal state (succeeded / failed / timed_out /
            # aborted) so a subsequent run starts from a clean
            # ``sys.modules`` for the workspace's ``bundles/`` tree.
            # Third-party packages survive (the snapshot already
            # contains them; the cleanup's ``if name in before:
            # continue`` guard short-circuits before deletion).
            # Both cleanups run during the migration window per
            # build doc AC 2.5.
            cleanup_bundle_imports(
                before=sys_modules_snapshot,
                workspace_root=self._store.runs_root.parent,
            )
            # Phase 3.1 Step 5 — release the run-context contextvar so
            # subsequent operations on this event loop don't observe
            # a stale handle.
            from workflows.run_context import (  # noqa: PLC0415
                reset_workflow_run_context,
            )

            reset_workflow_run_context(run_context_token)
        return terminal_status

    # ------------------------------------------------------------------
    # Resume entry point — Phase 3.4 Step 1 (master spec §10.2).
    # ------------------------------------------------------------------

    async def resume(
        self, *, response: dict[str, Any]
    ) -> WorkflowRunStatus:
        """Resume a paused run with the user's response.

        Master spec §10.2 — the entry point the CLI's ``kolsch
        workflows resume`` command invokes. Loads JSONL events,
        replays them into ``_captured`` / ``_pruned`` /
        ``_completed_or_skipped``, locates the open ``user_input``
        invocation via the ``waiting_at`` sidecar, validates the
        response shape, emits the cross-process
        ``workflow.run.resumed`` + the paused node's
        ``workflow.node.completed``, then re-enters the worklist via
        :meth:`_continue_worklist`. The worklist's early-skip check
        on ``_completed_or_skipped`` ensures already-completed nodes
        don't re-fire.

        Validation order matters — bad response shape must NOT
        trigger a status flip to ``running``. The operator's retry
        with a corrected ``--input response=...`` must find the run
        still in ``waiting_for_input`` so the gate at the top of
        this method admits the retry.
        """
        from workflows.module_isolation import (  # noqa: PLC0415
            cleanup_bundle_imports,
            snapshot_sys_modules,
        )
        from workflows.run_context import (  # noqa: PLC0415
            WorkflowRunContext,
            reset_workflow_run_context,
            set_workflow_run_context,
        )

        run_id = self._run.run_id
        run_dir = self._store.directory(run_id)

        # AC 1.4 — status gate. The currently-paused run is the only
        # resumable shape; succeeded / failed / running / aborted /
        # timed_out / pending all reject.
        if self._run.status != WorkflowRunStatus.WAITING_FOR_INPUT:
            raise WorkflowResumeRejected(
                run_id=str(run_id),
                status=str(self._run.status),
            )

        # Read the open-invocation sidecar before any further side
        # effects so a missing / unreadable sidecar surfaces as
        # ``WorkflowResumeRejected(status='inconsistent_state')``
        # rather than a half-applied resume.
        #
        # Phase 3.7 note: response validation moved BELOW the
        # sidecar/replay step so we can branch on the open node's
        # type. ``user_input`` pauses still require ``{response: str}``;
        # sub-workflow pauses (operator-driven cascade) accept any
        # response shape since there's no user message to deliver —
        # the parent is only re-checking its child's status. The
        # status flip to ``RUNNING`` happens AFTER validation, so a
        # malformed user_input response still leaves the run paused
        # for retry (the original Phase 3.4 AC 1.5 invariant).
        if not run_dir.waiting_at_path.exists():
            raise WorkflowResumeRejected(
                run_id=str(run_id),
                status="inconsistent_state",
            )
        waiting_invocation_id = run_dir.waiting_at_path.read_text(
            encoding="utf-8"
        ).strip()

        # Phase 3.4 — fresh module-isolation snapshot for the resume
        # process. The pre-pause process's snapshot is gone; this
        # resume's cleanup at terminal status removes only modules
        # loaded WHILE the resume process is active.
        sys_modules_snapshot = snapshot_sys_modules()

        # Phase 3.1 Step 5 — publish run context for the resume
        # process so the tool dispatcher / authz emitters append to
        # this run's JSONL. Phase 3.10 §16.4 — ``module_ns`` rides
        # along so a resume process loads its authored Python nodes
        # under the same prefix it would have under the initial-run
        # path.
        run_context_token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run_id,
                trace_id=self._run.trace_id,
                store=self._store,
                module_ns=self._module_ns,
            )
        )

        # Initialise state from the workflow inputs BEFORE replay
        # walks the JSONL so the Phase 3.5
        # ``workflow.state.mutated`` events land on top of the
        # baseline (replay applies set / delete ops directly to
        # ``self._state``). Reversing the order — overwriting AFTER
        # replay — would wipe every pre-pause mutation; resumed
        # workflows would render ``state.*`` references against the
        # bare inputs dict.
        self._state = dict(self._run.inputs)

        # AC 1.2 — replay historical events to rebuild executor state.
        # The replay populates ``_captured`` / ``_pruned`` /
        # ``_completed_or_skipped`` / ``_state`` AND surfaces the
        # open-invocations map (the paused user_input's
        # node_name → invocation_id entry is the marker we cross-check
        # against the sidecar).
        events = list(self._store.read_events(run_id))
        open_invocations = replay_into(self, events)

        open_node: str | None = None
        for node_name, inv in open_invocations.items():
            if inv == waiting_invocation_id:
                open_node = node_name
                break
        if open_node is None:
            # Reset run context before raising so we don't leak the
            # contextvar across the rejection.
            reset_workflow_run_context(run_context_token)
            raise WorkflowResumeRejected(
                run_id=str(run_id),
                status="inconsistent_state",
            )

        # Phase 3.7 — determine whether the paused node is a
        # ``user_input`` (validate response shape; existing path) or a
        # ``kolsch/nodes/workflow`` (operator-driven cascade; the
        # parent re-checks the child's status without a response).
        open_node_def = self._workflow.nodes.get(open_node)
        open_node_type = _node_type_ref(open_node_def) if open_node_def else ""
        is_workflow_node_pause = (
            open_node_type == "kolsch/nodes/workflow"
        )

        if not is_workflow_node_pause:
            # AC 1.5 — validate response shape BEFORE any side effects
            # that would flip the status. Bad shape → retry-friendly
            # ``WorkflowInputInvalid`` with the run still in
            # ``waiting_for_input``.
            if "response" not in response or not isinstance(
                response["response"], str
            ):
                reset_workflow_run_context(run_context_token)
                raise WorkflowInputInvalid(
                    ref=str(run_id),
                    reason=(
                        "resume response must include a 'response' string; "
                        f"received keys {list(response.keys())!r}"
                    ),
                )

        # AC 1.6 — chain the resumed event to the last pre-pause
        # event in the JSONL so the cross-process event chain is
        # unbroken.
        last_pre_pause_event_id = self._last_event_id_from_events(
            events
        )
        self._last_event_id = last_pre_pause_event_id

        # Compute topology BEFORE flipping status / emitting events
        # so a topology error (shouldn't happen for an already-
        # validated workflow but belt-and-braces) surfaces cleanly.
        topo, predecessors = self._compute_topology()

        # All gates passed — flip status back to RUNNING. The
        # meta.json status guides :meth:`WorkflowRunStore.read` to
        # report ``running`` for the resume process's lifetime; the
        # final terminal event in the JSONL takes precedence at
        # terminal time.
        self._store.update_status(
            run_id, WorkflowRunStatus.RUNNING
        )

        # Phase 3.4 Step 2 — atomically refresh the PID sidecar so
        # the orphan sweep + ``ps``-style introspection see the
        # resume process, not the (long-exited) pre-pause process.
        # The pre-pause PID was left intact during the pause arm
        # (the orphan sweep gated on the ``waiting_for_input``
        # status so the stale PID wasn't reconciled); now that the
        # resume process is actively running, the PID slot is its
        # responsibility again.
        run_dir.root.mkdir(parents=True, exist_ok=True)
        tmp_pid = run_dir.pid_path.with_suffix(".tmp")
        tmp_pid.write_text(str(os.getpid()), encoding="utf-8")
        tmp_pid.replace(run_dir.pid_path)

        terminal_status: WorkflowRunStatus
        try:
            # Emit ``workflow.run.resumed`` — chained to the JSONL
            # tail event (typically the paused user_input's
            # ``workflow.node.started``).
            resumed_id = emit_event(
                self._store,
                WorkflowRunResumedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=last_pre_pause_event_id,
                    payload={
                        "resumed_at_invocation_id": (
                            waiting_invocation_id
                        ),
                    },
                ),
            )
            self._last_event_id = resumed_id

            if is_workflow_node_pause:
                # Phase 3.7 — sub-workflow pause cascade. We do NOT
                # emit a completion event here; the parent's
                # WorkflowSubRunner will re-fire under the worklist
                # (with ``ctx.is_resume_mode=True`` + the recorded
                # child_run_id) and either harvest the child's output
                # (the runner emits the trailing
                # ``called_workflow_completed`` then returns the
                # harvested dict, and the worklist body emits
                # ``workflow.node.completed`` per its usual path) OR
                # raise (the executor's failure path emits
                # ``workflow.node.failed``).
                #
                # Record the open invocation so ``_continue_worklist``
                # reuses the original invocation_id instead of minting
                # a fresh one (preserves the cross-process chain — the
                # JSONL already carries the original
                # ``workflow.node.started`` event for this invocation).
                recorded_child = self._workflow_node_child_runs.get(
                    waiting_invocation_id
                )
                if recorded_child is None:
                    # Inconsistent: the open node is a workflow node
                    # but no ``called_workflow`` marker was replayed.
                    # Either the pause happened BEFORE the child was
                    # spawned (impossible — the runner emits
                    # ``called_workflow`` before the nested executor
                    # starts) OR the JSONL was edited. Reject loudly.
                    reset_workflow_run_context(run_context_token)
                    raise WorkflowResumeRejected(
                        run_id=str(run_id),
                        status="inconsistent_state",
                    )
                self._resumed_open_workflow_invocations[open_node] = (
                    InvocationId(waiting_invocation_id),
                    WorkflowRunId(recorded_child),
                )
                # Seed the per-invocation chain pointer at the
                # resumed-event id so the runner's first emit chains
                # back through the resume boundary.
                self._invocation_last_event_id[
                    InvocationId(waiting_invocation_id)
                ] = resumed_id
            else:
                # Emit ``workflow.node.completed`` for the paused
                # user_input — payload's ``output`` carries the
                # operator response verbatim (the dict the CLI parsed
                # from ``--input k=v`` pairs).
                completed_payload: dict[str, Any] = {
                    "node_name": open_node,
                    "invocation_id": waiting_invocation_id,
                    "output": dict(response),
                }
                completion_id = emit_event(
                    self._store,
                    WorkflowNodeCompletedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=resumed_id,
                        invocation_id=InvocationId(
                            waiting_invocation_id
                        ),
                        node_name=open_node,
                        payload=completed_payload,
                    ),
                )
                self._last_event_id = completion_id
                self._node_completed_event_id[open_node] = completion_id
                self._captured[open_node] = dict(response)
                self._completed_or_skipped.add(open_node)
            # For the workflow-node pause case we DELIBERATELY leave
            # ``_completed_or_skipped`` / ``_captured`` untouched for
            # ``open_node`` — the worklist below will re-fire the
            # workflow node (via the ``_resumed_open_workflow_invocations``
            # marker) and that re-fire is what produces the captured
            # output + completion event.

            # Re-enter the worklist. The early-skip check on
            # ``_completed_or_skipped`` short-circuits every node
            # we replayed (and the user_input we just completed);
            # the dispatch picks up at the first downstream node.
            terminal_status = await self._continue_worklist(
                run_id=run_id,
                run_dir=run_dir,
                topo=topo,
                predecessors=predecessors,
            )
        finally:
            # Phase 3.10 §16.4 — per-run prefix cleanup (alongside
            # the Phase 3.1 bundle cleanup; both fire during the
            # migration window per AC 2.5).
            self._module_ns.cleanup_for_run()
            cleanup_bundle_imports(
                before=sys_modules_snapshot,
                workspace_root=self._store.runs_root.parent,
            )
            reset_workflow_run_context(run_context_token)
        return terminal_status

    @staticmethod
    def _last_event_id_from_events(
        events: list[dict[str, Any]],
    ) -> Any:  # noqa: ANN401 — EventId envelope field
        """Return the ``event_id`` of the last event in ``events``.

        Master spec §11.2 — ``parent_event_id`` chains form the
        causal-parent backbone. The resume process emits its first
        event (``workflow.run.resumed``) chained to whatever was the
        JSONL tail at pause time (typically the paused user_input
        node's ``workflow.node.started``).
        """
        if not events:
            return None
        return events[-1].get("event_id")

    # ------------------------------------------------------------------
    # Fork entry point — Phase 3.8 Step 1 (master spec §9).
    # ------------------------------------------------------------------

    async def start_from_fork(  # noqa: PLR0915, PLR0912 — fork is structurally replay+continue; splitting hides the §9.1 ordering
        self,
        *,
        fork_event_id: str,
        input_overrides: dict[str, Any] | None = None,
    ) -> WorkflowRunStatus:
        """Fork a workflow run from a specific event in a parent run.

        Master spec §9.1 — the entry point ``kolsch workflows fork``
        invokes. Mirrors :meth:`resume` structurally: replay parent
        events to rebuild ``_captured`` / ``_state`` / ``_pruned`` /
        ``_completed_or_skipped``, then re-enter :meth:`_continue_worklist`
        so the SAME dispatch loop services initial-run / resume /
        fork. The differences vs. resume:

        - The parent's JSONL is read (not this run's). The forked run's
          JSONL starts EMPTY; this method writes its first event.
        - The first event written is :class:`WorkflowRunForkedFromEvent`
          (NOT ``workflow.run.started`` — the forked run inherits the
          parent's started baseline by replaying it). The new event
          kind keeps event-stream consumers' switch statements clean
          (mirrors Phase 3.4's ``workflow.run.resumed`` discipline).
        - ``input_overrides`` replace the fork-point node's captured
          output keys (or workflow-level inputs when forking at the
          ``workflow.run.started`` event).

        Side-effect discipline: upstream side effects (bash writes,
        external API calls, sub-workflow runs) are NOT undone. The
        forked run starts with the filesystem / external services in
        whatever state the parent's pre-fork prefix left them (spec
        §9.2). This is a locked decision — fork-and-undo lives in
        deferred scope.
        """
        from workflows.module_isolation import (  # noqa: PLC0415
            cleanup_bundle_imports,
            snapshot_sys_modules,
        )
        from workflows.run_context import (  # noqa: PLC0415
            WorkflowRunContext,
            reset_workflow_run_context,
            set_workflow_run_context,
        )

        overrides: dict[str, Any] = dict(input_overrides or {})
        run_id = self._run.run_id
        run_dir = self._store.directory(run_id)

        # Sanity — the caller (``_cmd_fork`` in Phase 3.8 Step 2)
        # constructs this run with ``forked_from`` populated. A bare
        # call without that pointer is a programming error, not an
        # operator-input error; surface as ``ValueError`` rather than
        # a WorkflowRuntimeError so the dispatcher's exception arms
        # don't accidentally render it as a CLI envelope.
        if self._run.forked_from is None:
            msg = (
                "start_from_fork requires WorkflowRun.forked_from to be "
                "set; construct the run via WorkflowRunStore.create("
                "forked_from=ForkedFrom(...))"
            )
            raise ValueError(msg)

        parent_id = WorkflowRunId(
            str(self._run.forked_from.run_id)
        )
        parent_dir = self._store.directory(parent_id)

        # Two distinct missing-parent shapes both raise the same
        # ``WorkflowRunNotFound`` per spec §9.5:
        #  - meta.json gone → store.read raises FileNotFoundError
        #  - events.jsonl gone (meta still present) → silent empty
        #    iterator from read_events; the operator's fork_event_id
        #    would otherwise look like a "not found" miss. Explicit
        #    check upfront makes the error precise.
        try:
            self._store.read(parent_id)
        except FileNotFoundError as exc:
            raise WorkflowRunNotFound(run_id=str(parent_id)) from exc
        if not parent_dir.events_path.exists():
            raise WorkflowRunNotFound(run_id=str(parent_id))

        # Read parent's events; locate the fork point. The slice is
        # inclusive — replay_into walks the fork event itself so a
        # fork at a ``workflow.node.completed`` correctly populates
        # ``_captured[node]`` from that event's payload.
        parent_events = list(self._store.read_events(parent_id))
        lookup = find_event_by_id(parent_events, fork_event_id)
        if lookup is None:
            msg = (
                f"fork_event_id {fork_event_id!r} not found in parent run "
                f"{str(parent_id)!r}'s events.jsonl. If you copied this id "
                f"from a chat capsule or agent session, fork at the "
                f"workflow-level event preceding the agent invocation "
                f"instead — spawned-session events (model.request.sent, "
                f"session.*) cannot be fork points (spec §9.5)."
            )
            raise WorkflowRuntimeError(msg)
        events_to_replay, fork_event = lookup

        # Mirror :meth:`run` / :meth:`resume` — fresh
        # ``sys.modules`` snapshot scoped to this process so the
        # cleanup at terminal status only removes what THIS process
        # loaded under ``bundles/``.
        sys_modules_snapshot = snapshot_sys_modules()

        # Publish the run context for the fork process. Sub-systems
        # (authz, tool dispatcher) append to THIS run's JSONL — not
        # the parent's — so the run id binding here is critical.
        # Phase 3.10 §16.4 — ``module_ns`` rides along so authored
        # Python nodes load under the FORKED run's prefix, not the
        # parent's; the parent's prefix-tagged entries already
        # belong to the parent process's cleanup.
        run_context_token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run_id,
                trace_id=self._run.trace_id,
                store=self._store,
                module_ns=self._module_ns,
            )
        )

        # Seed ``_state`` from the forked run's inputs BEFORE replay
        # so :func:`replay_into` applies ``workflow.state.mutated``
        # ops on top of the baseline. Same ordering invariant as
        # :meth:`resume`.
        self._state = dict(self._run.inputs)

        # Replay the parent's prefix into THIS executor's private
        # fields. Phase 3.4/3.5/3.7 handlers are reused verbatim;
        # the resulting state is byte-equivalent to whatever the
        # parent's executor held at the fork point — minus any
        # in-memory bookkeeping that the JSONL doesn't capture
        # (which by design IS the recoverable state).
        replay_into(self, events_to_replay)

        # Apply ``--input`` overrides per the spec §9.1 contract:
        # "replaces captured outputs at the fork point". The fork
        # event identifies WHICH node owns the captured output to
        # mutate. Two valid shapes:
        #   - fork at ``workflow.node.completed``: replace keys in
        #     ``_captured[<node>]`` (preserves untouched keys).
        #   - fork at ``workflow.run.started``: replace
        #     workflow-level inputs (and ``_state`` since state was
        #     initialized from inputs above).
        # Any other fork-event kind with overrides raises — the
        # operator probably wanted to fork at a completion event.
        fork_event_kind = fork_event.get("kind")
        if fork_event_kind == "workflow.node.completed":
            target_node = fork_event.get("node_name") or fork_event.get(
                "payload", {}
            ).get("node_name")
            if overrides:
                if target_node is None:
                    msg = (
                        f"fork event {fork_event_id!r} (kind "
                        f"{fork_event_kind!r}) has no resolvable "
                        "node_name; cannot route --input overrides"
                    )
                    raise WorkflowRuntimeError(msg)
                bucket = self._captured.setdefault(str(target_node), {})
                for key, value in overrides.items():
                    bucket[key] = value
        elif fork_event_kind == "workflow.run.started":
            for key, value in overrides.items():
                self._run.inputs[key] = value
                self._state[key] = value
        elif overrides:
            msg = (
                "--input overrides require forking at a "
                "workflow.node.completed or workflow.run.started event; "
                f"fork event {fork_event_id!r} is kind "
                f"{fork_event_kind!r}"
            )
            raise WorkflowRuntimeError(msg)

        # Compute topology BEFORE flipping status. Same ordering as
        # :meth:`resume` — a defensive topology error surfaces with
        # the run still in PENDING (the CLI's error envelope is the
        # right place for that, not a half-flipped status).
        topo, predecessors = self._compute_topology()

        # Flip status → RUNNING. The on-disk JSONL has no events yet,
        # so :meth:`WorkflowRunStore.read`'s tail scan returns no
        # terminal status; the meta sidecar's status governs.
        self._store.update_status(run_id, WorkflowRunStatus.RUNNING)

        # PID sidecar — atomic. The fork is a fresh process per Phase
        # 3.1 Step 4 + Phase 3.4 Step 2 discipline; the orphan sweep
        # will see THIS process if the fork crashes.
        run_dir.root.mkdir(parents=True, exist_ok=True)
        tmp_pid = run_dir.pid_path.with_suffix(".tmp")
        tmp_pid.write_text(str(os.getpid()), encoding="utf-8")
        tmp_pid.replace(run_dir.pid_path)

        terminal_status: WorkflowRunStatus
        try:
            # Emit ``workflow.run.forked_from`` as the FIRST event on
            # the forked JSONL. ``parent_event_id`` is None — this IS
            # the chain root for the forked run. The cross-JSONL link
            # to the parent run lives in the payload (forked_from_run_id
            # + fork_event_id) plus the inherited trace_id.
            forked_id = emit_event(
                self._store,
                WorkflowRunForkedFromEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=None,
                    payload=WorkflowRunForkedFromPayload(
                        forked_from_run_id=str(parent_id),
                        fork_event_id=fork_event_id,
                        input_overrides=dict(overrides),
                    ),
                ),
            )
            self._last_event_id = forked_id

            # Re-enter the worklist. The replay populated
            # ``_completed_or_skipped`` with every replayed completion
            # + prune; the early-skip check at the top of
            # ``_continue_worklist`` short-circuits those nodes, and
            # dispatch picks up at the first downstream node whose
            # predecessors are all decided.
            terminal_status = await self._continue_worklist(
                run_id=run_id,
                run_dir=run_dir,
                topo=topo,
                predecessors=predecessors,
            )
        finally:
            # Phase 3.10 §16.4 — per-run prefix cleanup (alongside
            # the Phase 3.1 bundle cleanup; both fire during the
            # migration window per AC 2.5).
            self._module_ns.cleanup_for_run()
            cleanup_bundle_imports(
                before=sys_modules_snapshot,
                workspace_root=self._store.runs_root.parent,
            )
            reset_workflow_run_context(run_context_token)
        return terminal_status

    # ------------------------------------------------------------------
    # Worklist body — shared by initial-run and resume paths.
    # ------------------------------------------------------------------

    async def _continue_worklist(  # noqa: PLR0915, PLR0912 — sequential algorithm; splitting hides the §8.1 ordering
        self,
        *,
        run_id: WorkflowRunId,
        run_dir: RunDirectory,
        topo: list[str],
        predecessors: dict[str, set[str]],
    ) -> WorkflowRunStatus:
        """Walk the worklist; return the terminal status.

        Extracted from :meth:`run` so :meth:`resume` shares the same
        dispatch loop. The only behavioural addition vs. the
        original inline body is the early-skip check on
        ``_completed_or_skipped`` at the top of the loop: it's a
        no-op for initial-run flow (the set is empty at start) and
        the load-bearing guard for resume (the replay pre-populates
        the set with every completed + pruned node, plus the
        just-completed user_input).

        Phase 3.10 §16.2 dispatch fork — ``auto``-mode workflows
        route through :meth:`_continue_worklist_concurrent` instead;
        the body below remains the byte-identical sequential path
        for every Phase 3.1 — Phase 3.9 contract.
        """
        if self._workflow.concurrency == "auto":
            return await self._continue_worklist_concurrent(
                run_id=run_id,
                run_dir=run_dir,
                topo=topo,
                predecessors=predecessors,
            )
        for node_name in topo:
            # Phase 3.4 Step 1 — resume support. Skip nodes whose
            # decision is already recorded (replayed from the JSONL
            # OR completed by the resume entry's user_input emission).
            # For initial-run the set is empty so this never fires.
            if node_name in self._completed_or_skipped:
                continue

            # Skip downstream nodes whose predecessors didn't all
            # reach a "completed or skipped" decision. The
            # validated DAG guarantees topo order respects the
            # predecessor relation, so a False here means a
            # short-circuit fired earlier in the walk.
            if not predecessors[node_name].issubset(
                self._completed_or_skipped
            ):
                # No way for the node to fire — mark it skipped so
                # downstream consumers also defer cleanly.
                self._completed_or_skipped.add(node_name)
                continue

            # Phase 3.3 Step 2 — pruned nodes (marked by the
            # cascade triggered when their last unblocked inbound
            # was suppressed) skip directly without emitting any
            # ``workflow.node.*`` framing. The cascade already
            # added the node to ``_completed_or_skipped`` AND
            # ``_pruned`` and emitted the
            # ``workflow.node.pruned`` event when appropriate.
            if node_name in self._pruned:
                continue

            node_def = self._workflow.nodes[node_name]
            # ``when:`` gate — Round 6 J5/β: a string with ``{{``
            # is Jinja; everything else (including bare booleans)
            # passes through literally.
            if not self._when_holds(node_def):
                # AND-strict rule 1: skip counts as completion.
                self._completed_or_skipped.add(node_name)
                # Phase 3.3 Step 2 — ``when:``-skip cascade
                # symmetry with branch-not-taken. The skipped
                # node never produced a captured output, so
                # every outgoing edge is structurally pruned
                # (the downstream's ``_render_bound_inputs``
                # must skip these stale bind RHSs — they'd
                # otherwise reference ``nodes.<skipped>.X``
                # against an absent capture). The cascade then
                # walks descendants whose only path was through
                # this node. No completion event was emitted
                # for the skipped source so cascaded prune
                # events have no parent_event_id to chain to —
                # the cascade emits no events for when-skip
                # subgraphs (preserves Phase 3.1's "skip is
                # silent" convention for when-gates).
                for edge in self._outgoing_edges(node_name):
                    self._pruned_edges.add((edge.from_, edge.to))
                self._cascade_prune(
                    source=node_name,
                    source_completion_event_id=None,
                    branch_value=None,
                )
                continue

            # Render this node's bound inputs from inbound edges.
            bound_inputs = self._render_bound_inputs(node_name)

            # Phase 3.7 — sub-workflow resume cascade. When the resume
            # entry recorded this node as an OPEN workflow-node
            # invocation (the parent was paused waiting for a child
            # to terminate), reuse the original invocation_id + skip
            # the fresh ``workflow.node.started`` emission (the
            # pre-pause process already wrote that event into the
            # JSONL). The WorkflowSubRunner branches on
            # ``ctx.is_resume_mode`` to read the child's status
            # instead of spawning a new child.
            resume_marker = self._resumed_open_workflow_invocations.pop(
                node_name, None
            )
            if resume_marker is not None:
                inv_id, recorded_child_run_id = resume_marker
                # The per-invocation chain pointer was seeded at the
                # resumed-event id in :meth:`resume`; do not overwrite.
                started_id = self._node_started_event_id.get(
                    inv_id, self._last_event_id
                )
                is_resume_mode = True
                recorded_child = recorded_child_run_id
            else:
                # Mint a fresh invocation id; emit
                # ``workflow.node.started``. Master spec §11.3 —
                # node-scoped events carry ``invocation_id`` +
                # ``node_name`` at the envelope top level; payload
                # also holds the legacy keys for back-compat with
                # Step 1-4 readers.
                inv_id = InvocationId(f"inv_{uuid.uuid4()}")
                started_payload: dict[str, Any] = {
                    "node_name": node_name,
                    "invocation_id": str(inv_id),
                    "type": _node_type_ref(node_def),
                    "inputs": bound_inputs,
                    # Master spec §11.3 caller_chain carrier:
                    # the workflow run's own caller_chain
                    # (the frames it was invoked through).
                    # v1 ships the empty tuple — the chain
                    # is populated by Phase 3.7 sub-workflow
                    # / fork wiring.
                    "caller_chain": list(
                        getattr(self._run, "caller_chain", ()) or ()
                    ),
                }
                # Phase 3.10 §16.3 — map nodes carry concurrency_mode +
                # concurrency on their own started payload so JSONL
                # consumers can identify sequential-vs-parallel
                # iteration without re-reading the source workflow.
                # The per-iteration started events (emitted by the map
                # runner itself) continue to carry §11.3
                # parent_invocation_id + iteration_index per Phase 3.7.
                if started_payload["type"] == "kolsch/nodes/map":
                    started_payload["concurrency_mode"] = (
                        "parallel"
                        if getattr(node_def, "parallel", False)
                        else "sequential"
                    )
                    started_payload["concurrency"] = getattr(
                        node_def, "concurrency", None
                    )
                started_id = emit_event(
                    self._store,
                    WorkflowNodeStartedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._last_event_id,
                        invocation_id=inv_id,
                        node_name=node_name,
                        payload=started_payload,
                    ),
                )
                self._last_event_id = started_id
                self._node_started_event_id[inv_id] = started_id
                is_resume_mode = False
                recorded_child = None
            # Phase 3.5 Step 1 — seed the per-invocation chain pointer
            # at the started event id. State runners advance it via
            # ``ctx.emit_workflow_event``; completion reads it.
            # Phase 3.7 — for sub-workflow resume the pointer was
            # already seeded at the resumed-event id in :meth:`resume`
            # so the runner's first emit chains to the resume
            # boundary (not back to the pre-pause started event).
            if not is_resume_mode:
                self._invocation_last_event_id[inv_id] = started_id

            ctx = self._build_ctx(
                inv_id=inv_id,
                node_def=node_def,
                parent_event_id=self._invocation_last_event_id.get(
                    inv_id, started_id
                ),
                is_resume_mode=is_resume_mode,
                recorded_child_run_id=recorded_child,
            )
            try:
                # Phase 3.3 §6.4.4 — strict-mode bind validation
                # against the receiver's typed ``Inputs(BaseModel)``.
                # Mismatch raises ``WorkflowBindTypeMismatch``
                # BEFORE the runner is invoked; the catch arm
                # below emits ``workflow.node.failed`` and the
                # run aborts via the existing Phase 3.1 failure
                # path. ``inputs_model_for`` returns ``None`` for
                # authored Python @nodes in Step 0 — Phase 3.3
                # Step 1 wires the catalog runner's introspected
                # model in alongside the branching dispatcher.
                self._validate_bound_inputs_strict(
                    node_name=node_name,
                    node_def=node_def,
                    bound_inputs=bound_inputs,
                )
                runner = self._registry.get(
                    _node_type_ref(node_def), node_name=node_name
                )
                # Per-node modifiers — apply retry FIRST so the
                # timeout deadline bounds the WHOLE retry budget
                # (not just the final attempt).
                runner = _apply_node_modifiers(runner, node_def=node_def)
                try:
                    output = await runner.run(
                        node_name=node_name,
                        node_def=node_def,
                        bound_inputs=bound_inputs,
                        state=self._state,
                        ctx=ctx,
                    )
                except BashNonZeroExit as bash_exc:
                    # Phase 3.3 §8.5.5 — branchable bash semantics.
                    # When the bash node carries at least one
                    # outgoing edge with ``branch: not None`` the
                    # non-zero exit is success-of-the-false-arm:
                    # surface the captured dict as the node's
                    # output and let the branch dispatcher route
                    # to ``branch: false`` edges. Without any
                    # ``branch:`` edge the non-zero exit
                    # propagates to the existing failure path so
                    # Phase 3.1's abort-on-failure rule still
                    # holds for plain bash usage.
                    if self._bash_treats_nonzero_as_success(node_name):
                        output = bash_exc.captured
                    else:
                        raise
            except WorkflowPausedForInput as pause:
                # Phase 3.4 — pause discipline (master spec §10.1).
                # The ``user_input`` runner raised the typed pause
                # signal AFTER the executor already emitted the
                # matching ``workflow.node.started`` event for
                # ``inv_id``. Per the spec, the absence of a
                # completion event in the JSONL IS the pause
                # marker. We DO NOT emit ``workflow.node.failed``
                # (this is normal control flow, not failure) and
                # we DO NOT emit ``workflow.run.failed`` /
                # ``workflow.run.completed`` (resume continues the
                # JSONL as one continuous stream across
                # processes).
                #
                # Two on-disk side effects:
                #
                # 1. ``waiting_at`` sidecar — the open invocation
                #    id, written atomically via ``tmp + rename``
                #    so resume can identify the open invocation
                #    without walking the JSONL backwards. For a
                #    SECOND pause (sequential user_input nodes),
                #    this OVERWRITES the previous sidecar.
                # 2. ``meta.json`` status flips to
                #    ``waiting_for_input`` so :meth:`read` (and
                #    the orphan sweep) observe the paused state.
                #
                # Module-isolation cleanup runs in the caller's
                # ``finally:`` arm — paused processes exit cleanly
                # and the next process (resume OR a new run on a
                # different workflow) starts from a fresh
                # ``sys.modules`` slate.
                tmp_wait = run_dir.waiting_at_path.with_suffix(
                    ".tmp"
                )
                tmp_wait.write_text(
                    pause.invocation_id, encoding="utf-8"
                )
                tmp_wait.replace(run_dir.waiting_at_path)
                self._store.update_status(
                    run_id, WorkflowRunStatus.WAITING_FOR_INPUT
                )
                return WorkflowRunStatus.WAITING_FOR_INPUT
            except Exception as exc:  # noqa: BLE001 — record + abort
                failed_id = emit_event(
                    self._store,
                    WorkflowNodeFailedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._node_started_event_id.get(
                            inv_id
                        ),
                        invocation_id=inv_id,
                        node_name=node_name,
                        payload={
                            "node_name": node_name,
                            "invocation_id": str(inv_id),
                            "error_class": type(exc).__name__,
                            "message": str(exc),
                        },
                    ),
                )
                self._last_event_id = emit_event(
                    self._store,
                    WorkflowRunFailedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=failed_id,
                        payload={
                            "reason": (
                                f"node {node_name!r} raised "
                                f"{type(exc).__name__}"
                            ),
                        },
                    ),
                )
                self._cleanup_waiting_at(run_dir)
                return WorkflowRunStatus.FAILED

            # Successful invocation — capture, mark complete.
            self._captured[node_name] = dict(output)
            self._completed_or_skipped.add(node_name)
            # Phase 3.3 §6.5 / §6.6 — branch dispatch. Compute
            # the source's branch value (``None`` for non-
            # branchable types) and record any outgoing edge
            # whose ``branch:`` field doesn't match as pruned.
            # Step 1 surfaces only the suppression — Step 2 ships
            # the cascade through descendants + the
            # ``workflow.node.pruned`` event emission.
            branch_value = compute_branch_value(node_def, output)
            self._dispatch_branch(
                node_name=node_name,
                branch_value=branch_value,
                inv_id=inv_id,
            )
            # Phase 3.2 §11.6.3 — agent nodes attach the spawned
            # session id so consumers can navigate from the
            # workflow event view to the spawned session's JSONL.
            # The runner stamps ``ctx.spawned_session_id`` before
            # returning; the gate on ``node_def.type`` is the
            # defensive belt-and-braces guarantee that a future
            # runner that forgets to clear the attribute can't
            # pollute non-agent payloads.
            completed_payload: dict[str, Any] = {
                "node_name": node_name,
                "invocation_id": str(inv_id),
                "output": dict(output),
            }
            # Phase 3.3 §11.1 — branch_value lands in the payload
            # ONLY when the node is actually USED as branchable
            # (carries at least one outgoing edge with
            # ``branch: not None``). Structurally branchable
            # nodes (e.g. plain non-branching bash) suppress the
            # field so non-branching workflows produce
            # byte-identical JSONLs vs. Phase 3.1 — the master
            # spec's no-regressions promise. Forensics readers
            # rely on presence-or-absence to decide whether the
            # node's outgoing edges were filtered.
            if (
                branch_value is not None
                and any(
                    edge.branch is not None
                    for edge in self._outgoing_edges(node_name)
                )
            ):
                completed_payload["branch_value"] = branch_value
            if (
                _node_type_ref(node_def) == "kolsch/nodes/agent"
                and ctx.spawned_session_id is not None
            ):
                completed_payload["spawned_session_id"] = str(
                    ctx.spawned_session_id
                )
            # Phase 3.7 §11.2 — workflow nodes attach the spawned
            # child run id so consumers can navigate from the parent's
            # workflow event view to the child's JSONL. Mirrors the
            # Phase 3.2 ``spawned_session_id`` shape for agent nodes;
            # gated by BOTH ``type == "kolsch/nodes/workflow"`` AND
            # ``spawned_run_id is not None`` so a future runner that
            # forgets to clear the attribute can't pollute non-workflow
            # payloads.
            if (
                _node_type_ref(node_def) == "kolsch/nodes/workflow"
                and ctx.spawned_run_id is not None
            ):
                completed_payload["child_run_id"] = str(ctx.spawned_run_id)
            # Phase 3.5 §11.3 — completion chains to the LAST
            # in-invocation event (the last ``workflow.state.mutated``
            # for state-mutating nodes; the node's own started event
            # otherwise — ``_invocation_last_event_id`` is seeded with
            # started_id at fire time and only advanced by
            # ``ctx.emit_workflow_event``).
            completion_id = emit_event(
                self._store,
                WorkflowNodeCompletedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._invocation_last_event_id.get(
                        inv_id,
                        self._node_started_event_id.get(inv_id),
                    ),
                    invocation_id=inv_id,
                    node_name=node_name,
                    payload=completed_payload,
                ),
            )
            self._last_event_id = completion_id
            self._node_completed_event_id[node_name] = completion_id

            # Phase 3.3 Step 2 — pruning cascade from the
            # branch-dispatch source. Every downstream node whose
            # only path to firing was through a pruned arm of THIS
            # source (or a descendant of it that also pruned)
            # lands in ``_pruned`` and emits
            # ``workflow.node.pruned`` chained to this completion
            # event. The cascade is a no-op for non-branchable
            # sources (they pruned no edges).
            self._cascade_prune(
                source=node_name,
                source_completion_event_id=completion_id,
                branch_value=branch_value,
            )

            # Phase 3.1 Step 4 — cooperative cancellation. The
            # ``cancel.flag`` sentinel is the CLI / operator's
            # signal for "stop after the current node finishes".
            # We check it AFTER ``workflow.node.completed`` is
            # emitted so the JSONL records the last successful
            # node before the run aborts (master spec §11.2).
            if run_dir.cancel_flag_path.exists():
                self._emit_aborted(run_id)
                self._cleanup_waiting_at(run_dir)
                return WorkflowRunStatus.ABORTED

        # Step 5 — assemble terminal-keyed outputs. Phase 3.10 §16.9 —
        # ``peak_in_flight`` is added to the payload ONLY when the
        # workflow opts into ``auto`` so sequential JSONL stays
        # byte-identical to Phase 3.9; the CLI envelope defaults to
        # ``1`` when the field is absent.
        outputs = self._assemble_terminal_outputs(topo)
        completed_payload: dict[str, Any] = {"outputs": outputs}
        if self._workflow.concurrency == "auto":
            completed_payload["peak_in_flight"] = self._peak_in_flight
        self._last_event_id = emit_event(
            self._store,
            WorkflowRunCompletedEvent(
                workflow_run_id=run_id,
                trace_id=self._run.trace_id,
                parent_event_id=self._last_event_id,
                payload=completed_payload,
            ),
        )
        # Phase 3.4 Step 1 — successful terminal post-resume: the
        # ``waiting_at`` sidecar from the prior pause is stale; the
        # JSONL now carries a terminal event so the sidecar is no
        # longer the on-disk marker for anything. The cleanup is a
        # no-op for initial-run flow (no sidecar was ever written).
        self._cleanup_waiting_at(run_dir)
        return WorkflowRunStatus.SUCCEEDED

    # ------------------------------------------------------------------
    # Phase 3.10 §16.2.1 — concurrent worklist for ``auto`` mode.
    # ------------------------------------------------------------------

    async def _continue_worklist_concurrent(  # noqa: PLR0915, PLR0912 — concurrent algorithm; splitting hides the §16.2.1 ordering
        self,
        *,
        run_id: WorkflowRunId,
        run_dir: RunDirectory,
        topo: list[str],
        predecessors: dict[str, set[str]],
    ) -> WorkflowRunStatus:
        """Phase 3.10 §16.2.1 — fan-out ready nodes via ``asyncio.gather``.

        Mirrors the sequential :meth:`_continue_worklist` body's per-node
        contract (when-gate, bind render, started/completed framing,
        cascade prune) but dispatches every ready node as a task and
        awaits the FIRST completion before recomputing the ready set.
        The run-start gate's :func:`workflows.validator._check_concurrency_invariants`
        already rejected workflows where this is unsafe, so the body
        below trusts that disjoint-write invariants hold.

        The dispatch loop's invariants:

        - ``cancel.flag`` is polled at the TOP of every iteration (NOT
          inside per-node tasks). On detection: every in-flight task
          is cancelled, ``workflow.run.aborted`` is emitted, sibling
          cancellations land their own ``workflow.node.failed`` with
          ``reason="user_cancel"``.
        - First failure wins: when an in-flight task raises, the
          remaining tasks are cancelled and emit
          ``workflow.node.failed`` with ``reason="sibling_failure"``;
          the run terminates as ``FAILED`` with the original cause on
          the ``workflow.run.failed`` event.
        - State-mutating runners (``set_state`` / ``delete_state``)
          serialise via :attr:`_state_mutation_lock` so the
          ``workflow.state.mutated`` stream is total-ordered in the
          JSONL (master spec §11.3 / §16.2.1).
        """
        in_flight: dict[asyncio.Task[dict[str, Any]], _PendingNode] = {}
        try:
            while True:
                # cancel.flag at the TOP of every iteration — once per
                # completed batch, not once per node (master spec §16.6).
                if run_dir.cancel_flag_path.exists():
                    await self._cancel_in_flight_and_emit_failed(
                        in_flight=in_flight,
                        run_id=run_id,
                        reason="user_cancel",
                    )
                    async with self._emit_lock:
                        self._emit_aborted(run_id)
                    self._cleanup_waiting_at(run_dir)
                    return WorkflowRunStatus.ABORTED

                # Compute ready set: nodes whose preds are all decided,
                # not already completed/pruned/in-flight.
                in_flight_names = {
                    pending.node_name for pending in in_flight.values()
                }
                ready_now: list[str] = []
                for node_name in topo:
                    if node_name in self._completed_or_skipped:
                        continue
                    if node_name in in_flight_names:
                        continue
                    if node_name in self._pruned:
                        continue
                    if not predecessors[node_name].issubset(
                        self._completed_or_skipped
                    ):
                        continue
                    ready_now.append(node_name)

                # Dispatch every ready node as a task.
                for node_name in ready_now:
                    pending = await self._prepare_concurrent_dispatch(
                        node_name=node_name, run_id=run_id
                    )
                    if pending is None:
                        # when-gate skipped or pruned — already updated
                        # the bookkeeping; no task to spawn.
                        continue
                    task = asyncio.create_task(
                        self._invoke_node_runner_for_dispatch(pending)
                    )
                    in_flight[task] = pending
                    if len(in_flight) > self._peak_in_flight:
                        self._peak_in_flight = len(in_flight)

                # Nothing left to do? Terminal success.
                if not in_flight:
                    break

                # Await first task to complete (success OR failure).
                # Master spec §16.6 — ``cancel.flag`` is polled at
                # gather-loop boundaries. A short timeout keeps the
                # flag observably responsive even when every in-flight
                # task is long-running (bash subprocesses, agent
                # calls) and would otherwise defer the next check
                # until at least one task completes. The timeout
                # loops back to the cancel.flag check; if no task
                # completed in that window, the loop simply
                # re-checks the flag and re-dispatches as needed.
                done, _pending_set = await asyncio.wait(
                    list(in_flight.keys()),
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=0.1,
                )
                if not done:
                    # No completions yet; loop back to the cancel.flag
                    # check at the top so a long-running batch stays
                    # cancellable.
                    continue

                # First failure cancels siblings — master spec §16.2.2.
                first_failure: tuple[_PendingNode, BaseException] | None = None
                for task in done:
                    pending = in_flight.pop(task)
                    exc = task.exception()
                    if exc is None:
                        output = task.result()
                        await self._finalize_concurrent_success(
                            pending=pending, output=output, run_id=run_id
                        )
                        continue
                    if first_failure is None:
                        first_failure = (pending, exc)
                    # Defer cancellation of siblings until after this
                    # for-loop so we don't process them in this batch.

                if first_failure is not None:
                    failing_pending, failing_exc = first_failure
                    await self._emit_node_failure_concurrent(
                        pending=failing_pending,
                        exc=failing_exc,
                        run_id=run_id,
                    )
                    await self._cancel_in_flight_and_emit_failed(
                        in_flight=in_flight,
                        run_id=run_id,
                        reason="sibling_failure",
                    )
                    await self._emit_run_failed_concurrent(
                        node_name=failing_pending.node_name,
                        exc=failing_exc,
                        run_id=run_id,
                    )
                    self._cleanup_waiting_at(run_dir)
                    return WorkflowRunStatus.FAILED

            # Terminal success — emit run.completed (always carries
            # peak_in_flight under auto mode per master spec §16.9).
            outputs = self._assemble_terminal_outputs(topo)
            completed_payload: dict[str, Any] = {
                "outputs": outputs,
                "peak_in_flight": self._peak_in_flight,
            }
            async with self._emit_lock:
                self._last_event_id = emit_event(
                    self._store,
                    WorkflowRunCompletedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._last_event_id,
                        payload=completed_payload,
                    ),
                )
            self._cleanup_waiting_at(run_dir)
            return WorkflowRunStatus.SUCCEEDED
        except BaseException:
            # Safety net — any uncaught exception out of the dispatch
            # loop (e.g. a programming bug) must cancel orphaned tasks
            # so asyncio doesn't complain about pending futures at
            # shutdown.
            for task in list(in_flight):
                if not task.done():
                    task.cancel()
            for task in list(in_flight):
                try:
                    await task
                except BaseException:  # noqa: BLE001 — drain only
                    pass
            raise

    async def _prepare_concurrent_dispatch(
        self,
        *,
        node_name: str,
        run_id: WorkflowRunId,
    ) -> _PendingNode | None:
        """Pre-dispatch logic for one node — mirrors the sequential
        body's lines from when-gate through ``workflow.node.started``.

        Returns ``None`` when the node was when-skipped or pruned (the
        bookkeeping has been updated and no task should spawn); returns
        a :class:`_PendingNode` carrying the inv_id + ctx + bound
        inputs needed to invoke the runner.
        """
        node_def = self._workflow.nodes[node_name]

        if not self._when_holds(node_def):
            # AND-strict rule 1 — when-skip cascades the same way the
            # sequential body does (silent — no events for when-skip
            # subgraphs, per Phase 3.3 Step 2 convention).
            self._completed_or_skipped.add(node_name)
            for edge in self._outgoing_edges(node_name):
                self._pruned_edges.add((edge.from_, edge.to))
            self._cascade_prune(
                source=node_name,
                source_completion_event_id=None,
                branch_value=None,
            )
            return None

        bound_inputs = self._render_bound_inputs(node_name)

        # Mint invocation id, emit started event under the emit lock.
        inv_id = InvocationId(f"inv_{uuid.uuid4()}")
        started_payload: dict[str, Any] = {
            "node_name": node_name,
            "invocation_id": str(inv_id),
            "type": _node_type_ref(node_def),
            "inputs": bound_inputs,
            "caller_chain": list(
                getattr(self._run, "caller_chain", ()) or ()
            ),
        }
        async with self._emit_lock:
            started_id = emit_event(
                self._store,
                WorkflowNodeStartedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._last_event_id,
                    invocation_id=inv_id,
                    node_name=node_name,
                    payload=started_payload,
                ),
            )
        self._last_event_id = started_id
        self._node_started_event_id[inv_id] = started_id
        self._invocation_last_event_id[inv_id] = started_id

        ctx = self._build_ctx(
            inv_id=inv_id,
            node_def=node_def,
            parent_event_id=started_id,
            is_resume_mode=False,
            recorded_child_run_id=None,
        )
        return _PendingNode(
            node_name=node_name,
            node_def=node_def,
            inv_id=inv_id,
            started_event_id=started_id,
            bound_inputs=bound_inputs,
            ctx=ctx,
        )

    async def _invoke_node_runner_for_dispatch(
        self, pending: _PendingNode
    ) -> dict[str, Any]:
        """Run the node's runner under the appropriate locks. Raises on
        any non-success path (catalog runner exceptions, BashNonZeroExit
        when the bash node isn't branchable, generic runner failure)."""
        # Strict-mode bind validation happens BEFORE the runner runs
        # so a mismatch still surfaces as ``WorkflowBindTypeMismatch``
        # carried by the failure path (mirrors the sequential body).
        self._validate_bound_inputs_strict(
            node_name=pending.node_name,
            node_def=pending.node_def,
            bound_inputs=pending.bound_inputs,
        )
        runner = self._registry.get(
            _node_type_ref(pending.node_def),
            node_name=pending.node_name,
        )
        runner = _apply_node_modifiers(runner, node_def=pending.node_def)

        touches_state = _node_type_ref(pending.node_def) in (
            "kolsch/nodes/set_state",
            "kolsch/nodes/delete_state",
        )

        async def _run_body() -> dict[str, Any]:
            try:
                return await runner.run(
                    node_name=pending.node_name,
                    node_def=pending.node_def,
                    bound_inputs=pending.bound_inputs,
                    state=self._state,
                    ctx=pending.ctx,
                )
            except BashNonZeroExit as bash_exc:
                if self._bash_treats_nonzero_as_success(pending.node_name):
                    return bash_exc.captured
                raise

        if touches_state:
            # Phase 3.10 §16.2.1 — state-mutation lock serialises the
            # ``workflow.state.mutated`` event stream so JSONL ordering
            # stays deterministic across multiple disjoint mutators.
            async with self._state_mutation_lock:
                return await _run_body()
        return await _run_body()

    async def _finalize_concurrent_success(
        self,
        *,
        pending: _PendingNode,
        output: dict[str, Any],
        run_id: WorkflowRunId,
    ) -> None:
        """Mirror the sequential body's post-success block: capture,
        branch dispatch, emit ``workflow.node.completed``, cascade
        prune. Runs serially in the main loop (the task is done; we
        own the bookkeeping mutations here)."""
        self._captured[pending.node_name] = dict(output)
        self._completed_or_skipped.add(pending.node_name)

        branch_value = compute_branch_value(pending.node_def, output)
        self._dispatch_branch(
            node_name=pending.node_name,
            branch_value=branch_value,
            inv_id=pending.inv_id,
        )

        completed_payload: dict[str, Any] = {
            "node_name": pending.node_name,
            "invocation_id": str(pending.inv_id),
            "output": dict(output),
        }
        if branch_value is not None and any(
            edge.branch is not None
            for edge in self._outgoing_edges(pending.node_name)
        ):
            completed_payload["branch_value"] = branch_value
        if (
            _node_type_ref(pending.node_def) == "kolsch/nodes/agent"
            and pending.ctx.spawned_session_id is not None
        ):
            completed_payload["spawned_session_id"] = str(
                pending.ctx.spawned_session_id
            )
        if (
            _node_type_ref(pending.node_def) == "kolsch/nodes/workflow"
            and pending.ctx.spawned_run_id is not None
        ):
            completed_payload["child_run_id"] = str(
                pending.ctx.spawned_run_id
            )

        async with self._emit_lock:
            completion_id = emit_event(
                self._store,
                WorkflowNodeCompletedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._invocation_last_event_id.get(
                        pending.inv_id,
                        self._node_started_event_id.get(pending.inv_id),
                    ),
                    invocation_id=pending.inv_id,
                    node_name=pending.node_name,
                    payload=completed_payload,
                ),
            )
        self._last_event_id = completion_id
        self._node_completed_event_id[pending.node_name] = completion_id

        self._cascade_prune(
            source=pending.node_name,
            source_completion_event_id=completion_id,
            branch_value=branch_value,
        )

    async def _emit_node_failure_concurrent(
        self,
        *,
        pending: _PendingNode,
        exc: BaseException,
        run_id: WorkflowRunId,
    ) -> None:
        """Emit ``workflow.node.failed`` for the node whose task raised.
        Mirrors the sequential body's failure-path emit; ``parent_event_id``
        chains to the node's started event (Phase 3.1 contract)."""
        async with self._emit_lock:
            failed_id = emit_event(
                self._store,
                WorkflowNodeFailedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._node_started_event_id.get(
                        pending.inv_id
                    ),
                    invocation_id=pending.inv_id,
                    node_name=pending.node_name,
                    payload={
                        "node_name": pending.node_name,
                        "invocation_id": str(pending.inv_id),
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
            )
        self._last_event_id = failed_id

    async def _cancel_in_flight_and_emit_failed(
        self,
        *,
        in_flight: dict[asyncio.Task[dict[str, Any]], _PendingNode],
        run_id: WorkflowRunId,
        reason: str,
    ) -> None:
        """Cancel every still-pending task, await its settlement, and
        emit ``workflow.node.failed`` with ``error_class="asyncio.CancelledError"``
        + the supplied ``reason`` for each.

        Master spec §16.2.2 / §16.6 — siblings of the failing/aborting
        node MUST surface their cancellation in the JSONL so the
        forensics view stays complete; silently dropping them would
        leave open ``workflow.node.started`` events with no matching
        terminator.
        """
        pending_items = list(in_flight.items())
        for task, _ in pending_items:
            task.cancel()
        for task, pending in pending_items:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — drain
                pass
            async with self._emit_lock:
                cancelled_id = emit_event(
                    self._store,
                    WorkflowNodeFailedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._node_started_event_id.get(
                            pending.inv_id
                        ),
                        invocation_id=pending.inv_id,
                        node_name=pending.node_name,
                        payload={
                            "node_name": pending.node_name,
                            "invocation_id": str(pending.inv_id),
                            "error_class": "asyncio.CancelledError",
                            "reason": reason,
                        },
                    ),
                )
            self._last_event_id = cancelled_id
            in_flight.pop(task, None)

    async def _emit_run_failed_concurrent(
        self,
        *,
        node_name: str,
        exc: BaseException,
        run_id: WorkflowRunId,
    ) -> None:
        """Emit ``workflow.run.failed`` carrying the original exception
        as the cause. Master spec §16.9 — under auto mode, the payload
        carries ``peak_in_flight``."""
        payload: dict[str, Any] = {
            "reason": (
                f"node {node_name!r} raised {type(exc).__name__}"
            ),
            "peak_in_flight": self._peak_in_flight,
        }
        async with self._emit_lock:
            self._last_event_id = emit_event(
                self._store,
                WorkflowRunFailedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._last_event_id,
                    payload=payload,
                ),
            )

    @staticmethod
    def _cleanup_waiting_at(run_dir: RunDirectory) -> None:
        """Remove the ``waiting_at`` sidecar if present.

        Phase 3.4 Step 1 — invoked from every terminal-status path
        in :meth:`_continue_worklist`. A sidecar present at terminal
        time is stale (the run finished, so the pause it marked is
        no longer awaiting input). The check + unlink is idempotent;
        a race with the orphan sweep or a concurrent operator action
        surfacing a ``FileNotFoundError`` is benign.
        """
        if not run_dir.waiting_at_path.exists():
            return
        try:
            run_dir.waiting_at_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Topology + predecessor closure.
    # ------------------------------------------------------------------

    def _compute_topology(self) -> tuple[list[str], dict[str, set[str]]]:
        """Kahn's algorithm + predecessor BFS.

        Returns ``(topo_order, predecessors)``. The DAG invariant was
        validated upstream by Step 5's validator, so the Kahn walk
        never stalls on a cycle.

        Phase 3.7 — nodes referenced ONLY as ``map.apply:`` workers are
        excluded from the worklist. Their lifecycle is owned by the
        :class:`workflows.builtins.map.MapRunner` (which dispatches
        the worker's runner per iteration); the Phase 3.0 validator's
        §7.1 reachability check already exempts these "library" nodes
        from the reachable-from-start requirement, so excluding them
        here keeps the topo aligned with the validator's expectations.
        A node that is BOTH a map worker AND a graph participant
        (referenced by an edge) still fires per its graph position —
        the exclusion is "only-worker" not "any-worker."
        """
        all_node_names = list(self._workflow.nodes)
        graph_edges: list[EdgeDef] = (
            list(self._workflow.graph.edges)
            if self._workflow.graph is not None
            else []
        )

        # Map.apply targets that don't otherwise participate in the
        # edge graph are worker templates — the map runner dispatches
        # them per iteration and the worklist must not fire them
        # independently.
        edge_participants: set[str] = set()
        for edge in graph_edges:
            edge_participants.add(edge.from_)
            edge_participants.add(edge.to)
        map_worker_only: set[str] = {
            getattr(node_def, "apply", None)
            for node_def in self._workflow.nodes.values()
            if hasattr(node_def, "apply")
            and isinstance(getattr(node_def, "apply", None), str)
        } - edge_participants
        node_names = [n for n in all_node_names if n not in map_worker_only]

        incoming: dict[str, list[str]] = {n: [] for n in node_names}
        outgoing: dict[str, list[str]] = {n: [] for n in node_names}
        for edge in graph_edges:
            if edge.from_ in incoming and edge.to in incoming:
                incoming[edge.to].append(edge.from_)
                outgoing[edge.from_].append(edge.to)

        in_degree = {n: len(incoming[n]) for n in node_names}
        queue: deque[str] = deque(
            n for n in node_names if in_degree[n] == 0
        )
        topo: list[str] = []
        while queue:
            current = queue.popleft()
            topo.append(current)
            for nxt in outgoing[current]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        # Predecessor closure: BFS back from each node along inbound
        # edges.
        predecessors: dict[str, set[str]] = {}
        for node_name in node_names:
            seen: set[str] = set()
            walk = list(incoming[node_name])
            while walk:
                src = walk.pop()
                if src in seen:
                    continue
                seen.add(src)
                walk.extend(incoming[src])
            predecessors[node_name] = seen
        return topo, predecessors

    # ------------------------------------------------------------------
    # `when:` gate evaluation.
    # ------------------------------------------------------------------

    def _when_holds(self, node_def: Any) -> bool:  # noqa: ANN401
        """Evaluate the node-level ``when:`` modifier.

        Per Round 6 J5/β: ``None`` → no gate (always fires). A bare
        YAML literal evaluates as Python truthiness. A Jinja-marked
        string is rendered against the current capture + state +
        inputs scope; the rendered value's truthiness is the gate.
        """
        when_value = getattr(node_def, "when", None)
        if when_value is None:
            return True
        if _is_jinja_marked(when_value):
            rendered = self._render_jinja(when_value)
            return bool(rendered)
        return bool(when_value)

    # ------------------------------------------------------------------
    # Strict-mode bind validation — Phase 3.3 §6.4.4.
    # ------------------------------------------------------------------

    def _validate_bound_inputs_strict(
        self,
        *,
        node_name: str,
        node_def: Any,  # noqa: ANN401 — NodeDefUnion is a Pydantic alias
        bound_inputs: dict[str, Any],
    ) -> None:
        """Validate ``bound_inputs`` against the receiver's typed
        ``Inputs(BaseModel)`` under ``strict=True``.

        Phase 3.3 Step 0 — built-in receivers only; authored Python
        @nodes return ``None`` from ``inputs_model_for`` and skip this
        check (Step 1 wires their catalog-introspected model). On
        Pydantic ``ValidationError``, the FIRST sub-error populates
        the raised :class:`WorkflowBindTypeMismatch` for actionable
        diagnostics; remaining sub-errors are surfaced via
        ``__cause__`` (Pydantic's standard error tree).
        """
        inputs_model = inputs_model_for(
            node_def, catalog_runner=self._registry.catalog_runner
        )
        if inputs_model is None:
            return
        try:
            inputs_model.model_validate(bound_inputs, strict=True)
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0] if errors else {"loc": (), "type": "unknown"}
            loc = first.get("loc") or ()
            bind_key = str(loc[0]) if loc else "<unknown>"
            expected = str(first.get("type", "unknown"))
            if bind_key in bound_inputs:
                actual = type(bound_inputs[bind_key]).__name__
            elif bind_key == "<unknown>":
                actual = type(bound_inputs).__name__
            else:
                actual = "missing"
            # The parser doesn't attach ruamel.yaml source positions
            # to NodeDef (loader uses ``typ='safe'``), so the
            # receiver's file/line fall back to the run's workflow
            # ref + line 0 per the Step 0 build doc. The fix-path
            # message names the offending bind regardless.
            raise WorkflowBindTypeMismatch(
                file=str(self._run.workflow_ref),
                line=0,
                node=node_name,
                bind=bind_key,
                expected_type=expected,
                actual_type=actual,
            ) from exc

    # ------------------------------------------------------------------
    # binds: rendering.
    # ------------------------------------------------------------------

    def _render_bound_inputs(self, node_name: str) -> dict[str, Any]:
        """Merge every inbound edge's rendered ``binds:`` into one dict.

        The validator's :class:`WorkflowBindCollision` check
        guarantees no two inbound edges share a bind key, so the
        merge is conflict-free.

        Phase 3.3 — pruned edges (those suppressed by upstream
        branch dispatch, recorded in ``_pruned_edges``) are SKIPPED.
        The receiver therefore sees only the fired inbound edges'
        binds; downstream Jinja that references a pruned edge's
        bind key resolves to ``Undefined`` (master spec §6.4.3).
        """
        bound: dict[str, Any] = {}
        if self._workflow.graph is None:
            return bound
        for edge in self._workflow.graph.edges:
            if edge.to != node_name:
                continue
            if (edge.from_, edge.to) in self._pruned_edges:
                continue
            for key, value in edge.binds.items():
                bound[key] = (
                    self._render_jinja(value)
                    if _is_jinja_marked(value)
                    else value
                )
        return bound

    # ------------------------------------------------------------------
    # Branch dispatch + pruning bookkeeping — Phase 3.3 §6.5 / §6.6.
    # ------------------------------------------------------------------

    def _outgoing_edges(self, node_name: str) -> list[EdgeDef]:
        """Return this node's outgoing graph edges (possibly empty)."""
        if self._workflow.graph is None:
            return []
        return [
            edge
            for edge in self._workflow.graph.edges
            if edge.from_ == node_name
        ]

    def _all_inbound_blocked(self, node_name: str) -> bool:
        """``True`` iff every one of ``node_name``'s inbound graph
        edges is structurally suppressed.

        An inbound edge is blocked when:

        - The edge itself was pruned by upstream branch dispatch
          (``(source, target) in _pruned_edges``), OR
        - The edge's source is decided-but-unfired: it lives in
          ``_completed_or_skipped`` but NOT in ``_captured`` (a
          ``when:``-skipped source OR a previously cascaded prune).

        Returns ``False`` for nodes with no inbound graph edges
        (the start node is never blocked) so the cascade's seed
        check doesn't spuriously prune roots.
        """
        if self._workflow.graph is None:
            return False
        inbound = [
            edge
            for edge in self._workflow.graph.edges
            if edge.to == node_name
        ]
        if not inbound:
            return False
        return all(self._inbound_edge_blocked(edge) for edge in inbound)

    def _inbound_edge_blocked(self, edge: EdgeDef) -> bool:
        """One-edge predicate used by :meth:`_all_inbound_blocked`."""
        if (edge.from_, edge.to) in self._pruned_edges:
            return True
        return (
            edge.from_ in self._completed_or_skipped
            and edge.from_ not in self._captured
        )

    def _dispatch_branch(
        self,
        *,
        node_name: str,
        branch_value: bool | None,
        inv_id: InvocationId,
    ) -> None:
        """Filter the source's outgoing edges by their ``branch:`` field.

        Master spec §6.6:

        - ``branch_value is None`` (non-branchable source): every
          outgoing edge fires unconditionally (validator guarantees
          no edge here carries ``branch:``).
        - ``branch_value is True``: edges with ``edge.branch in
          (True, None)`` fire; ``False`` arms are pruned.
        - ``branch_value is False``: edges with ``edge.branch in
          (False, None)`` fire; ``True`` arms are pruned.

        Pruned edges are added to ``_pruned_edges`` so
        ``_render_bound_inputs`` skips them and Step 2's cascade
        (run from the worklist immediately after dispatch) can
        suppress descendants. The defensive raise covers the case
        where a non-branchable source has a ``branch:`` edge in the
        graph (the validator already blocks this at parse time —
        this is the runtime belt-and-braces).
        """
        for edge in self._outgoing_edges(node_name):
            if edge.branch is None:
                continue
            if branch_value is None:
                raise WorkflowNodeFailed(
                    node=node_name,
                    invocation_id=str(inv_id),
                    cause=ValueError(
                        f"node {node_name!r} has branch edge to "
                        f"{edge.to!r} but produced no branch value"
                    ),
                )
            if edge.branch != branch_value:
                self._pruned_edges.add((edge.from_, edge.to))

    def _bash_treats_nonzero_as_success(self, node_name: str) -> bool:
        """``True`` iff this bash node carries at least one outgoing
        edge with ``branch: not None``.

        Phase 3.3 §8.5.5 — a branchable bash node treats non-zero
        exit as success-of-the-false-arm; a non-branching bash node
        retains Phase 3.1's abort-on-failure semantics.
        """
        return any(
            edge.branch is not None
            for edge in self._outgoing_edges(node_name)
        )

    def _cascade_prune(
        self,
        *,
        source: str,
        source_completion_event_id: Any,  # noqa: ANN401 — EventId | None
        branch_value: bool | None,
    ) -> None:
        """Reverse-reachability cascade — master spec §6.6.

        Walks downstream from ``source``. A descendant ``n`` is
        pruned IFF every inbound edge to ``n`` is structurally
        blocked (per :meth:`_all_inbound_blocked`). Pruned nodes are
        added to ``_pruned`` AND ``_completed_or_skipped`` so the
        AND-strict downstream check treats them as decided; their
        outgoing edges are added to ``_pruned_edges`` so any partial-
        fan-in receivers downstream of the cascade skip the dead
        binds during :meth:`_render_bound_inputs`.

        When ``source_completion_event_id`` is non-``None`` (the
        branch-not-taken cascade), each pruned descendant emits a
        :class:`WorkflowNodePrunedEvent` carrying ``upstream_source``
        + ``branch_value`` chained to the source's completion event.
        The ``when:``-skip cascade calls in with ``None`` (no
        completion was emitted for the skipped source); the cascade
        still prunes structurally but emits NO events — preserving
        Phase 3.1's "skip is silent" convention for when-gates.

        The cascade STOPS at any node with at least one fired inbound
        (multi-input partial-fan-in case) — that node fires with the
        fired edges' binds only. The membership check on ``_pruned``
        + ``_captured`` + ``_completed_or_skipped`` guarantees idempotence
        on cyclic invocation (the validator already rejects cycles,
        but the iterative seed expansion is cycle-safe regardless).
        """
        seen: set[str] = set()
        candidates: list[str] = [
            edge.to for edge in self._outgoing_edges(source)
        ]
        while candidates:
            node_name = candidates.pop()
            if node_name in seen:
                continue
            seen.add(node_name)
            if (
                node_name in self._pruned
                or node_name in self._captured
            ):
                continue
            if not self._all_inbound_blocked(node_name):
                continue
            self._pruned.add(node_name)
            self._completed_or_skipped.add(node_name)
            for edge in self._outgoing_edges(node_name):
                self._pruned_edges.add((edge.from_, edge.to))
            if (
                source_completion_event_id is not None
                and branch_value is not None
            ):
                self._emit_pruned_event(
                    node_name=node_name,
                    upstream_source=source,
                    branch_value=branch_value,
                    parent_event_id=source_completion_event_id,
                )
            candidates.extend(
                edge.to for edge in self._outgoing_edges(node_name)
            )

    def _emit_pruned_event(
        self,
        *,
        node_name: str,
        upstream_source: str,
        branch_value: bool,
        parent_event_id: Any,  # noqa: ANN401 — EventId envelope field
    ) -> None:
        """Append a :class:`WorkflowNodePrunedEvent` to the run JSONL.

        Master spec §11.2 ordering — the pruned event chains directly
        to the upstream source's :class:`WorkflowNodeCompletedEvent`
        so consumers can walk the prune subtree without re-parsing
        the graph. ``invocation_id`` is a fresh ``inv_<uuid>``
        sentinel — the node never produced a real invocation, but
        the :class:`schemas.identifiers.PrefixedId` shape requires
        a UUID-backed body, and the ``kind`` field
        (``workflow.node.pruned``) is the discriminator consumers
        actually read. ``_last_event_id`` is bumped to the pruned
        event so the next emitted event (typically the run-completed
        terminal) chains through it.
        """
        from workflows.events import WorkflowNodePrunedEvent  # noqa: PLC0415

        pruned_inv = InvocationId(f"inv_{uuid.uuid4()}")
        self._last_event_id = emit_event(
            self._store,
            WorkflowNodePrunedEvent(
                workflow_run_id=self._run.run_id,
                trace_id=self._run.trace_id,
                parent_event_id=parent_event_id,
                invocation_id=pruned_inv,
                node_name=node_name,
                payload={
                    "node_name": node_name,
                    "reason": "branch_not_taken",
                    "upstream_source": upstream_source,
                    "branch_value": branch_value,
                },
            ),
        )

    def _render_jinja(
        self,
        expression: str,
        *,
        inputs_overlay: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 — Jinja returns Any-shaped values
        """Render a Jinja-marked string under :data:`JINJA_ENV`.

        The scope exposes ``nodes`` (captured outputs), ``state``
        (mutable workflow state), and ``inputs`` (workflow inputs)
        per master spec §8.3 + §6.4.

        Phase 3.3 §6.4.1 — ``inputs_overlay`` is the receiver's
        edge-bound ``binds:`` dict; its keys overlay onto workflow-level
        inputs so a receiver's CONFIG STRINGS (e.g. bash
        ``command: "echo {{ inputs.greeting }}"``) see edge-bound
        keys via the SAME ``inputs.*`` namespace authored Python
        @nodes consume via the typed ``Inputs`` model. ``None``
        preserves Phase 3.1 behavior (workflow-level inputs only).
        """
        template = JINJA_ENV.from_string(expression)
        return template.render(
            nodes=self._captured,
            state=self._state,
            inputs={
                **dict(self._run.inputs),
                **(inputs_overlay or {}),
            },
        )

    # ------------------------------------------------------------------
    # NodeContext factory.
    # ------------------------------------------------------------------

    def _build_ctx(
        self,
        *,
        inv_id: InvocationId,
        node_def: Any,  # noqa: ANN401
        parent_event_id: Any = None,  # noqa: ANN401 — typed EventId in workflows.context
        is_resume_mode: bool = False,
        recorded_child_run_id: WorkflowRunId | None = None,
    ) -> NodeContext:
        """Mint a per-invocation :class:`NodeContext`.

        Phase 3.1 Step 1 plumbs the bare minimum: workflow_run_id +
        invocation_id + workspace_root + state + bundle (resolved
        from the node's ``type:`` ref). Step 5 wires the real
        ``emit_fn`` against the JSONL append surface; Step 1 gives
        runners a no-op so framing tests don't need to mock the
        emission path. Phase 3.2 threads ``parent_event_id`` so the
        agent runner can stamp the spawned session's ``spawned_by``
        payload with the originating ``workflow.node.started`` id.
        """
        type_ref = _node_type_ref(node_def)
        bundle = (
            parse_three_segment(type_ref)[0]
            if is_three_segment(type_ref)
            else "kolsch"
        )
        run_id = self._run.run_id

        def _emit_fn(
            bundle_: str,
            subkind: str,
            payload: Mapping[str, Any],
        ) -> None:
            # Author-emitted node events land here in Step 5. At Step 1
            # we record nothing — the framing-event tests don't depend
            # on author emissions and avoiding a write here keeps the
            # runner's NodeContext free of side effects.
            _ = (bundle_, subkind, payload)

        # Phase 3.4 — expose the executor's Jinja renderer to runners
        # via :meth:`NodeContext.render_jinja`. The ``user_input``
        # runner is the first consumer (rendering its ``prompt:``
        # against the same scope ``binds:`` rendering uses); later
        # phases may opt in. Wrapping in a closure keeps the
        # NodeContext free of an executor back-reference.
        def _render_jinja_for_runner(
            expression: str,
            inputs_overlay: Mapping[str, Any] | None,
        ) -> Any:
            return self._render_jinja(
                expression,
                inputs_overlay=(
                    dict(inputs_overlay)
                    if inputs_overlay is not None
                    else None
                ),
            )

        # Phase 3.5 — hook for runners that need to emit fully-typed
        # :class:`WorkflowEvent` envelopes. The state runners use
        # this for ``workflow.state.mutated``. We advance two chain
        # pointers per emission:
        #
        # 1. ``_last_event_id`` — drives cross-node chain (the next
        #    ``workflow.node.started`` chains to the latest emit).
        # 2. ``_invocation_last_event_id[inv_id]`` — drives the
        #    per-invocation chain so the matching
        #    ``workflow.node.completed`` correctly chains to the
        #    LAST in-invocation emission (master spec §11.3).
        #
        # ``_node_started_event_id[inv_id]`` is NOT advanced: it's
        # the parent for ``workflow.node.failed`` (which chains to
        # the started event by contract — a partial mutation that
        # raised an exception still surfaces as "this invocation
        # failed", not "the last partial mutation failed").
        async def _emit_workflow_event(event: WorkflowEvent) -> EventId:
            # Phase 3.10 §16.5 — JSONL line framing under concurrent
            # dispatch. The lock is uncontended in sequential mode
            # so JSONL byte output is byte-identical to Phase 3.9.
            async with self._emit_lock:
                new_id = emit_event(self._store, event)
            self._last_event_id = new_id
            self._invocation_last_event_id[inv_id] = new_id
            return new_id

        return NodeContext(
            workflow_run_id=str(run_id),
            invocation_id=str(inv_id),
            workspace_root=self._store.runs_root.parent,
            log=logging.getLogger(f"workflows.{run_id}"),
            secrets={},
            state=self._state,
            bundle=bundle,
            emit_fn=_emit_fn,
            parent_event_id=parent_event_id,
            trace_id=self._run.trace_id,
            caller_chain=tuple(getattr(self._run, "caller_chain", ()) or ()),
            workflow_inputs=dict(self._run.inputs),
            render_jinja_callback=_render_jinja_for_runner,
            emit_workflow_event_callback=_emit_workflow_event,
            # Phase 3.7 — thread the executor's nesting metadata into
            # the per-invocation context so runners (the workflow-sub
            # runner first; map's worker dispatch second) can introspect
            # depth + chain + the parent workflow object without
            # back-referencing the executor.
            call_depth=self._call_depth,
            call_chain=self._call_chain,
            workflow=self._workflow,
            # Phase 3.7 Step 3 — resume-cascade markers. ``True`` only
            # when this dispatch is the re-fire of an open workflow
            # node from a paused parent; the WorkflowSubRunner reads
            # both fields together to skip the spawn arm and read the
            # child's status instead.
            is_resume_mode=is_resume_mode,
            recorded_child_run_id=recorded_child_run_id,
        )

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    def _workflow_ref(self) -> str:
        return self._run.workflow_ref

    def _emit_aborted(self, run_id: WorkflowRunId) -> None:
        """Append the terminal ``workflow.run.aborted`` event.

        Phase 3.1 Step 4 — invoked on cooperative cancellation when the
        ``cancel.flag`` sentinel is observed (either pre-walk or at a
        node boundary). ``reason: user_cancel`` is the master spec
        §11.3 vocabulary for an operator-initiated abort; the
        sub-workflow / fork engines will add their own reasons in
        Phase 3.7 without disturbing this contract.
        """
        self._last_event_id = emit_event(
            self._store,
            WorkflowRunAbortedEvent(
                workflow_run_id=run_id,
                trace_id=self._run.trace_id,
                parent_event_id=self._last_event_id,
                payload={"reason": "user_cancel"},
            ),
        )

    def _assemble_terminal_outputs(
        self, topo: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        """Master spec §6.6 — the workflow output is a dict keyed by
        terminal node names. Terminals that DIDN'T fire (skipped or
        unreachable on a branch that wasn't taken) contribute ``None``.
        """
        if self._workflow.graph is None:
            # Node libraries have no graph; nothing to assemble.
            return {n: self._captured.get(n) for n in topo}
        outgoing: dict[str, list[str]] = {n: [] for n in topo}
        for edge in self._workflow.graph.edges:
            if edge.from_ in outgoing:
                outgoing[edge.from_].append(edge.to)
        return {
            n: self._captured.get(n)
            for n in topo
            if not outgoing[n]
        }


# ---------------------------------------------------------------------------
# Module-level helpers.
# ---------------------------------------------------------------------------


def _is_jinja_marked(value: Any) -> bool:  # noqa: ANN401
    """Round 6 J5/β — Jinja iff string containing ``"{{"``."""
    return isinstance(value, str) and "{{" in value


def _apply_node_modifiers(
    runner: NodeRunner, *, node_def: Any  # noqa: ANN401
) -> NodeRunner:
    """Apply per-node ``retry`` + ``timeout_seconds`` modifiers.

    Order matters: retry wraps the INNER runner; timeout wraps the
    retry'd runner. The result is that ``timeout_seconds`` bounds
    the WHOLE retry budget (initial + all retries + backoffs), not
    just the final attempt. Master spec §8.1 retry rule 3.
    """
    retries = int(getattr(node_def, "retry", 0) or 0)
    if retries > 0:
        from workflows.builtins.retry import with_retry  # noqa: PLC0415

        runner = with_retry(runner, retries=retries)
    timeout_seconds = getattr(node_def, "timeout_seconds", None)
    if timeout_seconds is not None and float(timeout_seconds) > 0:
        from workflows.builtins.timeout import with_timeout  # noqa: PLC0415

        runner = with_timeout(
            runner, timeout_seconds=float(timeout_seconds)
        )
    return runner


def _node_type_ref(node_def: Any) -> str:  # noqa: ANN401
    """Return the node's 3-seg ``type:`` ref.

    The ``copy_of:`` shape carries no ``type:`` — Step 5 resolves it
    by inheriting from the source node. Phase 3.1 Step 1 doesn't
    materialise the inheritance yet, so a ``copy_of:`` node falls
    through to the catalog-node generic case (Step 3 wires that).
    """
    type_value = getattr(node_def, "type", None)
    if isinstance(type_value, str):
        return type_value
    # ``CopyOfNodeDef`` — defer to a synthetic ref the registry will
    # report as "not implemented" until Step 5's copy_of resolution
    # lands.
    return "<copy_of>"
