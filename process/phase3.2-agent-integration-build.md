# [SPEC-003] Phase 3.2 — Agent node integration

**Type:** spec
**Doc #:** 003
**Status:** Draft
**Created:** 2026-06-26

---

## Glossary

| Term | Meaning |
| --- | --- |
| agent runner | The `NodeRunner` implementation for `kolsch/nodes/agent`. Lives at `src/workflows/builtins/agent.py`. |
| child session | The agent session spawned by an agent-node invocation. Carries the `from_workflow_run` pointer. |
| FromWorkflowRun | The Pydantic value object on `Session` recording the spawning workflow run + invocation + event. |
| outputs override | The optional per-invocation `outputs: IOSpec` field on `AgentNodeDef`. Reshapes consumption; does not touch agent identity. |
| ResolvedAgentConfig | The typed agent config built from the asset catalog entry. Defined at `src/schemas/config.py:175`. |
| AgentExecutionError | The exception `AgentEngine.execute()` raises on Fatal failure. Wraps the `thread.exchange_failed` event. |

---

## Background

Phase 3.0 shipped the parser/validator/CLI. Phase 3.1 shipped the sequential executor — worklist, module isolation, cancellation, event emission, and runtime dispatch for `bash`, `noop`, and authored Python. Eleven reserved built-in slots still raise `NodeTypeNotImplemented`; `kolsch/nodes/agent` is one of them, marked `"3.2"` in `src/workflows/executor.py:76`.

Phase 3.2 ships the **smallest possible execution surface** that proves the workflow → agent boundary: an `AgentRunner` registered against `kolsch/nodes/agent`, wired through the existing CrewAI-backed `AgentEngine.execute()` entry point at `src/engine/engine.py:188`. The verification gate is a two-node `bash → agent` workflow running end-to-end with structured output captured, cross-JSONL `from_workflow_run` linkage emitted, and `trace_id` propagated unchanged into the spawned session.

The phase ALSO adds the session-side parent pointer (`Session.from_workflow_run`) — a small backward-compatible schema extension that makes workflow-spawned sessions traceable.

**This spec breaks the phase into 5 sequential steps (Step 0 — Step 4)**, each with its own Coder and Reviewer cycle. Each step is independently shippable. The Coordinator drives the multi-step flow.

Related docs:

- `process/phase3.2-agent-integration-spec.md` — the master design spec (§4.1.2 elaboration, §8.5 runner contract, §11.6 cross-JSONL).
- `process/phase3.1-executor-build.md` — the predecessor build plan, particularly Steps 1, 2, 5 (this phase consumes what they shipped).
- `process/phase3.0-workflow-spec.md` — master Phase 3.0 design (§4.1.2 agent node-type LOCKED decisions).
- `docs/explanation/workflow-event-log.md` — author-facing event log conventions, including `spawned_by` polymorphism.

---

## Scope

**In scope (delivered across the 5 steps):**

- New value object `FromWorkflowRun` in `src/schemas/entities.py` carrying `{workflow_run_id, invocation_id, event_id}`.
- Optional `Session.from_workflow_run: FromWorkflowRun | None = None` field (backward-compatible).
- `SessionStore.create_session(..., from_workflow_run=None)` kwarg; injects `spawned_by` into the `session.opened` event payload per `docs/explanation/workflow-event-log.md` line 62.
- New module `src/workflows/builtins/agent.py` — `AgentRunner` implementing `NodeRunner`. Resolves agent ref via `AssetCatalog`, loads the agent YAML, builds `ResolvedAgentConfig`, applies the optional `outputs:` override, creates the child session with parent pointer, invokes `AgentEngine.execute()`, extracts structured output.
- Registration of `AgentRunner` against `kolsch/nodes/agent` in `src/workflows/builtins/__init__.py:register_builtins()`. `NodeTypeNotImplemented(phase="3.2")` stops firing for this slot.
- `ExecutionContext` constructed by the runner carries the workflow's `trace_id` so the spawned session's events inherit it unchanged.
- Caller-chain extension — the agent ref is prepended to the workflow's `ctx.caller_chain` before being passed into the engine.
- `AgentExecutionError` caught by the runner and re-raised as `WorkflowNodeFailed(node=, invocation_id=, cause=)`. The executor's existing failure path emits `workflow.node.failed`.
- `workflow.node.completed` payload extension for agent nodes — adds `spawned_session_id: str` so consumers can navigate to the spawned session's JSONL. Implemented as a new optional `NodeContext.spawned_session_id` attribute the runner sets before returning.
- Golden trace fixtures at `tests/golden/runs/` covering the workflow JSONL + spawned session JSONL pair.
- Starter workflow `bundles/kolsch/workflows/hello-agent.yaml` runs end-to-end via `kolsch workflows run kolsch/workflows/hello-agent`.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- `branch` / `filter` / `compute` runtime (Phase 3.3 / 3.6).
- `user_input` runtime (Phase 3.4).
- `read_file` / `write_file` / `list_files` / `set_state` / `delete_state` runtime (Phase 3.5).
- `map` / `workflow` (sub-workflow) runtime (Phase 3.7).
- Workflow-cancel-mid-agent integration with the agent layer's cancellation protocol (deferred; documented as a known cooperative limit in the master spec §8.5.3).
- `--include-spawned` CLI flag for inlining spawned JSONLs into the workflow event view (tracked as future QoL).
- Multi-agent fan-out via `map` (Phase 3.7).
- Concurrent agent invocation (Phase 3.10).

For every deferred runtime node type, the Phase 3.1 contract still applies: the executor dispatches to `NodeTypeNotImplemented` with the phase number that ships the runtime.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (`JINJA_ENV` at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py:248` — `AgentNodeDef`. Already correctly shaped (Phase 3.0). Phase 3.2 does NOT modify the parser.
- `src/workflows/executor.py:73` — `BUILTIN_PHASE_MAP[\"agent\"] == \"3.2\"`. Phase 3.2's registration shadows the not-implemented raise.
- `src/workflows/builtins/__init__.py` — `register_builtins(...)` is the bootstrap path. Extended in Step 3.
- `src/workflows/context.py` — `NodeContext`. Extended with optional `spawned_session_id` attribute in Step 2 / 3.
- `src/engine/engine.py:188` — `AgentEngine.execute(config, context, *, inputs)`. The agent invocation entry point.
- `src/engine/context.py:52` — `ExecutionContext`. Built by the runner.
- `src/engine/context.py:100` — `AgentExecutionError`. Caught by the runner.
- `src/schemas/config.py:58` — `IOSpec` Pydantic model. Reused verbatim for the override.
- `src/schemas/config.py:175` — `ResolvedAgentConfig`. The typed config the runner builds.
- `src/schemas/entities.py:30` — `Session` model. Extended in Step 0.
- `src/store/session_store.py:109` — `SessionStore.create_session(...)`. Signature extended in Step 0.
- `src/assets/catalog.py:74` — `AssetCatalog.lookup(ref, expected_type)`. Used by the runner.
- `src/workflows/errors.py:1185` — `WorkflowNodeFailed`. Raised by the runner on agent failure.
- `src/workflows/ids.py:46` — `WorkflowRunId`, `InvocationId`, `TraceId`.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch

# Bootstrap a fresh workspace for smoke testing
uv run kolsch --root /tmp/kolsch-agent-test setup --non-interactive

# Run the test suite
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

**Decisions already locked in master spec §4.1.2 + §8.5 + §11.6 (do NOT relitigate):**

- **No agent-identity overrides.** `model:` / `prompt:` / `system_prompt:` / `temperature:` / `max_iterations:` are agent-asset YAML concerns. The workflow node carries only `agent:` + optional `outputs:` + common modifiers (`description:` / `timeout_seconds:` / `when:` / `retry:` / `secrets:`).
- **The `outputs:` override is exempt.** Reshapes consumption; replaces the agent's declared output schema for THIS invocation only. No partial merging.
- **Agent failure → `WorkflowNodeFailed`.** `AgentExecutionError` is the only failure surface the runner expects from `AgentEngine.execute()`. Anything else is an invariant violation that propagates.
- **`trace_id` inherited unchanged.** Workflow's `trace_id` threads into the spawned session via `ExecutionContext`.
- **Caller chain prepends.** Agent ref prepended to workflow's `caller_chain` before the engine call.
- **No branching from agent nodes.** Downstream branching routes through a separate `branch` node (Phase 3.6).
- **`spawned_by` on `session.opened`.** The `{from_workflow_run_id, from_invocation_id, from_workflow_event_id}` payload SHIPS in Phase 3.2; the polymorphic agent→agent shape (`{from_session_id, from_tool_call_id}`) is a separate concern.

---

## Phase-level Acceptance Criteria

- A two-node `bash → agent` workflow runs end-to-end on a fresh workspace. Agent produces a Pydantic-validated structured output; downstream Jinja `{{ nodes.<agent_node>.<key> }}` references resolve.
- The workflow JSONL `runs/<wrk_id>/events.jsonl` contains `workflow.node.started` + `workflow.node.completed` for the agent node. The completed payload carries `spawned_session_id`.
- The spawned session's JSONL `sessions/<ses_id>/events.jsonl` has `session.opened` carrying `spawned_by: {from_workflow_run_id, from_invocation_id, from_workflow_event_id}` matching the workflow side.
- Both JSONLs share the same `trace_id` on every event.
- The `outputs:` override path enforces the workflow-side schema; downstream consumes overridden keys.
- Agent failure (max iterations, tool failure, output-schema mismatch) maps to `WorkflowNodeFailed` with `cause` set. Workflow JSONL ends with `workflow.node.failed` then `workflow.run.failed`; agent session JSONL ends with `thread.exchange_failed`.
- Per-node `timeout_seconds` fires `NodeTimeoutExceeded` if the agent invocation exceeds it.
- `Session.from_workflow_run` is `None` for chat-spawned sessions (backward-compat preserved).
- `register_builtins(...)` registers the agent runner; `RunnerRegistry.get("kolsch/nodes/agent")` returns the runner, NOT `NodeTypeNotImplemented`.
- The starter `bundles/kolsch/workflows/hello-agent.yaml` runs end-to-end via `kolsch workflows run kolsch/workflows/hello-agent`.
- No regressions: full suite still green.

---

## Phase-level Integration Tests

### Phase Test 1: End-to-end agent invocation
- **Run:** `uv run kolsch --root /tmp/kolsch-agent-test workflows run kolsch/workflows/hello-agent`
- **Expect:** exit 0; final outputs JSON printed to stdout; workflow JSONL contains agent node's `started`/`completed` pair; spawned session JSONL exists with `session.opened` carrying `spawned_by`; both JSONLs share `trace_id`.
- **Fail means:** runner wiring broken OR `from_workflow_run` plumbing broken.

### Phase Test 2: Outputs override
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_outputs_override -v`
- **Expect:** test fixtures an override schema; the (fake-LLM-backed) agent produces the overridden shape; the captured dict matches.
- **Fail means:** override precedence broken OR `ResolvedAgentConfig.io` patch wrong.

### Phase Test 3: Failure mapping
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_agent_failure_maps_to_node_failed -v`
- **Expect:** an agent configured to fail produces `WorkflowNodeFailed`; workflow JSONL ends with `workflow.node.failed` then `workflow.run.failed`; agent session JSONL ends with `thread.exchange_failed`.
- **Fail means:** exception remap missing OR event ordering violated.

### Phase Test 4: Trace_id propagation
- **Run:** `uv run pytest tests/component/test_workflow_agent_runner.py::test_trace_id_propagates_to_spawned_session -v`
- **Expect:** both JSONLs carry the same `trace_id` on every event.
- **Fail means:** `ExecutionContext` not stamped with workflow's trace_id.

### Phase Test 5: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green. Specifically: existing chat-spawned-session tests must still pass (`Session.from_workflow_run` defaults to `None`).

---

## Coordinator Prompt

> You are the strategic decision-maker for this task. You do not write or review code.
> Your job is to drive the 5-step Coder/Reviewer flow, make calls when there is ambiguity
> or disagreement, and unblock the work when it stalls.

**Your task:**

Ship Phase 3.2 of the kolsch workflow system through 5 sequential Coder/Reviewer cycles. This is the first **agent-integrating** sub-phase. Its purpose is wiring the workflow→agent boundary correctly: cross-JSONL parent pointer, trace_id propagation, caller-chain extension, error remap. The agent layer itself is untouched.

**Authoritative references:**

- THIS spec: `process/phase3.2-agent-integration-build.md` — the multi-step plan you're driving.
- Master design spec: `process/phase3.2-agent-integration-spec.md` — §4.1.2 elaboration, §8.5 runner, §11.6 cross-JSONL.
- Phase 3.1 build plan: `process/phase3.1-executor-build.md` — for the executor / runner contracts this phase consumes.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` — §4.1.2 locked decisions.

**The 5 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `FromWorkflowRun` value object + `Session.from_workflow_run` field + `SessionStore.create_session(..., from_workflow_run=)` kwarg + `spawned_by` payload injection into `session.opened`. Pure schema + store plumbing. | ~1 day |
| **Step 1** | `AgentRunner` skeleton — catalog lookup + agent YAML load + `ResolvedAgentConfig` assembly + `outputs:` override. NO engine call yet — tests inject a fake engine. NodeContext extension (`spawned_session_id` attribute). | ~1.5 days |
| **Step 2** | Engine invocation + error mapping — wire `AgentEngine.execute()` via constructed `ExecutionContext` carrying `trace_id` + extended caller chain; catch `AgentExecutionError`; re-raise as `WorkflowNodeFailed`; extract structured output. | ~1.5 days |
| **Step 3** | `register_builtins(...)` wiring + executor payload extension (`workflow.node.completed` carries `spawned_session_id` for agent nodes) + caller-chain prepend at the engine boundary. | ~1 day |
| **Step 4** | Golden trace fixtures at `tests/golden/runs/` (workflow JSONL + spawned session JSONL pair) + starter `kolsch/workflows/hello-agent.yaml` runs end-to-end. Phase Tests 1-4 + regression. | ~1.5 days |

**Flow per step:**

1. Hand the Coder the section titled **"Step N — Coder Prompt"** from this spec verbatim. Include the spec file path. Coder reads it + the master spec sections it references.
2. Coder implements + replies "Step N Complete — [summary]", "Blocked — ...", or "Decision needed — ...".
3. On Complete: hand the Reviewer the section titled **"Step N — Reviewer Prompt"** verbatim. Reviewer verifies + replies "Step N Approved" or "Changes requested — ...".
4. On Approved: advance to Step N+1. On Changes requested: send the change list back to the Coder.
5. After Step 4 is Approved: run the **Phase-level Integration Tests** above. If all pass, mark this spec Status: Done and annotate `phase3.x-roadmap.md`'s Phase 3.2 line with shipping commit SHA.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision (identity overrides, branching from agents, override schema merging) → REJECT, cite §4.1.2, restate the decision.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append the call as a note to THIS spec, re-brief.
- Coder discovers Phase 3.0 / 3.1 contract was wrong and needs amendment → flag as a separate patch commit on this branch; do NOT widen Phase 3.2's scope.
- Scope creep (Phase 3.3+ node types sneaking in, agent-layer changes, fork hooks) → REJECT and pull back to listed scope.

**Done when:** All 5 steps Approved by Reviewer AND all 5 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.2 line annotated with the shipping SHA.

---

# STEP 0 — Session-side parent-pointer plumbing (PREREQ)

**Goal**: ship the `FromWorkflowRun` value object, the `Session.from_workflow_run` field, the extended `SessionStore.create_session(...)` signature, and the `spawned_by` payload injection into the `session.opened` event. Pure schema + store plumbing — no executor or runner wiring yet. This is the structural prereq for Steps 1-4.

**Files created/modified:**

- `src/schemas/entities.py` (modified) — add `FromWorkflowRun` Pydantic model; add optional `from_workflow_run` field to `Session`.
- `src/store/session_store.py` (modified) — extend `create_session(...)` signature with `from_workflow_run: FromWorkflowRun | None = None`; populate field on the model; inject `spawned_by` into the `session.opened` event payload when non-None.
- `tests/component/test_session_store_workflow_pointer.py` (new) — round-trip tests for the new field + payload injection.

**Effort**: ~1 day.

## Step 0 — Coder Prompt

> You are implementing Step 0 of a 5-step phase. This is the PREREQ that gates Steps 1-4.
> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the session-side parent-pointer plumbing — `FromWorkflowRun` value object, optional `Session.from_workflow_run` field, `SessionStore.create_session(..., from_workflow_run=)` kwarg, and the `spawned_by` payload injection into the `session.opened` event. NO runner wiring at this step — just the data model and store surface.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.2-agent-integration-build.md` — THIS spec. Step 0.
2. `process/phase3.2-agent-integration-spec.md` — §11.6.1 (FromWorkflowRun shape), §11.6.2 (session.opened payload extension).
3. `docs/explanation/workflow-event-log.md` line 62 — the canonical `spawned_by` payload shape.
4. `src/schemas/entities.py` — the existing `Session` model; mirror its `ConfigDict` style.
5. `src/store/session_store.py:109` — the existing `create_session(...)` for the signature to extend.

**What to implement:**

1. **`src/schemas/entities.py`** — add `FromWorkflowRun`:

   ```python
   class FromWorkflowRun(BaseModel):
       """Parent pointer on sessions spawned by workflow agent nodes.

       Populated by ``AgentRunner`` in Phase 3.2; surfaces in the
       ``session.opened`` event's ``spawned_by`` payload per
       ``docs/explanation/workflow-event-log.md`` line 62.
       """

       model_config = ConfigDict(frozen=True, extra="forbid")

       workflow_run_id: WorkflowRunId
       invocation_id: InvocationId
       event_id: EventId  # workflow.node.started event that spawned this session
   ```

   Then extend `Session`:

   ```python
   class Session(BaseModel):
       ...  # existing fields unchanged
       from_workflow_run: FromWorkflowRun | None = None
   ```

   Imports: `WorkflowRunId`, `InvocationId` from `src/workflows/ids.py`; `EventId` from existing location.

2. **`src/store/session_store.py`** — extend `create_session(...)`:

   ```python
   async def create_session(
       self,
       initial_agent_id: str,
       *,
       name: str | None = None,
       from_workflow_run: FromWorkflowRun | None = None,
   ) -> Session: ...
   ```

   Changes:
   - Pass `from_workflow_run=from_workflow_run` into the `Session(...)` constructor.
   - When `from_workflow_run is not None`, inject `spawned_by` into the `session.opened` event's `data` dict:

     ```python
     if from_workflow_run is not None:
         data["spawned_by"] = {
             "from_workflow_run_id": str(from_workflow_run.workflow_run_id),
             "from_invocation_id": str(from_workflow_run.invocation_id),
             "from_workflow_event_id": str(from_workflow_run.event_id),
         }
     ```

   - When `from_workflow_run is None`, `data` is UNCHANGED (no `spawned_by` key) — backward-compatible with every existing call site.

3. **Migration check** — grep call sites of `create_session` across the codebase. There should be ZERO existing callers passing `from_workflow_run=` (Phase 3.2 introduces it). All existing callers pass only `initial_agent_id` and `name` — they continue to work unchanged.

4. **`tests/component/test_session_store_workflow_pointer.py`**:
   - Without pointer: `await store.create_session("ag_foo", name="test")`; assert `session.from_workflow_run is None`; assert the `session.opened` event's `data` dict has NO `spawned_by` key.
   - With pointer: construct a `FromWorkflowRun(workflow_run_id=..., invocation_id=..., event_id=...)`; `await store.create_session("ag_foo", name="test", from_workflow_run=fwr)`; assert `session.from_workflow_run == fwr`; assert the event's `data["spawned_by"]` matches the expected shape with string-coerced IDs.
   - Round-trip the `Session` through `get_session(session_id)`; assert the field survives.

**Step 0 Acceptance Criteria:**

0.1. `FromWorkflowRun` Pydantic model exists; frozen; carries `workflow_run_id` / `invocation_id` / `event_id`.
0.2. `Session.from_workflow_run: FromWorkflowRun | None = None` field added; default preserves backward compatibility.
0.3. `SessionStore.create_session(...)` accepts an optional `from_workflow_run=` kwarg.
0.4. When `from_workflow_run` is set, the `session.opened` event's `data` carries `spawned_by` with the documented shape; IDs are str-coerced.
0.5. When `from_workflow_run` is None (default), the event payload is UNCHANGED from the prior shape.
0.6. Round-trip via `get_session(...)` returns the field.
0.7. All existing `create_session` call sites continue to work unchanged.
0.8. No regressions: existing chat / session tests pass.

**Step 0 Tests:**

- **Run:** `uv run pytest tests/component/test_session_store_workflow_pointer.py -v`
- **Expect:** all tests pass.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] `grep -rn create_session src/ tests/` — every existing call site verified compatible
- [ ] Existing session tests pass
- [ ] No regressions: `uv run pytest tests/ -q` green

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.5 — backward compatibility. Grep ALL existing `create_session` call sites; confirm none break. The `session.opened` event payload for chat-spawned sessions must be byte-identical to what shipped pre-3.2.
- AC 0.4 — the `spawned_by` shape exactly matches `docs/explanation/workflow-event-log.md` line 62 (`from_workflow_run_id`, `from_invocation_id`, `from_workflow_event_id` — note the `from_workflow_event_id` naming, NOT `event_id`).
- No runner / executor wiring shipped at this step. Grep `src/workflows/` and `src/store/session_store.py` — neither should import the other.

---

# STEP 1 — AgentRunner skeleton (no engine call yet)

**Goal**: ship the `AgentRunner` class with catalog lookup, agent YAML load, `ResolvedAgentConfig` assembly, and `outputs:` override application. NO `AgentEngine.execute()` call yet — Step 2 wires that in. Tests inject a fake engine to verify the assembly path.

**Files created/modified:**

- `src/workflows/builtins/agent.py` (new) — `AgentRunner` class with the skeleton.
- `src/workflows/context.py` (modified) — add optional `spawned_session_id: SessionId | None` attribute on `NodeContext`.
- `tests/component/test_workflow_agent_runner.py` (new) — assembly path tests via injected fake engine.

**Effort**: ~1.5 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Build the `AgentRunner` skeleton. Catalog lookup of the agent ref; agent YAML load; `ResolvedAgentConfig` assembly; `outputs:` override application; child-session creation with `FromWorkflowRun` pointer. Step 2 wires the real `AgentEngine.execute()` call; Step 1 STUBS it via an injected callable so the assembly path can be tested in isolation.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.2-agent-integration-build.md` — Step 1.
2. `process/phase3.2-agent-integration-spec.md` — §4.1.2.3 (`outputs:` override rules), §4.1.2.4 (no identity overrides), §8.5.1 (module location + protocol), §8.5.2 (algorithm).
3. `src/workflows/executor.py:101` — `NodeRunner` protocol. The runner satisfies this.
4. `src/workflows/parser.py:248` — `AgentNodeDef`. The runner consumes the typed model.
5. `src/schemas/config.py:175` — `ResolvedAgentConfig`. The runner builds it.
6. `src/assets/catalog.py:74` — `AssetCatalog.lookup(...)`. The runner calls it.
7. `src/store/session_store.py:109` — `SessionStore.create_session(...)` (extended in Step 0). The runner invokes it with the parent pointer.

**What to implement:**

1. **`NodeContext` extension** in `src/workflows/context.py`:

   ```python
   @dataclass(slots=True)
   class NodeContext:
       ...  # existing fields unchanged
       spawned_session_id: SessionId | None = None  # set by AgentRunner; read by executor's payload builder
   ```

   The attribute is `None` for every node type EXCEPT `agent`. The executor's `workflow.node.completed` payload builder (Step 3) reads it.

2. **`src/workflows/builtins/agent.py`** — `AgentRunner` class:

   ```python
   class AgentRunner:
       """Runtime for ``kolsch/nodes/agent`` — Phase 3.2 §8.5."""

       def __init__(
           self,
           *,
           catalog: AssetCatalog,
           session_store: SessionStore,
           engine_call: Callable[..., Awaitable[Event]],  # Step 2 wires AgentEngine.execute
       ) -> None:
           self._catalog = catalog
           self._session_store = session_store
           self._engine_call = engine_call

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

   `run(...)` algorithm (master spec §8.5.2):
   - Step 1.1: `entry = self._catalog.lookup(node_def.agent, expected_type="agent")`. If `None`: raise `WorkflowNodeFailed(node=node_name, invocation_id=str(ctx.invocation_id), cause=AgentRefUnresolved(ref=node_def.agent))`. (Defensive — validator should have caught it.)
   - Step 1.2: Load + parse the agent YAML at `entry.path`. Build `ResolvedAgentConfig` via the existing agent-management loader (find it; reuse the same path the chat surface uses).
   - Step 1.3: If `node_def.outputs is not None`, replace `config.io` with `IOSpec(mode=config.io.mode, inputs=config.io.inputs, outputs=node_def.outputs.outputs)`. The mode stays as the agent declared.
   - Step 1.4: Construct the `FromWorkflowRun` from `ctx`:
     ```python
     fwr = FromWorkflowRun(
         workflow_run_id=ctx.workflow_run_id,
         invocation_id=ctx.invocation_id,
         event_id=ctx.parent_event_id,
     )
     ```
   - Step 1.5: `session = await self._session_store.create_session(node_def.agent, name=f"{ctx.workflow_run_id}::{node_name}", from_workflow_run=fwr)`. Stamp `ctx.spawned_session_id = session.session_id` so Step 3's payload builder reads it.
   - Step 1.6: **Step 1 STUB** — call `event = await self._engine_call(config=resolved, inputs=bound_inputs, session_id=session.session_id, thread_id=session_default_thread_id)`. Step 2 replaces the kwargs with the real `ExecutionContext` shape. Tests pass a fake `engine_call` that returns a synthetic agent-message Event.
   - Step 1.7: Extract structured output from `event.data` per the existing `AgentMessageData` shape. Return as `dict[str, Any]`.

3. **`tests/component/test_workflow_agent_runner.py`** — fake-engine path:
   - **Assembly happy path:** stub a `FakeCatalog` returning an entry with a controlled YAML path; stub the YAML loader to return a known `ResolvedAgentConfig`; inject a fake `engine_call` that captures the args + returns a fixed agent-message event. Assert the runner returned the extracted output, set `ctx.spawned_session_id`, and called `session_store.create_session` with the right `FromWorkflowRun`.
   - **Outputs override:** same setup but with `node_def.outputs = IOSpec(mode="single_shot", inputs={}, outputs={"classification": {"type": "str"}})`. Assert `engine_call` was passed a `ResolvedAgentConfig` whose `config.io.outputs == {"classification": {"type": "str"}}` AND `config.io.inputs` is UNCHANGED from the agent's declared shape.
   - **Identity unchanged:** assert `config.model`, `config.prompt`, `config.system_prompt`, `config.temperature`, `config.max_iterations`, `config.tools` are byte-identical to what the agent YAML declared.
   - **Unknown ref:** `FakeCatalog.lookup(...)` returns `None`; assert `WorkflowNodeFailed` raised with `cause=AgentRefUnresolved`.

**Step 1 Acceptance Criteria:**

1.1. `AgentRunner` class implements the `NodeRunner` protocol (satisfies `runtime_checkable` `Protocol` if available; otherwise duck-typed `run(...)` method).
1.2. Catalog lookup + agent YAML load happen in the documented order.
1.3. `outputs:` override replaces `config.io.outputs` ONLY; `config.io.inputs` and identity fields untouched.
1.4. `FromWorkflowRun` constructed from `ctx.workflow_run_id` + `ctx.invocation_id` + `ctx.parent_event_id`.
1.5. `create_session(...)` called with the `from_workflow_run` kwarg; `ctx.spawned_session_id` set to the returned session ID.
1.6. Unknown agent ref raises `WorkflowNodeFailed` with `cause=AgentRefUnresolved`.
1.7. Test suite covers: assembly happy path; outputs override; identity-fields untouched; unknown ref.
1.8. NO real `AgentEngine.execute()` import — the `engine_call` callable is the injection seam. Grep `src/workflows/builtins/agent.py` for `AgentEngine` — ZERO matches.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] `engine_call` is the SOLE engine-side dependency (Step 2 swaps in the real `AgentEngine.execute()`)
- [ ] All test cases pass
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.3 — identity overrides forbidden. Read the runner source and confirm ONLY `config.io.outputs` is touched. Any code mutating `config.model`, `config.prompt`, etc. is a master-spec violation (§4.1.2.4).
- AC 1.8 — Step 2 ownership. Grep `src/workflows/builtins/agent.py` for `AgentEngine` — ZERO matches. The runner takes `engine_call: Callable` at construction.
- AC 1.5 — the `name=` passed to `create_session` should be deterministic (`f"{ctx.workflow_run_id}::{node_name}"` or similar) so spawned sessions are easy to find in `kolsch sessions list`.

---

# STEP 2 — Engine invocation + error mapping

**Goal**: wire the real `AgentEngine.execute()` call. Construct the `ExecutionContext` carrying the workflow's `trace_id` and extended caller chain. Catch `AgentExecutionError`; re-raise as `WorkflowNodeFailed`. Per-node timeout composes with `with_timeout` from Phase 3.1.

**Files created/modified:**

- `src/workflows/builtins/agent.py` (modified) — replace the `engine_call` callable with a real `AgentEngine` injection; construct `ExecutionContext`; catch + remap.
- `src/workflows/errors.py` (modified) — add `AgentRefUnresolved(WorkflowRuntimeError)` if not already shipped in Step 1 (it should be — back-fill if not).
- `tests/component/test_workflow_agent_runner.py` (modified) — add tests for engine wiring, error remap, trace_id propagation.

**Effort**: ~1.5 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Replace Step 1's `engine_call` stub with the real `AgentEngine.execute()` invocation. Construct an `ExecutionContext` carrying the workflow's `trace_id` and the extended caller chain (agent ref prepended). Catch `AgentExecutionError`; re-raise as `WorkflowNodeFailed(cause=...)`. The executor's existing failure path emits `workflow.node.failed` per Phase 3.1.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.2-agent-integration-build.md` — Step 2.
2. `process/phase3.2-agent-integration-spec.md` — §8.5.2 (algorithm), §8.5.3 (cancellation), §8.5.4 (timeouts), §11.6.4 (trace_id), §13.x (caller chain).
3. `src/engine/engine.py:188` — `AgentEngine.execute(config, context, *, inputs)` signature + raise semantics.
4. `src/engine/context.py:52` — `ExecutionContext` Pydantic model.
5. `src/engine/context.py:100` — `AgentExecutionError`.

**What to implement:**

1. **Constructor signature change** in `src/workflows/builtins/agent.py`:

   ```python
   class AgentRunner:
       def __init__(
           self,
           *,
           catalog: AssetCatalog,
           session_store: SessionStore,
           engine: AgentEngine,
       ) -> None:
           self._catalog = catalog
           self._session_store = session_store
           self._engine = engine
   ```

   Drop the `engine_call: Callable` seam. Tests in Step 2+ use a real `AgentEngine` instance with a fake-LLM adapter (existing test fixture pattern).

2. **`ExecutionContext` construction** — after `create_session(...)` returns, build:

   ```python
   exec_ctx = ExecutionContext(
       session_id=session.session_id,
       thread_id=session_default_thread,
       trace_id=ctx.trace_id,
       caller_chain=(node_def.agent, *ctx.caller_chain),
       trigger="workflow",
   )
   ```

   The exact field names match `src/engine/context.py:52`; consult that source for the canonical shape (fields may differ — e.g., `cancellation` may be a separate kwarg). The two binding constraints:
   - `trace_id` MUST be `ctx.trace_id` (the workflow run's trace_id, NOT a fresh one).
   - `caller_chain` MUST be `(node_def.agent, *ctx.caller_chain)` (agent ref prepended; proximate first).

3. **Engine invocation + error remap:**

   ```python
   try:
       event = await self._engine.execute(
           config=resolved,
           context=exec_ctx,
           inputs=bound_inputs,
       )
   except AgentExecutionError as exc:
       raise WorkflowNodeFailed(
           node=node_name,
           invocation_id=str(ctx.invocation_id),
           cause=exc,
       ) from exc
   ```

   No other catches at v1. The agent layer surfaces its own failure modes through `AgentExecutionError`; anything else is an invariant violation and should propagate (mirrors `PlatformInternalError` semantics).

4. **Output extraction** — from `event.data` (typed `AgentMessageData`). Find the field that carries the structured output (likely `event.data.payload` or `event.data.structured`; check `src/schemas/events.py`). Return as `dict[str, Any]`.

5. **Add `AgentRefUnresolved`** in `src/workflows/errors.py` if Step 1 did not back-fill it:

   ```python
   class AgentRefUnresolved(WorkflowRuntimeError):
       """Catalog could not resolve the agent ref. Validator drift safety net."""

       caller_message: ClassVar[str] = (
           "A workflow step references an agent that isn't installed. "
           "Contact your engineer."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = ("ref",)

       def __init__(self, *, ref: str) -> None:
           self.ref = ref
           super().__init__(
               _runtime_str(self, problem=f"agent ref {ref!r} could not be resolved")
           )
   ```

6. **Tests** in `tests/component/test_workflow_agent_runner.py`:
   - **Engine invocation:** wire a real `AgentEngine` with a fake-LLM adapter that returns a controlled response; run the runner; assert the captured output matches.
   - **Error remap:** stub the fake LLM to exhaust retries (or otherwise produce `ModelRetryExhausted`); assert `WorkflowNodeFailed` raised with `cause` instance of `AgentExecutionError`.
   - **trace_id propagation:** assert the `ExecutionContext` passed to `engine.execute()` carries `trace_id == ctx.trace_id`.
   - **Caller chain extension:** assert the `ExecutionContext.caller_chain == (node_def.agent, *ctx.caller_chain)`.

**Step 2 Acceptance Criteria:**

2.1. `AgentRunner.__init__` takes `engine: AgentEngine` (no more `engine_call` callable seam).
2.2. `ExecutionContext` constructed carries `trace_id == ctx.trace_id`.
2.3. `ExecutionContext.caller_chain == (node_def.agent, *ctx.caller_chain)` — proximate first.
2.4. `AgentExecutionError` caught; remapped to `WorkflowNodeFailed(cause=exc)`.
2.5. `AgentRefUnresolved` exists in `src/workflows/errors.py`; raised from the unknown-ref path.
2.6. Output extraction from `event.data` returns a `dict[str, Any]`.
2.7. Tests cover: engine invocation; error remap; trace_id propagation; caller chain extension.
2.8. No regressions.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] `AgentEngine` is the SOLE engine-side dependency
- [ ] `ExecutionContext` field names match `src/engine/context.py:52` exactly
- [ ] No regressions: `uv run pytest tests/ -q` green

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.2 — `trace_id` MUST be `ctx.trace_id`. A fresh trace_id here is a regression — the spawned session would be uncorrelatable with the workflow run.
- AC 2.3 — `caller_chain` order. Proximate first: agent ref FIRST, then workflow's prior chain. Reversed order breaks audit forensics.
- AC 2.4 — exception remap is `from exc` (preserves `__cause__`); the executor's failure path reads `WorkflowNodeFailed.__cause__` for the error envelope.

---

# STEP 3 — Registry wiring + executor payload extension + caller-chain

**Goal**: register `AgentRunner` in `register_builtins(...)`; extend the executor's `workflow.node.completed` payload builder to read `ctx.spawned_session_id` for agent nodes; verify caller-chain prepend at the engine boundary lands correctly.

**Files created/modified:**

- `src/workflows/builtins/__init__.py` (modified) — `register_builtins(...)` constructs and registers `AgentRunner`.
- `src/workflows/executor.py` (modified) — payload builder for `workflow.node.completed` includes `spawned_session_id` when set on `ctx`.
- `tests/component/test_workflow_executor.py` (modified) — add test asserting `workflow.node.completed` payload for agent nodes carries `spawned_session_id`.

**Effort**: ~1 day.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Wire the `AgentRunner` into the default `RunnerRegistry` via `register_builtins(...)`. Extend the executor's `workflow.node.completed` payload to include `spawned_session_id` when the runner stamped one on `NodeContext`. NO new dependency injection plumbing — Phase 3.1's `register_builtins(...)` already accepts the catalog + dependencies; Phase 3.2 adds `engine` + `session_store` to the param list.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.2-agent-integration-build.md` — Step 3.
2. `process/phase3.2-agent-integration-spec.md` — §8.5.1 (registration), §11.6.3 (payload extension).
3. `src/workflows/builtins/__init__.py` — Phase 3.1's `register_builtins(...)` signature.
4. `src/workflows/executor.py` — the payload-build path inside the worklist loop. Search for `workflow.node.completed`.

**What to implement:**

1. **`register_builtins(...)` extension** in `src/workflows/builtins/__init__.py`:

   ```python
   def register_builtins(
       registry: RunnerRegistry,
       *,
       catalog: AssetCatalog,
       engine: AgentEngine,           # NEW in Phase 3.2
       session_store: SessionStore,   # NEW in Phase 3.2
       # ... existing kwargs unchanged
   ) -> None:
       registry.register("kolsch/nodes/bash", BashRunner(...))
       registry.register("kolsch/nodes/noop", NoopRunner())
       registry.register(
           "kolsch/nodes/agent",
           AgentRunner(
               catalog=catalog,
               session_store=session_store,
               engine=engine,
           ),
       )
       # ... existing registrations unchanged
   ```

2. **Bootstrap call-site updates** — find every place `register_builtins(...)` is called (likely `src/cli/workflows.py:_cmd_run` and the test fixtures). Add `engine=` and `session_store=` arguments. The CLI bootstrap already constructs an `AgentEngine` for chat — reuse the same instance.

3. **Executor payload extension** in `src/workflows/executor.py` — inside the worklist loop, after the runner returns, the payload builder for `workflow.node.completed` reads:

   ```python
   payload: dict[str, Any] = {
       "node_name": node_name,
       "invocation_id": str(invocation_id),
       "output": captured_output,
   }
   if node_def.type == "kolsch/nodes/agent" and ctx.spawned_session_id is not None:
       payload["spawned_session_id"] = str(ctx.spawned_session_id)
   ```

   The gate by `node_def.type == "kolsch/nodes/agent"` is defensive — other node types should not set `spawned_session_id` at v1, but the gate guarantees correct shape if a future node type forgets to clear the attribute.

4. **Tests** in `tests/component/test_workflow_executor.py`:
   - Single-agent-node workflow: assert the `workflow.node.completed` event's payload carries `spawned_session_id` as a string matching the spawned session's ID.
   - Bash node in the same suite: assert NO `spawned_session_id` in the bash `workflow.node.completed` payload (default `None` on `NodeContext` correctly excluded).

**Step 3 Acceptance Criteria:**

3.1. `register_builtins(...)` accepts `engine=` + `session_store=` kwargs; registers `AgentRunner` against `kolsch/nodes/agent`.
3.2. All `register_builtins(...)` call sites updated to pass the new kwargs.
3.3. `RunnerRegistry.get("kolsch/nodes/agent")` returns the runner, NOT `NodeTypeNotImplemented`.
3.4. Executor's `workflow.node.completed` payload for agent nodes includes `spawned_session_id: str`.
3.5. Non-agent nodes' `workflow.node.completed` payload is UNCHANGED.
3.6. Tests cover agent payload + bash non-payload.
3.7. No regressions.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] `BUILTIN_PHASE_MAP["agent"] == "3.2"` still maps the slot; the registry's runner registration shadows the `NodeTypeNotImplemented` raise per Phase 3.1's `get(...)` logic
- [ ] CLI smoke: `uv run kolsch --root /tmp/kolsch-agent-test workflows show kolsch/workflows/hello-agent` works without crashing (NOTE: starter ships in Step 4)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.5 — non-agent payload UNCHANGED. Read the payload-builder source; the `spawned_session_id` insertion MUST be gated by `node_def.type == "kolsch/nodes/agent"` (not just `ctx.spawned_session_id is not None`) so a misbehaving future runner can't pollute non-agent payloads.
- AC 3.2 — every `register_builtins(...)` call site updated. Grep the repo; the missing-kwarg case fails fast at construction (Python keyword-only enforcement), so any miss surfaces in tests.

---

# STEP 4 — Golden trace fixtures + starter workflow demo

**Goal**: ship the reference JSONL goldens documenting the workflow JSONL + spawned session JSONL pair, and confirm the starter `kolsch/workflows/hello-agent.yaml` runs end-to-end via the executor. Final integration tests.

**Files created/modified:**

- `bundles/kolsch/workflows/hello-agent.yaml` (new) — starter demo. Two-node `bash → agent` workflow with a small agent that produces a structured output.
- `bundles/kolsch/agents/hello-agent.yaml` (new if missing) — the agent the workflow invokes. Fake-LLM-friendly for test mode; real provider for the smoke.
- `tests/golden/runs/` (new files) — paired JSONLs:
  - `workflow-agent-roundtrip.workflow.jsonl` — the workflow side.
  - `workflow-agent-roundtrip.session.jsonl` — the spawned session side.
  - Optional: `workflow-agent-outputs-override.workflow.jsonl` + `.session.jsonl`.
- `tests/golden/runs_test.py` (modified) — add agent fixtures to the parametrized golden runner.
- `tests/component/test_workflow_agent_runner_e2e.py` (new) — Phase Tests 1-4 (end-to-end, outputs override, failure mapping, trace_id propagation).

**Effort**: ~1.5 days.

## Step 4 — Coder Prompt

> Reply "Step 4 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the golden trace suite + the starter demo. The goldens document the cross-JSONL pair so future refactors that disturb the linkage fail loudly.

**What to implement:**

1. **`bundles/kolsch/workflows/hello-agent.yaml`** — two-node workflow:

   ```yaml
   policy:
     audit_level: rule_matched
   inputs:
     greeting: { type: str }
   nodes:
     prep:
       type: kolsch/nodes/bash
       command: "echo '{{ inputs.greeting }}, world'"
     classify:
       type: kolsch/nodes/agent
       agent: kolsch/agents/hello-agent
   graph:
     start: prep
     edges:
       - { from: prep, to: classify, binds: { context: "{{ nodes.prep.stdout }}" } }
   ```

   Adjust the binding shape to match the agent's declared input IOSpec.

2. **`bundles/kolsch/agents/hello-agent.yaml`** — a minimal agent with single_shot mode and a small `outputs:` declaration (e.g., `{classification: str, confidence: float}`). Pattern after an existing starter agent.

3. **Goldens** — run the workflow once against the fake-LLM-backed agent; capture both JSONLs; canonicalize volatile fields (`ts`, all `*Id` prefix-typed values) per Phase 3.1's substitution table; commit as goldens.

4. **`tests/golden/runs_test.py`** — extend the parametrize list to include the agent fixtures. Both files (`workflow.jsonl` + `session.jsonl`) are asserted; assert the canonicalized `trace_id` is identical across both files.

5. **`tests/component/test_workflow_agent_runner_e2e.py`**:
   - **Phase Test 1 — end-to-end:** `setup --non-interactive` a fresh workspace; `kolsch workflows run kolsch/workflows/hello-agent --input greeting=hello`; assert exit 0; assert both JSONLs exist with the expected events.
   - **Phase Test 2 — outputs override:** fixture a variant workflow with `outputs:` override on the agent node; assert the captured output matches the override schema; assert downstream Jinja consumption works.
   - **Phase Test 3 — failure mapping:** configure the agent to fail (e.g., output-schema mismatch with a misconfigured agent); assert `workflow.node.failed` then `workflow.run.failed` in the workflow JSONL, and `thread.exchange_failed` in the agent JSONL.
   - **Phase Test 4 — trace_id propagation:** assert every event in BOTH JSONLs carries the same `trace_id`.

**Step 4 Acceptance Criteria:**

4.1. `bundles/kolsch/workflows/hello-agent.yaml` exists; parses; validates; runs.
4.2. `bundles/kolsch/agents/hello-agent.yaml` exists; loads via the existing agent surface.
4.3. Paired golden JSONLs at `tests/golden/runs/workflow-agent-roundtrip.workflow.jsonl` + `.session.jsonl` shipped.
4.4. `runs_test.py` asserts semantic equality of both files with canonicalized volatile fields; asserts `trace_id` identical across both files.
4.5. `test_workflow_agent_runner_e2e.py` covers Phase Tests 1-4.
4.6. Manual smoke: `kolsch workflows run kolsch/workflows/hello-agent --input greeting=hi` succeeds on a fresh workspace.
4.7. No regressions.

**Handoff trigger:** All ACs met → "Step 4 Complete — [summary]".

## Step 4 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 4.4 — `trace_id` identity across BOTH JSONLs. The canonicalization should preserve the equivalence (replace both occurrences with the SAME sentinel `<trace_id>`); a sloppy canonicalizer that gives each file a unique sentinel masks the bug this golden is supposed to catch.
- AC 4.6 — manual smoke. Run it. The CLI path is the user-facing surface; passing tests doesn't guarantee the CLI bootstrap correctly threads `engine=` + `session_store=` into `register_builtins(...)`.
- AC 4.5 — the failure-mapping test (Phase Test 3) MUST observe both event streams. A passing test that only checks the workflow side hides cross-JSONL drift.

---

## Done when

- All 5 steps Approved by Reviewer.
- All 5 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.2 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/hello-agent --input greeting=hello
  ls /tmp/kolsch-fresh/runs/
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl
  ls /tmp/kolsch-fresh/sessions/
  cat /tmp/kolsch-fresh/sessions/ses_*/events.jsonl
  ```
