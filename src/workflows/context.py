"""`NodeContext` — runtime handle passed to every authored `@node` `run`.

The dataclass authored Python nodes receive at runtime. Carries the
identifying info (`workflow_run_id`, `invocation_id`, `bundle`), the
execution surface (`state`, `secrets`, `workspace_root`, `log`), and
callbacks the engine binds at construction so the node can emit
events, render Jinja expressions, and signal pauses.

Frozen — authored code cannot mutate `NodeContext` itself; the
read-only mapping wrappers on `secrets` and `state` enforce deep
immutability. Tests construct via
[`NodeContext.for_test`][workflows.context.NodeContext.for_test]
instead of hand-rolling the constructor.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.context import CancelToken
    from schemas import EventId, SessionId
    from schemas.events import Event
    from workflows.events import WorkflowEvent
    from workflows.ids import InvocationId, TraceId, WorkflowRunId

__all__ = ["NodeContext"]


# A minimal structural-logger contract: anything with the standard
# logging methods (`info` / `warning` / `error` / `debug`) satisfies
# it. The executor binds a structured logger here; `for_test` uses the
# stdlib logger so unit tests can construct a `NodeContext` without
# standing up the observability stack.
StructLogger = logging.Logger


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Runtime context for an authored `@node`'s `run` method.

    Fields are populated by the engine at construction time. Authored
    code reads these fields — no field on this dataclass is mutable
    from inside an authored node (`frozen=True` enforces it; the
    read-only mapping wrappers on `secrets` / `state` enforce deep
    immutability).

    **Spoof-resistance.** `bundle` is set by the engine from the
    catalog-resolved invocation file's bundle, NOT from
    `inspect`-ing the calling `@node`'s `__module__`. Re-exporting a
    node from another bundle cannot mock its bundle of origin — the
    engine tracks the resolution path that led to this invocation,
    and the wire prefix follows the catalog.
    """

    workflow_run_id: str
    invocation_id: str
    workspace_root: Path
    log: StructLogger
    secrets: Mapping[str, str]
    state: Mapping[str, Any]
    bundle: str
    emit_fn: Callable[[str, str, Mapping[str, Any]], None] = field(repr=False)
    # Workflow-level inputs (the baseline for the `inputs.*` Jinja
    # namespace). Built-in runners that render their config strings
    # (e.g. bash `command:`) merge this with the receiver's edge-bound
    # `bound_inputs` so the namespace matches the executor's
    # `_render_jinja(..., inputs_overlay=)` composition: workflow
    # inputs first, edge-bound overlay second (edge wins on conflict).
    # Empty mapping at construction outside the executor's worklist
    # (e.g. `for_test`).
    workflow_inputs: Mapping[str, Any] = field(default_factory=dict)
    # The executor-emitted `workflow.node.started` id for this
    # invocation. Runners that spawn child entities (the agent runner)
    # thread it into their parent-pointer payloads. `None` only when a
    # runner constructs a NodeContext outside the executor's worklist
    # (e.g. unit tests via `for_test`).
    parent_event_id: EventId | None = None
    # The workflow run's `trace_id`. Inherited UNCHANGED by every
    # spawned session's events so the two JSONLs cross-correlate
    # exactly. `None` only outside the executor's worklist.
    trace_id: TraceId | None = None
    # The workflow run's `caller_chain`. The agent runner prepends the
    # agent's ref before passing into the engine so authz audit events
    # record the workflow→agent frame. Proximate first; default `()`
    # for root-level invocations.
    caller_chain: tuple[str, ...] = field(default_factory=tuple)
    # Set by the agent runner to the session it spawned; read by the
    # executor's `workflow.node.completed` payload builder. Frozen
    # dataclass — runners mutate via `object.__setattr__` so the
    # "authored nodes cannot mutate context" invariant still holds for
    # the public surface.
    spawned_session_id: SessionId | None = None
    # Hook into the executor's Jinja renderer so runners (the
    # `user_input` runner first; `bash` and others may opt in later)
    # can render config strings against the SAME `nodes` / `state` /
    # `inputs` scope the executor uses for `binds:` rendering. The
    # callback is bound at executor construction; `None` outside the
    # executor's worklist (e.g. `for_test`). The positional second
    # argument is the receiver's edge-bound `bound_inputs` (overlays
    # onto workflow-level inputs in the `inputs.*` namespace).
    render_jinja_callback: Callable[[str, Mapping[str, Any] | None], Any] | None = (
        field(default=None, repr=False)
    )
    # Hook into the executor's JSONL append + last-event-id
    # bookkeeping so runners can emit fully-typed `WorkflowEvent`
    # envelopes (the `workflow.state.mutated` kind first; future kinds
    # may opt in). The callback is bound at executor construction;
    # `None` outside the executor's worklist (e.g. `for_test`).
    #
    # Naming note: the existing public `emit` takes the author-facing
    # `(subkind, payload)` shape and wraps the wire kind as
    # `node.<bundle>.<subkind>` — its surface is locked. A
    # `ctx.emit(event)` form would collide with that signature, so the
    # runner-facing hook is exposed as `emit_workflow_event` instead.
    # Same hook semantics, distinct method name.
    emit_workflow_event_callback: (
        Callable[[WorkflowEvent], Awaitable[EventId]] | None
    ) = field(default=None, repr=False)
    # Set by
    # [`WorkflowSubRunner`][workflows.builtins.workflow_sub.WorkflowSubRunner]
    # to the child workflow run it spawned; read by the executor's
    # `workflow.node.completed` payload builder so consumers can
    # navigate from a parent's workflow-node event to the child run's
    # JSONL. Mirrors `spawned_session_id` for agent nodes. Frozen
    # dataclass — the runner mutates via `object.__setattr__` so the
    # "authored nodes cannot mutate context" invariant still holds for
    # the public surface.
    spawned_run_id: WorkflowRunId | None = None
    # Depth + ordered ref chain of workflow→workflow calls from the
    # root entry point to (and including) this run's workflow ref.
    # `WorkflowSubRunner` reads `call_chain` to detect cycles (ref
    # repetition) BEFORE the catalog lookup, and reads `call_depth` to
    # choose the child executor's `_call_depth` constructor kwarg.
    # Root invocations carry `call_depth=0` and `call_chain=()`;
    # nested invocations inherit + extend.
    call_depth: int = 0
    call_chain: tuple[str, ...] = ()
    # The parent
    # [`Workflow`][workflows.parser.Workflow] so the
    # [`MapRunner`][workflows.builtins.map.MapRunner] can resolve its
    # `apply:` worker by name in the same workflow. Typed `Any` to
    # avoid the executor → parser import cycle at runtime.
    workflow: Any = None
    # Per-iteration tracking for `map` workers. The map runner stamps
    # `parent_invocation_id` (= the map node's invocation id) and a
    # 0-based `iteration_index` / total on each worker's NodeContext
    # so the per-iteration `workflow.node.started` / `_completed`
    # events carry the iteration context for forensics. All three are
    # `None` outside a map worker.
    parent_invocation_id: InvocationId | None = None
    iteration_index: int | None = None
    iteration_total: int | None = None
    # Resume-cascade markers. `is_resume_mode` is `True` only when the
    # executor is re-dispatching an OPEN workflow-node invocation that
    # paused waiting for a child run to terminate (the parent's own
    # `waiting_at` pointed at this invocation). `recorded_child_run_id`
    # carries the child run id recovered from the prior
    # `workflow.node.called_workflow` event in the parent's JSONL, so
    # `WorkflowSubRunner`'s resume branch can read the child's status
    # without re-walking the JSONL itself. Both `None` on initial-run
    # dispatch.
    is_resume_mode: bool = False
    recorded_child_run_id: WorkflowRunId | None = None
    # spec-011: the executor's in-process cancel handle. When set, the
    # `AgentRunner` (and any other sub-agent invocation runner)
    # composes it into the sub-agent's `ExecutionContext.cancellation`
    # via `CombinedCancelToken(cancel_token, existing_or_new)` so the
    # cancel reaches the sub-agent's LLM stream mid-node. `None` when
    # the executor was constructed without a cancel token (chat, CLI,
    # tests). Frozen dataclass — the executor plumbs this through
    # `_build_ctx` at NodeContext construction; no runtime mutation.
    cancel_token: CancelToken | None = None
    # spec-020: the sub-agent event forwarder seam. When set, the
    # `AgentRunner` (or any other runner that spawns a sub-agent) calls
    # this per-event as it iterates
    # `AgentEngine.execute_stream(...)` so each session-scoped
    # `tool.called` / `agent.message` / etc. reaches the executor's
    # observer channel and, eventually, the `WorkflowNodeRow` that
    # owns this invocation. `None` outside the executor's worklist
    # (e.g., `for_test`); production contexts (built by the executor's
    # `_build_ctx`) always set it. The runner MUST NOT raise on a
    # bound callback that itself raises — best-effort like the other
    # NodeContext seams.
    on_sub_agent_event_callback: Callable[[Event], None] | None = field(
        default=None, repr=False,
    )

    def derive_for_iteration(
        self,
        *,
        parent_invocation_id: InvocationId,
        iteration_index: int,
        iteration_total: int,
    ) -> NodeContext:
        """Return a per-iteration copy with a fresh `invocation_id`.

        Used by
        [`MapRunner`][workflows.builtins.map.MapRunner] to mint a
        NodeContext for each iteration's worker dispatch. The copy
        carries:

        - a fresh `invocation_id` (`inv_<uuid>`) so the worker's
          per-iteration `workflow.node.started` / `_completed` events
          have a distinct identifier;
        - `parent_invocation_id` set to the map node's invocation id
          so forensics can correlate iterations back to the map fire;
        - 0-based `iteration_index` + `iteration_total` for ordering
          forensics.

        Every other field (`workspace_root`, `log`, `secrets`,
        `state`, `bundle`, callbacks, `trace_id`, `caller_chain`,
        `workflow_inputs`, `spawned_session_id` / `spawned_run_id`,
        `call_depth` / `call_chain`, `workflow`) is inherited verbatim
        from `self`.
        """
        return replace(
            self,
            invocation_id=f"inv_{uuid.uuid4()}",
            parent_invocation_id=parent_invocation_id,
            iteration_index=iteration_index,
            iteration_total=iteration_total,
        )

    def require_trace_id(self) -> TraceId:
        """Return `self.trace_id`, raising if it is `None`.

        `trace_id` is `Optional` on the dataclass so `for_test`
        can construct a `NodeContext` without standing up the
        trace-id minting layer. Executor-populated contexts always
        set it, so runtime paths (the ones that construct
        `Workflow*Event` payloads) can call this to narrow the type
        without threading a per-site assertion.

        Raises:
            RuntimeError: `trace_id` is `None` — only reachable from
                a test factory that forgot to set it.
        """
        if self.trace_id is None:
            msg = (
                "NodeContext.trace_id is None; executor-populated contexts "
                "always set it — this call path is test-only."
            )
            raise RuntimeError(msg)
        return self.trace_id

    def require_parent_event_id(self) -> EventId:
        """Return `self.parent_event_id`, raising if it is `None`.

        Same shape as `require_trace_id`: `parent_event_id` is
        `Optional` for `for_test` construction, but any runtime code
        that threads it into a payload (e.g. the agent runner
        stamping `FromWorkflowRun.event_id`) can call this to
        narrow the type in one place.

        Raises:
            RuntimeError: `parent_event_id` is `None` — only
                reachable from a test factory that forgot to set it.
        """
        if self.parent_event_id is None:
            msg = (
                "NodeContext.parent_event_id is None; executor-populated "
                "contexts always set it — this call path is test-only."
            )
            raise RuntimeError(msg)
        return self.parent_event_id

    async def emit_workflow_event(self, event: WorkflowEvent) -> EventId:
        """Append `event` to the run's JSONL via the executor hook.

        Used by `SetStateRunner` / `DeleteStateRunner` to emit
        `workflow.state.mutated` events between the node's `started`
        and `completed` markers. Returns the minted event id so
        callers can chain subsequent emissions.

        Raises:
            RuntimeError: No callback is bound — i.e. when called on a
                `for_test` context that didn't pass one in. Production
                contexts (built by the executor) always have a
                callback.
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
    ) -> Any:
        """Render `expression` via the executor's Jinja environment.

        The `user_input` runner uses this to render the node's
        `prompt:` field BEFORE raising `WorkflowPausedForInput` so the
        pause signal carries the user-visible prompt. `inputs_overlay`
        threads the receiver's edge-bound `bound_inputs` into the
        `inputs.*` namespace.

        Args:
            expression: The Jinja source to render.
            inputs_overlay: Edge-bound inputs that overlay the
                workflow-level `inputs` namespace for this render.
                `None` means no overlay.

        Returns:
            The rendered value — Jinja's native return shape.

        Raises:
            RuntimeError: No renderer is bound — i.e. when called on a
                `for_test` context that didn't pass one in. Production
                contexts (built by the executor) always have a
                renderer.
        """
        if self.render_jinja_callback is None:
            msg = "NodeContext has no Jinja renderer bound"
            raise RuntimeError(msg)
        return self.render_jinja_callback(expression, inputs_overlay)

    def emit(self, subkind: str, payload: Mapping[str, Any]) -> None:
        """Emit a custom workflow event.

        The engine auto-prepends `node.<bundle>.` based on
        `self.bundle` (set by the engine at construction time from the
        catalog-resolved invocation file's bundle), so the wire `kind`
        written to the JSONL becomes `node.<bundle>.<subkind>`.
        Authors NEVER write the `node.<bundle>.` prefix themselves;
        the auto-prepend is the bundle-namespacing guarantee.

        Args:
            subkind: A freeform author-supplied suffix. No regex
                enforcement — authors choose any string.
            payload: The event data. Captured verbatim.
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
        """Construct a `NodeContext` for unit tests with sensible defaults.

        Tests of authored Python nodes use this instead of hand-rolling
        every constructor arg. Override any field by keyword.

        Defaults:

        - `workflow_run_id` / `invocation_id` — fixed sentinel strings
          (`"wrk_test"` / `"inv_test"`).
        - `bundle` — `"test"`.
        - `workspace_root` — a fresh `tempfile.mkdtemp()` path so
          file-touching nodes have a real directory.
        - `log` — the `"workflows.test"` stdlib logger.
        - `emit_fn` — a no-op (`lambda *_: None`).
        - `secrets` / `state` — empty dicts (`Mapping` accepts plain
          dicts; production wraps in `MappingProxyType`).
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
