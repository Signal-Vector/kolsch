"""``NodeContext`` — runtime handle passed to every authored ``@node`` ``run``.

Master spec §5.5 locks the contract. Step 1 only DECLARES the
dataclass + the ``emit`` method + the ``for_test`` classmethod; the
Phase 3.1+ executor binds ``emit_fn`` (and wraps it to prepend
``node.<bundle>.`` to subkinds) and supplies the live state /
secrets mappings wrapped in :class:`types.MappingProxyType`.

Public fields (post-R7-L7-3 renames): the underscore-prefixed
``_bundle`` / ``_emit_fn`` are GONE — both are public test seams,
named ``bundle`` and ``emit_fn``. ``frozen=True`` keeps them
read-only at runtime; tests that need to override them construct
via the :meth:`NodeContext.for_test` classmethod or pass them as
keyword args.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workflows.ids import InvocationId

if TYPE_CHECKING:
    from schemas import EventId, SessionId
    from workflows.events import WorkflowEvent
    from workflows.ids import TraceId, WorkflowRunId

__all__ = ["NodeContext"]


# A minimal structural-logger protocol: anything with the standard
# logging methods (``info`` / ``warning`` / ``error`` / ``debug``)
# satisfies the contract. The Phase 3.1+ executor binds a structured
# logger here; the for_test default uses the stdlib logger so unit
# tests can construct a ``NodeContext`` without standing up the
# observability stack.
StructLogger = logging.Logger


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Runtime context for an authored ``@node`` ``run`` (master spec §5.5).

    Fields are populated by the engine at construction time. Authored
    code reads these fields — no field on this dataclass is mutable
    from inside an authored node (``frozen=True`` enforces it; the
    read-only mapping wrappers on ``secrets`` / ``state`` enforce
    deep immutability).

    Spoof-resistance (R6 S4 + R7 L7-3): ``bundle`` is set by the
    engine from the catalog-resolved invocation file's bundle, NOT
    from ``inspect``-ing the calling ``@node``'s ``__module__``.
    Re-exporting a node from another bundle cannot fake its bundle
    of origin — the engine tracks the resolution path that led to
    this invocation, and the wire prefix follows the catalog.
    """

    workflow_run_id: str
    invocation_id: str
    workspace_root: Path
    log: StructLogger
    secrets: Mapping[str, str]
    state: Mapping[str, Any]
    bundle: str
    emit_fn: Callable[[str, str, Mapping[str, Any]], None] = field(repr=False)
    # Phase 3.3 §6.4 — workflow-level inputs (the baseline for the
    # ``inputs.*`` Jinja namespace). Built-in runners that render
    # their config strings (e.g. bash ``command:``) merge this with
    # the receiver's edge-bound ``bound_inputs`` so the namespace
    # matches the executor's ``_render_jinja(..., inputs_overlay=)``
    # composition: workflow inputs first, edge-bound overlay second
    # (edge wins on conflict). Empty mapping at construction outside
    # the executor's worklist (e.g. ``for_test``).
    workflow_inputs: Mapping[str, Any] = field(default_factory=dict)
    # Phase 3.2 — the executor-emitted ``workflow.node.started`` id for
    # this invocation. Runners that spawn child entities (the agent
    # runner) thread it into their parent-pointer payloads
    # (``FromWorkflowRun.event_id`` per master spec §11.6.1). ``None``
    # only when a runner constructs a NodeContext outside the executor's
    # worklist (e.g. unit tests via :meth:`for_test`).
    parent_event_id: EventId | None = None
    # Phase 3.2 — the workflow run's trace_id (master spec §11.6.4).
    # Inherited UNCHANGED by every spawned session's events so the two
    # JSONLs cross-correlate exactly. ``None`` only outside the
    # executor's worklist.
    trace_id: TraceId | None = None
    # Phase 3.2 — the workflow run's caller_chain (master spec §13.x).
    # The agent runner prepends the agent's ref before passing into the
    # engine so authz audit events record the workflow→agent frame.
    # Proximate first; default ``()`` for root-level invocations.
    caller_chain: tuple[str, ...] = field(default_factory=tuple)
    # Phase 3.2 — set by ``AgentRunner`` to the session it spawned, read
    # by the executor's ``workflow.node.completed`` payload builder
    # (master spec §11.6.3). Frozen dataclass — runners mutate via
    # ``object.__setattr__`` so the "authored nodes cannot mutate
    # context" invariant still holds for the public surface.
    spawned_session_id: SessionId | None = None
    # Phase 3.4 — hook into the executor's Jinja renderer so runners
    # (the ``user_input`` runner first; ``bash`` and others may opt in
    # later) can render config strings against the SAME ``nodes`` /
    # ``state`` / ``inputs`` scope the executor uses for ``binds:``
    # rendering. The callback is bound at :meth:`IterativeExecutor._build_ctx`
    # construction; ``None`` outside the executor's worklist (e.g.
    # :meth:`NodeContext.for_test`). The positional second argument is
    # the receiver's edge-bound ``bound_inputs`` (per master spec §6.4.1
    # — overlays onto workflow-level inputs in the ``inputs.*`` namespace).
    render_jinja_callback: (
        Callable[[str, Mapping[str, Any] | None], Any] | None
    ) = field(default=None, repr=False)
    # Phase 3.5 Step 0 — hook into the executor's JSONL append + last-
    # event-id bookkeeping so runners can emit fully-typed
    # :class:`WorkflowEvent` envelopes (the new
    # ``workflow.state.mutated`` kind first; future kinds may opt in).
    # The callback is bound at :meth:`IterativeExecutor._build_ctx`
    # construction; ``None`` outside the executor's worklist (e.g.
    # :meth:`NodeContext.for_test`).
    #
    # Naming note: the existing public :meth:`emit` takes the
    # author-facing ``(subkind, payload)`` shape and wraps the wire
    # kind as ``node.<bundle>.<subkind>`` — its surface is locked.
    # The Phase 3.5 build spec's example ``ctx.emit(event)`` would
    # collide with that signature, so the runner-facing hook is
    # exposed as :meth:`emit_workflow_event` instead. Same hook
    # semantics, distinct method name. Recorded in
    # ``process/phase3.5-file-io-state-build.md`` under "Decisions
    # made during Step 0".
    emit_workflow_event_callback: (
        Callable[[WorkflowEvent], Awaitable[EventId]] | None
    ) = field(default=None, repr=False)
    # Phase 3.7 — set by :class:`workflows.builtins.workflow_sub.WorkflowSubRunner`
    # to the child workflow run it spawned, read by the executor's
    # ``workflow.node.completed`` payload builder so consumers can
    # navigate from a parent's workflow-node event to the child run's
    # JSONL. Mirrors :attr:`spawned_session_id` for agent nodes. Frozen
    # dataclass — the runner mutates via ``object.__setattr__`` so the
    # "authored nodes cannot mutate context" invariant still holds for
    # the public surface.
    spawned_run_id: WorkflowRunId | None = None
    # Phase 3.7 — depth + ordered ref chain of workflow→workflow calls
    # from the root entry point to (and including) this run's workflow
    # ref. The :class:`WorkflowSubRunner` reads ``call_chain`` to detect
    # cycles (ref repetition) BEFORE the catalog lookup, and reads
    # ``call_depth`` to choose the child executor's ``_call_depth``
    # constructor kwarg. Root invocations carry ``call_depth=0`` and
    # ``call_chain=()``; nested invocations inherit + extend.
    call_depth: int = 0
    call_chain: tuple[str, ...] = ()
    # Phase 3.7 — the parent :class:`workflows.parser.Workflow` so the
    # :class:`workflows.builtins.map.MapRunner` can resolve its
    # ``apply:`` worker by name in the same workflow. Typed ``Any`` to
    # avoid the executor-→parser import cycle at runtime.
    workflow: Any = None
    # Phase 3.7 — per-iteration tracking for ``map`` workers. The map
    # runner stamps ``parent_invocation_id`` (= the map node's invocation
    # id) and a 0-based ``iteration_index`` / total on each worker's
    # NodeContext so the per-iteration ``workflow.node.started`` /
    # ``_completed`` events carry the iteration context for forensics
    # (master spec §11.3). All three are ``None`` outside a map worker.
    parent_invocation_id: InvocationId | None = None
    iteration_index: int | None = None
    iteration_total: int | None = None

    def derive_for_iteration(
        self,
        *,
        parent_invocation_id: InvocationId,
        iteration_index: int,
        iteration_total: int,
    ) -> NodeContext:
        """Return a per-iteration copy with a fresh ``invocation_id``.

        Phase 3.7 — used by :class:`workflows.builtins.map.MapRunner`
        to mint a NodeContext for each iteration's worker dispatch.
        The copy carries:

        - a fresh ``invocation_id`` (``inv_<uuid>``) so the worker's
          per-iteration ``workflow.node.started`` / ``_completed``
          events have a distinct identifier;
        - ``parent_invocation_id`` set to the map node's invocation id
          so forensics can correlate iterations back to the map fire;
        - 0-based ``iteration_index`` + ``iteration_total`` for
          ordering forensics.

        Every other field (workspace_root, log, secrets, state,
        bundle, callbacks, trace_id, caller_chain, workflow_inputs,
        spawned_session_id/run_id, call_depth/call_chain, workflow) is
        inherited verbatim from ``self``.
        """
        return replace(
            self,
            invocation_id=f"inv_{uuid.uuid4()}",
            parent_invocation_id=parent_invocation_id,
            iteration_index=iteration_index,
            iteration_total=iteration_total,
        )

    async def emit_workflow_event(self, event: WorkflowEvent) -> EventId:
        """Append ``event`` to the run's JSONL via the executor hook.

        Phase 3.5 — used by ``SetStateRunner`` / ``DeleteStateRunner``
        to emit ``workflow.state.mutated`` events between the node's
        ``started`` and ``completed`` markers. Returns the minted
        event id so callers can chain subsequent emissions.

        Raises :class:`RuntimeError` when no callback is bound — i.e.,
        when called on a :meth:`for_test` context that didn't pass
        one in. Production contexts (built by
        :meth:`IterativeExecutor._build_ctx`) always have a callback.
        """
        if self.emit_workflow_event_callback is None:
            msg = "NodeContext has no emit_workflow_event callback bound"
            raise RuntimeError(msg)
        return await self.emit_workflow_event_callback(event)

    def render_jinja(
        self,
        expression: str,
        *,
        inputs_overlay: Mapping[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 — Jinja returns Any-shaped values
        """Render ``expression`` via the executor's Jinja environment.

        Phase 3.4 — the ``user_input`` runner uses this to render the
        node's ``prompt:`` field BEFORE raising
        :class:`workflows.errors.WorkflowPausedForInput` so the pause
        signal carries the user-visible prompt. ``inputs_overlay``
        threads the receiver's edge-bound ``bound_inputs`` into the
        ``inputs.*`` namespace (master spec §6.4.1).

        Raises :class:`RuntimeError` when no renderer is bound — i.e.,
        when called on a :meth:`for_test` context that didn't pass one
        in. Production contexts (built by
        :meth:`IterativeExecutor._build_ctx`) always have a renderer.
        """
        if self.render_jinja_callback is None:
            msg = "NodeContext has no Jinja renderer bound"
            raise RuntimeError(msg)
        return self.render_jinja_callback(expression, inputs_overlay)

    def emit(self, subkind: str, payload: Mapping[str, Any]) -> None:
        """Emit a custom workflow event.

        ``subkind`` is the FREEFORM author-supplied suffix (no regex
        enforcement). The engine auto-prepends ``node.<bundle>.``
        based on ``self.bundle`` (set by the engine at construction
        time from the catalog-resolved invocation file's bundle, per
        R6 S4 + R7 L7-3), so the wire ``kind`` written to the JSONL
        becomes ``node.<bundle>.<subkind>``.

        Per R6-R1: the "author" segment from R5 R1's
        ``node.author.<bundle>.<subkind>`` is DROPPED — engine event
        kinds all start with ``workflow.``, ``policy.``, ``model.``,
        or ``tool.``, so ``node.*`` is unambiguously the author
        namespace and no disambiguating segment is needed.

        Authors NEVER write the ``node.<bundle>.`` prefix themselves;
        the auto-prepend is the bundle-namespacing guarantee.
        """
        self.emit_fn(self.bundle, subkind, payload)

    @classmethod
    def for_test(
        cls,
        *,
        workflow_run_id: str = "wrk_test",
        invocation_id: str = "inv_test",
        bundle: str = "test",
        workspace_root: Path | None = None,
        log: StructLogger | None = None,
        emit_fn: Callable[[str, str, Mapping[str, Any]], None] | None = None,
        secrets: Mapping[str, str] | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> NodeContext:
        """Round 7 A7-1 — sensible-defaults constructor for unit tests.

        Tests of authored Python nodes construct via
        ``NodeContext.for_test(...)`` instead of hand-rolling all
        eight fields. Override any field by keyword. Amends
        R6 J3 D ("no helper at all").

        Defaults:
        - ``workflow_run_id`` / ``invocation_id`` — fixed sentinel
          strings (``"wrk_test"`` / ``"inv_test"``).
        - ``bundle`` — ``"test"``.
        - ``workspace_root`` — a fresh ``tempfile.mkdtemp()`` path
          (so file-touching nodes have a real directory).
        - ``log`` — the ``"workflows.test"`` stdlib logger.
        - ``emit_fn`` — a no-op (``lambda *_: None``).
        - ``secrets`` / ``state`` — empty dicts (``Mapping``
          accepts plain dicts; production wraps in
          ``MappingProxyType``).
        """
        if workspace_root is None:
            workspace_root = Path(tempfile.mkdtemp(prefix="kolsch-nodectx-"))
        if log is None:
            log = logging.getLogger("workflows.test")
        if emit_fn is None:

            def _noop_emit(
                _bundle: str,
                _subkind: str,
                _payload: Mapping[str, Any],
            ) -> None:
                return None

            emit_fn = _noop_emit
        return cls(
            workflow_run_id=workflow_run_id,
            invocation_id=invocation_id,
            workspace_root=workspace_root,
            log=log,
            secrets=secrets if secrets is not None else {},
            state=state if state is not None else {},
            bundle=bundle,
            emit_fn=emit_fn,
        )
