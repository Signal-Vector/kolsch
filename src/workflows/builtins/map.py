"""`kolsch/nodes/map` runner — sequential or concurrent fan-out.

The map node references a WORKER node defined in the SAME workflow
via `apply:`. For every element in `over:` the runner dispatches
the worker's runner directly with a per-iteration scope
`{**bound_inputs, **rendered_with, item_as: element}`, collects the
worker's captured output dict, and returns `{"results": [...]}`.

**Dispatch mode.** `parallel: true` on a
[`MapNodeDef`][workflows.parser.MapNodeDef] opts into concurrent
iteration. Iterations dispatch via `asyncio.create_task` +
`asyncio.wait`; `concurrency: N` (the optional integer) gates
dispatch on an `asyncio.Semaphore`. First-failure cancels siblings:
cancelled tasks emit `workflow.node.failed` with
`error_class="asyncio.CancelledError"` and
`reason="sibling_failure"` in payload. The run-start gate already
rejects `parallel: true` maps whose workers transitively write
state, so the parallel path trusts that disjoint-write invariants
hold.

**Per-iteration events.** Two events land in the parent's JSONL
per iteration: `workflow.node.started` and `workflow.node.completed`
for the WORKER's node name (not the map node's). Both carry
`parent_invocation_id` (= the map node's invocation_id) and
`iteration_index` (0-based) in their payload so forensics can
correlate workers back to their parent map. The map node's own
`started` / `completed` events are emitted by the executor's
worklist body — the runner is responsible for the iteration-level
pair only.

**Failure semantics.** A worker raising mid-iteration aborts the
map (no continue-on-error at v1). The runner emits the failing
iteration's `workflow.node.failed` event, then re-raises so the
parent executor's existing failure path emits the map node's own
`workflow.node.failed`. Partial `results` are NOT persisted — the
map node's `completed` never fires, so downstream nodes don't
observe a half-populated list.

**Rendering schedule.** `over:` and `with:` constants are each
rendered ONCE at map fire-time. `item_as` is bound per iteration.
A `with: {time: "{{ now() }}"}` would therefore have the SAME
value across every iteration; an iteration-varying value belongs
inside the worker's own config strings (which the worker's runner
renders per fire).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from workflows._inputs_models import inputs_model_for
from workflows.errors import (
    WorkflowBindTypeMismatch,
    WorkflowNodeFailed,
)
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeFailedEvent,
    WorkflowNodeStartedEvent,
)
from workflows.executor import _is_jinja_marked
from workflows.ids import InvocationId, WorkflowRunId

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.executor import RunnerRegistry
    from workflows.parser import MapNodeDef

__all__ = ["MapRunner"]


class MapRunner:
    """Runner for `kolsch/nodes/map` — drives the worker per iteration.

    Takes the registry so it can dispatch the resolved worker's
    runner (mirrors how the executor's worklist resolves runners).
    No external CLI wiring needed beyond `register_builtins`.
    """

    def __init__(self, *, registry: RunnerRegistry) -> None:
        self._registry = registry

    async def run(
        self,
        *,
        node_name: str,
        node_def: MapNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Render → resolve worker → iterate (sequential or parallel) → collect.

        Raises:
            WorkflowNodeFailed: `over:` did not resolve to a list,
                or `apply:` references an undefined worker. A
                worker raising mid-iteration also surfaces here
                (after the per-iteration failed event lands).
        """
        # Step 1 — render `over:` to a Python list. Jinja-marked
        # strings render against the map's bound_inputs overlay; bare
        # YAML literals (already a list) flow through unchanged.
        over_raw = node_def.over
        if _is_jinja_marked(over_raw):
            rendered_over = ctx.render_jinja(over_raw, inputs_overlay=bound_inputs)
        else:
            rendered_over = over_raw
        if not isinstance(rendered_over, list):
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=TypeError(
                    f"map.over for node {node_name!r} must resolve to "
                    f"a list; got {type(rendered_over).__name__}"
                ),
            )

        # Step 2 — render `with:` constants ONCE (the values are
        # bound at map fire-time, not per iteration). A
        # `with: {time: "{{ now() }}"}` therefore has the same value
        # across every iteration; iteration-varying values belong in
        # the worker's own config.
        rendered_with: dict[str, Any] = {}
        for key, raw_value in node_def.with_.items():
            if _is_jinja_marked(raw_value):
                rendered_with[key] = ctx.render_jinja(
                    raw_value, inputs_overlay=bound_inputs
                )
            else:
                rendered_with[key] = raw_value

        # Step 3 — resolve the worker NodeDef by name in the SAME
        # workflow. The run-start validator already rejects an
        # undefined `apply:` at parse time; this is the defensive
        # runtime guard for callers that bypass the validator (rare
        # — the executor calls `validate_workflow_or_raise` at
        # run-start).
        workflow = ctx.workflow
        if workflow is None or node_def.apply not in workflow.nodes:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=ValueError(
                    f"map.apply on node {node_name!r} references "
                    f"undefined worker {node_def.apply!r}"
                ),
            )
        worker_def = workflow.nodes[node_def.apply]

        # Step 4 — fetch the worker's runner via the registry. A
        # missing-runner error from the registry (catalog drift)
        # surfaces as `NodeTypeNotImplemented` at this site; the
        # executor's outer catch arm wraps it into the map node's
        # `workflow.node.failed` and the run aborts.
        worker_runner = self._registry.get(
            getattr(worker_def, "type", "<copy_of>"),
            node_name=node_def.apply,
        )

        # Empty `over:` is a clean success — no iterations means no
        # iteration events; the captured output is `{"results": []}`.
        # Downstream Jinja consumers see an empty list and the usual
        # `| length` / `| selectattr` filters work without
        # special-casing.
        if not rendered_over:
            return {"results": []}

        total = len(rendered_over)

        # Dispatch fork. `parallel: true` opts into concurrent
        # iteration; `parallel: false` (the default) is the
        # sequential contract.
        if getattr(node_def, "parallel", False):
            results = await self._run_parallel(
                node_def=node_def,
                worker_def=worker_def,
                worker_runner=worker_runner,
                rendered_over=rendered_over,
                rendered_with=rendered_with,
                bound_inputs=bound_inputs,
                state=state,
                ctx=ctx,
                total=total,
            )
        else:
            results = await self._run_sequential(
                node_def=node_def,
                worker_def=worker_def,
                worker_runner=worker_runner,
                rendered_over=rendered_over,
                rendered_with=rendered_with,
                bound_inputs=bound_inputs,
                state=state,
                ctx=ctx,
                total=total,
            )
        return {"results": results}

    async def _run_sequential(
        self,
        *,
        node_def: MapNodeDef,
        worker_def: Any,
        worker_runner: Any,
        rendered_over: list[Any],
        rendered_with: dict[str, Any],
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
        total: int,
    ) -> list[dict[str, Any]]:
        """Sequential iteration — one worker invocation at a time.

        For every element in `rendered_over`, run a worker
        invocation via `_run_one_iteration` and accumulate the
        captured outputs. A failing iteration re-raises after the
        per-iteration `workflow.node.failed` event lands; partial
        results are dropped (the parent executor's failure path
        owns the map node's own failed emit).
        """
        results: list[dict[str, Any]] = []
        for index, element in enumerate(rendered_over):
            captured = await self._run_one_iteration(
                node_def=node_def,
                worker_def=worker_def,
                worker_runner=worker_runner,
                element=element,
                index=index,
                total=total,
                rendered_with=rendered_with,
                bound_inputs=bound_inputs,
                state=state,
                ctx=ctx,
            )
            results.append(captured)
        return results

    async def _run_parallel(
        self,
        *,
        node_def: MapNodeDef,
        worker_def: Any,
        worker_runner: Any,
        rendered_over: list[Any],
        rendered_with: dict[str, Any],
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
        total: int,
    ) -> list[dict[str, Any]]:
        """Concurrent iteration with first-failure-cancels-siblings.

        Each iteration runs as an `asyncio.Task`; the optional
        `node_def.concurrency` integer caps in-flight workers via
        `asyncio.Semaphore`. The `results` list preserves index
        order (`results[i]` is iteration `i`'s captured output)
        regardless of completion order — per-task results land in a
        dict keyed by index and the dict is flattened before return.

        When any task raises (other than a sibling cancellation),
        the remaining in-flight tasks are cancelled. Each cancelled
        task's `_run_one_iteration` catches `asyncio.CancelledError`
        AFTER its `started` event has been emitted and writes a
        sibling-failed event with `reason="sibling_failure"` before
        propagating. Iterations cancelled BEFORE acquiring the
        semaphore never emit any events (they never ran any code).
        """
        # Optional semaphore — gates per-task dispatch.
        semaphore: asyncio.Semaphore | None = None
        if node_def.concurrency is not None and node_def.concurrency > 0:
            semaphore = asyncio.Semaphore(node_def.concurrency)

        async def _bounded(index: int, element: Any) -> dict[str, Any]:
            if semaphore is not None:
                async with semaphore:
                    return await self._run_one_iteration(
                        node_def=node_def,
                        worker_def=worker_def,
                        worker_runner=worker_runner,
                        element=element,
                        index=index,
                        total=total,
                        rendered_with=rendered_with,
                        bound_inputs=bound_inputs,
                        state=state,
                        ctx=ctx,
                        cancel_reason="sibling_failure",
                    )
            return await self._run_one_iteration(
                node_def=node_def,
                worker_def=worker_def,
                worker_runner=worker_runner,
                element=element,
                index=index,
                total=total,
                rendered_with=rendered_with,
                bound_inputs=bound_inputs,
                state=state,
                ctx=ctx,
                cancel_reason="sibling_failure",
            )

        # Spawn every iteration as its own task; track by index.
        tasks: dict[asyncio.Task[dict[str, Any]], int] = {}
        for index, element in enumerate(rendered_over):
            task = asyncio.create_task(_bounded(index, element))
            tasks[task] = index

        results: dict[int, dict[str, Any]] = {}
        first_failure_exc: BaseException | None = None
        outstanding = dict(tasks)
        try:
            while outstanding:
                done, _pending = await asyncio.wait(
                    list(outstanding.keys()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    index = outstanding.pop(task)
                    exc = task.exception()
                    if exc is None:
                        results[index] = task.result()
                        continue
                    if first_failure_exc is None:
                        first_failure_exc = exc
                if first_failure_exc is not None:
                    # Cancel every still-outstanding task; their
                    # per-iteration cancellation arm emits the
                    # sibling-failed event before propagating.
                    for task in outstanding:
                        task.cancel()
                    # Drain the cancelled tasks so their per-iter
                    # arms get to fire before we re-raise.
                    for task in list(outstanding.keys()):
                        try:  # noqa: SIM105 — drain reads clearer than contextlib.suppress here
                            await task
                        except BaseException:  # noqa: BLE001, S110 — drain only
                            pass
                        outstanding.pop(task, None)
                    break
        finally:
            # Safety net — if anything escapes the loop with tasks
            # still live, cancel + drain so we don't leak coroutines.
            for task in list(outstanding.keys()):
                if not task.done():
                    task.cancel()
            for task in list(outstanding.keys()):
                try:  # noqa: SIM105 — drain reads clearer than contextlib.suppress here
                    await task
                except BaseException:  # noqa: BLE001, S110 — drain only
                    pass
                outstanding.pop(task, None)

        if first_failure_exc is not None:
            # Re-raise the original exception so the parent
            # executor's failure path emits the MAP node's own
            # ``workflow.node.failed`` with that error_class.
            raise first_failure_exc

        # Flatten in index order — the documented contract.
        return [results[i] for i in range(total)]

    async def _run_one_iteration(
        self,
        *,
        node_def: MapNodeDef,
        worker_def: Any,
        worker_runner: Any,
        element: Any,
        index: int,
        total: int,
        rendered_with: dict[str, Any],
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
        cancel_reason: str = "sibling_failure",
    ) -> dict[str, Any]:
        """Run one iteration's worker invocation and emit its event pair.

        Shared body for the sequential and parallel paths. The
        structure is:

        1. Build the worker's input overlay.
        2. Strict-mode bind validation against the worker's typed
           `Inputs(BaseModel)`.
        3. Mint per-iteration `NodeContext` + emit `started`.
        4. Dispatch the worker; emit `completed` on success or
           `failed` on exception. `asyncio.CancelledError` is
           treated as a sibling-cancellation event (`error_class` =
           `"asyncio.CancelledError"` + `reason` = `cancel_reason`);
           other exceptions carry their own `error_class`.
        5. Return the worker's captured output dict.
        """
        iteration_overlay = {
            **bound_inputs,
            **rendered_with,
            node_def.item_as: element,
        }

        # Strict-mode bind validation. Mismatch raises BEFORE the
        # iteration's started event so a bad bind surfaces as the
        # map node's own failure, not a per-iteration one (matching
        # the sequential body's contract).
        self._validate_iteration_overlay_strict(
            worker_name=node_def.apply,
            worker_def=worker_def,
            iteration_overlay=iteration_overlay,
        )

        iter_ctx = ctx.derive_for_iteration(
            parent_invocation_id=InvocationId(ctx.invocation_id),
            iteration_index=index,
            iteration_total=total,
        )

        iter_started_id = await ctx.emit_workflow_event(
            WorkflowNodeStartedEvent(
                workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                trace_id=ctx.trace_id,
                parent_event_id=ctx.parent_event_id,
                invocation_id=InvocationId(iter_ctx.invocation_id),
                node_name=node_def.apply,
                payload={
                    "node_name": node_def.apply,
                    "invocation_id": str(iter_ctx.invocation_id),
                    "type": getattr(worker_def, "type", "<copy_of>"),
                    "inputs": dict(iteration_overlay),
                    "caller_chain": list(ctx.caller_chain or ()),
                    # Iteration correlation for forensics.
                    "parent_invocation_id": str(ctx.invocation_id),
                    "iteration_index": index,
                    "iteration_total": total,
                },
            )
        )
        # Stamp the iteration's `parent_event_id` so any event the
        # worker emits (state mutations, agent spawn, etc.) chains
        # back to its own started event, not the map's.
        object.__setattr__(iter_ctx, "parent_event_id", iter_started_id)

        try:
            output = await worker_runner.run(
                node_name=node_def.apply,
                node_def=worker_def,
                bound_inputs=iteration_overlay,
                state=state,
                ctx=iter_ctx,
            )
        except asyncio.CancelledError:
            # Sibling cancellation. Emit the cancelled iteration's
            # failed event so the JSONL forensics view records WHICH
            # iteration was aborted. `reason` defaults to
            # `"sibling_failure"` (the only cancel surface inside a
            # parallel map at v1).
            await ctx.emit_workflow_event(
                WorkflowNodeFailedEvent(
                    workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                    trace_id=ctx.trace_id,
                    parent_event_id=iter_started_id,
                    invocation_id=InvocationId(iter_ctx.invocation_id),
                    node_name=node_def.apply,
                    payload={
                        "node_name": node_def.apply,
                        "invocation_id": str(iter_ctx.invocation_id),
                        "error_class": "asyncio.CancelledError",
                        "reason": cancel_reason,
                        "parent_invocation_id": str(ctx.invocation_id),
                        "iteration_index": index,
                    },
                )
            )
            raise
        except Exception as exc:
            # First-failure path — emit the failed iteration's
            # marker BEFORE re-raising so the JSONL records WHICH
            # iteration broke. The parent executor's failure path
            # then emits the map node's own `workflow.node.failed`
            # and the run aborts. Partial `results` are dropped on
            # the floor — they only become observable via the map's
            # `completed` event, which never fires here.
            await ctx.emit_workflow_event(
                WorkflowNodeFailedEvent(
                    workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                    trace_id=ctx.trace_id,
                    parent_event_id=iter_started_id,
                    invocation_id=InvocationId(iter_ctx.invocation_id),
                    node_name=node_def.apply,
                    payload={
                        "node_name": node_def.apply,
                        "invocation_id": str(iter_ctx.invocation_id),
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                        "parent_invocation_id": str(ctx.invocation_id),
                        "iteration_index": index,
                    },
                )
            )
            raise

        captured = dict(output) if isinstance(output, dict) else {}
        await ctx.emit_workflow_event(
            WorkflowNodeCompletedEvent(
                workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
                trace_id=ctx.trace_id,
                parent_event_id=iter_started_id,
                invocation_id=InvocationId(iter_ctx.invocation_id),
                node_name=node_def.apply,
                payload={
                    "node_name": node_def.apply,
                    "invocation_id": str(iter_ctx.invocation_id),
                    "output": captured,
                    "parent_invocation_id": str(ctx.invocation_id),
                    "iteration_index": index,
                },
            )
        )
        return captured

    @staticmethod
    def _validate_iteration_overlay_strict(
        *,
        worker_name: str,
        worker_def: Any,
        iteration_overlay: dict[str, Any],
    ) -> None:
        """Validate the iteration overlay against the worker's input model.

        Mirrors the executor's
        `IterativeExecutor._validate_bound_inputs_strict` — the
        strict-mode bind-validation contract. Authored-Python
        workers and non-built-in slots return `None` from
        `inputs_model_for` and skip the check; the v1 map runtime
        does NOT thread the catalog-runner introspection seam
        through, so authored workers in a map are validated by the
        worker's own runner only.
        """
        worker_inputs_model = inputs_model_for(worker_def)
        if worker_inputs_model is None:
            return
        try:
            worker_inputs_model.model_validate(iteration_overlay, strict=True)
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0] if errors else {"loc": (), "type": "unknown"}
            loc = first.get("loc") or ()
            bind_key = str(loc[0]) if loc else "<unknown>"
            expected = str(first.get("type", "unknown"))
            if bind_key in iteration_overlay:
                actual = type(iteration_overlay[bind_key]).__name__
            elif bind_key == "<unknown>":
                actual = type(iteration_overlay).__name__
            else:
                actual = "missing"
            raise WorkflowBindTypeMismatch(
                file=f"<map worker {worker_name}>",
                line=0,
                node=worker_name,
                bind=bind_key,
                expected_type=expected,
                actual_type=actual,
            ) from exc
