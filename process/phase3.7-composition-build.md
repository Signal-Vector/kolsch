# [SPEC-008] Phase 3.7 — Composition (map + workflow) runtime

**Type:** spec
**Doc #:** 008
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| sub-workflow | A workflow invoked by `kolsch/nodes/workflow`. Owns its own `WorkflowRun`, JSONL, and `_state`. |
| parent pointer | `WorkflowRun.parent: ParentRunPointer | None`. Mirrors Phase 3.2's `FromWorkflowRun` shape. |
| call chain | Ordered tuple of workflow refs from root to current child; used for cycle detection. |
| worker | The node referenced by `map.apply:`. Lives in the SAME workflow as the map node. |
| iteration overlay | The per-iteration `inputs.*` scope: `{**bound_inputs, **rendered_with, item_as: element}`. |

---

## Background

Phase 3.0 shipped the parser models (`WorkflowNodeDef` / `MapNodeDef`), inputs models, and the `WorkflowCallCycle` + `WorkflowExtendsDepthExceeded` (default `limit=16`) error classes. `BUILTIN_PHASE_MAP` maps `map`/`workflow` → `"3.7"`. The executor is REENTRANT — nothing prevents a child runner from constructing a nested `IterativeExecutor` and awaiting it.

Phase 3.2 shipped the **parent-pointer pattern** for spawned agent sessions. Phase 3.7 mirrors this verbatim for workflow→workflow nesting. Phase 3.4 shipped pause/resume; its `_replay.py` (line 36) explicitly anticipates Phase 3.7's nested-resume extension.

What's LEFT for Phase 3.7:

1. **`WorkflowRun.parent` field** + `ParentRunPointer` value object.
2. **`WorkflowRunStore.create(*, parent=)`** kwarg.
3. **`IterativeExecutor.__init__(*, _call_depth=0, _call_chain=())`** kwargs (private nesting metadata).
4. **`NodeContext` extensions** — `spawned_run_id`, `call_depth`, `call_chain`, `derive_for_iteration(...)`, access to the workflow for worker lookup.
5. **`WorkflowSubRunner`** at `src/workflows/builtins/workflow_sub.py`. Resolves child ref, mints parent pointer, creates child run with inherited `trace_id`, constructs nested executor, awaits child, harvests terminal outputs, propagates pause via `WorkflowPausedForInput` re-raise.
6. **`MapRunner`** at `src/workflows/builtins/map.py`. Sequential iteration; resolves worker by name in same workflow; renders `with:` once, `item_as` per element; dispatches the worker's runner directly per element; collects `{results: list[...]}`.
7. **Two new event kinds** `workflow.node.called_workflow` + `_completed` for parent-side forensics.
8. **`workflow.node.completed` payload extension** — `child_run_id` for workflow nodes.
9. **`_replay.py` extension** for the two new event kinds + nested resume.
10. **`compute_terminal_output_dict(workflow, captured, pruned)`** helper at `src/workflows/_terminal.py` — child workflow's inferred output dict (master spec §6.6).
11. **`ChildWorkflowFailed`** error class — wraps the child's failure when surfacing to the parent.
12. **`register_builtins(...)`** extension — `executor_factory=` kwarg (avoids circular import); registers both new runners.
13. **Orphan-sweep extension** — orphaned child runs whose parent is dead → `failed` with `reason="orphaned_child"`.
14. **Goldens + starters** — 3-level nesting fixture; cycle / depth rejection; nested pause+resume; map over agent classifier.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**.

Related docs:

- `process/phase3.7-composition-spec.md` — the master design spec.
- `process/phase3.4-user-input-build.md` — pause/resume machinery this phase extends.
- `process/phase3.2-agent-integration-build.md` — the parent-pointer pattern this phase mirrors.
- `process/phase3.0-workflow-spec.md` §4.6.1 / §4.6.2 / §7.4 / §7.5.

---

## Scope

**In scope (delivered across the 4 steps):**

- `ParentRunPointer` Pydantic value object at `src/schemas/entities.py` (or wherever `FromWorkflowRun` lives).
- `WorkflowRun.parent` field + `WorkflowRunStore.create(parent=)` plumbing.
- Executor `__init__` nesting kwargs + depth-cap check at construction.
- `NodeContext` extensions for nesting + iteration.
- `WorkflowSubRunner` (full lifecycle: spawn, run, pause cascade, terminal harvest).
- `MapRunner` (sequential, with iteration events).
- Two new event kinds + payload extension.
- `_replay.py` extension.
- `compute_terminal_output_dict` helper.
- `ChildWorkflowFailed` error class.
- `register_builtins(...)` extension.
- Orphan-sweep extension.
- Goldens + two new starter workflows (`bundles/kolsch/workflows/nested-greeting.yaml`, `bundles/kolsch/workflows/parallel-tickets.yaml`).
- Phase Tests 1-8 wired end-to-end.

**Out of scope:**

- Concurrent execution (3.10).
- Fork (3.8).
- Chat (3.9).
- Sub-workflow state inheritance (LOCKED NO at v1).
- Mid-iteration cancellation responsiveness (documented limitation).
- Auto-cascading parent resume (operator-driven cascade at v1; auto-cascade is future QoL).

**Locked decisions (do NOT relitigate):**

- `workflow.ref` is the child's asset ref; resolved via `catalog.lookup(ref, expected_type="workflow")`.
- `map.apply` is a node NAME in the same workflow.
- `map.item_as` REQUIRED (no default).
- `map.over` non-list raises `WorkflowNodeFailed`.
- Depth-16 cap is SHARED with `extends:`; same error class.
- Cycle is call-chain repetition.
- Child state isolated from parent.
- `trace_id` inherits unchanged.
- Map iterations sequential; events carry `parent_invocation_id` + `iteration_index`.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Key existing modules:**

- `src/workflows/run.py:70-91` — `WorkflowRun`. Phase 3.7 extends.
- `src/workflows/run_store.py:72-106` — `create(...)`. Phase 3.7 extends.
- `src/workflows/executor.py:254-267` — `IterativeExecutor.__init__`. Phase 3.7 extends.
- `src/workflows/executor.py:943-985` — payload builder. Phase 3.7 extends.
- `src/workflows/parser.py:337-352` — `WorkflowNodeDef` + `MapNodeDef`. No changes.
- `src/workflows/errors.py:247-273`, `:753-779`, `:1185` — reused.
- `src/workflows/_replay.py` — Phase 3.7 extends.
- `src/workflows/events.py` — Phase 3.7 adds two event kinds.
- `src/workflows/context.py` — Phase 3.7 extends.
- `src/workflows/builtins/__init__.py` — Phase 3.7 extends.
- `src/workflows/builtins/agent.py:46-150` — structural template.
- `src/cli/workflows.py:507-513` — `catalog.lookup` + `load_workflow` pattern (reused).

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-composite-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

See `process/phase3.7-composition-spec.md`'s "Phase-level Acceptance Criteria" section (full list).

---

## Phase-level Integration Tests

See `process/phase3.7-composition-spec.md`'s "Phase-level Integration Tests" section (Phase Tests 1-8).

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 4-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.7 of kolsch-engine workflows through 4 sequential cycles. This phase is the **largest leaf-runtime phase**: sub-workflow nesting introduces a new entity lifecycle (child runs), cross-JSONL forensics, depth + cycle detection, pause cascade, and the operator-driven resume cascade. Map iteration is structurally simpler but adds per-iteration event emission semantics.

**Authoritative references:**

- THIS spec: `process/phase3.7-composition-build.md`.
- Master spec: `process/phase3.7-composition-spec.md` — full §4.6.1 / §4.6.2 elaborations.
- Phase 3.4 build plan: `process/phase3.4-user-input-build.md` — pause/resume machinery.
- Phase 3.2 build plan: `process/phase3.2-agent-integration-build.md` — parent-pointer pattern.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §4.6.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `ParentRunPointer` + `WorkflowRun.parent` field + `WorkflowRunStore.create(parent=)` + executor `_call_depth`/`_call_chain` constructor kwargs (with depth check) + `NodeContext` extensions + two new event kinds + `workflow.node.completed.child_run_id` payload extension. Pure plumbing. | ~1.5 days |
| **Step 1** | `WorkflowSubRunner` — full lifecycle: catalog resolve, parent-pointer mint, child-run creation, nested executor construction, child run/pause cascade, terminal output harvest via `compute_terminal_output_dict`. NEW `ChildWorkflowFailed` error class. Tests cover: 3-level nesting, cycle rejection, depth-17 rejection, child failure propagation, pause cascade. | ~2.5 days |
| **Step 2** | `MapRunner` — sequential iteration; worker resolution; `with:` rendering; per-iteration event emission via `NodeContext.derive_for_iteration`; strict-mode bind validation per iteration; collect `{results: list[...]}`. Tests cover: happy path over 10 elements, non-list error, custom `item_as`, `with:` constants, partial failure propagates. | ~1.5 days |
| **Step 3** | `_replay.py` extension for two new event kinds + nested resume + operator-driven cascade test + orphan-sweep extension. Goldens for 3-level nesting + cycle + depth + nested-pause + map. Starter workflows. Phase Tests 1-8 wired. | ~2 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a locked decision (auto-cascade parent resume, parallel map, state inheritance, `worker_by_name` instead of `apply:`) → REJECT.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append to THIS spec, re-brief.
- Coder discovers a prior-phase contract was wrong → flag as separate patch; do NOT widen 3.7's scope.
- Scope creep (fork, chat, concurrent execution) → REJECT.

**Done when:** All 4 steps Approved AND all 8 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.7 line annotated with the shipping SHA.

---

# STEP 0 — Parent pointer + executor nesting kwargs + new event kinds (PREREQ)

**Goal**: ship `ParentRunPointer` + `WorkflowRun.parent` + `WorkflowRunStore.create(parent=)` + executor `_call_depth`/`_call_chain` + `NodeContext` extensions + two new event kinds + payload extension. NO runners yet.

**Files created/modified:**

- `src/schemas/entities.py` (modified) — add `ParentRunPointer` (or extend the existing `FromWorkflowRun` model file).
- `src/workflows/run.py` (modified) — `WorkflowRun.parent` field.
- `src/workflows/run_store.py` (modified) — `create(*, parent=None)` kwarg; persist in `meta.json`; round-trip on `read()`.
- `src/workflows/executor.py` (modified) — `__init__(*, _call_depth=0, _call_chain=())` kwargs; depth-cap check at construction; thread into `NodeContext`.
- `src/workflows/context.py` (modified) — `NodeContext.spawned_run_id` / `call_depth` / `call_chain` / `workflow` field + `derive_for_iteration(...)` method.
- `src/workflows/events.py` (modified) — `WorkflowEventKind.CALLED_WORKFLOW` + `CALLED_WORKFLOW_COMPLETED` + corresponding Pydantic event models.
- `src/workflows/executor.py` (modified) — payload builder gains `child_run_id` for workflow nodes.
- `tests/component/test_workflow_composition_prereq.py` (new) — coverage for the plumbing.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the plumbing. NO runners.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.7-composition-build.md` — Step 0.
2. `process/phase3.7-composition-spec.md` — §11 (event log extension).
3. `src/schemas/entities.py` — `FromWorkflowRun` (Phase 3.2; mirror the shape).
4. `src/workflows/run.py:70-91` — `WorkflowRun`.
5. `src/workflows/run_store.py:72-106` — `create`.
6. `src/workflows/executor.py:254-267, 943-985` — constructor + payload builder.
7. `src/workflows/context.py` — `NodeContext`.

**What to implement:**

1. **`ParentRunPointer`** in `src/schemas/entities.py`:

   ```python
   class ParentRunPointer(BaseModel):
       """Phase 3.7 — parent-workflow pointer on child runs.

       Mirrors :class:`FromWorkflowRun` (Phase 3.2's agent-session
       pointer). The shape is intentionally identical so cross-JSONL
       forensics tools handle both uniformly.
       """

       model_config = ConfigDict(frozen=True, extra="forbid")

       workflow_run_id: WorkflowRunId
       invocation_id: InvocationId
   ```

2. **`WorkflowRun.parent`** in `src/workflows/run.py`:

   ```python
   class WorkflowRun(BaseModel):
       ...  # existing
       parent: ParentRunPointer | None = None
   ```

3. **`WorkflowRunStore.create`** extension:

   ```python
   def create(
       self, *,
       workflow_ref: str,
       inputs: dict[str, Any] | None = None,
       trace_id: TraceId | None = None,
       parent: ParentRunPointer | None = None,
   ) -> WorkflowRun:
       run = WorkflowRun(
           ...,
           parent=parent,
       )
       # persist parent in meta.json; deserialize on read()
       ...
   ```

   `read()` MUST round-trip the field; meta.json should serialize `parent` as a nested dict.

4. **`IterativeExecutor.__init__`** extension:

   ```python
   def __init__(
       self, *,
       workflow: Workflow,
       run: WorkflowRun,
       store: WorkflowRunStore,
       registry: RunnerRegistry,
       catalog: AssetCatalog,
       _call_depth: int = 0,
       _call_chain: tuple[str, ...] = (),
   ) -> None:
       if _call_depth > 16:
           raise WorkflowExtendsDepthExceeded(
               file=run.workflow_ref,
               line=0,
               depth=_call_depth,
               limit=16,
           )
       self._call_depth = _call_depth
       self._call_chain = _call_chain
       ...
   ```

5. **`NodeContext` extensions** in `src/workflows/context.py`:

   ```python
   @dataclass(slots=True)
   class NodeContext:
       ...
       spawned_run_id: WorkflowRunId | None = None
       call_depth: int = 0
       call_chain: tuple[str, ...] = ()
       workflow: Any = None  # the parent Workflow for worker lookup by map runner
       parent_invocation_id: InvocationId | None = None
       iteration_index: int | None = None
       iteration_total: int | None = None

       def derive_for_iteration(
           self,
           *,
           node_name: str,
           parent_invocation_id: InvocationId,
           iteration_index: int,
           iteration_total: int,
       ) -> "NodeContext":
           """Return a per-iteration copy with a fresh invocation_id."""
           return NodeContext(
               ...,
               invocation_id=InvocationId(f"inv_{uuid.uuid4()}"),
               parent_invocation_id=parent_invocation_id,
               iteration_index=iteration_index,
               iteration_total=iteration_total,
               # inherit the rest from self
           )
   ```

   The executor's `_build_ctx` populates `call_depth=self._call_depth`, `call_chain=self._call_chain`, `workflow=self._workflow` so runners can introspect.

6. **Two new event kinds** in `src/workflows/events.py`:

   ```python
   class WorkflowEventKind(StrEnum):
       ...
       CALLED_WORKFLOW = "workflow.node.called_workflow"
       CALLED_WORKFLOW_COMPLETED = "workflow.node.called_workflow_completed"

   class WorkflowNodeCalledWorkflowPayload(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       node_name: str
       invocation_id: str
       child_workflow_run_id: str
       child_workflow_ref: str

   class WorkflowNodeCalledWorkflowEvent(NodeScopedWorkflowEvent):
       kind: Literal[WorkflowEventKind.CALLED_WORKFLOW] = ...
       payload: WorkflowNodeCalledWorkflowPayload

   class WorkflowNodeCalledWorkflowCompletedPayload(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       node_name: str
       invocation_id: str
       child_workflow_run_id: str
       child_status: str  # "succeeded" / "failed" / "waiting_for_input" / "aborted" / "timed_out"

   class WorkflowNodeCalledWorkflowCompletedEvent(NodeScopedWorkflowEvent):
       kind: Literal[WorkflowEventKind.CALLED_WORKFLOW_COMPLETED] = ...
       payload: WorkflowNodeCalledWorkflowCompletedPayload
   ```

7. **Payload builder extension** — in `src/workflows/executor.py`'s `workflow.node.completed` payload builder (where Phase 3.2's `spawned_session_id` is added, ~line 970):

   ```python
   if (
       _node_type_ref(node_def) == "kolsch/nodes/workflow"
       and ctx.spawned_run_id is not None
   ):
       completed_payload["child_run_id"] = str(ctx.spawned_run_id)
   ```

8. **Tests in `tests/component/test_workflow_composition_prereq.py`**:
   - `ParentRunPointer` frozen + extra=forbid.
   - `WorkflowRun.parent` defaults to None; round-trips through `WorkflowRunStore.create()` + `read()`.
   - Executor with `_call_depth=17` raises `WorkflowExtendsDepthExceeded(depth=17, limit=16)` at construction.
   - Executor with `_call_depth=0`/`_call_chain=()` defaults preserved (backward-compat).
   - `NodeContext.derive_for_iteration` creates a fresh `invocation_id`; carries `parent_invocation_id`/`iteration_index`.
   - Two new event kinds serialize/deserialize cleanly.
   - `workflow.node.completed` for a (synthesized) workflow node with `spawned_run_id` set carries `child_run_id` in payload. Other node types unaffected.

**Step 0 Acceptance Criteria:**

0.1. `ParentRunPointer` exists; frozen; carries `workflow_run_id`/`invocation_id`.
0.2. `WorkflowRun.parent` field; backward-compat default `None`.
0.3. `WorkflowRunStore.create(parent=)` round-trips through meta.json.
0.4. Executor accepts `_call_depth`/`_call_chain`; raises `WorkflowExtendsDepthExceeded(depth=N, limit=16)` for depth > 16.
0.5. `NodeContext` extended with nesting + iteration attributes + `derive_for_iteration`.
0.6. Two new event kinds shipped.
0.7. `workflow.node.completed` payload extension for workflow nodes.
0.8. All existing tests pass; no regressions.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] `grep WorkflowSubRunner src/` → ZERO matches (Step 1 owns)
- [ ] `grep MapRunner src/` → ZERO matches (Step 2 owns)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.3 — meta.json round-trip. Read the persistence code; the `parent` field must serialize as a NESTED dict (not a flat string). Round-trip via `store.read(run_id)`; assert deep equality.
- AC 0.4 — depth check at construction (not at run-start). The check MUST happen before any work begins so a nested executor that would exceed depth never starts.
- AC 0.7 — payload gating. The `child_run_id` insertion MUST be gated by BOTH `type == "kolsch/nodes/workflow"` AND `spawned_run_id is not None`. Otherwise a future runner setting `spawned_run_id` pollutes non-workflow payloads.

---

# STEP 1 — WorkflowSubRunner (full lifecycle)

**Goal**: ship the workflow-node runtime including cycle detection, depth-cap check (defensive), nested executor construction, child run/pause cascade, terminal output harvest, child-failure propagation.

**Files created/modified:**

- `src/workflows/builtins/workflow_sub.py` (new) — `WorkflowSubRunner`.
- `src/workflows/_terminal.py` (new) — `compute_terminal_output_dict(workflow, captured, pruned)`.
- `src/workflows/errors.py` (modified) — `ChildWorkflowFailed(WorkflowRuntimeError)`.
- `src/workflows/builtins/__init__.py` (modified) — `register_builtins(...)` accepts `executor_factory=` kwarg; registers `WorkflowSubRunner`.
- `src/cli/workflows.py` (modified) — `_cmd_run` constructs an `executor_factory` lambda and threads it into `register_builtins`.
- `tests/component/test_workflow_sub_runner.py` (new) — coverage.

**Effort**: ~2.5 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship `WorkflowSubRunner`. Pause cascade rides on Phase 3.4's `WorkflowPausedForInput` re-raise; the resume cascade is operator-driven (not auto).

**Authoritative references — read BEFORE coding:**

1. `process/phase3.7-composition-build.md` — Step 1.
2. `process/phase3.7-composition-spec.md` — §4.6.1 (full).
3. `src/workflows/builtins/agent.py:46-150` — structural template (parent-pointer + child-spawn pattern).
4. `src/workflows/run_store.py` — child-run creation.
5. `src/cli/workflows.py:507-513` — `catalog.lookup` + `load_workflow` pattern.

**What to implement:**

1. **`compute_terminal_output_dict`** in `src/workflows/_terminal.py`:

   ```python
   def compute_terminal_output_dict(
       workflow: Workflow,
       captured: dict[str, dict[str, Any]],
       pruned: set[str],
   ) -> dict[str, Any]:
       """Phase 3.7 — child workflow's inferred output dict per master spec §6.6.

       Returns a dict keyed by terminal node names. Each value is:
       - that terminal's captured output dict, if the node fired;
       - None, if the node was pruned (branch-not-taken cascade);
       - None, if the node was skipped (when: gate fired false — same
         as pruned; the executor's _completed_or_skipped tracks both).
       """
       outgoing: dict[str, set[str]] = {n: set() for n in workflow.nodes}
       if workflow.graph is not None:
           for edge in workflow.graph.edges:
               outgoing.setdefault(edge.from_, set()).add(edge.to)
       terminals = [n for n in workflow.nodes if not outgoing.get(n)]
       return {
           name: (None if name in pruned else captured.get(name))
           for name in terminals
       }
   ```

2. **`ChildWorkflowFailed`** in `src/workflows/errors.py`:

   ```python
   class ChildWorkflowFailed(WorkflowRuntimeError):
       caller_message: ClassVar[str] = (
           "A sub-workflow step failed. Share the run ID with your engineer."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id", "status")

       def __init__(self, *, run_id: str, status: str = "failed") -> None:
           self.run_id = run_id
           self.status = status
           super().__init__(
               _runtime_str(
                   self,
                   problem=f"child workflow run {run_id!r} terminated with status {status!r}",
               )
           )
   ```

3. **`WorkflowSubRunner`** in `src/workflows/builtins/workflow_sub.py`:

   Follow the §4.6.1.1 algorithm in the spec verbatim. Key implementation notes:
   - The `executor_factory` is a callable taking the same kwargs as `IterativeExecutor.__init__` and returning a constructed executor. Phase 3.7 uses this seam to avoid circular imports between `builtins/` and `executor.py`.
   - Cycle detection runs BEFORE the catalog lookup (cheap; uses `ctx.call_chain`).
   - On `WorkflowPausedForInput` from the nested `nested.run()`, the runner emits `workflow.node.called_workflow_completed` with `child_status="waiting_for_input"`, then RE-RAISES so the parent's executor catches it and pauses.
   - On `WorkflowRunStatus.FAILED` / `ABORTED` / `TIMED_OUT`, raise `WorkflowNodeFailed(cause=ChildWorkflowFailed(...))` so the executor's existing failure path emits `workflow.node.failed`.

4. **`register_builtins(...)`** extension:

   ```python
   def register_builtins(
       registry: RunnerRegistry,
       *,
       ...,  # existing kwargs
       executor_factory: Callable[..., IterativeExecutor] | None = None,  # NEW
   ) -> None:
       ...
       if executor_factory is not None:
           registry.register(
               "kolsch/nodes/workflow",
               WorkflowSubRunner(
                   catalog=catalog,
                   store=store,
                   executor_factory=executor_factory,
                   registry=registry,
               ),
           )
   ```

   The `store` kwarg may also be new; add if not already present. (Phase 3.4 added the store for resume; reuse if there.)

5. **CLI bootstrap** in `src/cli/workflows.py:_cmd_run` (and `_cmd_resume`):

   ```python
   def _executor_factory(**kwargs):
       return IterativeExecutor(**kwargs)

   register_builtins(
       registry,
       ...,
       executor_factory=_executor_factory,
   )
   ```

6. **Tests in `tests/component/test_workflow_sub_runner.py`**:
   - **Happy path:** parent workflow with one `workflow:` node pointing at a 2-node child; child runs; parent harvests terminal outputs; assert downstream parent node consumes `{{ nodes.<workflow_node>.<terminal_name>.<key> }}` correctly.
   - **3-level nesting:** great-grandparent → grandparent → parent → child. All four JSONLs exist; share `trace_id`; each child's `parent` field correct.
   - **Cycle detection:** workflow `A` calls `A`. Expected: `WorkflowCallCycle(cycle=("kolsch/workflows/a", "kolsch/workflows/a"))`. Run aborts.
   - **Indirect cycle:** `A` → `B` → `A`. Expected: cycle with chain `("A", "B", "A")`.
   - **Depth 17 rejected:** chain of 17 workflows, each calling the next. The 17th depth check raises `WorkflowExtendsDepthExceeded(depth=17, limit=16)`.
   - **Child failure propagates:** child has a failing bash node; child's status = FAILED. Parent's runner raises `WorkflowNodeFailed(cause=ChildWorkflowFailed(run_id=..., status="failed"))`.
   - **Pause cascade:** child has `user_input`; child pauses; parent's worklist catches `WorkflowPausedForInput` and pauses too. Both `waiting_at` sidecars correct.
   - **`child_run_id` in payload:** assert parent's `workflow.node.completed` for the workflow node carries `child_run_id`.
   - **`called_workflow` events emitted:** assert parent's JSONL contains `workflow.node.called_workflow` BEFORE `workflow.node.started` of the workflow node and `_completed` AFTER.
   - **State isolation:** parent sets `state.x=1`; child sets `state.x=99`; after child returns, parent's `state.x == 1`.
   - **Trace_id inheritance:** all events across parent+child JSONLs share the same `trace_id`.

**Step 1 Acceptance Criteria:**

1.1. `WorkflowSubRunner` ships with the full §4.6.1.1 algorithm.
1.2. Cycle detection via `ctx.call_chain` walk; raises `WorkflowCallCycle`.
1.3. Depth check at nested-executor construction; raises `WorkflowExtendsDepthExceeded`.
1.4. Child runs with inherited `trace_id` + parent pointer.
1.5. Two new events emitted at correct ordering.
1.6. `child_run_id` in `workflow.node.completed` payload.
1.7. Child failure → `WorkflowNodeFailed(cause=ChildWorkflowFailed)`.
1.8. `WorkflowPausedForInput` from child re-raises through parent runner; parent pauses per Phase 3.4 discipline.
1.9. Child state isolated from parent.
1.10. `compute_terminal_output_dict` correctly computes terminal dict (pruned = `None`).
1.11. `register_builtins(executor_factory=...)` extension wired through CLI.
1.12. Tests cover all 10 listed scenarios.
1.13. No regressions.

**Self-check before handoff:**

- [ ] All 13 ACs met
- [ ] Manual smoke: a 2-level workflow runs end-to-end via CLI
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.2 — cycle detection. The chain MUST include the CURRENT ref before the check; otherwise A→A's first call passes (chain is empty), second call catches it on entry. Test fixtures verify both direct and indirect cycles.
- AC 1.8 — pause cascade re-raise. Read the runner source: after emitting `_completed` with `child_status="waiting_for_input"`, the runner MUST re-raise `WorkflowPausedForInput`. A swallowed pause silently hangs the parent's worklist.
- AC 1.9 — state isolation. The child's `IterativeExecutor` MUST initialize `_state = dict(child_run.inputs)`, NOT from the parent's `_state`. Reading parser code confirms `WorkflowRun.inputs` is the source; the parent's runner passed `bound_inputs` as the child's `inputs`, not parent's `_state`.

---

# STEP 2 — MapRunner (sequential iteration)

**Goal**: ship the map-node runtime — sequential iteration; worker resolution; per-iteration event emission; collected results.

**Files created/modified:**

- `src/workflows/builtins/map.py` (new) — `MapRunner`.
- `src/workflows/builtins/__init__.py` (modified) — register.
- `src/workflows/executor.py` (modified) — payload extension on iteration events (`parent_invocation_id` + `iteration_index` in `workflow.node.started`).
- `tests/component/test_workflow_map.py` (new) — coverage.

**Effort**: ~1.5 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship `MapRunner`. Per-iteration events emit via the executor's existing emit infrastructure; the runner constructs per-iteration NodeContexts.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.7-composition-build.md` — Step 2.
2. `process/phase3.7-composition-spec.md` — §4.6.2 (full).

**What to implement:**

1. **`MapRunner`** in `src/workflows/builtins/map.py` — implement per §4.6.2.1 algorithm in the spec.

   Key implementation notes:
   - Worker resolution: `worker_def = ctx.workflow.nodes[node_def.apply]`. The `ctx.workflow` field was added in Step 0. Defensive raise if missing (validator should have caught it).
   - Worker runner fetch: `worker_runner = self._registry.get(worker_def.type, node_name=node_def.apply)`. Reuses the registry.
   - Per-iteration NodeContext: `ctx.derive_for_iteration(...)` (added in Step 0). The new context has a fresh `invocation_id` so iteration events are distinct.
   - **Iteration event emission**: the worker runner's `run()` does NOT itself emit `workflow.node.started`/`completed` — the executor's worklist body does. The map runner emits them by calling `ctx.emit(WorkflowNodeStartedEvent(...))` BEFORE invoking the worker, and `ctx.emit(WorkflowNodeCompletedEvent(...))` AFTER. Payload includes `parent_invocation_id=ctx.invocation_id` (the map node's invocation_id) + `iteration_index`.
   - Strict-mode bind validation: use `_inputs_model_for(worker_def)` (Phase 3.3) to validate the iteration overlay against the worker's typed `Inputs(BaseModel)`. Mismatch raises `WorkflowBindTypeMismatch`.

2. **`register_builtins`** extension:

   ```python
   registry.register("kolsch/nodes/map", MapRunner(registry=registry))
   ```

   The map runner takes the registry to dispatch worker types. NO new external kwarg.

3. **Tests in `tests/component/test_workflow_map.py`**:
   - **Happy path 10 elements:** map over `[1, 2, ..., 10]` with worker doing `bash echo $item`; assert `{results: [10 dicts]}` each with the right stdout.
   - **Custom `item_as`:** `apply: classify_one, item_as: ticket`; assert worker sees `inputs.ticket`.
   - **`with:` constants:** `with: {prefix: "OPS-"}`; assert each iteration sees `inputs.prefix == "OPS-"`.
   - **`with:` Jinja-rendered:** `with: {prefix: "{{ inputs.tag }}"}`; assert the rendered value is bound.
   - **Non-list raises:** `over: "{{ inputs.scalar }}"` with scalar bound to `"abc"`; `WorkflowNodeFailed`.
   - **Worker undefined:** `apply: nonexistent_node`; `WorkflowNodeFailed`.
   - **Partial failure propagates:** worker fails on 3rd iteration; run aborts; only 2 worker `workflow.node.completed` events emitted; map node emits `workflow.node.failed`.
   - **Per-iteration events:** assert each iteration's `workflow.node.started`/`completed` has `parent_invocation_id == <map_inv_id>` AND `iteration_index == N`.
   - **Empty over:** `over: []`; assert `{results: []}`; no iteration events.
   - **Downstream consumes:** parent workflow's downstream `filter` consumes `nodes.<map>.results`; integration test.

**Step 2 Acceptance Criteria:**

2.1. `MapRunner` ships with §4.6.2.1 algorithm.
2.2. Worker resolved by name in same workflow.
2.3. `over:` non-list raises `WorkflowNodeFailed`.
2.4. `over: []` returns `{results: []}` cleanly.
2.5. `with:` rendered once; constants bound per iteration.
2.6. `item_as` correctly binds per-element.
2.7. Per-iteration `workflow.node.started`/`completed` events carry `parent_invocation_id` + `iteration_index`.
2.8. Worker failure aborts the map; partial results discarded.
2.9. Strict-mode bind validation runs per iteration.
2.10. Registered in `register_builtins(...)`.
2.11. Tests cover all listed scenarios.
2.12. No regressions.

**Self-check before handoff:**

- [ ] All 12 ACs met
- [ ] Manual smoke: a workflow with `map` over Phase 3.6 classifier runs end-to-end
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.7 — per-iteration event payload. Inspect a generated JSONL; confirm `parent_invocation_id` is the map node's invocation_id (NOT the worker's), and `iteration_index` is 0-based.
- AC 2.5 — `with:` rendering. The rendering happens ONCE at map fire-time (not per iteration). A `with: {time: "{{ now() }}"}` would have the same value across all iterations.
- AC 2.8 — partial-failure cleanup. After worker N fails, no `workflow.node.completed` for the map node (so `_captured["<map>"]` stays empty). Downstream nodes don't see partial results.

---

# STEP 3 — Replay extension + orphan sweep + goldens + starters + Phase Tests

**Goal**: ship `_replay.py` extension for the two new event kinds + nested resume + orphan-sweep update + goldens + two starter workflows + Phase Tests 1-8.

**Files created/modified:**

- `src/workflows/_replay.py` (modified) — handle `workflow.node.called_workflow` + `_completed` + nested `workflow.run.resumed`.
- `src/workflows/run_store.py` (or wherever orphan sweep lives, from Phase 3.1 Step 4) — extension: orphaned children → `failed` with `reason="orphaned_child"`.
- `bundles/kolsch/workflows/nested-greeting.yaml` (new) — parent calls `hello-paused`.
- `bundles/kolsch/workflows/parallel-tickets.yaml` (new) — map over tickets dispatching classifier.
- `tests/golden/runs/` (new files) — paired JSONLs for the new scenarios.
- `tests/component/test_workflow_composition_e2e.py` (new) — Phase Tests 1-8 wired.

**Effort**: ~2 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the replay extension + orphan sweep + goldens + starters + Phase Tests.

**What to implement:**

1. **`_replay.py` extension**:

   ```python
   elif kind == "workflow.node.called_workflow":
       # Mark the workflow-node invocation as having spawned a child.
       # The child_run_id is recorded for the upcoming resume cascade.
       started[event["node_name"]] = event["invocation_id"]
       # No state mutation; this is a forensics marker.

   elif kind == "workflow.node.called_workflow_completed":
       # Forensics marker; replay does NOT close the open-invocation slot
       # (that's the workflow.node.completed event's job).
       pass
   ```

   For nested `workflow.run.resumed` (a child being resumed inside the parent's run): the parent's JSONL does NOT contain the child's resumed event — that's in the CHILD's JSONL. The parent's replay only walks the parent's events. So no parent-side handling needed.

2. **Operator-driven cascade test**: a Phase Test scenario where:
   - Parent runs; child pauses; both `WAITING_FOR_INPUT`.
   - Operator runs `kolsch workflows resume <leaf>`; leaf completes.
   - Operator runs `kolsch workflows resume <parent>`; parent's runner detects child SUCCEEDED via `store.read(child_run_id)`; harvests; parent completes.

   The runner's resume-detection branch (per spec §4.6.1.3) is the load-bearing path. Implement it:

   ```python
   # Inside WorkflowSubRunner.run(), early:
   # On resume, the parent's replay restored captured state, including
   # which workflow-node invocations have spawned children. We detect a
   # spawned child by looking for a prior `workflow.node.called_workflow`
   # event in the JSONL (the replay marker). If present AND the child is
   # paused, raise. If present AND the child terminated, harvest.
   if ctx.is_resume_mode:
       child_run_id = <look up from replay markers>
       if child_run_id is not None:
           child_run = self._store.read(child_run_id)
           if child_run.status == WorkflowRunStatus.WAITING_FOR_INPUT:
               raise WorkflowNodeFailed(
                   node=node_name, invocation_id=str(ctx.invocation_id),
                   cause=ValueError(
                       f"child run {child_run_id} is still paused; "
                       f"resume it first with `kolsch workflows resume {child_run_id}`"
                   ),
               )
           if child_run.status == WorkflowRunStatus.SUCCEEDED:
               # Harvest from the child's events + return.
               return _harvest_from_child_jsonl(child_run, self._store)
           # FAILED / ABORTED / TIMED_OUT
           raise WorkflowNodeFailed(...)
   ```

   `ctx.is_resume_mode` is a new NodeContext attribute set by `IterativeExecutor.resume(...)`. The executor's `_continue_worklist` populates it.

3. **Orphan-sweep extension**:

   ```python
   def reconcile_orphans(...) -> None:
       for run_id in <all runs>:
           run = store.read(run_id)
           if run.status == WorkflowRunStatus.WAITING_FOR_INPUT:
               continue  # Phase 3.4 — paused runs are NOT orphans
           if run.status != WorkflowRunStatus.RUNNING:
               continue
           if not _pid_alive(run.pid):
               if run.parent is not None:
                   # Phase 3.7 — orphaned child whose parent is dead.
                   reason = "orphaned_child"
               else:
                   reason = "orphaned"
               store.update_status(run_id, WorkflowRunStatus.FAILED, reason=reason)
               # append synthetic workflow.run.failed event
   ```

4. **Starter workflows**:

   `bundles/kolsch/workflows/nested-greeting.yaml`:
   ```yaml
   inputs:
     name: { type: str }
   nodes:
     prep:
       type: kolsch/nodes/bash
       command: "echo 'preparing greeting for {{ inputs.name }}'"
     sub:
       type: kolsch/nodes/workflow
       ref: kolsch/workflows/hello-paused
     wrap:
       type: kolsch/nodes/bash
       command: "echo 'wrapped: {{ nodes.sub.<terminal>.<key> }}'"
   graph:
     start: prep
     edges:
       - { from: prep, to: sub, binds: { greeting: "hi" } }
       - { from: sub, to: wrap }
   ```

   `bundles/kolsch/workflows/parallel-tickets.yaml`:
   ```yaml
   inputs:
     tickets: { type: list }
   nodes:
     classify_one:
       type: kolsch/nodes/agent
       agent: kolsch/agents/classifier
     classify_all:
       type: kolsch/nodes/map
       apply: classify_one
       over: "{{ inputs.tickets }}"
       item_as: ticket_text
     count_urgent:
       type: kolsch/nodes/compute
       value: "{{ nodes.classify_all.results | selectattr('label', 'eq', 'urgent') | list | length }}"
   graph:
     start: classify_all
     edges:
       - { from: classify_all, to: count_urgent }
   ```

5. **Goldens** — capture all five new scenarios; canonicalize volatile fields.

6. **`test_workflow_composition_e2e.py`** — Phase Tests 1-8 wired.

**Step 3 Acceptance Criteria:**

3.1. `_replay.py` extension for two new event kinds.
3.2. Operator-driven resume cascade tested end-to-end.
3.3. Orphan-sweep extension correctly reconciles orphaned children.
3.4. Two starter workflows ship; both parse, validate, run end-to-end.
3.5. Five goldens at `tests/golden/runs/` cover nesting / cycle / depth / nested-pause / map.
3.6. `test_workflow_composition_e2e.py` covers Phase Tests 1-8.
3.7. Manual smoke: `nested-greeting` runs with pause→resume cascade; `parallel-tickets` runs with map outputs feeding compute.
3.8. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.2 — operator-driven cascade. RUN IT manually. The two-step `resume <leaf>` then `resume <parent>` is the user-facing contract; an automation regression here is silently wrong.
- AC 3.3 — orphan sweep distinction. Confirm `reason="orphaned_child"` for children whose parent crashed; `reason="orphaned"` for top-level runs.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 8 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.7 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  # 1. Nested workflow with pause cascade:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/nested-greeting --input name=Alice
  # ... pauses; note <leaf_run_id> and <parent_run_id>
  uv run kolsch --root /tmp/kolsch-fresh workflows resume <leaf_run_id> --input response=hi
  uv run kolsch --root /tmp/kolsch-fresh workflows resume <parent_run_id>
  # 2. Map over tickets:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/parallel-tickets \
      --input 'tickets=["server down urgent","add a button","fix login urgent"]'
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl | grep iteration_index
  ```
