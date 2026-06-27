# [SPEC-007] Phase 3.6 — Branch + Filter + Compute runtime

**Type:** spec
**Doc #:** 007
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| branch node | `kolsch/nodes/branch` — evaluates `condition:` (Jinja boolean), returns `{value: bool}`. ORTHOGONAL to the edge-level `branch:` field shipped Phase 3.3. |
| filter node | `kolsch/nodes/filter` — per-element predicate-based list reducer. Returns `{result: list[T]}`. |
| compute node | `kolsch/nodes/compute` — general-purpose Jinja expression returning `{value: <any>}`. Subsumes 8 aggregation node types via stock Jinja filters. |
| NativeEnvironment | Already shipped via `src/workflows/jinja_env.py:56-66` as `WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)` with `NativeCodeGenerator` + `native_concat`. Returns typed Python values for single-expression templates; falls back to strings for multi-segment templates. |
| aggregation recipes | The 8 dropped node types (`sum`/`count`/`min`/`max`/`sort`/`flatten`/`any`/`all`) expressible as `compute.value:` Jinja expressions per master spec §4.0 lines 234-245. |

---

## Background

Phases 3.0/3.1/3.3 prepared more of Phase 3.6's surface than the original roadmap entry assumed:

- **`NativeEnvironment` is already shipped** at `src/workflows/jinja_env.py:56-66`. The hybrid `WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)` uses `NativeCodeGenerator` + `native_concat` so single-expression templates return typed Python values while preserving sandbox semantics. **Phase 3.6 does NOT migrate the Jinja env.** The work is already done; this phase verifies behavior and adds regression coverage.
- **Parser models** (`BranchNodeDef`/`FilterNodeDef`/`ComputeNodeDef` at `src/workflows/parser.py:313-365`) — Phase 3.0 stubbed all three correctly. No parser changes needed.
- **`*Inputs(BaseModel)`** at parser.py:158-176 — all three exist with `extra="allow"`.
- **`_BUILTIN_TO_INPUTS`** at `src/workflows/_inputs_models.py:42-57` — already includes all three slots. Strict-mode bind validation runs uniformly.
- **`BUILTIN_PHASE_MAP`** at executor.py:82-102 — `branch`/`filter`/`compute` already mapped to `"3.6"`.
- **Validator branching invariants** at validator.py:540-616 — `BranchNodeDef` already in `branchable_names` + `require_both_arms`; `WorkflowBranchMissingArm` already enforced.
- **Edge dispatch via `compute_branch_value`** at executor.py:630-642 — already invoked after every node completion; agnostic to type.
- **`compute_branch_value(node_def, captured)`** at `src/workflows/branching.py:25-56` — already handles bash exit codes + authored Python `bool` returns. Phase 3.6 adds a one-case extension for `kolsch/nodes/branch`.

What's LEFT for Phase 3.6:

1. **Three small runners** at `src/workflows/builtins/{branch,filter,compute}.py`. Each is a Jinja render + return-shape narrow.
2. **`compute_branch_value` extension** — one new `if type_ref == "kolsch/nodes/branch"` case.
3. **`register_builtins(...)` extension** — three `registry.register(...)` calls; no new constructor kwargs (the runners are dependency-free).
4. **Triage walkthrough starter** + 8-recipe coverage + NativeEnvironment regression suite.

The verification gate is the master spec §6.0.1 triage walkthrough running end-to-end with both branch arms across two test runs, plus all 8 aggregation recipes verified as `compute.value:` expressions returning correctly-typed results.

**This spec breaks the phase into 3 sequential steps (Step 0 — Step 2)**, smaller than prior builds because the surface is small.

Related docs:

- `process/phase3.6-branch-filter-compute-spec.md` — master design spec (§4.4.1 / §4.5.1 / §4.6.3 elaborations, aggregation recipe table).
- `process/phase3.3-modifiers-branching-build.md` — the orthogonal edge-`branch:` runtime + `compute_branch_value` mechanism this phase extends.
- `process/phase3.0-workflow-spec.md` §6.0.1 — the triage walkthrough.
- `docs/explanation/workflow-foundation.md` §2.7.1 — NativeEnvironment behavior contract.

---

## Scope

**In scope (delivered across the 3 steps):**

- `BranchRunner` at `src/workflows/builtins/branch.py` — renders `condition:`, returns `{value: bool}`.
- `FilterRunner` at `src/workflows/builtins/filter.py` — renders `over:` → list, iterates with `item_as` scope, renders `condition:` per element, returns `{result: list[T]}`.
- `ComputeRunner` at `src/workflows/builtins/compute.py` — renders `value:`, returns `{value: <any>}`.
- `compute_branch_value` extension at `src/workflows/branching.py` — one new case for `kolsch/nodes/branch`.
- Three `registry.register(...)` calls in `register_builtins(...)`.
- `bundles/kolsch/workflows/triage-tickets.yaml` — the master spec §6.0.1 walkthrough running end-to-end.
- `bundles/kolsch/agents/classifier.yaml` + `bundles/kolsch/agents/urgent_responder.yaml` + `bundles/kolsch/agents/normal_responder.yaml` (fake-LLM-backed for test mode).
- `tests/component/test_workflow_compute_recipes.py` — 8 parametrized aggregation-recipe cases.
- `tests/component/test_workflow_native_env.py` — typed-vs-string regression coverage.
- `tests/component/test_workflow_filter.py` — `item_as` rebinding, non-list error.
- `tests/component/test_workflow_branch_node.py` — branch dispatch via `compute_branch_value` extension.
- Goldens at `tests/golden/runs/` for the triage workflow (both arms).

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- `map` runtime (Phase 3.7) — though `over:` grammar will be reused there.
- `workflow` sub-workflow runtime (Phase 3.7).
- Fork (Phase 3.8).
- Concurrent execution (Phase 3.10).
- Custom Jinja filters beyond the existing `from_json`.

**Decisions already locked (do NOT relitigate):**

- Branch node type vs edge branch field are ORTHOGONAL.
- `filter` and `compute` are non-branchable; validator enforces.
- `BranchRunner.condition` and `ComputeRunner.value` accept YAML literals + Jinja strings per Round 6 J5/β.
- `FilterRunner.over` non-list raises; runner is the gate (validator doesn't type-check Jinja results).
- `item_as:` defaults to `"item"`; per-element binding shadows same-named bound_inputs.
- `bool(rendered)` coercion in branch runner — defensive against non-bool Jinja results.
- NO new event kinds.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (`NativeCodeGenerator` + `ImmutableSandboxedEnvironment` hybrid), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/jinja_env.py:56-66` — `WorkflowJinjaEnvironment`. Phase 3.6 verifies; does NOT modify.
- `src/workflows/executor.py:1131-1159` — `_render_jinja(expression, *, inputs_overlay=)`. Phase 3.6 runners use via `ctx.render_jinja(...)`.
- `src/workflows/branching.py:25-56` — `compute_branch_value`. Phase 3.6 extends.
- `src/workflows/executor.py:630-642` — edge dispatch at fire time. No changes.
- `src/workflows/validator.py:540-616` — branching invariants. No changes.
- `src/workflows/parser.py:313-365` — three NodeDef shapes. No changes.
- `src/workflows/parser.py:158-176` — three Inputs shapes. No changes.
- `src/workflows/_inputs_models.py:42-57` — `_BUILTIN_TO_INPUTS`. No changes.
- `src/workflows/builtins/__init__.py:67-130` — `register_builtins(...)`. Phase 3.6 adds three registrations.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-compute-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- `branch` node with `condition:` Jinja string evaluates correctly; YAML literal `true`/`false` works too; downstream branch dispatch fires the correct arm.
- `filter` node renders `over:` to a list, iterates with `item_as` scope, renders `condition:` per element, returns `{result: [...]}`.
- `filter` over an empty list returns `{result: []}` cleanly.
- `filter` over a non-list raises `WorkflowNodeFailed`; run aborts.
- `compute` node renders `value:` and captures the typed result; YAML literals pass through.
- All 8 aggregation recipes work and return correctly-typed results.
- Triage walkthrough runs end-to-end across both branch arms.
- NativeEnvironment string-field non-regression: bash `command: "echo {{ name }}"` still produces a string command.
- `compute_branch_value` reads the branch node's `value` correctly.
- All three runners registered.
- No regressions.

---

## Phase-level Integration Tests

### Phase Test 1: Triage walkthrough
- **Run:** `uv run kolsch --root /tmp/kolsch-compute-test workflows run kolsch/workflows/triage-tickets --input ticket_text="server down, urgent"` + a second run with `ticket_text="add a button"`.
- **Expect:** first → urgent_handler fires, normal_handler pruned; second → normal_handler fires, urgent_handler pruned.

### Phase Test 2: Aggregation recipes
- **Run:** `uv run pytest tests/component/test_workflow_compute_recipes.py -v`
- **Expect:** all 8 cases pass with correct types.

### Phase Test 3: Filter with item_as
- **Run:** `uv run pytest tests/component/test_workflow_filter.py::test_filter_item_as_custom -v`

### Phase Test 4: NativeEnvironment regression
- **Run:** `uv run pytest tests/component/test_workflow_native_env.py -v`

### Phase Test 5: Filter non-list raises
- **Run:** `uv run pytest tests/component/test_workflow_filter.py::test_filter_non_list_raises -v`

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 3-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.6 through 3 sequential cycles. This phase is small — three tiny runners on top of a mature substrate. The non-obvious work is verifying NativeEnvironment behavior across the 8 aggregation recipes (the load-bearing test that the typed-value flow works end-to-end) and codifying the master spec §6.0.1 triage walkthrough as a real workflow.

**Authoritative references:**

- THIS spec: `process/phase3.6-branch-filter-compute-build.md`.
- Master design spec: `process/phase3.6-branch-filter-compute-spec.md` — §4.4.1 / §4.5.1 / §4.6.3.
- Phase 3.3 build plan: `process/phase3.3-modifiers-branching-build.md` — for the edge-`branch:` mechanism + `compute_branch_value` this phase extends.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §4.0 / §4.4 / §4.5 / §4.6 / §6.0.1.
- `docs/explanation/workflow-foundation.md` §2.7.1 — NativeEnvironment contract.

**The 3 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | Three runners (`BranchRunner` / `FilterRunner` / `ComputeRunner`) + `compute_branch_value` extension + `register_builtins(...)` extension. Tests cover each runner's happy path + the new compute_branch_value case. | ~1.5 days |
| **Step 1** | 8 aggregation recipes + NativeEnvironment regression suite + filter edge cases (empty list, non-list, custom `item_as`). | ~1 day |
| **Step 2** | Triage walkthrough starter workflow + the three supporting agent YAMLs + goldens (both arms) + Phase Tests 1, 4 wired end-to-end. | ~1.5 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision (custom Jinja filters, branch node truthy/falsy coercion strictness, filter over non-iterable bahvior) → REJECT.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append to THIS spec, re-brief.
- Coder discovers a NativeEnvironment regression in existing tests → flag as a separate patch on this branch (it's pre-existing infrastructure, not Phase 3.6 scope).
- Scope creep (`map` runtime, sub-workflow, fork, custom filters) → REJECT.

**Done when:** All 3 steps Approved AND all 6 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.6 line annotated with the shipping SHA.

---

# STEP 0 — Three runners + compute_branch_value extension + registration

**Goal**: ship `BranchRunner`, `FilterRunner`, `ComputeRunner` + the one-line `compute_branch_value` extension + `register_builtins(...)` extension. Tests cover each runner's happy path. Step 1 owns the broader test surface (aggregation recipes, NativeEnvironment regression, edge cases).

**Files created/modified:**

- `src/workflows/builtins/branch.py` (new) — `BranchRunner`.
- `src/workflows/builtins/filter.py` (new) — `FilterRunner`.
- `src/workflows/builtins/compute.py` (new) — `ComputeRunner`.
- `src/workflows/branching.py` (modified) — extend `compute_branch_value` with the `kolsch/nodes/branch` case.
- `src/workflows/builtins/__init__.py` (modified) — register three new runners.
- `tests/component/test_workflow_branch_node.py` (new) — happy-path coverage.
- `tests/component/test_workflow_filter.py` (new) — happy-path coverage (edge cases in Step 1).
- `tests/component/test_workflow_compute.py` (new) — happy-path coverage (8 recipes in Step 1).

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the three runners + the `compute_branch_value` extension + register all three. Happy-path tests only; Step 1 owns the broader test surface.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.6-branch-filter-compute-build.md` — Step 0.
2. `process/phase3.6-branch-filter-compute-spec.md` — §4.4.1, §4.5.1, §4.6.3.
3. `src/workflows/branching.py:25-56` — existing `compute_branch_value`.
4. `src/workflows/builtins/bash.py` — runner pattern (for reference; the new runners are simpler).
5. `src/workflows/builtins/set_state.py` — closest existing pattern (pure-Jinja runner from Phase 3.5).

**What to implement:**

1. **`src/workflows/builtins/branch.py`**:

   ```python
   class BranchRunner:
       """Runtime for kolsch/nodes/branch — Phase 3.6 §4.6.3."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: BranchNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           raw = node_def.condition
           if isinstance(raw, str) and raw.lstrip().startswith("{{"):
               rendered = ctx.render_jinja(raw, inputs_overlay=bound_inputs)
           else:
               rendered = raw
           return {"value": bool(rendered)}
   ```

2. **`src/workflows/builtins/filter.py`**:

   ```python
   class FilterRunner:
       """Runtime for kolsch/nodes/filter — Phase 3.6 §4.4.1."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: FilterNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           rendered_over = self._render_if_jinja(node_def.over, ctx, bound_inputs)
           if not isinstance(rendered_over, list):
               raise TypeError(
                   f"node {node_name!r} filter.over must resolve to a list, "
                   f"got {type(rendered_over).__name__}"
               )
           result: list[Any] = []
           for element in rendered_over:
               element_overlay = {**bound_inputs, node_def.item_as: element}
               rendered_cond = self._render_if_jinja(
                   node_def.condition, ctx, element_overlay
               )
               if bool(rendered_cond):
                   result.append(element)
           return {"result": result}

       @staticmethod
       def _render_if_jinja(value, ctx, overlay):
           if isinstance(value, str) and value.lstrip().startswith("{{"):
               return ctx.render_jinja(value, inputs_overlay=overlay)
           return value
   ```

3. **`src/workflows/builtins/compute.py`**:

   ```python
   class ComputeRunner:
       """Runtime for kolsch/nodes/compute — Phase 3.6 §4.5.1."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: ComputeNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           raw = node_def.value
           if isinstance(raw, str) and raw.lstrip().startswith("{{"):
               rendered = ctx.render_jinja(raw, inputs_overlay=bound_inputs)
           else:
               rendered = raw
           return {"value": rendered}
   ```

4. **`compute_branch_value` extension** in `src/workflows/branching.py`:

   ```python
   def compute_branch_value(node_def, captured) -> bool | None:
       type_ref = getattr(node_def, "type", None)
       if type_ref == _BASH_TYPE_REF:
           return bool(captured.get("exit_code") == 0)
       if type_ref == "kolsch/nodes/branch":   # NEW Phase 3.6
           value = captured.get("value")
           if isinstance(value, bool):
               return value
           return None
       value = captured.get("value")
       if isinstance(value, bool):
           return value
       return None
   ```

   The new case is symmetric with the authored-Python path (both read `captured["value"]`); separating it makes the spec-mapping explicit and future-proofs if branch's capture key ever changes.

5. **`register_builtins(...)` extension** in `src/workflows/builtins/__init__.py`:

   ```python
   registry.register("kolsch/nodes/branch", BranchRunner())
   registry.register("kolsch/nodes/filter", FilterRunner())
   registry.register("kolsch/nodes/compute", ComputeRunner())
   ```

   No new constructor kwargs; all three runners are dependency-free.

6. **Tests** (happy path only — Step 1 expands):

   - `tests/component/test_workflow_branch_node.py`:
     - **Jinja condition:** `BranchNodeDef(condition="{{ inputs.x > 5 }}")` with `bound_inputs={x: 10}`; assert `{value: True}`.
     - **YAML literal true:** `BranchNodeDef(condition=True)`; assert `{value: True}` (passes through, no render).
     - **YAML literal false:** `BranchNodeDef(condition=False)`; assert `{value: False}`.
     - **Branch dispatch:** workflow `start → branch → (true_handler | false_handler)` where `start` sets up the condition; assert the correct arm fires; the other is pruned (Phase 3.3 cascade).
     - **`compute_branch_value` extension:** unit test on `compute_branch_value(BranchNodeDef(...), {"value": True})` returns `True`; `{"value": "string"}` returns `None`; `{"value": 1}` returns `None` (int, not bool).

   - `tests/component/test_workflow_filter.py`:
     - **Happy path with default `item_as`:** `over: "{{ inputs.items }}"`, `condition: "{{ item > 5 }}"`, `bound_inputs={items: [3, 7, 5, 10]}`; assert `{result: [7, 10]}`.
     - **Object filtering:** `over: "{{ inputs.tickets }}"`, `condition: "{{ ticket.urgent }}"`, `item_as: ticket`, `bound_inputs={tickets: [{urgent: true, ...}, {urgent: false, ...}]}`; assert only urgent tickets returned.

   - `tests/component/test_workflow_compute.py`:
     - **Jinja string:** `value: "{{ 1 + 1 }}"`; assert `{value: 2}` (int, not "2").
     - **YAML literal int:** `value: 42`; assert `{value: 42}`.
     - **YAML literal list:** `value: [1, 2, 3]`; assert `{value: [1, 2, 3]}`.

**Step 0 Acceptance Criteria:**

0.1. `BranchRunner` ships; renders `condition:` (Jinja or literal); returns `{value: bool}`.
0.2. `FilterRunner` ships; renders `over:` to list (raises on non-list); iterates with `item_as` scope; returns `{result: list[T]}`.
0.3. `ComputeRunner` ships; renders `value:` (Jinja or literal); returns `{value: <any>}`.
0.4. `compute_branch_value` handles `kolsch/nodes/branch` with the same `isinstance(value, bool)` guard.
0.5. `register_builtins(...)` registers all three; `RunnerRegistry.get("kolsch/nodes/<each>")` returns the runner, NOT `NodeTypeNotImplemented`.
0.6. Tests cover happy paths for each runner + the `compute_branch_value` extension + branch dispatch end-to-end.
0.7. No regressions.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] No new event kinds emitted (grep `events.py` — no new enum values)
- [ ] No new error classes (grep `errors.py` — no additions)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.2 — `item_as` scope shadowing. Read the runner source: the per-element overlay MUST inject `node_def.item_as: element` AFTER `**bound_inputs` so the element wins on collision. Reversed order silently masks the per-element binding.
- AC 0.4 — `isinstance(value, bool)` check. Read the source. A `True` is `bool`; an `int(1)` is also truthy but isinstance is False — the defensive guard prevents accidental int→branch routing.
- AC 0.5 — registration. `kolsch workflows run` on a workflow using any of the three types must NOT raise `NodeTypeNotImplemented`. Manual smoke: write a tiny workflow with `compute(value: "{{ 1 + 1 }}")` and confirm it runs.

---

# STEP 1 — Aggregation recipes + NativeEnvironment regression + filter edge cases

**Goal**: ship the 8-recipe coverage + NativeEnvironment string-vs-typed regression suite + filter empty-list/non-list/custom-item-as cases.

**Files created/modified:**

- `tests/component/test_workflow_compute_recipes.py` (new) — parametrized 8-recipe coverage.
- `tests/component/test_workflow_native_env.py` (new) — typed-vs-string regression.
- `tests/component/test_workflow_filter.py` (modified) — add edge cases.

**Effort**: ~1 day.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the 8-recipe coverage + NativeEnvironment regression + filter edge cases. NO new runtime code (Step 0 shipped it); pure test suite.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.6-branch-filter-compute-build.md` — Step 1.
2. `process/phase3.6-branch-filter-compute-spec.md` — §4.5.1.2 (the 8-recipe table).
3. `docs/explanation/workflow-foundation.md` §2.7.1 — NativeEnvironment contract.

**What to implement:**

1. **`tests/component/test_workflow_compute_recipes.py`** — parametrized over the 8 recipes:

   ```python
   FIXTURE_ITEMS = [
       {"name": "alpha", "score": 10, "urgent": False},
       {"name": "beta",  "score": 5,  "urgent": True},
       {"name": "gamma", "score": 7,  "urgent": False},
   ]

   RECIPES = [
       # (recipe_name, value_expression, expected_type, expected_value)
       ("sum",     "{{ items | sum(attribute='score') }}",                              int,    22),
       ("count",   "{{ items | length }}",                                              int,    3),
       ("min",     "{{ items | min(attribute='score') }}",                              dict,   {"name": "beta", "score": 5, "urgent": True}),
       ("max",     "{{ items | max(attribute='score') }}",                              dict,   {"name": "alpha", "score": 10, "urgent": False}),
       ("sort",    "{{ items | sort(attribute='score', reverse=true) }}",               list,   [{"name": "alpha", ...}, {"name": "gamma", ...}, {"name": "beta", ...}]),
       ("flatten", "{{ [[1, 2], [3, 4]] | sum(start=[]) }}",                            list,   [1, 2, 3, 4]),
       ("any",     "{{ items | selectattr('urgent') | list | length > 0 }}",            bool,   True),
       ("all",     "{{ items | rejectattr('urgent') | list | length == 0 }}",           bool,   False),
   ]

   @pytest.mark.parametrize("recipe_name, expression, expected_type, expected_value", RECIPES)
   async def test_aggregation_recipe(recipe_name, expression, expected_type, expected_value):
       # Construct a minimal workflow with a single compute node.
       # Run it; assert the captured output's type AND value.
       ...
   ```

   The flatten recipe uses a literal nested list rather than `items` because the fixture's items aren't lists — the master spec's flatten example uses `items` but assumes a list-of-lists context; the test inverts to test the recipe shape itself.

2. **`tests/component/test_workflow_native_env.py`** — typed-vs-string regression:

   ```python
   # Single-expression templates preserve types
   async def test_int_arithmetic_returns_int():
       # workflow with compute(value: "{{ 1 + 1 }}") → assert captured value == 2 AND type is int
       ...

   async def test_list_length_returns_int():
       ...

   async def test_list_passes_through_typed():
       # compute(value: "{{ inputs.items }}") with bound_inputs={items: [1, 2, 3]} → assert captured value == [1, 2, 3] AND type is list
       ...

   async def test_bool_returns_bool():
       # compute(value: "{{ 1 == 1 }}") → assert captured value is True (bool, not str "True")
       ...

   async def test_dict_attribute_access_returns_value():
       # compute(value: "{{ inputs.ticket.urgent }}") with bound_inputs={ticket: {urgent: True}} → assert captured value is True
       ...

   # Multi-segment templates produce strings
   async def test_multi_segment_returns_string():
       # bash(command: "echo {{ inputs.name }}") → captures stdout from a shell-invoked string command
       ...

   async def test_string_concatenation_returns_string():
       # compute(value: "{{ 'hello ' + 'world' }}") → assert captured value == "hello world" AND type is str
       ...

   # Round-trip via filter → compute
   async def test_filter_then_compute_typed_chain():
       # filter(over: items, condition: ...) → compute(value: "{{ nodes.f.result | length }}") → assert int result
       # Confirms typed values flow through the chain without string-coerce
       ...
   ```

3. **`tests/component/test_workflow_filter.py`** — add edge cases:

   ```python
   async def test_filter_empty_list_returns_empty():
       # filter(over: "{{ inputs.empty }}", ...) with bound_inputs={empty: []} → {result: []}
       ...

   async def test_filter_non_list_raises():
       # filter(over: "{{ inputs.scalar }}", ...) with bound_inputs={scalar: "not a list"} → WorkflowNodeFailed
       # Workflow JSONL ends with workflow.node.failed → workflow.run.failed
       ...

   async def test_filter_item_as_custom():
       # filter(over: tickets, condition: "{{ ticket.urgent }}", item_as: ticket) → only urgent tickets
       ...

   async def test_filter_item_as_shadows_bound_input():
       # bound_inputs has key "item"; filter has item_as: item (default); per-element binding wins
       ...

   async def test_filter_predicate_raises_aborts_run():
       # filter with condition that raises on second element → WorkflowNodeFailed; partial result discarded
       ...
   ```

**Step 1 Acceptance Criteria:**

1.1. All 8 aggregation recipes pass with correct types and values.
1.2. NativeEnvironment regression suite covers: single-expression typed returns (int/list/bool/dict); multi-segment string returns; filter → compute typed chain; bash command-string non-regression.
1.3. Filter edge cases covered: empty list, non-list (raises), custom `item_as`, scope shadowing, predicate-raises-aborts.
1.4. No new runtime code.
1.5. No regressions.

**Self-check before handoff:**

- [ ] All 5 ACs met
- [ ] Each recipe test asserts BOTH type AND value
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.1 — type assertions. A test that says `assert captured["value"] == 2` PASSES even if the value is the string `"2"` (Python `2 == "2"` is False, but the diff is subtle). The test MUST assert `isinstance(captured["value"], int)` separately. Read the source.
- AC 1.2 — `bash command:` non-regression. Run the existing bash test suite; confirm `command: "echo {{ inputs.name }}"` still produces a string the shell can execute. A typed return here would crash the shell.

---

# STEP 2 — Triage walkthrough starter + supporting agents + goldens + Phase Tests

**Goal**: ship the master spec §6.0.1 triage walkthrough as a real starter workflow. Three supporting agent YAMLs (fake-LLM-backed). Goldens for both branch arms.

**Files created/modified:**

- `bundles/kolsch/workflows/triage-tickets.yaml` (new) — the walkthrough.
- `bundles/kolsch/agents/classifier.yaml` (new) — classifies ticket text → label.
- `bundles/kolsch/agents/urgent_responder.yaml` (new).
- `bundles/kolsch/agents/normal_responder.yaml` (new).
- `tests/golden/runs/triage-urgent.workflow.jsonl` (new).
- `tests/golden/runs/triage-normal.workflow.jsonl` (new).
- `tests/component/test_workflow_branching_e2e.py` (new) — Phase Tests 1 + 6 wired.

**Effort**: ~1.5 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the triage walkthrough + supporting agents + goldens.

**What to implement:**

1. **`bundles/kolsch/workflows/triage-tickets.yaml`** — master spec §6.0.1 codified:

   ```yaml
   inputs:
     ticket_text: { type: str }
   nodes:
     classify:
       type: kolsch/nodes/agent
       agent: kolsch/agents/classifier
     is_urgent:
       type: kolsch/nodes/branch
       condition: "{{ inputs.label == 'urgent' }}"
     urgent_handler:
       type: kolsch/nodes/agent
       agent: kolsch/agents/urgent_responder
     normal_handler:
       type: kolsch/nodes/agent
       agent: kolsch/agents/normal_responder
   graph:
     start: classify
     edges:
       - from: classify
         to: is_urgent
         binds: { label: "{{ nodes.classify.label }}" }
       - from: is_urgent
         to: urgent_handler
         branch: true
       - from: is_urgent
         to: normal_handler
         branch: false
   ```

2. **`bundles/kolsch/agents/classifier.yaml`** — minimal agent that emits `{label: "urgent" | "normal"}` based on `ticket_text`. Use the existing fake-LLM-backed pattern from Phase 3.2's `hello-agent.yaml`. The agent's `IOSpec.outputs` declares `{label: str}`.

3. **`bundles/kolsch/agents/urgent_responder.yaml`** + **`bundles/kolsch/agents/normal_responder.yaml`** — minimal responders.

4. **Goldens** — run each scenario (urgent + normal); canonicalize volatile fields; commit. Both goldens share the same workflow ref; the difference is which branch fires.

5. **`tests/component/test_workflow_branching_e2e.py`** — Phase Tests 1, 4 (NativeEnvironment regression is the same suite from Step 1; cross-reference rather than duplicate), plus an integration scenario verifying the full triage cycle end-to-end with cross-JSONL spawned-session linkage (the agents spawn sessions per Phase 3.2 — confirm they still work in this branching context).

**Step 2 Acceptance Criteria:**

2.1. `triage-tickets.yaml` parses, validates, runs end-to-end.
2.2. Both branch arms exercised via two test runs.
2.3. Three supporting agent YAMLs ship; each loads via the asset surface.
2.4. Two goldens for the urgent + normal scenarios.
2.5. `test_workflow_branching_e2e.py` covers Phase Tests 1 + 6.
2.6. Manual smoke: `uv run kolsch ... workflows run kolsch/workflows/triage-tickets --input ticket_text="server down, urgent"` exits 0; `urgent_handler` fires; `normal_handler` pruned.
2.7. No regressions.

**Handoff trigger:** All ACs met → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.6 — RUN IT. Both inputs. Inspect both JSONLs. Confirm `branch_value` payload + `workflow.node.pruned` events for the un-fired arm.
- AC 2.4 — golden canonicalization. The two goldens should differ ONLY in which arm fires; the volatile-field substitution must not mask which branch_value was True/False.

---

## Done when

- All 3 steps Approved by Reviewer.
- All 6 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.6 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  # Branch on the "urgent" arm:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/triage-tickets --input ticket_text="server down, urgent"
  # ... urgent_handler fires; normal_handler pruned
  # Branch on the "normal" arm:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/triage-tickets --input ticket_text="add a button"
  # ... normal_handler fires; urgent_handler pruned
  # Confirm the 8 aggregation recipes:
  uv run pytest tests/component/test_workflow_compute_recipes.py -v
  ```
