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

import logging
import os
import uuid
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from assets.refs import is_three_segment, parse_three_segment
from workflows._inputs_models import inputs_model_for
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.context import NodeContext
from workflows.errors import NodeTypeNotImplemented, WorkflowBindTypeMismatch
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeFailedEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunAbortedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunStartedEvent,
    emit_event,
)
from workflows.ids import InvocationId, WorkflowRunId
from workflows.jinja_env import JINJA_ENV
from workflows.run import WorkflowRunStatus

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog
    from workflows.parser import EdgeDef, Workflow
    from workflows.run import WorkflowRun
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
    ) -> None:
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

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    async def run(self) -> WorkflowRunStatus:  # noqa: PLR0915 — sequential algorithm; splitting hides the §8.1 ordering
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
        # a failed run doesn't leak its handle.
        run_context_token = set_workflow_run_context(
            WorkflowRunContext(
                run_id=run_id,
                trace_id=self._run.trace_id,
                store=self._store,
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
        # Step 1-4 tests.
        self._last_event_id = emit_event(
            self._store,
            WorkflowRunStartedEvent(
                workflow_run_id=run_id,
                trace_id=self._run.trace_id,
                parent_event_id=None,
                payload={
                    "workflow_ref": self._workflow_ref(),
                    "inputs": dict(self._run.inputs),
                    "trace_id": str(self._run.trace_id),
                },
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
            # Step 4 — walk the worklist; each node either dispatches,
            # is skipped via ``when: false``, or fails.
            for node_name in topo:
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

                node_def = self._workflow.nodes[node_name]
                # ``when:`` gate — Round 6 J5/β: a string with ``{{``
                # is Jinja; everything else (including bare booleans)
                # passes through literally.
                if not self._when_holds(node_def):
                    # AND-strict rule 1: skip counts as completion.
                    self._completed_or_skipped.add(node_name)
                    continue

                # Render this node's bound inputs from inbound edges.
                bound_inputs = self._render_bound_inputs(node_name)

                # Mint a fresh invocation id; emit ``workflow.node.started``.
                # Master spec §11.3 — node-scoped events carry
                # ``invocation_id`` + ``node_name`` at the envelope
                # top level; payload also holds the legacy keys for
                # back-compat with Step 1-4 readers.
                inv_id = InvocationId(f"inv_{uuid.uuid4()}")
                started_id = emit_event(
                    self._store,
                    WorkflowNodeStartedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._last_event_id,
                        invocation_id=inv_id,
                        node_name=node_name,
                        payload={
                            "node_name": node_name,
                            "invocation_id": str(inv_id),
                            "type": _node_type_ref(node_def),
                            "inputs": bound_inputs,
                            # Master spec §11.3 caller_chain carrier:
                            # the workflow run's own caller_chain (the
                            # frames it was invoked through). v1 ships
                            # the empty tuple — the chain is
                            # populated by Phase 3.7 sub-workflow /
                            # fork wiring.
                            "caller_chain": list(
                                getattr(self._run, "caller_chain", ()) or ()
                            ),
                        },
                    ),
                )
                self._last_event_id = started_id
                self._node_started_event_id[inv_id] = started_id

                ctx = self._build_ctx(
                    inv_id=inv_id,
                    node_def=node_def,
                    parent_event_id=started_id,
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
                    output = await runner.run(
                        node_name=node_name,
                        node_def=node_def,
                        bound_inputs=bound_inputs,
                        state=self._state,
                        ctx=ctx,
                    )
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
                    return WorkflowRunStatus.FAILED

                # Successful invocation — capture, mark complete.
                self._captured[node_name] = dict(output)
                self._completed_or_skipped.add(node_name)
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
                if (
                    _node_type_ref(node_def) == "kolsch/nodes/agent"
                    and ctx.spawned_session_id is not None
                ):
                    completed_payload["spawned_session_id"] = str(
                        ctx.spawned_session_id
                    )
                self._last_event_id = emit_event(
                    self._store,
                    WorkflowNodeCompletedEvent(
                        workflow_run_id=run_id,
                        trace_id=self._run.trace_id,
                        parent_event_id=self._node_started_event_id.get(
                            inv_id
                        ),
                        invocation_id=inv_id,
                        node_name=node_name,
                        payload=completed_payload,
                    ),
                )

                # Phase 3.1 Step 4 — cooperative cancellation. The
                # ``cancel.flag`` sentinel is the CLI / operator's
                # signal for "stop after the current node finishes".
                # We check it AFTER ``workflow.node.completed`` is
                # emitted so the JSONL records the last successful
                # node before the run aborts (master spec §11.2).
                if run_dir.cancel_flag_path.exists():
                    self._emit_aborted(run_id)
                    terminal_status = WorkflowRunStatus.ABORTED
                    return terminal_status

            # Step 5 — assemble terminal-keyed outputs.
            outputs = self._assemble_terminal_outputs(topo)
            self._last_event_id = emit_event(
                self._store,
                WorkflowRunCompletedEvent(
                    workflow_run_id=run_id,
                    trace_id=self._run.trace_id,
                    parent_event_id=self._last_event_id,
                    payload={"outputs": outputs},
                ),
            )
            terminal_status = WorkflowRunStatus.SUCCEEDED
        finally:
            # Phase 3.1 Step 3 — module-isolation cleanup runs on
            # EVERY terminal state (succeeded / failed / timed_out /
            # aborted) so a subsequent run starts from a clean
            # ``sys.modules`` for the workspace's ``bundles/`` tree.
            # Third-party packages survive (the snapshot already
            # contains them; the cleanup's ``if name in before:
            # continue`` guard short-circuits before deletion).
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
    # Topology + predecessor closure.
    # ------------------------------------------------------------------

    def _compute_topology(self) -> tuple[list[str], dict[str, set[str]]]:
        """Kahn's algorithm + predecessor BFS.

        Returns ``(topo_order, predecessors)``. The DAG invariant was
        validated upstream by Step 5's validator, so the Kahn walk
        never stalls on a cycle.
        """
        node_names = list(self._workflow.nodes)
        graph_edges: list[EdgeDef] = (
            list(self._workflow.graph.edges)
            if self._workflow.graph is not None
            else []
        )

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
        inputs_model = inputs_model_for(node_def)
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
        """
        bound: dict[str, Any] = {}
        if self._workflow.graph is None:
            return bound
        for edge in self._workflow.graph.edges:
            if edge.to != node_name:
                continue
            for key, value in edge.binds.items():
                bound[key] = (
                    self._render_jinja(value)
                    if _is_jinja_marked(value)
                    else value
                )
        return bound

    def _render_jinja(  # noqa: ANN401 — Jinja returns Any-shaped values
        self,
        expression: str,
        *,
        inputs_overlay: dict[str, Any] | None = None,
    ) -> Any:
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
