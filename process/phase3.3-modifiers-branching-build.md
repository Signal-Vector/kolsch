# [SPEC-004] Phase 3.3 — Branching runtime + multi-input bind validation

**Type:** spec
**Doc #:** 004
**Status:** Draft
**Created:** 2026-06-26

---

## Glossary

| Term | Meaning |
| --- | --- |
| branch dispatch | Filtering a branchable node's outgoing edges by their `branch: true/false` field against the node's computed branch value. |
| branchable node | `bash` (via `exit_code == 0`) OR authored Python `@node` returning `bool` or `tuple[bool, BaseModel]`. |
| inputs.* namespace | The per-node Jinja scope name that exposes the receiver's edge-bound `binds:` keys. New runtime in 3.3. |
| pruning | Marking a node as "will never fire" because all its inbound edges were branched-not-taken. Pruned nodes emit `workflow.node.pruned`. |
| strict-mode bind validation | Pydantic v2 `model_validate(..., strict=True)` against the receiver's typed `Inputs(BaseModel)` model. Raises `WorkflowBindTypeMismatch` on mismatch. |

---

## Background

Phase 3.0 shipped the parser + validator. Phase 3.1 shipped MORE of the modifier surface than the original 3.3 roadmap entry assumed:

- `when:` gate (`src/workflows/executor.py:617`) — shipped.
- `retry:` (`src/workflows/builtins/retry.py`) — shipped (bare loop, no tenacity).
- Multi-input edge STRUCTURE + AND-strict semantics (executor.py:369-375) — shipped.
- `WorkflowBindCollision` parse-time check (validator.py:516-530) — shipped.
- Validator branching invariants (validator.py:540-590) — shipped.

What's LEFT for Phase 3.3:

1. **Runtime branch dispatch** — `src/workflows/executor.py` never reads `edge.branch` (grep-confirmed). The dispatch must read it, compute the source's branch value, filter outgoing edges, and cascade pruning.
2. **`inputs.*` Jinja namespace at receivers** — `_render_jinja()` (executor.py:658-670) exposes only workflow-level inputs; edge-bound data is invisible to a receiver's CONFIG STRINGS (it reaches the runner via the `bound_inputs` kwarg). Per master spec §6.4, built-in node config like `command: "echo {{ inputs.context }}"` must see the edge-bound `context` key.
3. **Runtime `WorkflowBindTypeMismatch`** — the class exists (`src/workflows/errors.py:490-528`) but is never raised. The runtime strict-mode validation hook is missing.
4. **Pruning cascade + `workflow.node.pruned` event** — new mechanism; the SINGLE new event-kind addition to master spec §11.1.

Phase 3.3 is a **runtime-only phase**: zero parser changes, zero validator changes, zero new error classes. The validator's parse-time branching invariants already block ill-formed YAML; Phase 3.3 is the executor honoring what the validator promised.

The verification gate is three fixtures: a multi-input fan-in workflow whose receiver consumes `{{ inputs.* }}` keys; a bash node whose non-zero exit routes to a false-branch; an authored Python `@node` returning `tuple[bool, BaseModel]` whose branch value drives downstream edge selection.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**, each with its own Coder and Reviewer cycle.

Related docs:

- `process/phase3.3-modifiers-branching-spec.md` — the master design spec (§6.4 inputs namespace, §6.5 branch value extraction, §6.6 edge filtering + pruning, §8.5.5 bash semantics, §11.1 event extension).
- `process/phase3.1-executor-build.md` — predecessor build plan; particularly the AND-strict rules in Step 1 + the event-emission contract in Step 5.
- `process/phase3.0-workflow-spec.md` — master Phase 3.0 design (§2.5 branching, §4.0 output schema table, §5.4 @node return types, §6.4 binds + inputs namespace, §7.3 branching invariants).

---

## Scope

**In scope (delivered across the 4 steps):**

- `IterativeExecutor._render_jinja(expression, *, inputs_overlay=None)` — per-node overlay; the receiver's edge-bound dict shadows workflow-level inputs.
- Strict-mode bind validation after `_render_bound_inputs(node_name)`, raising `WorkflowBindTypeMismatch` with `file`/`line` populated from the receiver's YAML position.
- `src/workflows/branching.py` (new module) — `compute_branch_value(node_def, captured) -> bool | None`.
- Edge filtering in the worklist loop — after the runner returns, filter outgoing edges by branch value; cascade pruning with reverse-reachability.
- `workflow.node.pruned` event emission (the SINGLE new event kind).
- `workflow.node.completed` payload extension — branchable nodes carry `branch_value: bool` in payload.
- Bash success-vs-failure semantics revision — non-zero exit is success when at least one outgoing edge has `branch: not None`; otherwise abort (existing Phase 3.1 behavior).
- Bash `command:` rendering threaded through the executor's `_render_jinja(..., inputs_overlay=bound_inputs)` so the bash node's `command:` config sees the edge-bound `inputs.*` namespace.
- Authored Python `@node` capture shape confirmation — verify `CatalogNodeRunner` returns `{"value": bool}` (bool return) or `{"value": bool, **model.model_dump()}` (tuple return); adjust if drift.
- Three golden trace fixtures at `tests/golden/runs/`: branching bash, branching authored Python, multi-input fan-in with mixed branches.
- Starter workflow `bundles/kolsch/workflows/branching-demo.yaml` runs end-to-end.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- `branch` node type runtime (Phase 3.6). The edge `branch:` field (this phase) is orthogonal to the `kolsch/nodes/branch` node type.
- `filter` / `compute` (Phase 3.6).
- `user_input` (Phase 3.4).
- `read_file` / `write_file` / `list_files` / `set_state` / `delete_state` (Phase 3.5).
- `map` / `workflow` sub-workflow (Phase 3.7).
- Edge-level `when:` gate runtime — `EdgeDef.when` is parsed; runtime is deferred to Phase 3.6 (decided at Coordinator's call in Step 1; the recommended default).
- Concurrent branch execution (Phase 3.10).

**Decisions already locked (do NOT relitigate):**

- `branch:` on edges takes literal `true`/`false` only.
- Bash branchability is structural (parser-validated); non-zero exit is success-of-false-arm only when `branch: false` outgoing edge exists.
- Authored Python branchability is signature-derived (return type `bool` or `tuple[bool, BaseModel]`); the validator already enforces `branch:` edges only from branchable sources.
- `when:`-skip cascades through ALL outgoing edges (preserved from Phase 3.1).
- Pruning cascades via reverse-reachability; a node is pruned IFF every inbound edge is pruned-or-sourced-from-pruned.
- No `workflow.node.skipped` event; `workflow.node.pruned` IS its own event kind because pruning is a structural decision, not a per-node modifier.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2 (strict-mode for bind validation), Jinja2 (`JINJA_ENV` at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py:499` — `EdgeDef.branch` field exists; runtime reads it.
- `src/workflows/parser.py:117-176` — per-built-in `Inputs(BaseModel)` models. Used by strict-mode validation.
- `src/workflows/executor.py:380-490` — the worklist body. Phase 3.3 inserts validation + branching + pruning here.
- `src/workflows/executor.py:617-631` — `_when_holds()` already shipped; do NOT modify.
- `src/workflows/executor.py:637-670` — `_render_bound_inputs` + `_render_jinja`; Phase 3.3 extends `_render_jinja` with `inputs_overlay=`.
- `src/workflows/builtins/bash.py:141-145` — `BashRunner.run`; Phase 3.3 may adjust to route `command:` rendering through the executor (or accept `inputs_overlay`).
- `src/workflows/builtins/catalog_node_runner.py:260-275` — confirms canonical `{"value": bool}` / `{"value": bool, **model}` capture shape.
- `src/workflows/errors.py:490-528` — `WorkflowBindTypeMismatch`; reused, no class change.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-branch-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- Bash node with `branch: true` + `branch: false` outgoing edges: zero exit fires true-branch; non-zero exit fires false-branch; run continues either way.
- Bash node with NO `branch:` edges: non-zero aborts the run (Phase 3.1 behavior preserved).
- Authored Python `@node` returning `bool` drives branch dispatch.
- Authored Python `@node` returning `tuple[bool, BaseModel]` drives branch dispatch on the bool AND exposes the model's fields as captured output.
- Multi-input receiver consumes `{{ inputs.<key> }}` in its config strings; rendering pulls from edge-bound `binds:` first, workflow-level inputs second.
- Receiver whose bound inputs fail Pydantic strict-mode raises `WorkflowBindTypeMismatch` with `expected_type` + `actual_type` populated; run aborts.
- Pruned nodes emit `workflow.node.pruned`; do NOT emit `workflow.node.started`/`completed`.
- Pruned subgraph correctly cascades; nodes with at least one fired inbound edge are NOT pruned.
- Multi-input receiver with mixed fired/pruned inbound edges fires with the fired edges' binds only; pruned bind keys are absent from `inputs.*` (Jinja `Undefined`).
- `workflow.node.completed` for branchable nodes carries `branch_value`.
- Workflow's inferred output dict carries `None` for any pruned terminal node.
- `when:`-skip cascades via the same pruning machinery (structural equivalence).
- No regressions: non-branching workflows produce byte-identical JSONLs.

---

## Phase-level Integration Tests

### Phase Test 1: Bash exit-code branching
- **Run:** `uv run kolsch --root /tmp/kolsch-branch-test workflows run kolsch/workflows/branching-demo`
- **Expect:** exit 0; bash's `completed` carries `branch_value=False`; false-branch downstream fires; true-branch downstream pruned.
- **Fail means:** branch dispatch broken OR pruning cascade broken.

### Phase Test 2: Authored Python bool branching
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_python_bool_branching -v`
- **Expect:** `@node` returning `True` triggers true-branch edges; false-branch pruned.
- **Fail means:** `compute_branch_value` extraction wrong OR capture shape regressed.

### Phase Test 3: Multi-input + inputs.* namespace
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_multi_input_namespace -v`
- **Expect:** receiver with `command: "echo {{ inputs.greeting }} from {{ inputs.source }}"` renders both keys correctly.
- **Fail means:** `_render_jinja` scope composition broken.

### Phase Test 4: Strict-mode bind validation
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_bind_type_mismatch_raises -v`
- **Expect:** binding `attempt: "5"` into receiver expecting `attempt: int` raises `WorkflowBindTypeMismatch`; run aborts.
- **Fail means:** strict-mode not wired OR exception fields wrong.

### Phase Test 5: Partial fan-in
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_pruning_with_partial_fan_in -v`
- **Expect:** receiver with one fired + one pruned inbound edge fires with the fired edge's binds only.
- **Fail means:** pruning cascade incorrectly marks receiver as pruned.

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green; non-branching JSONLs byte-identical (modulo timestamps).

---

## Coordinator Prompt

> You are the strategic decision-maker for this task. You do not write or review code.
> Your job is to drive the 4-step Coder/Reviewer flow, make calls when there is ambiguity
> or disagreement, and unblock the work when it stalls.

**Your task:**

Ship Phase 3.3 of the kolsch workflow system through 4 sequential Coder/Reviewer cycles. This is a **runtime-only** sub-phase: the parser and validator already prepared everything; Phase 3.3 is the executor reading the prepared data correctly.

**Authoritative references:**

- THIS spec: `process/phase3.3-modifiers-branching-build.md`.
- Master design spec: `process/phase3.3-modifiers-branching-spec.md` — §6.4 (inputs namespace), §6.5 (branch value), §6.6 (filtering + pruning), §8.5.5 (bash semantics), §11.1 (event extension).
- Phase 3.1 spec: `process/phase3.1-executor-spec.md` — §8.1 AND-strict rules + §11.2 event ordering.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §2.5, §4.0, §5.4, §6.4, §7.3, §12.1.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `inputs.*` namespace + strict-mode bind validation. `_render_jinja(..., inputs_overlay=)`; route bash `command:` through the new scope. Raise `WorkflowBindTypeMismatch` from the worklist on Pydantic strict failure. NO branching yet. | ~1.5 days |
| **Step 1** | `compute_branch_value(node_def, captured)` helper at `src/workflows/branching.py` + edge filtering at fire time + bash success-vs-failure revision. Branchable bash with `branch:` edges + non-zero exit succeeds. NO pruning cascade yet — pruned edges are silently skipped (single-level prune only; the validator's invariants prevent ill-formed cases at v1 of Step 1). | ~1.5 days |
| **Step 2** | Pruning cascade via reverse-reachability + `workflow.node.pruned` event emission + `branch_value` in `workflow.node.completed` payload. Multi-input partial-fan-in semantics. | ~1.5 days |
| **Step 3** | Golden trace fixtures (3) at `tests/golden/runs/` + starter `bundles/kolsch/workflows/branching-demo.yaml` runs end-to-end + Phase Tests 1-5. | ~1.5 days |

**Flow per step:**

1. Hand the Coder the section titled **"Step N — Coder Prompt"** verbatim. Coder reads it + the master spec sections it references.
2. Coder implements + replies "Step N Complete — [summary]", "Blocked — ...", or "Decision needed — ...".
3. On Complete: hand the Reviewer the section titled **"Step N — Reviewer Prompt"** verbatim. Reviewer verifies + replies "Step N Approved" or "Changes requested — ...".
4. On Approved: advance to Step N+1. On Changes requested: send the change list back to the Coder.
5. After Step 3 is Approved: run the **Phase-level Integration Tests** above. If all pass, mark this spec Status: Done and annotate `phase3.x-roadmap.md`'s Phase 3.3 line with shipping commit SHA.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision (truthy/falsy `branch:`, `tenacity` swap-in, edge-`when:` runtime in scope) → REJECT, cite the spec, restate.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append to THIS spec, re-brief.
- Coder discovers Phase 3.0/3.1 contract was wrong and needs amendment → flag as separate patch on this branch; do NOT widen Phase 3.3's scope.
- Scope creep (Phase 3.6 branch-node-type sneaking in, `filter`/`compute` runtime, Phase 3.4 user_input) → REJECT and pull back.
- Edge-level `when:` runtime — the recommended call is DEFER to Phase 3.6 unless the Coder presents a clean fit; the edge `when:` runtime requires its own event-emission story and the current scope already covers the master-spec branching contract.

**Done when:** All 4 steps Approved by Reviewer AND all 6 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.3 line annotated with the shipping SHA.

---

# STEP 0 — `inputs.*` Jinja namespace + strict-mode bind validation (PREREQ)

**Goal**: ship the per-node `inputs.*` namespace at receivers (overlay onto workflow-level inputs) AND the runtime strict-mode validation of edge-bound dicts against each receiver's typed `Inputs(BaseModel)`. NO branch dispatch yet — Steps 1+2 own that. This prereq unblocks branching by ensuring the data plumbing the dispatcher will read is correctly populated.

**Files created/modified:**

- `src/workflows/executor.py` (modified) — `_render_jinja(expression, *, inputs_overlay=None)` signature; bash config-rendering routed through the overlay; strict-mode validation hook after `_render_bound_inputs(node_name)`.
- `src/workflows/builtins/bash.py` (modified) — `BashRunner.run` receives `bound_inputs` (already) AND consults the executor for config rendering (new pattern: pass `inputs_overlay` into the runner's render call).
- `src/workflows/_inputs_models.py` (new helper) — `_inputs_model_for(node_def) -> type[BaseModel]` returning the receiver's `Inputs` model. For built-ins, dispatch by `node_def.type`. For authored Python @nodes, introspect the catalog runner's recorded input model.
- `tests/component/test_workflow_inputs_namespace.py` (new) — coverage for the overlay scope.
- `tests/component/test_workflow_bind_validation.py` (new) — coverage for `WorkflowBindTypeMismatch` raises.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> You are implementing Step 0 of a 4-step phase. PREREQ that gates Steps 1-3.
> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the per-node `inputs.*` Jinja namespace and the runtime strict-mode bind validation. NO branching yet.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.3-modifiers-branching-build.md` — Step 0.
2. `process/phase3.3-modifiers-branching-spec.md` — §6.4 (full).
3. `src/workflows/executor.py:637-670` — the existing `_render_bound_inputs` + `_render_jinja`.
4. `src/workflows/parser.py:117-176` — typed `Inputs` models (BashInputs, NoopInputs, etc.).
5. `src/workflows/builtins/bash.py:141` — `BashRunner.run` (where `command:` is rendered today).
6. `src/workflows/errors.py:490-528` — `WorkflowBindTypeMismatch` (existing class; reuse, do NOT modify).

**What to implement:**

1. **`_render_jinja` extension** in `src/workflows/executor.py`:

   ```python
   def _render_jinja(
       self,
       expression: str,
       *,
       inputs_overlay: dict[str, Any] | None = None,
   ) -> Any:
       template = JINJA_ENV.from_string(expression)
       return template.render(
           nodes=self._captured,
           state=self._state,
           inputs={
               **dict(self._run.inputs),
               **(inputs_overlay or {}),
           },
       )
   ```

   Every existing caller of `_render_jinja` (e.g., the bind-rendering loop in `_render_bound_inputs` and the `when:` evaluator) continues to work — `inputs_overlay=None` preserves Phase 3.1 behavior.

2. **`_inputs_model_for` helper** — new module `src/workflows/_inputs_models.py`:

   ```python
   def _inputs_model_for(node_def: NodeDefUnion) -> type[BaseModel] | None:
       """Return the receiver's Inputs(BaseModel) model, or None if N/A."""
       # Built-in dispatch by type string.
       _BUILTIN_TO_INPUTS = {
           "kolsch/nodes/bash": BashInputs,
           "kolsch/nodes/noop": NoopInputs,
           "kolsch/nodes/agent": AgentInputs,
           ...  # every existing built-in
       }
       type_ref = node_def.type
       if type_ref in _BUILTIN_TO_INPUTS:
           return _BUILTIN_TO_INPUTS[type_ref]
       # Authored Python @node — defer to catalog runner introspection.
       return None  # Step 0 returns None for authored nodes; Step 0 validation only runs on built-ins.
   ```

   **Decision needed flag:** authored Python `@node` strict-mode validation requires accessing the catalog runner's introspected `Inputs` model. Three options:
   - (a) Ship Step 0 with built-in-only validation; defer authored-Python strict-mode to Step 1 alongside the branching work.
   - (b) Add a method `CatalogNodeRunner.inputs_model_for(catalog_entry)` and call it from `_inputs_model_for`.
   - (c) Walk the catalog entry's recorded import path and introspect the `@node` directly.

   **Recommended:** (a). The runtime check for built-ins covers most of the surface; the authored-Python path joins it in Step 1's branch dispatcher (which already touches the catalog runner). Document the decision in `_inputs_models.py`'s docstring.

3. **Strict-mode validation hook** — in `src/workflows/executor.py`, inside the worklist body, AFTER `bound_inputs = self._render_bound_inputs(node_name)` and BEFORE the runner is invoked:

   ```python
   inputs_model = _inputs_model_for(node_def)
   if inputs_model is not None:
       try:
           inputs_model.model_validate(bound_inputs, strict=True)
       except ValidationError as exc:
           first = exc.errors()[0]
           bind_key = str(first["loc"][0]) if first["loc"] else "<unknown>"
           expected = first["type"]
           actual_value = bound_inputs.get(bind_key, "<missing>")
           actual = type(actual_value).__name__ if actual_value != "<missing>" else "missing"
           raise WorkflowBindTypeMismatch(
               file=<receiver_file>,
               line=<receiver_line>,
               node=node_name,
               bind=bind_key,
               expected_type=str(expected),
               actual_type=str(actual),
           ) from exc
   ```

   The `<receiver_file>` and `<receiver_line>` come from the parsed `NodeDef`'s position record. If parser doesn't attach positions, populate `file=str(self._workflow.source_path)` and `line=0`; the master spec's strict-no-code Fix portion still holds. The exception is caught by the existing worklist try/except (executor.py:443), which emits `workflow.node.failed` then `workflow.run.failed` per Phase 3.1.

4. **Bash config rendering through overlay** — in `src/workflows/builtins/bash.py`, the runner currently renders `command:` against the executor's `_render_jinja` (via dependency injection? Check the current path). Whatever the path, ensure the bash runner's `command:`/`cwd:`/`env:` rendering uses `inputs_overlay=bound_inputs`. The cleanest path:
   - Add `inputs_overlay: dict[str, Any] | None = None` as a kwarg on the runner's render-config helper.
   - Executor calls the runner with `bound_inputs` (already passed via the protocol's `bound_inputs` arg); the runner internally calls back to the executor for rendering, threading `inputs_overlay=bound_inputs`.
   - If the runner currently renders via a closure or direct `JINJA_ENV` call, refactor minimally to route through the executor.

5. **Test fixtures:**

   `tests/component/test_workflow_inputs_namespace.py`:
   - **Overlay rendering:** workflow with two-edge fan-in into a bash node; `command: "echo {{ inputs.greeting }} {{ inputs.source }}"`. Assert the rendered command interpolates both keys correctly.
   - **Workflow-level fallback:** workflow with NO inbound edges to the start node; start node's config references `{{ inputs.greeting }}` from the workflow's inputs. Assert it renders correctly.
   - **Edge-overlay shadows workflow-level:** workflow input `greeting: "hello"`; edge binds `greeting: "hi from edge"`. Assert the rendered value is `"hi from edge"`.

   `tests/component/test_workflow_bind_validation.py`:
   - **Type mismatch raises:** receiver expects `attempt: int`; edge binds `attempt: "{{ '5' }}"` (string). Assert `WorkflowBindTypeMismatch` raised with `expected_type` containing "int" and `actual_type == "str"`.
   - **Extra key rejected:** receiver has `extra="forbid"`; edge binds an unknown key. Assert `WorkflowBindTypeMismatch` raised (or a clear error — confirm the model surfaces extras as Pydantic errors).
   - **Empty binds OK for models with no required fields:** noop receiver, empty binds; no error.
   - **Validation runs BEFORE the runner:** stub a runner that records invocation; trigger a type mismatch; assert the runner was NOT called (proves validation is the gate).

**Step 0 Acceptance Criteria:**

0.1. `_render_jinja(expression, *, inputs_overlay=None)` extended; `inputs_overlay=None` preserves Phase 3.1 behavior.
0.2. `_inputs_model_for(node_def)` returns the correct `Inputs` model for every built-in type; authored Python returns `None` (deferred to Step 1).
0.3. After `_render_bound_inputs`, the worklist invokes `inputs_model.model_validate(bound_inputs, strict=True)`; mismatch raises `WorkflowBindTypeMismatch` with `file`/`line`/`node`/`bind`/`expected_type`/`actual_type` populated.
0.4. Bash runner's `command:`/`cwd:`/`env:` rendering uses `inputs_overlay=bound_inputs`; `{{ inputs.<bind_key> }}` in a bash command resolves correctly.
0.5. `WorkflowBindTypeMismatch` is caught by the existing failure path and surfaces as `workflow.node.failed` + `workflow.run.failed`.
0.6. Type mismatch ABORTS the run (current Phase 3.1 abort-on-failure rule).
0.7. Workflow-level `inputs` rendering is byte-identical for any node WHOSE bound_inputs is empty (no overlay) — backward compat.
0.8. NO branch dispatch shipped yet (Step 1 owns that).

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Existing non-branching workflows produce byte-identical JSONLs
- [ ] `uv run pytest tests/ -q` green
- [ ] `grep edge.branch src/workflows/executor.py` returns ZERO matches (Step 1 owns the dispatcher)

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.4 — bash `command:` actually renders with the overlay. Read the runner source. If `command:` rendering happens inside `BashRunner.run` against a closure'd `JINJA_ENV`, the overlay won't reach it without explicit threading.
- AC 0.5 — exception remap preserves `__cause__`. The `raise WorkflowBindTypeMismatch(...) from exc` pattern.
- AC 0.7 — backward compat. Run a Phase 3.1 fixture workflow and diff its JSONL byte-for-byte (modulo timestamps + IDs) against a golden from before this step. Any drift in non-branching scenarios is a regression.
- AC 0.8 — Step 1 ownership. Grep `src/workflows/executor.py` for `edge.branch` — ZERO matches at this step. The branching dispatcher lives in Step 1.

---

# STEP 1 — Branch value extraction + edge filtering at fire time

**Goal**: ship the `compute_branch_value` helper and the edge-filtering logic in the worklist body. Revise bash success-vs-failure semantics. NO pruning cascade yet — pruned edges are silently skipped (single-level only); their downstream nodes will block forever waiting on AND-strict, which Step 2 fixes by adding the cascade. Step 1's tests use fixtures whose pruned subgraphs are immediately terminal (no downstream nodes), so the cascade gap isn't observable.

**Files created/modified:**

- `src/workflows/branching.py` (new) — `compute_branch_value(node_def, captured) -> bool | None`.
- `src/workflows/executor.py` (modified) — edge filtering in the worklist; bash success-vs-failure revision; `branch_value` in `workflow.node.completed` payload.
- `src/workflows/_inputs_models.py` (modified) — authored Python `@node` introspection branch added.
- `src/workflows/builtins/catalog_node_runner.py` (modified if drift) — confirm `{"value": bool}` / `{"value": bool, **model}` capture shape.
- `tests/component/test_workflow_branching.py` (new) — coverage for branch value extraction + single-level edge filtering.

**Effort**: ~1.5 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the branch value extraction + edge filtering. NO cascade. NO pruning event. Just the gate that decides which edges fire.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.3-modifiers-branching-build.md` — Step 1.
2. `process/phase3.3-modifiers-branching-spec.md` — §6.5 (branch value extraction), §6.6 (filtering — IGNORE cascade for Step 1), §8.5.5 (bash semantics).
3. `src/workflows/executor.py:478-490` — the worklist's post-runner-success path.
4. `src/workflows/builtins/catalog_node_runner.py:260-275` — confirm capture shape.

**What to implement:**

1. **`src/workflows/branching.py`** — new module:

   ```python
   def compute_branch_value(
       node_def: NodeDefUnion,
       captured: dict[str, Any],
   ) -> bool | None:
       """Phase 3.3 — extract the branchable boolean for edge dispatch.
       
       Returns None for non-branchable nodes.
       """
       type_ref = node_def.type
       if type_ref == "kolsch/nodes/bash":
           return bool(captured.get("exit_code") == 0)
       # Authored Python: branchable iff captured has a "value" key
       # whose value is a bool.
       value = captured.get("value")
       if isinstance(value, bool):
           return value
       return None
   ```

   The check `isinstance(value, bool)` is intentional — `int`/`str` "truthy" values don't accidentally become branchable. The validator already enforces that `branch:` edges originate from branchable sources; the runtime guard is defensive.

2. **Edge filtering in the worklist body** — in `src/workflows/executor.py`, AFTER the runner returns successfully:

   ```python
   self._captured[node_name] = dict(output)
   self._completed_or_skipped.add(node_name)
   
   # Phase 3.3 — branch dispatch.
   branch_value = compute_branch_value(node_def, output)
   for edge in self._outgoing_edges(node_name):
       if edge.branch is None:
           self._enqueue_if_ready(edge.to)
           continue
       if branch_value is None:
           # Validator should have prevented this; defensive raise.
           raise WorkflowNodeFailed(
               node=node_name,
               invocation_id=str(inv_id),
               cause=ValueError(
                   f"node {node_name!r} has branch edge but produced no branch value"
               ),
           )
       if edge.branch == branch_value:
           self._enqueue_if_ready(edge.to)
       # else: edge is pruned at this step; Step 2 handles cascade.
   ```

   Step 1's "pruned" edges are silently skipped — no event emitted, no cascade. The downstream nodes hang in the worklist queue forever (or until cancel). Step 1's fixtures use immediately-terminal branches (no downstream) so the gap isn't observable. Document this in a `TODO(step-2)` comment so the next step's coder sees the intentional gap.

3. **Bash success-vs-failure revision** — the worklist's existing failure path catches any exception from the runner and aborts. Phase 3.3 adds a wrapper: for bash nodes whose outgoing edge set contains at least one edge with `branch is not None`:

   ```python
   def _bash_treats_nonzero_as_success(self, node_name: str) -> bool:
       """True iff this bash node has at least one branch: edge."""
       for edge in self._outgoing_edges(node_name):
           if edge.branch is not None:
               return True
       return False
   ```

   Modify the bash runner's behavior (or wrap at the executor) so non-zero exit returns the captured `{stdout, stderr, exit_code}` instead of raising, WHEN `_bash_treats_nonzero_as_success` is True. When False, current Phase 3.1 behavior (non-zero → `WorkflowNodeFailed`) preserved.

   The cleanest wiring: catch the bash runner's non-zero-exit exception at the executor level (introduce a typed `BashNonZeroExit` exception in `src/workflows/errors.py` raised by `BashRunner`) and handle:
   ```python
   except BashNonZeroExit as exc:
       if self._bash_treats_nonzero_as_success(node_name):
           output = exc.captured  # {stdout, stderr, exit_code}
           # fall through to the success path
       else:
           raise  # bubbles to the existing failure path → WorkflowNodeFailed
   ```

4. **`branch_value` in `workflow.node.completed` payload** — when `branch_value is not None`, add `"branch_value": branch_value` to the event payload:

   ```python
   completed_payload = {
       "node_name": node_name,
       "invocation_id": str(inv_id),
       "output": output,
   }
   if branch_value is not None:
       completed_payload["branch_value"] = branch_value
   if node_def.type == "kolsch/nodes/agent" and ctx.spawned_session_id is not None:
       completed_payload["spawned_session_id"] = str(ctx.spawned_session_id)
   ```

5. **`_inputs_model_for` extension for authored Python @nodes** — extend the helper to handle authored-Python by introspecting the `CatalogNodeRunner`'s recorded `Inputs` model. Method signature on the runner: `inputs_model_for(catalog_entry: AssetEntry) -> type[BaseModel] | None`. Used by `_inputs_model_for` when the type ref is a non-built-in 3-seg.

6. **Capture shape confirmation** — run an authored-Python fixture and assert `CatalogNodeRunner` returns:
   - For `bool` return: `{"value": <bool>}`.
   - For `tuple[bool, BaseModel]` return: `{"value": <bool>, **model.model_dump()}`.
   If the runner returns a different shape today, adjust the runner AND document the change in this build doc as a small drift correction.

7. **Tests in `tests/component/test_workflow_branching.py`**:
   - **Bash true-arm:** workflow `bash → x` where bash exits 0 and edge has `branch: true`. Assert x fires.
   - **Bash false-arm:** workflow `bash → y` where bash exits 1 and edge has `branch: false`. Assert y fires.
   - **Bash non-zero with no branch edge:** workflow `bash → z` (no branch). Bash exits 1. Assert run fails (existing behavior).
   - **@node bool:** authored `@node` returning `True` with edges `true → x` + `false → y`. Assert x fires, y queued-but-never-fires.
   - **@node tuple[bool, BaseModel]:** same with tuple return; assert the BaseModel fields land in `captured` so downstream can consume them.
   - **branch_value in payload:** assert `workflow.node.completed` for a branchable source carries `branch_value: bool`.
   - **branch_value absent for non-branchable:** noop or agent node's `completed` payload has NO `branch_value` key.

**Step 1 Acceptance Criteria:**

1.1. `compute_branch_value(node_def, captured)` returns the correct bool for bash + bool-@node + tuple-@node; returns `None` for noop / agent / non-branchable @node.
1.2. Edge filtering reads `edge.branch` and fires only matching arms; non-`branch:` edges fire regardless of source branchability.
1.3. Bash non-zero exit is SUCCESS when at least one outgoing edge has `branch is not None`; FAILURE otherwise (Phase 3.1 behavior).
1.4. `workflow.node.completed` payload carries `branch_value` for branchable sources; absent for non-branchable.
1.5. `_inputs_model_for` returns the correct model for authored Python @nodes via `CatalogNodeRunner` introspection.
1.6. Authored Python @node capture shape confirmed as `{"value": bool}` / `{"value": bool, **model}`.
1.7. Tests cover the 7 scenarios above.
1.8. NO `workflow.node.pruned` event yet (Step 2 owns it); pruned edges are silently skipped.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Step 1 fixtures use immediately-terminal branches (no downstream) so cascade-gap isn't observable
- [ ] `grep workflow.node.pruned src/workflows/` returns ZERO matches (Step 2 owns it)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.1 — the `isinstance(value, bool)` check is the load-bearing branchability guard. Without it, an `int` return value would accidentally become branchable. Read the source.
- AC 1.3 — bash semantics. Test fixture: bash exits 1 with `branch: false` edge present → run continues with the false-branch firing. Test fixture: bash exits 1 with NO `branch:` edge → run aborts. Both shapes must work.
- AC 1.4 — `branch_value` payload presence/absence by node type. Inspect a generated JSONL.
- AC 1.8 — Step 2 ownership. The intentional cascade gap MUST be marked with `TODO(step-2)`; grep confirms.

---

# STEP 2 — Pruning cascade + `workflow.node.pruned` event

**Goal**: ship the pruning cascade (reverse-reachability) AND the `workflow.node.pruned` event. Multi-input partial-fan-in semantics. After this step, branching workflows correctly handle arbitrary subgraph pruning.

**Files created/modified:**

- `src/workflows/executor.py` (modified) — `_pruned: set[str]` tracking; cascade after edge filtering; `workflow.node.pruned` event emission; AND-strict reads pruned-as-completion.
- `src/workflows/_events.py` (modified) — `WorkflowNodePrunedEvent` Pydantic model added to the event taxonomy.
- `tests/component/test_workflow_branching.py` (modified) — add cascade + partial-fan-in cases.

**Effort**: ~1.5 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the pruning cascade and the `workflow.node.pruned` event. The cascade extends from each pruned edge via reverse-reachability and stops at any node that has at least one fired incoming edge (partial-fan-in case).

**Authoritative references — read BEFORE coding:**

1. `process/phase3.3-modifiers-branching-build.md` — Step 2.
2. `process/phase3.3-modifiers-branching-spec.md` — §6.6 (full, including §6.6.1 partial-fan-in), §11.1 (event payload).
3. `process/phase3.1-executor-spec.md` §8.1 AND-strict rules (rule 1 — skip counts as completion).
4. `src/workflows/_events.py` — existing event Pydantic models; mirror the shape for the new event.

**What to implement:**

1. **`_pruned: set[str]` tracking** in `IterativeExecutor`:

   ```python
   class IterativeExecutor:
       def __init__(self, ...) -> None:
           ...
           self._pruned: set[str] = set()
   ```

   Pruned nodes are added to `_completed_or_skipped` AT THE SAME MOMENT they're added to `_pruned` so AND-strict's existing check (predecessors are completed-or-skipped before firing) treats pruned ancestors as decided.

2. **Cascade algorithm** — when an edge is pruned at Step 1's filter:

   ```python
   def _cascade_prune(
       self,
       *,
       newly_pruned_edges: list[EdgeDef],
       upstream_source: str,
       branch_value: bool,
   ) -> None:
       """Reverse-reachability cascade. A node is pruned IFF every
       inbound edge is pruned OR sourced from a pruned node."""
       candidates: list[str] = [e.to for e in newly_pruned_edges]
       while candidates:
           node_name = candidates.pop()
           if node_name in self._pruned:
               continue
           if node_name in self._completed_or_skipped:
               continue  # already fired or skipped via when:; no prune
           # Every inbound edge of this node must be from a pruned source
           # OR be itself a pruned-edge.
           if not self._all_inbound_pruned(node_name):
               continue  # at least one fired inbound edge; do NOT prune
           # Prune.
           self._pruned.add(node_name)
           self._completed_or_skipped.add(node_name)
           self._emit_pruned_event(
               node_name=node_name,
               upstream_source=upstream_source,
               branch_value=branch_value,
           )
           # Cascade to this node's outgoing edges' targets.
           candidates.extend(e.to for e in self._outgoing_edges(node_name))
   ```

   The `_all_inbound_pruned(node_name)` helper checks every inbound edge: is the source in `_pruned`, OR was the edge itself pruned at the source's branching step? Maintaining a `_pruned_edges: set[tuple[str, str]]` (source, target) makes the check O(inbound_edges).

3. **`WorkflowNodePrunedEvent`** in `src/workflows/_events.py`:

   ```python
   class WorkflowNodePrunedEvent(BaseEvent):
       kind: Literal["workflow.node.pruned"] = "workflow.node.pruned"
       payload: WorkflowNodePrunedPayload
   
   class WorkflowNodePrunedPayload(BaseModel):
       node_name: str
       reason: Literal["branch_not_taken"] = "branch_not_taken"
       upstream_source: str
       branch_value: bool
   ```

   Mirror the shape of other workflow.node.* events. Common envelope fields per master spec §11.3 (`workflow_run_id`, `trace_id`, `parent_event_id`, `ts`) are inherited from the base event class.

4. **Emit pruned events** — `_emit_pruned_event(...)` calls `emit_event(self._store, WorkflowNodePrunedEvent(...))`. The `parent_event_id` is the SOURCE's `workflow.node.completed` event id (already tracked in Phase 3.1's `_node_started_event_id` map; add a `_node_completed_event_id` map if not already there).

5. **AND-strict treats pruned as completion** — the existing AND-strict check reads `_completed_or_skipped`. Since pruned nodes are added there at prune time, no additional logic needed.

6. **Tests in `tests/component/test_workflow_branching.py`** — add:

   - **Single-level cascade:** `bash → x → y`. Bash branches false; edge to `x` has `branch: true`. Assert `x` and `y` both pruned; both emit `workflow.node.pruned`; x's event references bash as `upstream_source`; y's references x (or bash?). Decide: y's `upstream_source` is `x` (the direct predecessor) — document.
   - **Multi-input partial-fan-in:** `A → R, B → R`. A branches false to R; B fires normally. Assert R fires with B's binds only; assert R is NOT pruned; assert `workflow.node.pruned` is NOT emitted for R.
   - **Partial-fan-in missing required key:** R's `Inputs` model requires a key that came only from A (pruned). Assert `WorkflowBindTypeMismatch` raises with `expected_type` from the model and `actual_type = "missing"`.
   - **Pruned terminal contributes null:** terminal node `t` is pruned. Assert workflow's inferred output dict has `"t": None`.
   - **`when:`-skip cascade:** node `s` has `when: false`; downstream `d` has only `s` as predecessor. Assert `d` is pruned via the same machinery (structural equivalence). Document: Phase 3.3 unifies `when:`-skip cascade with branch-not-taken cascade — the existing Phase 3.1 behavior is reimplemented under the new cascade for symmetry.

7. **Multi-input partial-fan-in `bound_inputs`** — when R fires after partial-fan-in, `_render_bound_inputs(R)` already iterates inbound edges and merges their binds. Pruned edges' binds are simply absent (the edge filter skipped them at the source's step). No extra logic needed.

**Step 2 Acceptance Criteria:**

2.1. `WorkflowNodePrunedEvent` shipped; emitted for every pruned node with documented payload.
2.2. Cascade correctly handles single-level pruning (every descendant of a pruned-only ancestor is pruned).
2.3. Cascade correctly handles multi-input partial-fan-in (a receiver with at least one fired inbound edge is NOT pruned).
2.4. AND-strict treats pruned nodes as completion (downstream receivers fire when all inbound sources are completed-or-skipped-or-pruned).
2.5. Workflow's inferred output dict carries `None` for any pruned terminal node.
2.6. `when:`-skip cascade unifies with branch-not-taken cascade (same code path).
2.7. Partial-fan-in receiver whose `Inputs` model requires a key from a pruned edge raises `WorkflowBindTypeMismatch` with `actual_type = "missing"`.
2.8. No regressions.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Cascade is O(graph size); no infinite loop on cyclic graphs (validator prevents cycles, but the cascade should be cycle-safe regardless via the `_pruned` membership check)
- [ ] `workflow.node.pruned` events have correct `parent_event_id`
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.3 — partial-fan-in. Read the cascade source: confirm `_all_inbound_pruned(node_name)` correctly returns False when at least one inbound edge was fired (not pruned).
- AC 2.4 — AND-strict integration. Run a multi-input fixture and inspect the worklist queue ordering.
- AC 2.5 — inferred output dict with pruned terminals. Run a fixture; assert the final JSONL `workflow.run.completed` payload has `null` for the pruned terminal.
- AC 2.6 — `when:`-skip cascade unification. Read the source: is the same `_cascade_prune` code path used? If not, document why two cascades coexist (acceptable, but flag).

---

# STEP 3 — Golden trace fixtures + starter demo + integration tests

**Goal**: ship reference JSONL goldens for the three branching scenarios + starter `branching-demo.yaml` runs end-to-end. Final integration tests.

**Files created/modified:**

- `bundles/kolsch/workflows/branching-demo.yaml` (new) — bash exit-code branching demo.
- `bundles/kolsch/nodes/classify.py` (new if needed) — authored `@node` returning `tuple[bool, BaseModel]` for Phase Test 2.
- `tests/golden/runs/` (new files):
  - `branching-bash.workflow.jsonl` — bash false-branch route.
  - `branching-python.workflow.jsonl` — authored @node tuple branch.
  - `branching-multi-input.workflow.jsonl` — partial-fan-in case.
- `tests/golden/runs_test.py` (modified) — add branching fixtures.
- `tests/component/test_workflow_branching_e2e.py` (new) — Phase Tests 1-5.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the golden trace suite + the starter demo + the Phase Tests.

**What to implement:**

1. **`bundles/kolsch/workflows/branching-demo.yaml`** — minimal bash branching:

   ```yaml
   inputs:
     check_cmd: { type: str }
   nodes:
     check:
       type: kolsch/nodes/bash
       command: "{{ inputs.check_cmd }}"
     on_success:
       type: kolsch/nodes/bash
       command: "echo 'check passed'"
     on_failure:
       type: kolsch/nodes/bash
       command: "echo 'check failed (recovered)'"
   graph:
     start: check
     edges:
       - { from: check, to: on_success, branch: true }
       - { from: check, to: on_failure, branch: false }
   ```

2. **`bundles/kolsch/nodes/classify.py`** — authored `@node` for the Python branching fixture:

   ```python
   from pydantic import BaseModel
   from workflows import node, NodeContext
   
   class ClassifyInputs(BaseModel):
       text: str
   
   class ClassifyOutputs(BaseModel):
       category: str
       score: float
   
   @node
   async def classify(inputs: ClassifyInputs, ctx: NodeContext) -> tuple[bool, ClassifyOutputs]:
       is_urgent = "urgent" in inputs.text.lower()
       return is_urgent, ClassifyOutputs(
           category="urgent" if is_urgent else "normal",
           score=0.95 if is_urgent else 0.10,
       )
   ```

3. **Three golden JSONL fixtures** under `tests/golden/runs/` — capture the workflow JSONL for each scenario; canonicalize volatile fields per Phase 3.1's substitution table.

4. **`runs_test.py` extension** — extend the parametrize list with the three branching fixtures.

5. **`test_workflow_branching_e2e.py`** — Phase Tests 1-5 per the master spec.

**Step 3 Acceptance Criteria:**

3.1. `bundles/kolsch/workflows/branching-demo.yaml` parses, validates, runs end-to-end.
3.2. `bundles/kolsch/nodes/classify.py` ships; @node decorator + signature + return type all validate.
3.3. Three golden JSONLs at `tests/golden/runs/`; `runs_test.py` asserts semantic equality.
3.4. `test_workflow_branching_e2e.py` covers Phase Tests 1-5.
3.5. Manual smoke: `uv run kolsch --root /tmp/kolsch-branch-test workflows run kolsch/workflows/branching-demo --input check_cmd="false"` succeeds; `on_failure` fires; `on_success` pruned.
3.6. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.5 — manual smoke. Run with `check_cmd="false"` AND `check_cmd="true"`. Inspect the JSONL for each: confirm `branch_value` payload + `workflow.node.pruned` events.
- AC 3.6 — non-branching workflows unchanged. Run an existing 3.1-era fixture and diff its JSONL.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 6 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.3 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/branching-demo --input check_cmd="false"
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl  # expect branch_value=False + on_failure fires + on_success pruned
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/branching-demo --input check_cmd="true"
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl  # expect branch_value=True + on_success fires + on_failure pruned
  ```
