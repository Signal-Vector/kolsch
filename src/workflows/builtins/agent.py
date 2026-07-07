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
           [`AgentEngine.execute`][engine.engine.AgentEngine.execute]
           synchronously.
        7. Catch
           [`AgentExecutionError`][engine.context.AgentExecutionError];
           remap to
           [`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed]
           so the executor's existing failure path emits
           `workflow.node.failed`.
        8. Extract the structured output from the returned
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
            caller_chain=(node_def.agent, *ctx.caller_chain),
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

        # Step 7: invoke + error remap. The engine's only documented
        # surface failure is `AgentExecutionError`; we remap to
        # `WorkflowNodeFailed` so the executor's existing failure
        # path emits the canonical `workflow.node.failed` +
        # `workflow.run.failed` pair. The `raise ... from exc` form
        # preserves `__cause__` for the error envelope.
        try:
            event = await self._engine.execute(
                resolved,
                exec_ctx,
                inputs=bound_inputs,
            )
        except AgentExecutionError as exc:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=exc,
            ) from exc

        # Step 8: extract structured output. `agent.message.data`
        # carries `content: dict[str, Any]`.
        content = event.data.get("content") if event.data else None
        return dict(content) if isinstance(content, dict) else {}
