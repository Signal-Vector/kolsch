"""``kolsch/nodes/agent`` runner — Phase 3.2.

Master spec §8.5. The runner is the workflow→agent boundary: catalog
lookup of the ``agent:`` ref, ``ResolvedAgentConfig`` build via the
existing chat-side loader, optional per-invocation ``outputs:``
override, child-session creation carrying the :class:`FromWorkflowRun`
parent pointer, and synchronous invocation of
:meth:`engine.AgentEngine.execute`.

Step 1 shipped the SKELETON (catalog → builder → session) behind a
stubbed ``engine_call`` callable. Step 2 replaces the seam with a real
:class:`engine.AgentEngine` injection, constructs the
:class:`engine.ExecutionContext` carrying the workflow's ``trace_id``
and an extended caller chain (agent ref prepended), and remaps
:class:`engine.AgentExecutionError` (the engine's Fatal failure
transport) to :class:`workflows.errors.WorkflowNodeFailed` so the
executor's existing failure path emits ``workflow.node.failed`` +
``workflow.run.failed`` per Phase 3.1.

Step 3 wires registration into ``register_builtins(...)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine import ExecutionContext
from engine.context import AgentExecutionError
from schemas import FromWorkflowRun, IOSpec
from workflows.errors import AgentRefUnresolved, WorkflowNodeFailed

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
    """Runtime for ``kolsch/nodes/agent`` — master spec §8.5."""

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
        """Algorithm — master spec §8.5.2.

        Step-by-step:

        1. Defensive catalog lookup. The Phase 3.0 validator already
           catches missing refs; this guard protects against drift
           (asset deleted between validate and run).
        2. Build the typed :class:`ResolvedAgentConfig` via the
           injected loader (chat surface's path).
        3. If ``node_def.outputs`` is set, replace ``config.io.outputs``
           with the override schema. Identity fields and inputs are
           UNCHANGED — §4.1.2.3 / §4.1.2.4 locked decisions.
        4. Mint the :class:`FromWorkflowRun` parent pointer from
           ``ctx`` (workflow_run_id + invocation_id + parent_event_id).
        5. Create the child session with the pointer; stamp
           ``ctx.spawned_session_id`` so the executor's payload
           builder (Step 3) can read it.
        6. Build the :class:`ExecutionContext` carrying the workflow's
           ``trace_id`` (master spec §11.6.4) and the extended caller
           chain (agent ref prepended, master spec §13.x). Invoke
           :meth:`AgentEngine.execute` synchronously.
        7. Catch :class:`AgentExecutionError`; remap to
           :class:`WorkflowNodeFailed(cause=)`. The executor's existing
           failure path emits ``workflow.node.failed``.
        8. Extract the structured output from the returned
           ``agent.message`` event's ``data["content"]``.
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

        # Step 3: optional ``outputs:`` override — replace ONLY the
        # ``outputs`` slice of ``config.io``. Identity fields (model,
        # prompt, system_prompt, temperature, max_iterations, tools)
        # and ``config.io.inputs`` are untouched per §4.1.2.4.
        if node_def.outputs is not None:
            patched_io = IOSpec(
                mode=resolved.io.mode,
                inputs=resolved.io.inputs,
                outputs=node_def.outputs.outputs,
            )
            resolved = resolved.model_copy(update={"io": patched_io})

        # Step 4: mint the parent pointer.
        fwr = FromWorkflowRun(
            workflow_run_id=ctx.workflow_run_id,
            invocation_id=ctx.invocation_id,
            event_id=ctx.parent_event_id,
        )

        # Step 5: spawn the child session. The store keys sessions by
        # the resolved ``agent_id`` (prefixed ``ag_...``); the catalog
        # ``ref`` is the 3-segment user-facing handle and would fail
        # the ``AgentId`` shape check. Deterministic ``name=`` so
        # spawned sessions are easy to spot in ``kolsch sessions list``
        # — Step 1 Reviewer AC for AC 1.5.
        session = await self._session_store.create_session(
            str(resolved.agent_id),
            name=f"{ctx.workflow_run_id}::{node_name}",
            from_workflow_run=fwr,
        )
        # ``NodeContext`` is frozen to keep authored nodes from
        # mutating it; the runner sets the engine-side outbound field
        # via ``object.__setattr__`` so the public surface stays
        # immutable for nodes themselves.
        object.__setattr__(ctx, "spawned_session_id", session.session_id)

        # Step 6: build the engine context and invoke. Trigger shape
        # follows the agent's declared mode — workflow agent nodes are
        # naturally single-shot (the workflow drives the agent once
        # with bound inputs and consumes structured output); we fall
        # back to the ``user_message`` shape if the agent declares
        # ``conversational`` so the engine's preflight doesn't reject.
        threads = await self._session_store.list_threads(session.session_id)
        # Phase 1 convention: ``create_session`` allocates one default
        # thread, so the list is guaranteed non-empty (an empty result
        # would be a store invariant violation, not a runner concern).
        default_thread_id = threads[0].thread_id

        if resolved.io.mode == "single_shot":
            trigger: dict[str, Any] = {
                "kind": "invocation",
                "inputs": bound_inputs,
            }
        else:
            # Conversational — bound_inputs become the user message
            # content payload. Phase 3.2 v1 doesn't ship a conversational
            # workflow-agent recipe; this branch keeps the engine
            # preflight happy if a starter agent declares the mode.
            trigger = {"kind": "user_message", "content": bound_inputs}

        exec_ctx = ExecutionContext(
            thread_id=default_thread_id,
            trigger=trigger,
            trace_id=ctx.trace_id,
            caller_chain=(node_def.agent, *ctx.caller_chain),
        )

        # Step 7: invoke + error remap. The engine's only documented
        # surface failure is :class:`AgentExecutionError` per
        # agent-engine-contract §1.6; we remap to
        # :class:`WorkflowNodeFailed` so the executor's existing
        # failure path emits the canonical ``workflow.node.failed`` +
        # ``workflow.run.failed`` pair. The ``raise ... from exc``
        # form preserves ``__cause__`` for the error envelope.
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

        # Step 8: extract structured output. ``agent.message.data``
        # carries ``content: dict[str, Any]`` per §B.8.
        content = event.data.get("content") if event.data else None
        return dict(content) if isinstance(content, dict) else {}
