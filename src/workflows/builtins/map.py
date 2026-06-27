"""``kolsch/nodes/map`` runner — Phase 3.7 §4.6.2.

Sequential fan-out over a list. The map node references a WORKER
node defined in the SAME workflow via ``apply:``; for every element
in ``over:`` the runner dispatches the worker's runner directly with
a per-iteration scope ``{**bound_inputs, **rendered_with, item_as:
element}``, collects the worker's captured output dict, and returns
``{"results": [...]}``.

Two iteration events land in the parent's JSONL per iteration:
``workflow.node.started`` and ``workflow.node.completed`` for the
WORKER's node name (not the map node's). Both carry
``parent_invocation_id`` (= the map node's invocation_id) and
``iteration_index`` (0-based) in their payload so forensics can
correlate workers back to their parent map per master spec §11.3.
The map node's own ``started`` / ``completed`` events are emitted by
the executor's worklist body (Phase 3.1) — the runner is responsible
for the iteration-level pair only.

Failure semantics: a worker raising mid-iteration aborts the map
(sequential, no continue-on-error at v1). The runner emits the
failing iteration's ``workflow.node.failed`` event, then re-raises
so the parent executor's existing failure path emits the map node's
own ``workflow.node.failed``. Partial ``results`` are NOT
persisted — the map node's ``completed`` never fires, so downstream
nodes don't observe a half-populated list. (Future QoL:
``on_error: continue | skip | route_to`` per master spec §1.2.)

Rendering schedule: ``over:`` and ``with:`` constants are each
rendered ONCE at map fire-time (per spec §4.6.2 — constants).
``item_as`` is bound per iteration. A ``with: {time: "{{ now() }}"}``
would therefore have the SAME value across every iteration; an
iteration-varying value belongs inside the worker's own config
strings (which the worker's runner renders per fire).
"""

from __future__ import annotations

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
    """Runtime for ``kolsch/nodes/map`` — master spec §4.6.2.

    Takes the registry so it can dispatch the resolved worker's
    runner (mirrors how the executor's worklist resolves runners).
    No external CLI wiring needed beyond ``register_builtins``.
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
        """§4.6.2.1 algorithm — render → resolve → iterate → collect."""

        # Step 1 — render ``over:`` to a Python list. Jinja-marked
        # strings render against the map's bound_inputs overlay; bare
        # YAML literals (already a list) flow through unchanged.
        over_raw = node_def.over
        if _is_jinja_marked(over_raw):
            rendered_over = ctx.render_jinja(
                over_raw, inputs_overlay=bound_inputs
            )
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

        # Step 2 — render ``with:`` constants ONCE (per master spec
        # §4.6.2 — the values are bound at map fire-time, not per
        # iteration). A ``with: {time: "{{ now() }}"}`` therefore has
        # the same value across every iteration; iteration-varying
        # values belong in the worker's own config.
        rendered_with: dict[str, Any] = {}
        for key, raw_value in node_def.with_.items():
            if _is_jinja_marked(raw_value):
                rendered_with[key] = ctx.render_jinja(
                    raw_value, inputs_overlay=bound_inputs
                )
            else:
                rendered_with[key] = raw_value

        # Step 3 — resolve the worker NodeDef by name in the SAME
        # workflow. The Phase 3.0 validator (§7.4) already rejects an
        # undefined ``apply:`` at parse time; this is the defensive
        # runtime guard for callers that bypass the validator (rare —
        # the executor calls ``validate_workflow_or_raise`` at run-
        # start).
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
        # surfaces as :class:`workflows.errors.NodeTypeNotImplemented`
        # at this site; the executor's outer catch arm wraps it into
        # the map node's ``workflow.node.failed`` and the run aborts.
        worker_runner = self._registry.get(
            getattr(worker_def, "type", "<copy_of>"),
            node_name=node_def.apply,
        )

        # Empty ``over:`` is a clean success — no iterations means no
        # iteration events; the captured output is ``{"results": []}``.
        # Downstream Jinja consumers see an empty list and the usual
        # ``| length`` / ``| selectattr`` filters work without
        # special-casing.
        if not rendered_over:
            return {"results": []}

        # Step 5 — sequential iteration. Each iteration:
        #
        # 1. Builds the worker's input overlay: bound_inputs (the
        #    map's own edges' binds) overlaid with rendered_with
        #    constants overlaid with the item under ``item_as``.
        # 2. Runs strict-mode bind validation against the worker's
        #    typed ``Inputs(BaseModel)`` (Phase 3.3 §6.4.4 contract).
        # 3. Mints a fresh per-iteration NodeContext (distinct
        #    ``invocation_id`` so events don't collide).
        # 4. Emits the worker's ``started`` event on the parent's
        #    JSONL, chained to the prior in-invocation event.
        # 5. Dispatches the worker's ``run`` synchronously.
        # 6. Emits the worker's ``completed`` event.
        # 7. Appends the worker's captured output dict to ``results``.
        total = len(rendered_over)
        results: list[dict[str, Any]] = []
        for index, element in enumerate(rendered_over):
            iteration_overlay = {
                **bound_inputs,
                **rendered_with,
                node_def.item_as: element,
            }

            # Strict-mode bind validation per Phase 3.3 §6.4.4. The
            # worker's ``Inputs(BaseModel)`` validates the overlay
            # under ``strict=True``; mismatch raises
            # :class:`WorkflowBindTypeMismatch` so the map (and the
            # run) abort at the first bad iteration.
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
                        # Phase 3.7 §11.3 — iteration correlation.
                        "parent_invocation_id": str(ctx.invocation_id),
                        "iteration_index": index,
                        "iteration_total": total,
                    },
                )
            )
            # Stamp the iteration's ``parent_event_id`` so any event
            # the worker emits (state mutations, agent spawn, etc.)
            # chains back to its own started event, not the map's.
            object.__setattr__(iter_ctx, "parent_event_id", iter_started_id)

            try:
                output = await worker_runner.run(
                    node_name=node_def.apply,
                    node_def=worker_def,
                    bound_inputs=iteration_overlay,
                    state=state,
                    ctx=iter_ctx,
                )
            except Exception as exc:
                # Emit the failed iteration's marker BEFORE re-raising
                # so the JSONL records WHICH iteration broke. The
                # parent executor's failure path then emits the map
                # node's own ``workflow.node.failed`` and the run
                # aborts. Partial ``results`` are dropped on the
                # floor — they only become observable via the map's
                # ``completed`` event, which never fires here.
                await ctx.emit_workflow_event(
                    WorkflowNodeFailedEvent(
                        workflow_run_id=WorkflowRunId(
                            ctx.workflow_run_id
                        ),
                        trace_id=ctx.trace_id,
                        parent_event_id=iter_started_id,
                        invocation_id=InvocationId(
                            iter_ctx.invocation_id
                        ),
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
            results.append(captured)

        return {"results": results}

    @staticmethod
    def _validate_iteration_overlay_strict(
        *,
        worker_name: str,
        worker_def: Any,  # noqa: ANN401 — NodeDefUnion is a Pydantic alias
        iteration_overlay: dict[str, Any],
    ) -> None:
        """Run the worker's typed ``Inputs(BaseModel)`` against the
        iteration overlay under ``strict=True``.

        Mirrors :meth:`workflows.executor.IterativeExecutor._validate_bound_inputs_strict`
        — the strict-mode bind-validation contract Phase 3.3 ships
        per master spec §6.4.4. Authored-Python workers and
        non-built-in slots return ``None`` from ``inputs_model_for``
        and skip the check; the Phase 3.7 v1 map runtime does NOT
        thread the catalog-runner introspection seam through, so
        authored workers in a map are validated by the worker's own
        runner only.
        """
        worker_inputs_model = inputs_model_for(worker_def)
        if worker_inputs_model is None:
            return
        try:
            worker_inputs_model.model_validate(
                iteration_overlay, strict=True
            )
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
