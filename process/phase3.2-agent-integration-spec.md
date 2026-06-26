# Phase 3.2 — Agent Node Integration Spec

**Intro.** This spec covers Phase 3.2 — wiring the `agent` node type into the Phase 3.1 executor so a workflow can invoke a catalogued kolsch agent, consume its structured output, and trace the spawned session back to the workflow run. It builds on Phase 3.0 (parser + validator) and Phase 3.1 (sequential executor: bash + authored Python + noop).

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.2 — the roadmap entry that scopes this phase.
- `process/phase3.0-workflow-spec.md` §4.1.2 — locked agent-node-type design (asset ref, `outputs:` override, identity-override carve-outs).
- `process/phase3.1-executor-spec.md` §8.1, §11.5 — the executor algorithm and cross-JSONL correlation rules this phase plugs into.
- `docs/explanation/workflow-event-log.md` — author-facing event log conventions, including the `spawned_by` payload on `session.opened`.
- `docs/explanation/workflow-foundation.md` §2.4 — the `from_workflow_run` foundational rule.

---

## Glossary

| Term | Meaning |
| --- | --- |
| agent runner | The `NodeRunner` implementation for `kolsch/nodes/agent`. Lives at `src/workflows/builtins/agent.py`. |
| child session | The agent session spawned by an `agent:` node invocation. Carries the `from_workflow_run` pointer back to the spawning workflow. |
| ResolvedAgentConfig | The typed agent config built from the asset catalog entry, fed to `AgentEngine.execute()`. Defined at `src/schemas/config.py:175`. |
| AgentExecutionError | The exception raised by `AgentEngine.execute()` on Fatal failure, wrapping the `thread.exchange_failed` event. Defined at `src/engine/context.py:100`. |
| outputs override | The optional per-invocation `outputs: IOSpec` field on `AgentNodeDef`. Reshapes the workflow's consumption of the agent response without touching agent identity. |
| caller chain | Ordered tuple of refs naming the agent / workflow frames the action's call arrived through. Proximate first. |

---

## Background

Phase 3.1 shipped the **sequential executor** — the worklist, module isolation, cancellation cooperation, event-emission contract, and runtime dispatch for `bash`, `noop`, and authored Python `@node`. The 11 reserved built-in slots without runners (including `agent`) raise `NodeTypeNotImplemented` with the phase number that ships their runtime — for `kolsch/nodes/agent`, that's Phase 3.2 (per `src/workflows/executor.py:76`).

Phase 3.2 is the **smallest possible execution surface** above 3.1 that proves the workflow→agent boundary works: registering an `AgentRunner` against `kolsch/nodes/agent`, threading the workflow's bound inputs into a `ResolvedAgentConfig`, calling `AgentEngine.execute()` (the existing CrewAI-backed entry point at `src/engine/engine.py:188`), extracting the agent's structured output, and emitting the cross-JSONL `from_workflow_run` linkage so the spawned session is traceable back to the workflow invocation.

The verification gate is a two-node workflow (`bash → agent`) that runs end-to-end with the agent producing a Pydantic-validated structured output. The agent session's JSONL carries `spawned_by: {from_workflow_run_id, from_invocation_id, from_workflow_event_id}` on its `session.opened` event; both JSONLs share the same `trace_id`. A second fixture covers the `outputs:` override path. A third covers `AgentExecutionError` mapping to `WorkflowNodeFailed`.

**This spec breaks the phase into 5 sequential steps**, each with its own Coder and Reviewer cycle. The build plan lives at `process/phase3.2-agent-integration-build.md`.

---

## Scope

**In scope (delivered across the 5 steps):**

- New module `src/workflows/builtins/agent.py` — `AgentRunner` implementing `NodeRunner`. Resolves the `agent:` ref via `AssetCatalog`, loads the agent's YAML, builds a `ResolvedAgentConfig`, calls `AgentEngine.execute()`, extracts structured output, returns the captured dict.
- Registration of `AgentRunner` against `kolsch/nodes/agent` in `src/workflows/builtins/__init__.py:register_builtins()` so `NodeTypeNotImplemented(phase="3.2")` stops firing for the agent slot.
- New optional `from_workflow_run: FromWorkflowRun | None` field on the `Session` entity model at `src/schemas/entities.py:30`. The shape is `{workflow_run_id: WorkflowRunId, invocation_id: InvocationId, event_id: EventId}`. Backward-compatible (defaults to `None` for non-workflow-spawned sessions).
- `SessionStore.create_session(...)` gains a new optional `from_workflow_run: FromWorkflowRun | None = None` kwarg that populates the new field AND injects a `spawned_by` payload into the `session.opened` event per `docs/explanation/workflow-event-log.md` line 62.
- Per-invocation `outputs: IOSpec` override — when present on `AgentNodeDef`, the agent runner overrides the agent's declared `IOSpec.outputs` before passing the resolved config to `AgentEngine.execute()`. Both validation paths (agent-declared schema AND workflow-overridden schema) emit `WorkflowNodeFailed` on mismatch.
- Error mapping — `AgentExecutionError` from `AgentEngine.execute()` is caught by `AgentRunner` and re-raised as `WorkflowNodeFailed(node=, invocation_id=, cause=)` so the executor's existing failure path (per Phase 3.1) emits the right `workflow.node.failed` event. The original `thread.exchange_failed` event remains in the agent session's JSONL — cross-JSONL via `trace_id`.
- `trace_id` propagation — the `ExecutionContext` constructed for the agent invocation carries the workflow run's `trace_id`. The spawned session's events inherit it unchanged per master-spec §11.3.
- Caller-chain extension — when the agent runner constructs the synthetic `principal` / `caller_chain` for the agent invocation, the workflow's identity is prepended to the agent's own caller chain so authz audit events captured downstream show the workflow → agent frame correctly.
- `workflow.node.completed` payload extension — for agent nodes specifically, the payload includes the child session ID (`spawned_session_id: SessionId`) so consumers can navigate from the workflow event log directly to the agent thread.
- Golden trace fixtures at `tests/golden/runs/` covering the two-JSONL pair (workflow JSONL + agent session JSONL) for a reference agent invocation.
- Starter workflow demo update — the Phase 3.0/3.1 starter `kolsch/workflows/hello.yaml` gains an agent-using sibling `bundles/kolsch/workflows/hello-agent.yaml` that runs end-to-end on a fresh workspace.

**Out of scope (deferred to later sub-phases per `phase3.x-roadmap.md`):**

- `branch` / `filter` / `compute` runtime (Phase 3.3, 3.6) — even when an agent's output drives a branching decision, the branching node itself is the next-phase runtime; Phase 3.2 ships ONLY the agent leaf.
- `user_input` (Phase 3.4) — an agent node that pauses for user input is composed via the separate `user_input` leaf, which lands in 3.4.
- `read_file` / `write_file` / `list_files` / `set_state` / `delete_state` (Phase 3.5).
- `map` over agents (Phase 3.7 ships the `map` composite; until then, agent fan-out is not expressible).
- Sub-workflow nesting (Phase 3.7).
- `kolsch workflows fork` of an agent-using run (Phase 3.8) — fork replay of captured agent outputs is unrelated; Phase 3.2 only writes the events, not replay logic.
- Chat slash `/workflow` (Phase 3.9).
- Concurrent agent invocation (Phase 3.10).

**Decisions explicitly locked in master spec §4.1.2 (do NOT relitigate):**

- **No agent-identity overrides at any phase.** The `model:`, `prompt:`, `system_prompt:`, `temperature:`, and `max_iterations:` fields belong to the agent asset's YAML, NOT the workflow node's invocation. The `agent:` ref is the ONLY identity hook.
- **The `outputs:` override is exempt.** It reshapes consumption, not identity. Reuses the existing `IOSpec` grammar from `src/schemas/config.py:58`.
- **Context management lives at the agent level.** Workflow nodes do NOT carry `context_strategy:` / `max_tokens:` knobs. Agent-layer context-overflow surfaces as `WorkflowNodeFailed`.
- **Multi-provider fallback lives at the agent level.** Workflow nodes inherit whatever fallback chain the agent asset declares.
- **No branching from the agent node.** Branching off agent results routes through a downstream `branch` node (Phase 3.6).
- **Failure aborts the whole run.** Per the Phase 3.1 abort-on-failure rule. No `on_error: continue|skip|route_to` at v1.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (`JINJA_ENV` singleton at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py:248` — `AgentNodeDef`. Already correctly shaped from Phase 3.0 (`type` / `agent` / `outputs` + `_BaseNodeDef` modifiers). Phase 3.2 does NOT modify the parser; it consumes the already-typed model.
- `src/workflows/executor.py:73` — `BUILTIN_PHASE_MAP` has `"agent": "3.2"`. Phase 3.2's registration removes the `NodeTypeNotImplemented` raise for this slot via `RunnerRegistry.register("kolsch/nodes/agent", AgentRunner(...))`.
- `src/workflows/builtins/__init__.py` — `register_builtins(registry, *, deps)` is the bootstrap path. Phase 3.2 extends it to register the agent runner alongside `bash` and `noop`.
- `src/engine/engine.py:188` — `AgentEngine.execute(config, context, *, inputs)` returns `Event` (type `AGENT_MESSAGE`) on success, raises `AgentExecutionError(failed_event)` on Fatal failure. THE agent invocation entry point. The runner calls it directly.
- `src/engine/context.py:52` — `ExecutionContext` Pydantic model. The runner constructs one carrying the workflow's `trace_id` so the spawned session inherits it.
- `src/engine/context.py:100` — `AgentExecutionError`. Caught by the runner and remapped to `WorkflowNodeFailed`.
- `src/schemas/config.py:58` — `IOSpec` model. Reused verbatim for the per-invocation `outputs:` override.
- `src/schemas/config.py:175` — `ResolvedAgentConfig`. The typed config the runner builds and hands to `AgentEngine.execute()`.
- `src/schemas/entities.py:30` — `Session` model. Phase 3.2 adds the optional `from_workflow_run` field.
- `src/store/session_store.py:109` — `SessionStore.create_session(...)`. Phase 3.2 extends the signature to accept `from_workflow_run=` and to inject `spawned_by` into the `session.opened` event payload.
- `src/assets/catalog.py:74` — `AssetCatalog.lookup(ref, expected_type)`. The runner calls `catalog.lookup(agent_ref, expected_type="agent")` to resolve the agent YAML path.
- `src/workflows/errors.py:1185` — `WorkflowNodeFailed(node=, invocation_id=, cause=)`. The runner wraps `AgentExecutionError` (and any agent-layer Fatal) in this.
- `src/workflows/ids.py:46` — `WorkflowRunId`, `InvocationId`, `TraceId`. Threaded into the `ExecutionContext` and into the `from_workflow_run` pointer.
- `src/workflows/context.py` — `NodeContext` (the per-invocation context the executor hands runners). The agent runner reads `ctx.workflow_run_id`, `ctx.invocation_id`, `ctx.trace_id`, `ctx.caller_chain` from it.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch

# Bootstrap a fresh workspace for smoke testing
uv run kolsch --root /tmp/kolsch-agent-test setup --non-interactive

# Run the test suite
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## §4.1.2 Agent Node Runtime (elaboration)

Master spec §4.1.2 locked the YAML schema. This section locks the **runtime contract** Phase 3.2 ships.

### §4.1.2.1 Input flow

The agent's declared input schema (the `inputs:` half of its `IOSpec`) defines what the workflow MUST provide as the agent's first turn. Phase 3.2's binding rules:

1. The workflow's inbound edges to the agent node carry `binds:` entries. Each bind's RHS is Jinja-rendered against `nodes=captured, state=state, inputs=run.inputs` per Phase 3.1's `_render_bind`.
2. The rendered bind dict is passed verbatim into `AgentEngine.execute(..., inputs=bound_inputs)`. The agent engine validates `bound_inputs` against the agent's declared input `IOSpec` per its existing contract; on mismatch, `AgentExecutionError` is raised → mapped to `WorkflowNodeFailed`.
3. The runner does NOT pre-validate inputs against the agent's schema — that's the agent layer's responsibility. Phase 3.2 forwards what the workflow author bound and lets the agent layer reject misshapes.
4. No automatic shape conversion. If the agent declares `inputs: {ticket: {type: object}}` and the workflow's bind RHS resolves to a string, the agent layer raises and the runner remaps.

### §4.1.2.2 Output flow

The agent invocation returns an `Event` of type `AGENT_MESSAGE` (per `src/engine/engine.py:200`). The runner extracts the structured payload:

1. If the agent declared an `outputs:` `IOSpec` AND the workflow node did NOT override, the agent layer has already validated the response shape — the structured fields land in the `Event.data` payload at a well-defined key. The runner returns those fields as the captured output dict.
2. If the workflow node carries `outputs:` (the override), the runner passes the override into `ResolvedAgentConfig.io.outputs` BEFORE calling `execute()`. The agent layer validates against the override; the runner returns the validated dict.
3. The captured output dict is what the executor stores at `captured[<agent_node_name>]`. Downstream Jinja can reference `{{ nodes.<agent_node_name>.<output_key> }}`.

### §4.1.2.3 The `outputs:` override

Precedence + validation rules:

- **Precedence:** workflow-node `outputs:` (if present and non-empty) > agent's declared `IOSpec.outputs` > implicit `{message: str}` fallback (the agent engine's default when no schema is declared).
- **Validation location:** the agent layer. The runner builds a `ResolvedAgentConfig` with `config.io.outputs = override or agent_yaml.io.outputs` and hands it to `AgentEngine.execute()`. The existing agent-layer validation pipeline (which already knows how to enforce `IOSpec.outputs`) handles both the declared-schema AND override paths identically.
- **Failure mode:** when the agent's response does not satisfy the active output schema, the agent layer raises (currently `ModelRetryExhausted` after retry, per `src/errors/engine.py:65`). This becomes a `THREAD_EXCHANGE_FAILED` event, which becomes `AgentExecutionError`, which the runner catches and re-raises as `WorkflowNodeFailed`.
- **No schema merging.** When override is present, the agent's declared output schema is REPLACED entirely for THIS invocation. No partial merge, no shape compatibility check. The author is responsible for ensuring the override is satisfiable.

### §4.1.2.4 No identity overrides

The runner constructs the `ResolvedAgentConfig` from the agent YAML loaded via `AssetCatalog.lookup(agent_ref, expected_type="agent")` and applies ONLY the `outputs:` override. Specifically:

- `config.model` — UNCHANGED from agent YAML.
- `config.prompt` / `config.system_prompt` — UNCHANGED.
- `config.temperature` / agent-level model params — UNCHANGED.
- `config.max_iterations` — UNCHANGED.
- `config.tools` — UNCHANGED (agent's declared tool list).
- `config.io.outputs` — REPLACED by override when present.
- `config.io.inputs` — UNCHANGED (the workflow MUST satisfy the agent's declared input schema).

Any future workflow-node field that would override identity must be rejected at the parser layer with a clear migration message. Phase 3.0's `AgentNodeDef` uses `extra="forbid"` (inherited from `_BaseNodeDef`), so unknown fields already fail parse — Phase 3.2 does not relax this.

---

## §8.5 Agent runner (executor wiring)

### §8.5.1 Module location and protocol

`src/workflows/builtins/agent.py` ships a single class:

```python
class AgentRunner(NodeRunner):
    def __init__(
        self,
        *,
        catalog: AssetCatalog,
        engine: AgentEngine,
        session_store: SessionStore,
    ) -> None: ...

    async def run(
        self,
        *,
        node_name: str,
        node_def: AgentNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]: ...
```

Dependencies are injected at construction (no module-level singletons). `register_builtins(registry, *, catalog, engine, session_store, ...)` in `src/workflows/builtins/__init__.py` constructs the runner and calls `registry.register("kolsch/nodes/agent", AgentRunner(...))`.

### §8.5.2 Algorithm

The `run` method:

1. **Resolve the agent ref.** `entry = catalog.lookup(node_def.agent, expected_type="agent")`. If `None` or wrong type → raise `WorkflowNodeFailed(cause=AgentRefUnresolved(...))`. This SHOULD be unreachable in practice — Phase 3.0's validator already verifies agent refs at run-start — but defensive: validator drift cannot wedge the runtime.
2. **Load + parse the agent YAML** via the existing asset-management loader. Build the `ResolvedAgentConfig`.
3. **Apply `outputs:` override.** If `node_def.outputs is not None`, replace `config.io = IOSpec(mode=config.io.mode, inputs=config.io.inputs, outputs=node_def.outputs.outputs)`. The mode stays as the agent declared (single_shot vs conversational).
4. **Create the child session.** `session = await session_store.create_session(node_def.agent, name=<derived>, from_workflow_run=FromWorkflowRun(workflow_run_id=ctx.workflow_run_id, invocation_id=ctx.invocation_id, event_id=ctx.parent_event_id))`. The store writes `session.opened` with the `spawned_by` payload (§11.6).
5. **Construct `ExecutionContext`** carrying `thread_id=<session's default thread>`, `trigger=<workflow>`, `cancellation=<from_node_ctx>`, and stamp the `trace_id` per master spec §11.3.
6. **Invoke** `event = await engine.execute(config=resolved, context=exec_ctx, inputs=bound_inputs)`. On `AgentExecutionError as exc`:
   - Raise `WorkflowNodeFailed(node=node_name, invocation_id=str(ctx.invocation_id), cause=exc)`. The executor's existing failure path emits `workflow.node.failed`.
7. **Extract the structured output.** From `event.data` (typed `AgentMessageData`), pull the structured fields validated against the active output schema. Return them as a `dict[str, Any]`. The executor stores at `captured[node_name]` and emits `workflow.node.completed`.
8. **Attach the spawned session ID.** The runner returns the captured dict; the executor's `workflow.node.completed` payload builder reads `ctx.spawned_session_id` (set by the runner on the NodeContext before returning) and adds it to the payload per §11.6.

### §8.5.3 Cancellation

The agent runner honors `cancel.flag` cooperatively at one boundary: BEFORE invoking `AgentEngine.execute()`. Once the agent invocation is in flight, the executor cannot interrupt it — the agent-layer cancellation protocol (its own `ExecutionContext.cancellation`) handles mid-thread cancellation if the operator wires it. Phase 3.2 does NOT wire workflow-cancel into agent-layer cancellation; that integration is deferred.

In practice: if the operator runs `kolsch workflows cancel <run_id>` while an agent node is mid-thread, the workflow waits for the agent to complete (or fail/timeout per the agent's own deadlines), THEN observes `cancel.flag` at the next node boundary and emits `workflow.run.aborted`. The agent's events still land in its session JSONL — they aren't orphaned.

### §8.5.4 Timeouts

The Phase 3.1 `with_timeout` decorator wraps the agent runner uniformly: `node_def.timeout_seconds` gates the entire `run()` invocation. On `asyncio.TimeoutError`, raises `NodeTimeoutExceeded` per the existing contract.

Note: the agent layer may have its OWN per-completion timeouts (the `exchange.timeout` `FATAL` at `src/errors/engine.py:151`). Those fire independently and surface as `AgentExecutionError` → `WorkflowNodeFailed`. The two timeout mechanisms compose naturally:

- If the agent's exchange timeout fires first → `WorkflowNodeFailed(cause=ExchangeTimeout)`.
- If the workflow node's timeout fires first → `NodeTimeoutExceeded`.

Both are terminal per Phase 3.1's abort-on-failure rule.

---

## §11.6 Cross-JSONL: workflow → session linkage

Master spec §11.5 (in `process/phase3.1-executor-spec.md`) established the cross-JSONL pattern. Phase 3.2 LOCKS the concrete payload shapes.

### §11.6.1 `FromWorkflowRun` shape

New value object at `src/schemas/entities.py` (alongside `Session`):

```python
class FromWorkflowRun(BaseModel):
    """Parent pointer on sessions spawned by workflow agent nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_run_id: WorkflowRunId
    invocation_id: InvocationId
    event_id: EventId  # The workflow.node.started event that spawned this session.
```

`Session` gains:

```python
class Session(BaseModel):
    ...  # existing fields unchanged
    from_workflow_run: FromWorkflowRun | None = None
```

Backward-compatible — default `None` means "this session was spawned by chat or another non-workflow surface."

### §11.6.2 `session.opened` payload extension

When `SessionStore.create_session(..., from_workflow_run=fwr)` is called with a non-None pointer, the `session.opened` event's `data` dict gains a `spawned_by` entry per the foundation rule at `docs/explanation/workflow-event-log.md` line 62:

```python
data = {
    "session_id": session_id,
    "initial_agent": {"id": agent_id, "name": agent_id},
    "spawned_by": {
        "from_workflow_run_id": str(fwr.workflow_run_id),
        "from_invocation_id": str(fwr.invocation_id),
        "from_workflow_event_id": str(fwr.event_id),
    },
}
```

When `from_workflow_run=None` (the default), `spawned_by` is omitted from the payload — backward-compatible with existing chat-spawned sessions.

The polymorphic `spawned_by: {from_session_id, from_tool_call_id}` shape mentioned in the foundation doc (agent→agent spawning) is NOT shipped in Phase 3.2 — that's a separate concern.

### §11.6.3 `workflow.node.completed` payload extension

For agent nodes specifically, the executor's `workflow.node.completed` payload gains a `spawned_session_id: str` entry alongside the existing `output: dict`. Consumers (CLI `events` viewer, future chat capsule) can navigate from a workflow event directly to the spawned session's JSONL.

The extension is implemented in the executor's payload builder, gated on `node_def.type == "kolsch/nodes/agent"`. The runner sets the value via a new optional attribute on `NodeContext` (`spawned_session_id: SessionId | None`) that the executor reads after `run()` returns. Other node types leave it `None`; no payload change for them.

### §11.6.4 `trace_id` propagation

The `ExecutionContext` the runner builds carries `trace_id = ctx.trace_id` (the workflow run's trace_id). The agent engine threads this into every event it emits — `session.opened`, all per-completion events, `agent.message`, etc. The two JSONLs share `trace_id`; cross-correlation is exact.

Test verifies: spawn an agent node from a workflow run with `trace_id=tr_abc`; assert every event in BOTH the workflow JSONL and the spawned session JSONL carries `trace_id=tr_abc`.

---

## §13.x Caller chain extension for agent nodes

When the agent runner invokes `AgentEngine.execute()`, the workflow's identity must extend the caller chain so downstream authz audit events (per master spec §13.5 `audit_level`) record the workflow → agent frame correctly.

Phase 3.2 contract:

1. The `NodeContext` the executor hands the runner carries `ctx.caller_chain: tuple[str, ...]` populated per Phase 3.1's contract (proximate first; for a root-run agent node, the tuple is `(workflow_ref,)`).
2. The runner extends with the agent's ref before invoking the engine: `extended = (node_def.agent,) + ctx.caller_chain`.
3. The `ExecutionContext` carries `extended` so the agent layer's tool calls and authz decisions see the full frame stack.
4. The agent's `workflow.node.started` event (for the agent node, in the workflow JSONL) carries `caller_chain = ctx.caller_chain` (the workflow-side frame). The agent session's events carry `caller_chain = extended` (proximate first: agent ref, then workflow ref).

This composes correctly with sub-workflow nesting (Phase 3.7) and chat-spawned agents — each new frame prepends to the chain. The agent's own `agent_policy` rules apply naturally to the agent layer's tool calls; the workflow's policy layer applies to anything the workflow itself authorizes (currently bash; future tool nodes).

The implementation is a one-line tuple prepend in the runner. No new authz primitives.

---

## Pause / Resume

Agent nodes do NOT pause. The agent invocation is synchronous from the workflow's POV (the runner awaits `engine.execute()` to completion). Pause/resume of the workflow is governed by the `user_input` node (Phase 3.4) and the workflow timeout primitive — both are orthogonal to agent invocation.

The agent engine MAY internally have its own pause-resume around tool calls or user_input tools; that's an agent-layer concern. If an agent invocation pauses inside the agent layer, the workflow runner waits indefinitely (subject to `node_def.timeout_seconds` and `Workflow.timeout_seconds`).

---

## CLI Surface

Phase 3.2 adds NO new CLI commands. The Phase 3.1 `kolsch workflows run` command unchanged; an agent-using workflow runs end-to-end via the same surface. `kolsch workflows events <run_id>` displays the workflow JSONL; to view the spawned agent session, the operator follows the `spawned_session_id` payload via the existing `kolsch sessions show <session_id>` command (already extant from Phase 2.x).

A future QoL (not in Phase 3.2 scope) may add `kolsch workflows events <run_id> --include-spawned` to inline-merge the spawned session JSONLs into the workflow view; tracked separately.

---

## Phase-level Acceptance Criteria

- A two-node workflow `bash → agent` runs end-to-end on a fresh workspace. The agent produces a Pydantic-validated structured output; downstream Jinja `{{ nodes.<agent_node>.<key> }}` references resolve correctly.
- The resulting `runs/<wrk_id>/events.jsonl` contains `workflow.node.started`, `workflow.node.completed` for the agent node. The completed payload carries `spawned_session_id`.
- The spawned session's `sessions/<ses_id>/events.jsonl` carries `session.opened` with `spawned_by: {from_workflow_run_id, from_invocation_id, from_workflow_event_id}` matching the workflow side.
- Both JSONLs share the same `trace_id` on every event.
- The `outputs:` override path: a workflow node with a custom `outputs:` IOSpec gets its custom schema enforced; downstream Jinja consumes the overridden keys.
- Agent failure (max iterations, tool failure, output-schema mismatch) maps to `WorkflowNodeFailed` with `cause=AgentExecutionError`. The workflow JSONL ends with `workflow.node.failed` then `workflow.run.failed`; the agent session JSONL ends with `thread.exchange_failed` (already extant).
- Per-node `timeout_seconds` fires `NodeTimeoutExceeded` if the agent invocation exceeds it.
- `Session.from_workflow_run` is `None` for chat-spawned sessions (backward-compat preserved).
- `register_builtins(...)` registers the agent runner; `RunnerRegistry.get("kolsch/nodes/agent")` returns the runner, NOT `NodeTypeNotImplemented`.
- The starter `kolsch/workflows/hello-agent.yaml` runs end-to-end via `kolsch workflows run kolsch/workflows/hello-agent`.
- No regressions: full suite still green.

---

## Phase-level Integration Tests

### Phase Test 1: End-to-end agent invocation
- **Run:** `uv run kolsch --root /tmp/kolsch-agent-test workflows run kolsch/workflows/hello-agent`
- **Expect:** exit 0; final outputs JSON printed to stdout; `runs/<wrk_id>/events.jsonl` contains the agent node's `started`/`completed` pair; the spawned session JSONL exists at `sessions/<ses_id>/events.jsonl` with `session.opened` carrying `spawned_by`; both JSONLs share `trace_id`.
- **Fail means:** agent runner wiring broken OR `from_workflow_run` plumbing broken.

### Phase Test 2: Outputs override
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_outputs_override -v`
- **Expect:** test fixtures a workflow whose agent node declares `outputs: {classification: str, confidence: float}`; the agent (configured to emit a different default schema) produces the overridden shape; downstream consumption succeeds.
- **Fail means:** override precedence / `ResolvedAgentConfig` patching broken.

### Phase Test 3: Failure mapping
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_agent_failure_maps_to_node_failed -v`
- **Expect:** test fixtures an agent configured to exhaust retries; the runner catches `AgentExecutionError` and raises `WorkflowNodeFailed`; the workflow JSONL ends with `workflow.node.failed` carrying `cause` info; the agent JSONL ends with `thread.exchange_failed`.
- **Fail means:** exception remap missing OR event ordering violated.

### Phase Test 4: Trace_id propagation
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_trace_id_propagates_to_spawned_session -v`
- **Expect:** both JSONLs carry the same `trace_id` on every event.
- **Fail means:** `ExecutionContext` not stamped with workflow's trace_id.

### Phase Test 5: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green. Specifically: existing chat-spawned-session tests must still pass (Session's `from_workflow_run` defaults to `None`).

---
