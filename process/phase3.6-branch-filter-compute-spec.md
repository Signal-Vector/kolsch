# Phase 3.6 — Branch + Filter + Compute Spec

**Intro.** This spec covers Phase 3.6 — the runtime for three Jinja-driven leaf node types: `branch` (boolean route gate), `filter` (predicate-based list reducer), and `compute` (general-purpose Jinja expression that subsumes the 8 dropped aggregations). Builds on Phases 3.0 (parser/validator), 3.1 (sequential executor), 3.2 (agent), 3.3 (branching infrastructure — edge `branch:` field, multi-input fan-in, NativeEnvironment-typed inputs.* namespace), 3.4 (pause/resume), and 3.5 (state mutators + file IO).

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.6 — the roadmap entry.
- `process/phase3.0-workflow-spec.md` §4.4.1 (filter), §4.5.1 (compute), §4.6.3 (branch node), §6.0.1 (triage walkthrough), the 8-aggregation recipe table at §4.0 lines 234-245.
- `process/phase3.3-modifiers-branching-spec.md` — the orthogonal edge `branch:` field runtime + `compute_branch_value` mechanism this phase extends.
- `docs/explanation/workflow-foundation.md` §2.7.1 — NativeEnvironment behavior contract.

---

## Glossary

| Term | Meaning |
| --- | --- |
| branch node type | The dedicated `kolsch/nodes/branch` leaf — evaluates `condition:` (Jinja boolean), returns `{value: bool}`. ORTHOGONAL to the edge-level `branch:` field. |
| edge `branch:` field | The Phase 3.3 routing predicate on edges — `branch: true \| false \| null`. Filters which outgoing edges of a branchable source actually fire. Distinct from the branch node type. |
| filter node | `kolsch/nodes/filter` — per-element predicate-based list reducer. Returns `{result: list[T]}`. |
| compute node | `kolsch/nodes/compute` — general-purpose Jinja expression returning `{value: <any>}`. Subsumes 8 aggregation node types via stock Jinja filters. |
| NativeEnvironment | `jinja2.nativetypes`-powered evaluation that preserves Python types (`{{ items }}` returns the actual list, not its `str()` repr). Already shipped via `src/workflows/jinja_env.py:56-66` as `WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)` with `NativeCodeGenerator` + `native_concat`. |
| `item_as:` | Renames the per-iteration variable in `filter`'s `condition:` scope. Default `"item"`. Same grammar Phase 3.7 reuses for `map`. |
| triage walkthrough | Master spec §6.0.1's end-to-end "classify → branch → urgent_handler / normal_handler" pipeline. The canonical verification reference for this phase. |

---

## Background

Phase 3.0 shipped the parser models (`BranchNodeDef` / `FilterNodeDef` / `ComputeNodeDef` at `src/workflows/parser.py:313-365`) and the inputs models (`BranchInputs` / `FilterInputs` / `ComputeInputs` at parser.py:158-176 — all `extra="allow"`). Phase 3.3 shipped:

- **NativeEnvironment-by-default `JINJA_ENV`** at `src/workflows/jinja_env.py:56-66`. The hybrid `WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)` uses `NativeCodeGenerator` + `native_concat` to return typed Python values while preserving sandbox semantics. Single-expression templates (`{{ items }}`) return real Python types; multi-segment templates (`"echo {{ name }}"`) join to strings. **This phase does NOT migrate JINJA_ENV** — the work is already done. Phase 3.6 verifies typed-value flow end-to-end and adds regression coverage.
- **`compute_branch_value(node_def, captured) -> bool | None`** at `src/workflows/branching.py:25-56`. Currently handles bash exit codes + authored Python `bool` / `tuple[bool, BaseModel]` returns. Phase 3.6 extends it with a one-line case for `kolsch/nodes/branch`.
- **Validator branching invariants** at `src/workflows/validator.py:540-616`. Already treats `BranchNodeDef` as branchable AND enforces "both arms required" via `WorkflowBranchMissingArm`. No validator changes needed.
- **Edge dispatch at fire time** at `src/workflows/executor.py:630-642` — invokes `compute_branch_value(...)` after every node completion. Agnostic to node type; reads the bool and prunes outgoing edges accordingly.
- **`_BUILTIN_TO_INPUTS`** at `src/workflows/_inputs_models.py:42-57` — already has entries for `kolsch/nodes/{branch,filter,compute}`. Strict-mode bind validation will run uniformly across all three.

What's LEFT for Phase 3.6:

1. **Three small runners** at `src/workflows/builtins/{branch,filter,compute}.py`. Each is essentially a Jinja render + a return-shape narrow:
   - `BranchRunner`: render `condition:`, return `{value: bool(<rendered>)}`.
   - `FilterRunner`: render `over:` → list; iterate with `item_as` scope variable; render `condition:` per element; return `{result: [items_where_truthy]}`.
   - `ComputeRunner`: render `value:`, return `{value: <rendered>}`.
2. **`compute_branch_value` extension** — one new case for `kolsch/nodes/branch`:
   ```python
   if type_ref == "kolsch/nodes/branch":
       value = captured.get("value")
       if isinstance(value, bool):
           return value
       return None
   ```
   Mirrors the authored-Python path; the branch runner stores its boolean at `captured["value"]`, the existing edge dispatch picks it up.
3. **Registration** — `register_builtins(...)` adds three `registry.register(...)` calls. No new dependencies (the runners are pure Jinja + list manipulation).
4. **Triage walkthrough** as the end-to-end verification fixture — the master spec §6.0.1 example codified as a starter workflow.
5. **8-aggregation recipe coverage** — each recipe (sum, count, min, max, sort, flatten, any, all) verified as a `compute.value:` expression returning the correctly-typed result. NativeEnvironment regression coverage rides alongside.
6. **NativeEnvironment string-field regression** — confirm that string-only fields (bash `command:`, file-IO `path:`, etc.) still produce strings after interpolation. Existing tests partially cover this; this phase adds a focused suite.

The verification gate is four scenarios: the triage walkthrough runs end-to-end with `classify → is_urgent → (urgent_handler | normal_handler)` pruning correctly; each of the 8 aggregation recipes returns the typed result; `filter` over a list-of-objects works with `item_as`; NativeEnvironment preserves types through the `over:` → predicate → result chain (no string-coerce roundtrip).

**This spec breaks the phase into 3 sequential steps** because the surface is small and self-contained. The build plan lives at `process/phase3.6-branch-filter-compute-build.md`.

---

## Scope

**In scope (delivered across the 3 steps):**

- **`BranchRunner`** at `src/workflows/builtins/branch.py`. Renders `node_def.condition` via `ctx.render_jinja(..., inputs_overlay=bound_inputs)`. If the rendered value is a `bool`, return `{"value": rendered_bool}`. If it's a string-typed Jinja result that is still YAML-truthy (e.g., a literal `condition: true` passing through as the Python `True`), accept. Anything else — e.g., the rendered result is a string like `"true"` because the condition wasn't Jinja-marked AND wasn't a YAML bool literal — coerce via `bool(rendered)` and document the case (rare in practice; the parser accepts `condition: Any` so authors who set `condition: true` get `True` directly without rendering). Branch node always has a `branch:` true/false split — validator-enforced. Capture shape: `{value: bool}`.
- **`FilterRunner`** at `src/workflows/builtins/filter.py`. Renders `node_def.over` via `ctx.render_jinja` to obtain a list. If the result is not a list (e.g., the author bound a non-list to the source), raise `WorkflowNodeFailed(cause=TypeError(...))` — bubbles to the executor's failure path. Iterates the list; for each element, renders `node_def.condition` with the element bound under the name `node_def.item_as` (default `"item"`) overlayed on the Jinja scope. Collects elements whose rendered condition is truthy. Capture shape: `{result: list[T]}`.
- **`ComputeRunner`** at `src/workflows/builtins/compute.py`. Renders `node_def.value` via `ctx.render_jinja`. If `value:` is a YAML literal (non-Jinja-marked, non-string), pass through verbatim — `{"value": literal}`. If it's a Jinja-marked string, render and capture the typed result. Capture shape: `{value: <any>}`.
- **`compute_branch_value` extension** — one new case for the `kolsch/nodes/branch` type. The existing dispatch logic reads `captured["value"]` regardless of node type when it's a `bool`; the branch runner stores its result there.
- **Registration** — `register_builtins(...)` adds three calls; no new constructor kwargs needed (the runners are dependency-free — pure Jinja eval).
- **`item_as:` scope handling** — `FilterRunner` injects the per-element binding under the user-named key into a per-element overlay. The existing `ctx.render_jinja(..., inputs_overlay=...)` mechanism handles this; the runner builds a per-element overlay dict at each iteration:
  ```python
  for element in rendered_list:
      element_overlay = {node_def.item_as: element, **bound_inputs}
      truthy = ctx.render_jinja(node_def.condition, inputs_overlay=element_overlay)
      if truthy:
          result.append(element)
  ```
  The element-scope variable shadows any same-named input from edges — documented as intended.
- **Triage walkthrough starter** — `bundles/kolsch/workflows/triage-tickets.yaml` runs end-to-end. The agent nodes use a fake-LLM-backed `kolsch/agents/classifier` for test mode; the branch arms route correctly; pruned terminal contributes `null` to the workflow output dict per Phase 3.3 semantics.
- **8-aggregation recipe fixture** — `tests/component/test_workflow_compute_recipes.py` parametrized over the 8 recipes from master spec §4.0 (lines 234-245). Each recipe is a `compute.value:` expression run against a fixture list; assert the captured value's TYPE and CONTENT.
- **NativeEnvironment regression coverage** — `tests/component/test_workflow_native_env.py` covering:
  - Single-expression templates preserve types: `{{ 1 + 1 }}` → `2` (int), `{{ items | length }}` → int, `{{ ticket.urgent }}` → bool.
  - Multi-segment templates produce strings: `"echo {{ name }}"` → `"echo Alice"` (str).
  - `over:` → typed list → iterate works: `compute(value: "{{ tickets | sort(attribute='priority') }}")` returns a sorted list of dicts, not a string.
  - String-field non-regression: bash `command: "echo {{ name }}"` still works exactly as in Phase 3.1.
- **Goldens** at `tests/golden/runs/`: triage workflow (with both branch arms exercised across two test runs); compute recipes; filter over object list; branch-with-jinja-condition.

**Out of scope (deferred to later sub-phases per `phase3.x-roadmap.md`):**

- `map` runtime (Phase 3.7) — though the `over:` grammar this phase ships is the same one `map` will reuse.
- `workflow` sub-workflow runtime (Phase 3.7).
- Fork (Phase 3.8) — compute/filter/branch results aren't state mutations; they ride in `workflow.node.completed` payloads (which Phase 3.4's replay already restores). No replay changes needed.
- Concurrent execution (Phase 3.10) — the three runners are pure functions of their inputs; they parallelize trivially when 3.10 ships.
- Author-named Jinja filters / custom filters — Phase 3.6 uses ONLY stock Jinja2 filters. Custom filters (`from_json` is the one existing exception) are out of scope.
- `filter.condition` referring to RUNNING state mutations — the filter iterates `over:` once at fire time; state changes mid-iteration are not observed. (Theoretically a `set_state` inside the iteration would be visible by Jinja, but the filter does not invoke other nodes; this is a non-issue at v1.)

**Decisions explicitly locked (do NOT relitigate):**

- **`branch` node type is ORTHOGONAL to edge `branch:` field.** The dedicated node TYPE computes a boolean; the edge FIELD routes by the source's branch value. A workflow can use one, both, or neither. Master spec §4.6.3 and Phase 3.3 §6.5 both lock this.
- **`condition:` on `BranchNodeDef` is the node's primary work, NOT a `when:` gate.** Same word, different semantics by node type. `BranchNodeDef.condition` always runs (whether true or false drives downstream routing); `when:` modifier is a SKIP gate (when false, the node doesn't fire at all). The disambiguation is by `type:`.
- **`filter` is non-branchable.** Master spec §4.4.1 line 353 + Phase 3.3 validator. A `filter` node's outgoing edges may NOT carry `branch:`. Validator already enforces.
- **`compute` is non-branchable.** Wrap with a `branch` node downstream if the computed result needs to gate branching. Master spec §4.5.1.
- **`compute.value` accepts YAML literals AND Jinja strings (Round 6 J5/β).** A literal `value: 42` captures as `{value: 42}` without Jinja involvement. A Jinja string `value: "{{ items | sum }}"` renders. The dispatch is: if `isinstance(value, str) and value.lstrip().startswith("{{")`, render; otherwise pass through.
- **`BranchNodeDef.condition` ALSO accepts YAML literals and Jinja strings.** `condition: true` is a no-op true branch (useful for placeholder workflows); `condition: "{{ ... }}"` is the typical case.
- **`item_as:` defaults to `"item"`.** Author may override (`item_as: ticket`).
- **`FilterRunner.over` must resolve to a list.** Non-list (dict, scalar, etc.) raises and the run aborts. The validator does not enforce this at parse time (Jinja results are runtime-typed); the runner is the gate.
- **NativeEnvironment is already universal.** Every Jinja render site in the executor + every builtin runner routes through `JINJA_ENV`. Phase 3.6 verifies the contract; no migration.
- **No `workflow.compute.evaluated` event kind.** Compute results ride in `workflow.node.completed.payload.output.value`. Phase 3.4's replay restores `_captured` from completion events, so compute results are correctly restored on resume. No new event kind ships in Phase 3.6.
- **The 8 aggregation recipes use stock Jinja2 filters ONLY.** `sum`, `length`, `min`, `max`, `sort`, `selectattr`, `rejectattr`, `list`, `default` — all part of the standard Jinja2 distribution. No custom filter additions in Phase 3.6.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (with `NativeCodeGenerator` + `ImmutableSandboxedEnvironment`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/jinja_env.py:56-66` — `WorkflowJinjaEnvironment` (the hybrid native + sandboxed env). Already shipped; Phase 3.6 verifies behavior, does not modify.
- `src/workflows/jinja_env.py:124` — `render_jinja()` wrapper translating `SecurityError` → `WorkflowJinjaSecurityError`.
- `src/workflows/executor.py:1131-1159` — `_render_jinja(expression, *, inputs_overlay=)`. Already supports the Phase 3.3 overlay. Phase 3.6's three runners use it via `ctx.render_jinja(...)`.
- `src/workflows/context.py` — `NodeContext.render_jinja(...)` from Phase 3.4.
- `src/workflows/branching.py:25-56` — `compute_branch_value(node_def, captured)`. Phase 3.6 adds the branch-node case.
- `src/workflows/executor.py:630-642` — edge dispatch via `compute_branch_value`. No changes; pulls the new branch-node value transparently.
- `src/workflows/validator.py:540-616` — branching invariants. `BranchNodeDef` already in `require_both_arms`. No changes.
- `src/workflows/parser.py:313-365` — `BranchNodeDef` / `FilterNodeDef` / `ComputeNodeDef`. Already exist.
- `src/workflows/parser.py:158-176` — `*Inputs(BaseModel)`. Already exist.
- `src/workflows/executor.py:82-102` — `BUILTIN_PHASE_MAP`. Already maps `branch`/`filter`/`compute` → `"3.6"`.
- `src/workflows/_inputs_models.py:42-57` — `_BUILTIN_TO_INPUTS`. Already includes all three.
- `src/workflows/builtins/__init__.py:67-130` — `register_builtins(...)`. Phase 3.6 adds three `registry.register(...)` calls.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-compute-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## §4.6.3 Branch node runtime (elaboration)

### §4.6.3.1 Runner algorithm

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
            rendered = raw  # YAML literal — true/false typically; pass-through
        return {"value": bool(rendered)}
```

**Coercion rule.** The `bool(rendered)` cast handles two cases at once:
- Jinja returns a real Python `bool` (most common): `bool(True) == True`.
- Author wrote a non-bool literal (e.g., `condition: 1` or `condition: "yes"`): coerced to truthiness. The validator does not constrain `condition: Any`'s value type at parse time; the runner coerces.

The result is ALWAYS a real `bool`. Phase 3.3's `compute_branch_value` (extended in this phase) reads `captured["value"]` and does its own `isinstance(value, bool)` check — guarantees consistent dispatch.

### §4.6.3.2 Output shape

`{value: bool}` per master spec §4.0 lines 214-230. Addressable as `nodes.<branch_node>.value` in downstream Jinja. Branch nodes can ALSO route via their own outgoing `branch: true | false` edges per Phase 3.3 — the typical pattern is "branch node evaluates condition + its two outgoing edges carry `branch: true` / `branch: false`."

### §4.6.3.3 Validator interaction

`BranchNodeDef` is in `validator.py`'s `require_both_arms` set. A branch node WITHOUT both `branch: true` AND `branch: false` outgoing edges fails parse with `WorkflowBranchMissingArm`. No runtime check needed — the validator gates correctness before the runner ever fires.

---

## §4.4.1 Filter node runtime (elaboration)

### §4.4.1.1 Runner algorithm

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
        # Render `over:` to a Python list (NativeEnvironment preserves types).
        rendered_over = self._render_if_jinja(node_def.over, ctx, bound_inputs)
        if not isinstance(rendered_over, list):
            raise TypeError(
                f"node {node_name!r} filter.over must resolve to a list, "
                f"got {type(rendered_over).__name__}"
            )
        # Iterate; per-element predicate rendering.
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

### §4.4.1.2 Per-element scope

The `element_overlay` builds `bound_inputs` first then injects `node_def.item_as: element` — element wins on name collision (intentional). Authors who care about the collision use a distinct `item_as` name.

The element is exposed at `{{ <item_as> }}` (e.g., default `{{ item }}`). Attribute access works for dict-of-items (`{{ item.urgent }}`) because NativeEnvironment preserves dict types. List-of-scalars also works (`{{ item > 5 }}`).

### §4.4.1.3 Edge cases

- **Empty list:** `over:` renders to `[]`; result is `{result: []}`. Run continues normally.
- **Non-list `over:`:** raises (per §4.4.1.1). The validator does NOT enforce list-typing at parse time — Jinja results are runtime-only. The runner is the gate.
- **Predicate raises:** the exception bubbles to the executor's failure path → `WorkflowNodeFailed`. The partial `result` is discarded; the run aborts per Phase 3.1 semantics.
- **Predicate returns non-bool:** `bool(rendered_cond)` coerces (matches Python's truthiness; matches Phase 3.6's branch-node coercion rule).

### §4.4.1.4 Output shape

`{result: list[T]}` per master spec §4.0. Addressable as `{{ nodes.<filter_node>.result }}`. Downstream nodes typically consume via another `filter` / `compute` (e.g., `compute(value: "{{ nodes.urgent_only.result | length }}")`).

---

## §4.5.1 Compute node runtime (elaboration)

### §4.5.1.1 Runner algorithm

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
            rendered = raw  # YAML literal — pass through
        return {"value": rendered}
```

### §4.5.1.2 The aggregation recipe table

Per master spec §4.0 lines 234-245, the 8 dropped aggregation node types are expressible as `compute.value:` Jinja expressions:

| Old node | `compute.value:` Jinja | Returns |
|---|---|---|
| `sum` | `{{ items \| sum(attribute='score') }}` | int / float |
| `count` | `{{ items \| length }}` | int |
| `min` | `{{ items \| min(attribute='score') }}` | dict (the min element) |
| `max` | `{{ items \| max(attribute='score') }}` | dict (the max element) |
| `sort` | `{{ items \| sort(attribute='score', reverse=true) }}` | list[dict] |
| `flatten` | `{{ items \| sum(start=[]) }}` (one level) | list |
| `any` | `{{ items \| selectattr('urgent') \| list \| length > 0 }}` | bool |
| `all` | `{{ items \| rejectattr('urgent') \| list \| length == 0 }}` | bool |

All 8 use stock Jinja2 filters; no custom filter additions. NativeEnvironment ensures the returned types are correct (int/float/list/bool/dict, NOT their string repr).

Each recipe ships as a parametrized test case in `tests/component/test_workflow_compute_recipes.py`. The test fixture is a small list of objects: `items = [{name: "a", score: 10, urgent: false}, {name: "b", score: 5, urgent: true}, {name: "c", score: 7, urgent: false}]`. Each recipe runs against this list and the captured `{value}` is asserted for both content and type.

### §4.5.1.3 Output shape

`{value: <evaluated>}` per master spec §4.0. The captured value type matches what the Jinja expression returned — NativeEnvironment's load-bearing contract.

### §4.5.1.4 Branchability

`compute` is NOT branchable. The validator rejects `branch:` edges from compute sources. Authors wanting to branch on a computed boolean wrap: `compute → branch → (true_handler | false_handler)`.

---

## §11 Event log (no new event kinds)

Phase 3.6 ships ZERO new event kinds. The three runners emit only the standard `workflow.node.started` + `workflow.node.completed` per Phase 3.1's framing.

For branch nodes, the existing Phase 3.3 extension applies: `workflow.node.completed.payload.branch_value` is set to the rendered bool. Pruned downstream nodes emit `workflow.node.pruned` per Phase 3.3.

For filter / compute nodes, `workflow.node.completed.payload.output` carries the result dict (`{result: ...}` or `{value: ...}`). Phase 3.4 replay handles these via the existing `output` payload restoration; nothing new.

---

## §12 Error classes (no additions)

Phase 3.6 reuses existing classes:

- `WorkflowNodeFailed` (`src/workflows/errors.py:1185`) — wraps `TypeError` from filter's non-list `over:`, Jinja `UndefinedError` from missing scope references, etc.
- `WorkflowJinjaSecurityError` (`src/workflows/errors.py:1248-1277`) — already raised by `render_jinja` when the sandbox blocks unsafe access. No change.
- `WorkflowBindTypeMismatch` (`src/workflows/errors.py:490-528`) — Phase 3.3's runtime check on the receiver's `Inputs` model. Applies uniformly; no specific Phase 3.6 wiring.

No new error classes ship in Phase 3.6.

---

## Phase-level Acceptance Criteria

- `branch` node with `condition: "{{ inputs.label == 'urgent' }}"`: renders true → fires `branch: true` outgoing edges; renders false → fires `branch: false` edges; pruned arm cascades correctly (Phase 3.3 semantics).
- `branch` node with YAML literal `condition: true` (or `false`) works without Jinja rendering — useful for placeholder workflows.
- `filter` node renders `over:` to a Python list (NativeEnvironment) and applies `condition:` per element; `item_as:` rebinds the per-element variable.
- `filter` over an empty list returns `{result: []}` without error.
- `filter` over a non-list raises `WorkflowNodeFailed`; the run aborts.
- `compute` node renders `value:` (Jinja) and captures the typed result; YAML literals pass through.
- All 8 aggregation recipes (`sum` / `count` / `min` / `max` / `sort` / `flatten` / `any` / `all`) work via `compute.value:` and return the correctly-typed result (int, list, bool, dict).
- The triage walkthrough (`bundles/kolsch/workflows/triage-tickets.yaml`) runs end-to-end with both branch arms exercised across two test runs.
- NativeEnvironment string-field non-regression: bash `command: "echo {{ name }}"` still produces a string command, not a typed object.
- `compute_branch_value` returns the branch node's `value` correctly; the edge dispatch fires the matching arm.
- All three runners registered; `RunnerRegistry.get("kolsch/nodes/<each>")` returns the runner.
- No regressions: full suite still green; non-branching workflows produce byte-identical JSONLs.

---

## Phase-level Integration Tests

### Phase Test 1: Triage walkthrough (urgent + normal)
- **Run:** `uv run kolsch --root /tmp/kolsch-compute-test workflows run kolsch/workflows/triage-tickets --input ticket_text="server down, urgent"` then again with `ticket_text="add a button"`.
- **Expect:** first run fires `urgent_handler`, prunes `normal_handler`; second run fires `normal_handler`, prunes `urgent_handler`. Both runs complete with the expected terminal output.
- **Fail means:** branch node runtime broken OR edge dispatch broken OR pruning cascade broken.

### Phase Test 2: Aggregation recipes
- **Run:** `uv run pytest tests/component/test_workflow_compute_recipes.py -v`
- **Expect:** all 8 parametrized cases pass; captured types match expected (int, list, bool, dict).
- **Fail means:** NativeEnvironment regression OR recipe expression wrong.

### Phase Test 3: Filter with item_as
- **Run:** `uv run pytest tests/component/test_workflow_filter.py::test_filter_item_as_custom -v`
- **Expect:** workflow with `filter: {over: "{{ inputs.tickets }}", condition: "{{ ticket.urgent }}", item_as: ticket}` correctly filters by the `urgent` attribute and returns the matching dicts.
- **Fail means:** `item_as` scope binding broken.

### Phase Test 4: NativeEnvironment regression suite
- **Run:** `uv run pytest tests/component/test_workflow_native_env.py -v`
- **Expect:** single-expression typed returns (int, list, bool, dict); multi-segment string returns (`"echo {{ name }}"` → string); string-field non-regression on bash command.
- **Fail means:** typed-vs-string boundary regressed.

### Phase Test 5: Filter non-list raises
- **Run:** `uv run pytest tests/component/test_workflow_filter.py::test_filter_non_list_raises -v`
- **Expect:** `filter.over` resolving to a string raises `WorkflowNodeFailed`; run aborts; JSONL ends with `workflow.run.failed`.
- **Fail means:** type-check missing.

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green.

---
