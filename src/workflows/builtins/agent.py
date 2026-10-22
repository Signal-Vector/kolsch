"""`kolsch/nodes/agent` runner — the workflow→agent boundary.

Catalog lookup of the node's `agent:` ref, `ResolvedAgentConfig`
build via the chat-side loader, optional per-invocation `outputs:`
override, child-session creation carrying the
[`FromWorkflowRun`][schemas.entities.FromWorkflowRun] parent
pointer, and synchronous invocation of
[`AgentEngine.execute`][engine.engine.AgentEngine.execute].
[`AgentExecutionError`][engine.context.AgentExecutionError] — the
engine's Fatal failure transport — is remapped to
[`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed] so the
executor's existing failure path emits `workflow.node.failed` +
`workflow.run.failed`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine import ExecutionContext
from engine.context import AgentExecutionError, CancelToken, CombinedCancelToken
from schemas import FromWorkflowRun, IOSpec
from schemas.events import EventType
from workflows.errors import AgentRefUnresolved, WorkflowNodeFailed
from workflows.ids import InvocationId, WorkflowRunId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from assets.catalog import AssetCatalog
    from engine import AgentEngine
    from schemas import ResolvedAgentConfig
    from store import SessionStore
    from workflows.context import NodeContext
    from workflows.parser import AgentNodeDef


__all__ = ["AgentRunner"]


def _extract_nested_error(failed_event: Any) -> dict[str, Any] | None:
    """Extract `{type, message, event_id}` from a `thread.exchange_failed` event.

    The engine records the terminal typed error as a
    :class:`schemas.error.Error` envelope on the event's
    ``data["error"]``. Bug-055: the workflow executor's
    `workflow.node.failed` payload includes this on `nested_error`
    so the real cause (e.g. `llm.connector_unknown`) surfaces
    without operators grepping the spawned agent's sub-session
    JSONL for the `ev_...` id. Returns `None` when the shape doesn't
    match — the caller falls back to the exception message alone.
    """
    if failed_event is None:
        return None
    data = getattr(failed_event, "data", None) or {}
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    err_type = error.get("type")
    err_message = error.get("message")
    if not isinstance(err_type, str) or not isinstance(err_message, str):
        return None
    event_id = getattr(failed_event, "event_id", None)
    return {
        "type": err_type,
        "message": err_message,
        "event_id": str(event_id) if event_id is not None else None,
    }


class AgentRunner:
    """Runner for `kolsch/nodes/agent` — spawns a child agent session.

    Constructed once at registration with the catalog, session
    store, config builder, and engine. Every invocation resolves the
    node's `agent:` ref, builds the agent config, spawns a child
    session linked back to this run via
    [`FromWorkflowRun`][schemas.entities.FromWorkflowRun], and
    invokes [`AgentEngine.execute`][engine.engine.AgentEngine.execute]
    synchronously. The agent's structured `agent.message.content`
    becomes the node's captured output.
    """

    def __init__(
        self,
        *,
        catalog: AssetCatalog,
        session_store: SessionStore,
        config_builder: Callable[[str], Awaitable[ResolvedAgentConfig]],
        engine: AgentEngine,
    ) -> None:
        self._catalog = catalog
        self._session_store = session_store
        self._config_builder = config_builder
        self._engine = engine

    async def run(
        self,
        *,
        node_name: str,
        node_def: AgentNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Spawn a child agent session and return its structured output.

        Steps in order:

        1. Defensive catalog lookup. The run-start validator already
           catches missing refs; this guard protects against drift
           (asset deleted between validate and run).
        2. Build the typed
           [`ResolvedAgentConfig`][schemas.ResolvedAgentConfig] via
           the injected loader (chat surface's path).
        3. If `node_def.outputs` is set, replace `config.io.outputs`
           with the override schema. Identity fields and inputs are
           untouched — the override only narrows the agent's output
           shape for this invocation.
        4. Mint the
           [`FromWorkflowRun`][schemas.entities.FromWorkflowRun]
           parent pointer from `ctx` (workflow_run_id +
           invocation_id + parent_event_id).
        5. Create the child session with the pointer; stamp
           `ctx.spawned_session_id` so the executor's payload
           builder can read it.
        6. Build the
           [`ExecutionContext`][engine.ExecutionContext] carrying
           the workflow's `trace_id` and the extended caller chain
           (agent ref prepended). Invoke
           [`AgentEngine.execute_stream`][engine.engine.AgentEngine.execute_stream]
           and iterate — spec-020 tees each yielded session-scoped
           `Event` to `ctx.on_sub_agent_event_callback` so the chat
           UI's `WorkflowNodeRow` can render live sub-agent activity
           inside the expanded body. Terminal events
           (`AGENT_MESSAGE`, `THREAD_EXCHANGE_FAILED`) are captured
           for the runner's return value.
        7. Catch
           [`AgentExecutionError`][engine.context.AgentExecutionError]
           OR translate a trailing `thread.exchange_failed` event
           into the same exception; remap to
           [`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed]
           so the executor's existing failure path emits
           `workflow.node.failed`.
        8. Extract the structured output from the captured
           `agent.message` event's `data["content"]`.

        Args:
            node_name: The workflow-level node handle.
            node_def: The parsed agent-node definition.
            bound_inputs: The receiver's edge-bound inputs, already
                rendered by the executor.
            state: The workflow's live state dict; the agent runner
                does not consume it directly at this version.
            ctx: The per-invocation
                [`NodeContext`][workflows.context.NodeContext]
                carrying ids, callbacks, and trace plumbing.

        Returns:
            The agent's `agent.message.content` dict — empty when
            the message carries no structured content.

        Raises:
            WorkflowNodeFailed: The agent ref could not be resolved,
                or the engine raised
                [`AgentExecutionError`][engine.context.AgentExecutionError].
        """
        _ = state  # reserved — Step 5+ may surface state into trigger

        # Step 1: defensive catalog lookup.
        entry = self._catalog.lookup(node_def.agent, expected_type="agent")
        if entry is None:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=AgentRefUnresolved(ref=node_def.agent),
            )

        # Step 2: build the resolved config via the chat-side loader.
        resolved = await self._config_builder(node_def.agent)

        # Step 3: optional `outputs:` override — replace ONLY the
        # `outputs` slice of `config.io`. Identity fields (model,
        # prompt, system_prompt, temperature, max_iterations, tools)
        # and `config.io.inputs` are untouched — the override is
        # an output-shape narrowing, not a re-skinning.
        if node_def.outputs is not None:
            patched_io = IOSpec(
                mode=resolved.io.mode,
                inputs=resolved.io.inputs,
                outputs=node_def.outputs.outputs,
            )
            resolved = resolved.model_copy(update={"io": patched_io})

        # Step 4: mint the parent pointer.
        fwr = FromWorkflowRun(
            workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
            invocation_id=InvocationId(ctx.invocation_id),
            event_id=ctx.require_parent_event_id(),
        )

        # Step 5: spawn the child session. The store keys sessions by
        # the resolved `agent_id` (prefixed `ag_...`); the catalog
        # `ref` is the 3-segment user-facing handle and would fail
        # the `AgentId` shape check. Deterministic `name=` so
        # spawned sessions are easy to spot in `kolsch sessions list`.
        # `trace_id` is threaded so the spawned session's
        # `session.opened` event correlates with the workflow run
        # from the very first event; subsequent engine-emitted events
        # inherit it via
        # [`ExecutionContext`][engine.ExecutionContext].
        session = await self._session_store.create_session(
            str(resolved.agent_id),
            name=f"{ctx.workflow_run_id}::{node_name}",
            from_workflow_run=fwr,
            trace_id=str(ctx.trace_id) if ctx.trace_id is not None else None,
        )
        # `NodeContext` is frozen to keep authored nodes from
        # mutating it; the runner sets the engine-side outbound field
        # via `object.__setattr__` so the public surface stays
        # immutable for nodes themselves.
        object.__setattr__(ctx, "spawned_session_id", session.session_id)

        # Step 6: build the engine context and invoke. Trigger shape
        # follows the agent's declared mode — workflow agent nodes are
        # naturally single-shot (the workflow drives the agent once
        # with bound inputs and consumes structured output); we fall
        # back to the `user_message` shape if the agent declares
        # `conversational` so the engine's preflight doesn't reject.
        threads = await self._session_store.list_threads(session.session_id)
        # `create_session` allocates one default thread, so the list
        # is guaranteed non-empty (an empty result would be a store
        # invariant violation, not a runner concern).
        default_thread_id = threads[0].thread_id

        if resolved.io.mode == "single_shot":
            trigger: dict[str, Any] = {
                "kind": "invocation",
                "inputs": bound_inputs,
            }
        else:
            # Conversational — bound_inputs become the user message
            # content payload. The v1 release doesn't ship a
            # conversational workflow-agent recipe; this branch keeps
            # the engine preflight happy if a starter agent declares
            # the mode.
            trigger = {"kind": "user_message", "content": bound_inputs}

        exec_ctx = ExecutionContext(
            thread_id=default_thread_id,
            trigger=trigger,
            trace_id=ctx.trace_id,
            # Callers only (bug-158): the engine prepends the agent's
            # own resolved ref when it builds the exchange chain.
            caller_chain=tuple(ctx.caller_chain),
            # spec-011 r-review r1: stamp the workflow context onto
            # the sub-agent's ExecutionContext so downstream consumers
            # (spec-014's `InlineApprovalCallback._derive_source`) can
            # tag emitted `kolsch.prompt.requested` events as
            # workflow-scoped. `ctx.workflow_run_id` is a plain str on
            # NodeContext; wrap in the typed `WorkflowRunId` for the
            # engine surface.
            workflow_run_id=WorkflowRunId(ctx.workflow_run_id),
            workflow_node_name=node_name,
        )

        # spec-011: compose the workflow-level cancel token into the
        # sub-agent's cancellation handle. When the executor was
        # constructed with a `cancel_token`, `NodeContext.cancel_token`
        # carries it here; compose with any exchange-scoped token
        # (typically `None` at this point — the engine mints a no-op
        # if unset) via `CombinedCancelToken` so cancel-on-any
        # semantics reach the sub-agent's LLM stream.
        # `ExecutionContext` is a pydantic model with `extra="forbid"`
        # but NOT frozen, so assignment to existing fields works.
        if ctx.cancel_token is not None:
            exec_ctx.cancellation = CombinedCancelToken(
                ctx.cancel_token,
                exec_ctx.cancellation or CancelToken(),
            )

        # Step 7: invoke via `execute_stream` and tee every event
        # through `ctx.on_sub_agent_event_callback` (spec-020) BEFORE
        # collecting the terminal `agent.message`. Unlike `execute()`,
        # `execute_stream()` yields the failed event as the final
        # item rather than raising; we mirror the `execute()` semantics
        # (raise `AgentExecutionError`) by catching the trailing
        # `thread.exchange_failed` event and constructing the same
        # exception the engine's `execute()` would. `AgentExecutionError`
        # is then remapped to `WorkflowNodeFailed` so the executor's
        # existing failure path emits the canonical
        # `workflow.node.failed` + `workflow.run.failed` pair.
        #
        # spec-020: the forwarder call is best-effort — the callback
        # is bound by the executor's `_build_ctx` and swallows raises
        # via `_emit_sub_agent_event`, but we double-guard here with
        # a suppressor so a bad wire on a callback that DIDN'T come
        # from `_build_ctx` (e.g. a monkey-patched test context)
        # can't take down the agent invocation. Nested agents:
        # per spec §Open questions, we forward everything for v1;
        # the inner agent's events flow through the same callback
        # (they share the outer node's `(node_name, invocation_id)`
        # because they inherit the same `NodeContext`).
        forward = ctx.on_sub_agent_event_callback
        agent_message: Any = None
        failed_event: Any = None
        try:
            stream = self._engine.execute_stream(
                resolved,
                exec_ctx,
                inputs=bound_inputs,
            )
            async for event in stream:
                if forward is not None:
                    try:  # noqa: SIM105 — comment documents the deliberate swallow
                        forward(event)
                    except Exception:  # noqa: BLE001, S110 — best-effort tee
                        # The executor-bound callback already swallows
                        # via `_emit_sub_agent_event`. Belt-and-braces
                        # for test-injected callbacks.
                        pass
                if event.type == EventType.AGENT_MESSAGE:
                    agent_message = event
                elif event.type == EventType.THREAD_EXCHANGE_FAILED:
                    failed_event = event
        except AgentExecutionError as exc:
            # An engine that raised (rare — `execute_stream` normally
            # yields the failed event as the last item) still lands
            # here so we can remap. Preserve the original semantic:
            # `AgentExecutionError` → `WorkflowNodeFailed`. Bug-055:
            # thread the child's typed error onto the wrapper so the
            # executor's `workflow.node.failed` emit carries the real
            # cause without operators having to grep the spawned
            # session's JSONL for the `ev_...` id.
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=exc,
                nested_error=_extract_nested_error(exc.failed_event),
            ) from exc

        if failed_event is not None:
            # Mirror `AgentEngine.execute()`'s translation.
            translated = AgentExecutionError(failed_event)
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=translated,
                nested_error=_extract_nested_error(failed_event),
            ) from translated

        # Step 8: extract structured output. `agent.message.data`
        # carries `content: dict[str, Any]`. Missing `agent_message`
        # here indicates an engine invariant violation — the success
        # path always emits it. Mirror `AgentEngine.execute()`'s
        # unreachable-branch handling by raising the same shape.
        if agent_message is None:
            from errors import PlatformInternalError  # noqa: PLC0415

            msg = (
                "AgentRunner: execute_stream completed without an "
                "agent.message or failed event"
            )
            raise PlatformInternalError(
                original_type="workflow.agent_runner.invariant_violation",
                args=[msg],
            )
        content = agent_message.data.get("content") if agent_message.data else None
        return dict(content) if isinstance(content, dict) else {}
