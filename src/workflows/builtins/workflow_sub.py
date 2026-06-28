"""``kolsch/nodes/workflow`` runner — Phase 3.7 §4.6.1.

The workflow-sub runtime is the workflow→workflow boundary: catalog
lookup of the child ``ref:``, parent-pointer mint, child-run creation
with inherited ``trace_id``, nested executor construction with the
extended ``_call_chain`` / ``_call_depth`` metadata (used for cycle +
depth caps), child run with pause-cascade re-raise, and terminal
output harvest via :func:`workflows._terminal.compute_terminal_output_dict`.

The runner emits two parent-side forensics events around the child's
lifecycle: :class:`workflows.events.WorkflowNodeCalledWorkflowEvent`
BEFORE the nested executor begins, and
:class:`workflows.events.WorkflowNodeCalledWorkflowCompletedEvent`
AFTER it returns / pauses / fails. Cross-JSONL navigation goes
through ``trace_id`` (universal), ``child_run_id`` (parent→child via
the workflow node's ``workflow.node.completed`` payload), and
``WorkflowRun.parent`` (child→parent).

Pause cascade (master spec §10.1 + §4.6.1.2): when the nested
executor raises :class:`workflows.errors.WorkflowPausedForInput`, the
runner emits ``called_workflow_completed`` with
``child_status="waiting_for_input"`` then RE-RAISES so the parent's
executor catches the pause at the same site as a leaf user_input
runner would (Phase 3.4 worklist catch arm). A swallowed pause
silently hangs the parent's worklist; the explicit re-raise is the
load-bearing contract.

Failure propagation (§4.6.1.1): a child reaching ``failed`` /
``aborted`` / ``timed_out`` surfaces as
:class:`workflows.errors.WorkflowNodeFailed(cause=ChildWorkflowFailed(...))`
so the parent's executor emits the canonical
``workflow.node.failed`` + ``workflow.run.failed`` pair.

Resume cascade is operator-driven at v1 (§4.6.1.3): the parent does
NOT auto-resume the child. The leaf is resumed first; the operator
then resumes the parent and the runner detects the child reached a
terminal-ish status and harvests. The resume-detection branch lands
in Step 3 alongside the ``_replay.py`` extension; Step 1 ships the
forward path only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from schemas.entities import ParentRunPointer
from workflows._terminal import compute_terminal_output_dict
from workflows.errors import (
    ChildWorkflowFailed,
    WorkflowCallCycle,
    WorkflowExtendsDepthExceeded,
    WorkflowNodeFailed,
    WorkflowPausedForInput,
)
from workflows.events import (
    WorkflowNodeCalledWorkflowCompletedEvent,
    WorkflowNodeCalledWorkflowCompletedPayload,
    WorkflowNodeCalledWorkflowEvent,
    WorkflowNodeCalledWorkflowPayload,
)
from workflows.ids import InvocationId, WorkflowRunId
from workflows.parser import load_workflow
from workflows.run import WorkflowRunStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from assets.catalog import AssetCatalog
    from workflows.context import NodeContext
    from workflows.executor import IterativeExecutor, RunnerRegistry
    from workflows.parser import WorkflowNodeDef
    from workflows.run_store import WorkflowRunStore

__all__ = ["WorkflowSubRunner"]


_DEPTH_LIMIT = 16


class WorkflowSubRunner:
    """Runtime for ``kolsch/nodes/workflow`` — master spec §4.6.1.

    Constructed with the catalog + run-store + executor-factory +
    registry. The ``executor_factory`` seam exists because the
    runtime executor module would otherwise create a circular import:
    ``workflows.builtins`` registers runners against
    :class:`workflows.executor.RunnerRegistry`, and the executor
    can't import the runners back without a cycle. The factory is a
    plain callable taking the same kwargs as
    :class:`workflows.executor.IterativeExecutor` — the CLI wires
    it as ``lambda **kw: IterativeExecutor(**kw)``.
    """

    def __init__(
        self,
        *,
        catalog: AssetCatalog,
        store: WorkflowRunStore,
        executor_factory: Callable[..., IterativeExecutor],
        registry: RunnerRegistry,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._executor_factory = executor_factory
        self._registry = registry

    async def run(
        self,
        *,
        node_name: str,
        node_def: WorkflowNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """§4.6.1.1 algorithm — see module docstring for the full prose."""
        _ = state  # child runs are state-isolated (§4.6.1, locked).

        child_ref = node_def.ref

        # Phase 3.7 Step 3 — operator-driven resume cascade. The
        # executor's resume entry detected this workflow-node
        # invocation as the OPEN pause point and pre-populated
        # ``ctx.recorded_child_run_id`` from the replayed
        # ``called_workflow`` marker. We DON'T spawn a new child here
        # — we read the existing child's status off disk and either
        # harvest its terminal output, raise a still-paused error, or
        # propagate a child failure.
        if ctx.is_resume_mode and ctx.recorded_child_run_id is not None:
            return await self._resume_cascade(
                node_name=node_name,
                child_ref=child_ref,
                child_run_id=ctx.recorded_child_run_id,
                ctx=ctx,
            )

        # Step 1 — cycle detection BEFORE the catalog lookup. The
        # auto-include in ``IterativeExecutor.__init__`` guarantees
        # ``ctx.call_chain`` carries this run's workflow_ref as the
        # tail entry, so direct A→A recursion is caught on the first
        # would-be call (not after one extra level).
        if child_ref in ctx.call_chain:
            cycle = (*ctx.call_chain, child_ref)
            raise WorkflowCallCycle(
                file=str(ctx.workflow_run_id),
                line=0,
                cycle=cycle,
            )

        # Step 2 — defensive depth check (the nested executor's
        # constructor enforces the same cap; this raises a clean
        # error BEFORE catalog work for the common case of a single
        # rogue chain).
        if ctx.call_depth + 1 > _DEPTH_LIMIT:
            raise WorkflowExtendsDepthExceeded(
                file=str(ctx.workflow_run_id),
                line=0,
                depth=ctx.call_depth + 1,
                limit=_DEPTH_LIMIT,
            )

        # Step 3 — catalog resolution. Missing refs raise
        # ``WorkflowNodeFailed`` so the executor's existing failure
        # path emits ``workflow.node.failed`` (parity with the agent
        # runner's :class:`AgentRefUnresolved` remap).
        entry = self._catalog.lookup(child_ref, expected_type="workflow")
        if entry is None:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=ValueError(
                    f"unknown workflow ref {child_ref!r}"
                ),
            )
        child_workflow = load_workflow(Path(entry.path))

        # Step 4 — parent pointer + child-run creation. Trace id
        # inherits unchanged per master spec §11.3 so cross-JSONL
        # correlation (parent + every child) shares a single id.
        parent_ptr = ParentRunPointer(
            workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
            invocation_id=InvocationId(ctx.invocation_id),
        )
        child_run = self._store.create(
            workflow_ref=child_ref,
            inputs=dict(bound_inputs),
            trace_id=ctx.trace_id,
            parent=parent_ptr,
        )

        # Step 5 — stamp ``spawned_run_id`` on the (frozen) ctx so
        # the parent executor's ``workflow.node.completed`` payload
        # builder picks it up as ``child_run_id``. Frozen dataclass
        # — write via ``object.__setattr__`` mirrors the agent
        # runner's ``spawned_session_id`` stamp.
        object.__setattr__(ctx, "spawned_run_id", child_run.run_id)

        # Step 6 — emit ``workflow.node.called_workflow`` on the
        # PARENT'S JSONL BEFORE constructing the nested executor so a
        # crash mid-construction still leaves the forensics marker
        # behind. Chains to the workflow-node's ``started`` event via
        # the executor's per-invocation chain pointer (Phase 3.5's
        # ``emit_workflow_event`` hook advances both the per-invocation
        # pointer and ``_last_event_id``).
        await ctx.emit_workflow_event(
            WorkflowNodeCalledWorkflowEvent(
                workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                trace_id=ctx.trace_id,
                parent_event_id=ctx.parent_event_id,
                invocation_id=InvocationId(ctx.invocation_id),
                node_name=node_name,
                payload=WorkflowNodeCalledWorkflowPayload(
                    node_name=node_name,
                    invocation_id=str(ctx.invocation_id),
                    child_workflow_run_id=str(child_run.run_id),
                    child_workflow_ref=child_ref,
                ),
            )
        )

        # Step 7 — construct the nested executor. Cycle/depth caps
        # were already checked above; the constructor's own depth
        # check is the belt-and-braces guarantee.
        nested = self._executor_factory(
            workflow=child_workflow,
            run=child_run,
            store=self._store,
            registry=self._registry,
            catalog=self._catalog,
            _call_depth=ctx.call_depth + 1,
            _call_chain=(*ctx.call_chain, child_ref),
        )

        # Step 8 — run the child. Two pause-signalling shapes from
        # the nested executor:
        #
        # 1. ``WorkflowPausedForInput`` bubbles up (the inner
        #    ``user_input`` runner raised it AND the nested executor
        #    chose to re-raise rather than swallow — currently the
        #    Phase 3.4 executor swallows + returns, so this branch is
        #    documentary / belt-and-braces).
        # 2. ``nested.run()`` returns ``WAITING_FOR_INPUT`` after
        #    swallowing the pause + writing the child's sidecar +
        #    flipping the child's status. This is the live Phase 3.4
        #    contract.
        #
        # In BOTH cases the parent must pause too: the parent's outer
        # worklist needs a ``WorkflowPausedForInput`` to catch so it
        # writes the parent's ``waiting_at`` sidecar (with the
        # workflow-node's invocation id, so the eventual resume
        # re-enters this runner) and flips the parent's status. The
        # synthesised pause for the return-shape case carries the
        # workflow-node's invocation id verbatim — that's the right
        # value to land in the parent's sidecar.
        try:
            status = await nested.run()
        except WorkflowPausedForInput:
            await self._emit_called_completed(
                ctx=ctx,
                node_name=node_name,
                child_run_id=str(child_run.run_id),
                child_status=str(WorkflowRunStatus.WAITING_FOR_INPUT),
            )
            raise

        if status == WorkflowRunStatus.WAITING_FOR_INPUT:
            await self._emit_called_completed(
                ctx=ctx,
                node_name=node_name,
                child_run_id=str(child_run.run_id),
                child_status=str(WorkflowRunStatus.WAITING_FOR_INPUT),
            )
            raise WorkflowPausedForInput(
                node_name=node_name,
                invocation_id=str(ctx.invocation_id),
                prompt=(
                    f"sub-workflow {child_ref!r} paused waiting for "
                    f"input; resume it with "
                    f"`kolsch workflows resume {child_run.run_id}`"
                ),
            )

        # Step 9 — terminal-status branching. SUCCEEDED is the only
        # success arm; every other terminal becomes a node failure
        # wrapping ``ChildWorkflowFailed`` so the parent's existing
        # ``workflow.node.failed`` path emits cleanly.
        if status == WorkflowRunStatus.SUCCEEDED:
            await self._emit_called_completed(
                ctx=ctx,
                node_name=node_name,
                child_run_id=str(child_run.run_id),
                child_status=str(WorkflowRunStatus.SUCCEEDED),
            )
            return compute_terminal_output_dict(
                child_workflow,
                nested._captured,
                nested._pruned,
            )

        # FAILED / ABORTED / TIMED_OUT → propagate as node failure.
        await self._emit_called_completed(
            ctx=ctx,
            node_name=node_name,
            child_run_id=str(child_run.run_id),
            child_status=str(status),
        )
        raise WorkflowNodeFailed(
            node=node_name,
            invocation_id=str(ctx.invocation_id),
            cause=ChildWorkflowFailed(
                run_id=str(child_run.run_id),
                status=str(status),
            ),
        )

    async def _resume_cascade(
        self,
        *,
        node_name: str,
        child_ref: str,
        child_run_id: WorkflowRunId,
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Phase 3.7 §4.6.1.3 — operator-driven resume cascade.

        Called when the parent is resuming an OPEN workflow-node
        invocation that previously paused waiting for its child. The
        child's status is read off disk; three terminal arms:

        - ``SUCCEEDED`` → harvest the child's terminal output dict
          from its JSONL's ``workflow.run.completed.payload.outputs``
          and return it. The parent's worklist body emits the
          ``workflow.node.completed`` event with this dict.
        - ``WAITING_FOR_INPUT`` → the leaf is still paused. The
          operator must resume the leaf FIRST. Raise a
          :class:`WorkflowNodeFailed` whose ``cause`` is an explicit
          ValueError so the executor's failure path surfaces a
          clear "resume <leaf> first" hint.
        - ``FAILED`` / ``ABORTED`` / ``TIMED_OUT`` → wrap in
          :class:`ChildWorkflowFailed` and propagate via
          ``WorkflowNodeFailed``, matching the initial-spawn path's
          failure semantics so downstream tooling reads the same
          envelope shape across spawn and resume.

        Stamps ``ctx.spawned_run_id`` so the parent's
        ``workflow.node.completed`` payload still carries
        ``child_run_id`` (parity with the initial-spawn path; the
        forensics view of resumed runs matches the live-run view).
        """
        object.__setattr__(ctx, "spawned_run_id", child_run_id)
        child_run = self._store.read(child_run_id)

        if child_run.status == WorkflowRunStatus.WAITING_FOR_INPUT:
            await self._emit_called_completed(
                ctx=ctx,
                node_name=node_name,
                child_run_id=str(child_run_id),
                child_status=str(WorkflowRunStatus.WAITING_FOR_INPUT),
            )
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=ValueError(
                    f"child workflow run {child_run_id!r} is still "
                    f"paused; resume it first with "
                    f"`kolsch workflows resume {child_run_id}` "
                    f"before resuming the parent"
                ),
            )

        if child_run.status == WorkflowRunStatus.SUCCEEDED:
            await self._emit_called_completed(
                ctx=ctx,
                node_name=node_name,
                child_run_id=str(child_run_id),
                child_status=str(WorkflowRunStatus.SUCCEEDED),
            )
            return self._harvest_from_child_jsonl(child_run_id)

        # FAILED / ABORTED / TIMED_OUT — propagate as node failure.
        await self._emit_called_completed(
            ctx=ctx,
            node_name=node_name,
            child_run_id=str(child_run_id),
            child_status=str(child_run.status),
        )
        raise WorkflowNodeFailed(
            node=node_name,
            invocation_id=str(ctx.invocation_id),
            cause=ChildWorkflowFailed(
                run_id=str(child_run_id),
                status=str(child_run.status),
            ),
        )

    def _harvest_from_child_jsonl(
        self, child_run_id: WorkflowRunId
    ) -> dict[str, Any]:
        """Walk the child's JSONL for its terminal-output dict.

        Phase 3.7 — at resume the child's :class:`IterativeExecutor`
        is long-gone (it ran in a different process); its in-memory
        ``_captured`` + ``_pruned`` aren't accessible. The child's
        ``workflow.run.completed`` event's payload carries the same
        terminal-keyed output dict that
        :func:`workflows._terminal.compute_terminal_output_dict`
        produced at child run-end (the executor's
        ``_assemble_terminal_outputs`` writes it there). Returning
        that dict gives the parent's downstream consumers the same
        ``nodes.<workflow_node>.<terminal>.<key>`` shape regardless
        of whether the parent harvested live (initial spawn) or via
        resume cascade.
        """
        for event in self._store.read_events(child_run_id):
            if event.get("kind") == "workflow.run.completed":
                return dict(event.get("payload", {}).get("outputs", {}))
        return {}

    async def _emit_called_completed(
        self,
        *,
        ctx: NodeContext,
        node_name: str,
        child_run_id: str,
        child_status: str,
    ) -> None:
        """Append the parent-side ``called_workflow_completed`` event.

        Single emission point keeps the chain bookkeeping consistent
        across the three terminal arms (succeeded / pause / failure
        family). ``emit_workflow_event`` advances the executor's per-
        invocation chain pointer so the eventual
        ``workflow.node.completed`` chains correctly.
        """
        await ctx.emit_workflow_event(
            WorkflowNodeCalledWorkflowCompletedEvent(
                workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                trace_id=ctx.trace_id,
                parent_event_id=ctx.parent_event_id,
                invocation_id=InvocationId(ctx.invocation_id),
                node_name=node_name,
                payload=WorkflowNodeCalledWorkflowCompletedPayload(
                    node_name=node_name,
                    invocation_id=str(ctx.invocation_id),
                    child_workflow_run_id=child_run_id,
                    child_status=child_status,
                ),
            )
        )
