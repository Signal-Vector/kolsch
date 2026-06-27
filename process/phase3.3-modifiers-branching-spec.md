# Phase 3.3 — Branching Runtime + Multi-Input Bind Validation Spec

**Intro.** This spec covers Phase 3.3 — the runtime dispatch for edge-level `branch:`, bash exit-code branching, authored Python `bool` / `tuple[bool, BaseModel]` branching, the `inputs.*` Jinja scope at receivers, and the runtime `WorkflowBindTypeMismatch` strict-mode validation. It builds on Phases 3.0 (parser/validator), 3.1 (sequential executor with `when:` + `retry:` already shipped), and 3.2 (agent integration).

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.3 — the roadmap entry that scopes this phase.
- `process/phase3.0-workflow-spec.md` §2.5, §4.1.1, §5.4, §6.4, §7.1, §7.3, §12.1 — locked decisions on branching, edge schema, return-type contracts, bind validation, and error classes.
- `process/phase3.1-executor-spec.md` §8.1 (AND-strict rules), §11.2 (event ordering) — the rules the new dispatch must satisfy.
- `docs/explanation/workflow-event-log.md` — author-facing event log conventions.

---

## Glossary

| Term | Meaning |
| --- | --- |
| branch dispatch | Filtering a branchable node's outgoing edges by the edge's `branch: true/false` field against the node's computed branch value. |
| branchable node | A node whose runtime output can drive edge selection: `bash` (via `exit_code == 0`), authored Python `@node` returning `bool` or `tuple[bool, BaseModel]`. |
| branch value | The boolean a branchable node produces at completion. `bash`: `(exit_code == 0)`. Authored Python: the `bool` itself (return = `bool`) or the tuple's first element (return = `tuple[bool, BaseModel]`). |
| inputs.* namespace | The per-node Jinja scope name that exposes that node's edge-bound `binds:` keys to config-field interpolation. New in Phase 3.3. |
| pruned subgraph | The set of nodes downstream of a never-fired branch edge. Each such node never starts; terminal-node-keyed outputs contribute `None` to the workflow's inferred output dict. |
| strict-mode bind validation | Pydantic `strict=True` check that each rendered bind value satisfies the receiver's typed `Inputs(BaseModel)` model. Raised as `WorkflowBindTypeMismatch`. |

---

## Background

Phase 3.1 shipped more of the modifier surface than the original roadmap entry assumed:

- **`when:` gate evaluation** — shipped via `IterativeExecutor._when_holds()` (`src/workflows/executor.py:617`). Truthy/falsy YAML literal OR Jinja-marked string. Skip counts as completion per AND-strict rule 1.
- **`retry:` modifier** — shipped via `with_retry()` decorator at `src/workflows/builtins/retry.py`. The decision to use a bare 6-line loop instead of `tenacity` is locked at `retry.py` line 19 (no new dependency).
- **Multi-input edge STRUCTURE** — shipped: parser's `EdgeDef` (`src/workflows/parser.py:499-522`) carries `from`/`to`/`binds`/`branch`/`when`; executor's `_render_bound_inputs()` (`src/workflows/executor.py:637-656`) merges every inbound edge's `binds:` into one dict for the receiver.
- **AND-strict trigger semantics** — shipped: `_completed_or_skipped` tracking + predecessor closure at executor.py:369-375.
- **`WorkflowBindCollision`** — shipped at parse time in `src/workflows/validator.py` (raises when two edges into the same target bind the same key).
- **Validator branching invariants** — shipped: `WorkflowBranchOnNonBranchable` + `WorkflowBranchMissingArm` at `src/workflows/validator.py:540-590`.

What 3.1 LEFT for 3.3 is the **runtime wiring** for the four pieces the parser/validator have been preparing for since Phase 3.0:

1. **Runtime branch dispatch** — the executor reads `edge.branch` AT FIRE TIME and filters which outgoing edges of a branchable source actually trigger. `bash` non-zero fires false-branch edges instead of aborting. Authored Python `bool` / `tuple[bool, BaseModel]` returns drive the same dispatch. Today: `edge.branch` is parsed and validated but `src/workflows/executor.py` never reads it (grep confirms; only the BUILTIN_PHASE_MAP comment + an unrelated capture-rule comment mention it).
2. **The `inputs.*` Jinja namespace** — `_render_jinja()` (`src/workflows/executor.py:658-670`) currently exposes `inputs=dict(self._run.inputs)` — the workflow's TOP-LEVEL inputs only, identical for every node. Per master spec §6.4 line 698, edge-bound `binds:` keys must appear in the receiver's Jinja scope as `{{ inputs.<key> }}` so built-in node config fields (e.g., `command: "echo {{ inputs.context }}"`) can reference upstream output. Today: only authored Python @nodes get edge-bound data (via the `bound_inputs` runner kwarg); built-in nodes' config strings see only workflow-level inputs.
3. **Runtime `WorkflowBindTypeMismatch`** — the class exists at `src/workflows/errors.py:490-528` but is never raised; the runtime strict-mode validation hook is missing. Per master spec §6.4 lines 706-707, the engine must validate every rendered bind value against the receiver's typed `Inputs(BaseModel)` model under `strict=True`. The error class's constructor takes `file`/`line` (a parse-time shape inherited from `WorkflowValidationError`); Phase 3.3 reuses it for runtime raises with `file`/`line` populated from the source YAML node — preserves caller-message uniformity at the cost of a slightly unusual classification (the parent hierarchy is `WorkflowValidationError`, which the validator-as-runtime-gate posture from Phase 3.5+'s state validation work also adopts).
4. **`inputs.*` namespace for branchable bash** — when bash's `command:` references `{{ inputs.* }}`, the same scope rule applies; the bash runner must render its config AFTER receiving the bound inputs.

This is a **runtime-only phase**: zero parser changes, zero validator changes, zero new error classes. The validator's branching invariants already block ill-formed YAML at parse time — Phase 3.3 is the executor honoring what the validator promised would arrive.

The verification gate is three fixtures: a multi-input fan-in workflow whose receiver consumes `{{ inputs.* }}` keys correctly; a bash node whose non-zero `exit_code` routes to the false-branch edge instead of aborting; an authored Python `@node` returning `tuple[bool, BaseModel]` whose branch value drives downstream edge selection.

**This spec breaks the phase into 4 sequential steps**, each with its own Coder and Reviewer cycle. The build plan lives at `process/phase3.3-modifiers-branching-build.md`.

---

## Scope

**In scope (delivered across the 4 steps):**

- **`inputs.*` Jinja namespace** — `IterativeExecutor._render_jinja()` accepts a per-node `inputs_overlay` parameter populated from the receiver's `_render_bound_inputs(node_name)` result. The scope becomes `inputs=<merged dict>`: edge-bound keys overlay onto workflow-level inputs (edge-bound wins on conflict; conflicts are rare because parse-time `WorkflowBindCollision` blocks duplicate edge keys, and the workflow-vs-edge name collision is documented as the author's responsibility). For the start node, the overlay is empty; the workflow-level inputs are the entire scope.
- **Runtime bind type validation** — after `_render_bound_inputs(node_name)` produces the receiver's edge-bound dict, the executor validates the dict against the receiver's `Inputs(BaseModel)` model under `strict=True` (Pydantic v2 `model_validate(..., strict=True)`). Mismatch raises `WorkflowBindTypeMismatch` with `file`/`line` populated from the receiver's YAML position. The exception aborts the run via Phase 3.1's existing failure path.
- **Branch value extraction** — a typed helper `compute_branch_value(node_def, captured_output) -> bool | None` returning `None` for non-branchable nodes:
  - `bash`: `captured_output["exit_code"] == 0`.
  - Authored Python `@node` returning `bool`: `captured_output["value"]`.
  - Authored Python `@node` returning `tuple[bool, BaseModel]`: `captured_output["value"]` (the bool was stored at the `value` key per master spec §4.0 line 217).
  - All other node types (`noop`, `agent`, future leaves): `None`.
- **Edge filtering at fire time** — after a source completes, the executor computes its branch value (via the helper above) and filters outgoing edges:
  - If `branch_value is None` (non-branchable source): all outgoing edges fire as before (every edge's `branch:` must be `None` here per the validator).
  - If `branch_value is True`: only edges with `branch is True` OR `branch is None` fire.
  - If `branch_value is False`: only edges with `branch is False` OR `branch is None` fire.
- **Pruned subgraph handling** — nodes whose ONLY path to the source was through a never-fired branch edge are marked permanently "not-going-to-fire" (call this set the **pruned set**). Pruned nodes count as completion for AND-strict purposes (rule 1's extension — see §8.6). Pruned nodes do NOT emit `workflow.node.started` or `workflow.node.completed`. Their slot in the workflow's inferred output dict contributes `None` per master spec §6.6.
- **Bash non-zero is NOT a failure** — when bash exits non-zero AND has at least one outgoing edge with `branch: false`, the executor treats it as success (emits `workflow.node.completed`), routes to the false-branch edges, and continues. When bash exits non-zero AND has no `branch:` edges (the validator allowed this — non-branching bash usage), the existing behavior holds: failure aborts the run per Phase 3.1.
- **Authored Python `bool` / `tuple[bool, BaseModel]` capture shape** — confirm the `CatalogNodeRunner` returns the expected `{"value": bool}` or `{"value": bool, **model.model_dump()}` shape per master spec §4.0 line 217. If the runner currently returns a different shape, Phase 3.3 adjusts it AND the `compute_branch_value` helper consumes the canonical form.
- **Cancellation, timeout, retry composition** — all three modifiers apply BEFORE branch value computation. A failed-after-retries bash node never reaches branch dispatch; the run aborts per the abort-on-failure rule. A timed-out bash node likewise aborts. Cancellation at the next node boundary remains the workflow-level abort surface.
- **Event payload extension** — `workflow.node.completed` for branchable nodes gains an optional `branch_value: bool | None` payload entry (when non-`None`, names which arms fired). Useful for forensics; the JSONL viewer can render it.
- **Pruned-node sentinel event** — the executor emits one `workflow.node.pruned` event per pruned node carrying `{node_name, reason: "branch_not_taken", upstream_source: <source_node>}`. This is the SINGLE addition to the executor's emitted event-kind list (master spec §11.1 stays accurate by extension); the event is silent on success workflows and important for cross-JSONL forensics.
- **Multi-input + branching composition** — when a multi-input receiver's incoming edges come from MULTIPLE sources whose branch values disagree, normal AND-strict semantics apply: each incoming edge fires or is pruned independently per its source's branch value, and the receiver fires when ALL inbound edges have reached a terminal decision (fire-or-prune). A receiver with at least one inbound edge pruned but at least one fired is NOT itself pruned — the receiver fires with the bound inputs from the fired edges only, and the pruned edges' bind keys are absent from `inputs.*`. Authors using `default()` filters handle absent keys idiomatically (per Phase 3.1 §8.1 rule 1's `when:`-skip parallel).
- **Golden trace fixtures** at `tests/golden/runs/` for: branching bash (non-zero takes false-branch); branching authored Python (`tuple[bool, BaseModel]`); multi-input fan-in with mixed branches.
- **Starter workflow demo** — `bundles/kolsch/workflows/branching-demo.yaml` runs end-to-end exercising bash exit-code branching.

**Out of scope (deferred to later sub-phases per `phase3.x-roadmap.md`):**

- `branch` node type runtime (Phase 3.6). The `branch:` field on EDGES (this phase) is orthogonal to the `branch` NODE TYPE (Phase 3.6) per the comment at `src/workflows/executor.py:84`. Phase 3.3 does not register a runner for `kolsch/nodes/branch`.
- `filter` / `compute` runtime (Phase 3.6).
- `user_input` runtime (Phase 3.4).
- `read_file` / `write_file` / `list_files` / `set_state` / `delete_state` runtime (Phase 3.5).
- `map` / `workflow` (sub-workflow) runtime (Phase 3.7).
- Edge-level `when:` gate runtime — the parser carries an `EdgeDef.when` field (parsed but not enforced at runtime). Whether to ship edge-level `when:` in Phase 3.3 OR defer is decided at the Coordinator's call during Step 1 (recommended: defer; the orthogonal complexity is significant and the current scope already covers the master-spec branching contract). If deferred, the field stays parsed-but-warned at parse time with a tracker; Phase 3.6 sweeps it up with the branch node.
- Concurrent branch execution (Phase 3.10).

**Decisions explicitly locked in master spec §2.5 / §4.1.1 / §5.4 / §6.4 / §7.3 (do NOT relitigate):**

- **`branch:` on edges takes literal `true`/`false` only.** No truthy/falsy expressions on edges; the parser already enforces this. (A future "branchable enum" — `branch: error | success | timeout` — is a deferred QoL, not Phase 3.3 work.)
- **Bash branchability is structural.** Bash IS branchable; whether the workflow USES it as branchable depends on whether outgoing edges carry `branch:`. A bash node with no `branch:` edges still aborts on non-zero exit. A bash node with at least one `branch: true` AND `branch: false` outgoing edge routes by exit code; non-zero is success-of-the-false-arm.
- **Authored Python branchability is signature-derived.** A `@node` returning `bool` or `tuple[bool, BaseModel]` is branchable. A `@node` returning a `BaseModel` is NOT branchable; the validator rejects `branch:` edges from such nodes (`WorkflowBranchOnNonBranchable`).
- **`when:` gate skip pruning extends to outgoing branch edges.** A skipped source (via `when: false`) does NOT fire any outgoing edge — branchable or not. All downstream paths are pruned per the cascade rule below.
- **Pruning cascades.** A node is pruned if EVERY incoming edge is pruned. A node fires if AT LEAST ONE incoming edge fires. Mixed (some fired, some pruned) is the multi-input partial-fan-in case described above: fires with the fired edges' binds only.
- **No `workflow.node.skipped` event.** Skipped nodes are implicit (per master spec §11.2). The new `workflow.node.pruned` event is explicit because pruning is a STRUCTURAL DECISION (branch-not-taken cascading), not a per-node MODIFIER decision (`when:` false). The distinction matters for forensics and cross-JSONL correlation.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2 (strict-mode for the new bind validation), Jinja2 (`JINJA_ENV` at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py:499` — `EdgeDef` carries `from`/`to`/`binds`/`branch`/`when`. Phase 3.3 reads `branch:` at runtime; does NOT mutate the model.
- `src/workflows/parser.py:117-176` — the typed `Inputs(BaseModel)` models per built-in (`BashInputs`, `NoopInputs`, etc.). Phase 3.3 validates the receiver's bound dict against the matching model under `strict=True`.
- `src/workflows/validator.py:516-590` — branching invariants already enforced at parse time. Phase 3.3 trusts these; the executor's runtime code may assume `branch:` edges originate only from branchable sources and that both arms are present.
- `src/workflows/executor.py:380-490` — the worklist body. Phase 3.3 inserts: (a) bind-type validation after `_render_bound_inputs`; (b) branch-value computation after the runner returns; (c) edge filtering before extending the worklist; (d) pruning cascade.
- `src/workflows/executor.py:617-631` — `_when_holds()` already shipped. Phase 3.3 does NOT modify it.
- `src/workflows/executor.py:637-670` — `_render_bound_inputs()` + `_render_jinja()` already shipped. Phase 3.3 extends `_render_jinja()` to accept a per-node `inputs_overlay` kwarg.
- `src/workflows/builtins/bash.py:141-145` — `BashRunner` already returns `{"stdout", "stderr", "exit_code"}`. Phase 3.3 may extend the runner to render `command:` using the merged `inputs.*` scope (currently it renders against the workflow-level scope only). Verify this in Step 0 — if `command:` is already rendered via `_render_jinja` from the executor with the overlay, no runner change needed.
- `src/workflows/builtins/catalog_node_runner.py:260-275` — already parses all three @node return forms. Phase 3.3 confirms the canonical capture shape (`{"value": bool}` and `{"value": bool, **model.model_dump()}`) and adjusts if drift.
- `src/workflows/errors.py:490-528` — `WorkflowBindTypeMismatch` class exists with parse-time-shaped constructor (`file`/`line`/`node`/`bind`/`expected_type`/`actual_type`). Phase 3.3 raises it from the executor with these args populated from the receiver's YAML position. NO class signature change.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
git status

# Bootstrap a fresh workspace for smoke testing
uv run kolsch --root /tmp/kolsch-branch-test setup --non-interactive

# Run the test suite
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## §6.4 The `inputs.*` Jinja namespace (runtime contract)

### §6.4.1 Scope composition

`IterativeExecutor._render_jinja(expression, *, inputs_overlay=None)` builds the Jinja scope as:

```python
scope = {
    "nodes": self._captured,
    "state": self._state,
    "inputs": {
        **dict(self._run.inputs),
        **(inputs_overlay or {}),
    },
}
```

Workflow-level inputs are the baseline; edge-bound keys overlay. Conflicts (a workflow input named `context` and an edge bind also named `context`) resolve to the edge bind's value. Authors who care about disambiguation use explicit names; the parser's `WorkflowBindCollision` check (already shipped) prevents cross-edge conflicts. Workflow-vs-edge name collisions are diagnostic-only (logged at workflow-run startup) — no error.

### §6.4.2 Caller responsibility

The executor's worklist body computes `bound_inputs = self._render_bound_inputs(node_name)` before invoking the runner (already shipped). Phase 3.3 ALSO computes the overlay scope for the node's OWN config-field interpolation. Both happen at the same boundary — once `bound_inputs` is materialized, it is:

1. Validated against the receiver's typed `Inputs(BaseModel)` under strict mode (§6.4.4 below).
2. Used as the runner's `bound_inputs` kwarg (existing behavior; for authored Python @nodes, this becomes the `Inputs` Pydantic model).
3. Used as `inputs_overlay=` when the runner or executor renders that node's CONFIG STRINGS (the node's `command:` / `path:` / `agent:` ref / etc. — anywhere `{{ inputs.<key> }}` appears in the node's own YAML).

For built-in nodes whose config rendering happens in the runner (currently `BashRunner.run`), Phase 3.3 routes the rendering through the executor's `_render_jinja` (or passes `inputs_overlay` to the runner so the runner can render with the correct scope). The Step 1 design call picks the cleaner path.

### §6.4.3 Pruned edges and missing keys

When a multi-input receiver has SOME inbound edges fired and others pruned, the pruned edges' bind keys are absent from `bound_inputs`. The Jinja scope's `inputs.<pruned_key>` resolves to `Undefined` (Jinja's standard); authors use `{{ inputs.maybe_key | default('fallback') }}` to handle. This parallels Phase 3.1's `when:`-skip behavior per AND-strict rule 1.

### §6.4.4 Strict-mode bind validation

After `bound_inputs = self._render_bound_inputs(node_name)`, the executor calls:

```python
node_def_inputs_model = _inputs_model_for(node_def)  # e.g., BashInputs for type=kolsch/nodes/bash
try:
    node_def_inputs_model.model_validate(bound_inputs, strict=True)
except ValidationError as exc:
    # Pick the first error to populate the exception fields. The
    # ValidationError carries N sub-errors; we report on the first
    # for actionability. The other sub-errors are documented as a
    # related-issues note (§14.4) but not raised separately.
    first = exc.errors()[0]
    raise WorkflowBindTypeMismatch(
        file=<receiver_yaml_file>,
        line=<receiver_yaml_line>,
        node=node_name,
        bind=str(first["loc"][0]),
        expected_type=first["type"],  # Pydantic's type tag, e.g., "int_parsing"
        actual_type=type(<value_at_loc>).__name__,
    ) from exc
```

The `<receiver_yaml_file>` and `<receiver_yaml_line>` come from the YAML position attached to the parsed `NodeDef` during Phase 3.0 parsing (ruamel.yaml retains source positions). If the receiver lacks a position record (synthesized nodes — none today, but future map-iteration workers may not carry one), the file/line are populated from the workflow document itself.

The `BashInputs` model already exists and has `extra="forbid"` (parser.py:147); the validator above will reject extra keys too — useful for catching typos like `binds: { stdoot: ... }`.

For authored Python @nodes (`CatalogNodeRunner`), the receiver's `Inputs` model is the user-declared Pydantic model in the `@node`-decorated function's first parameter. The same strict-mode validation applies.

**Edge case — empty `binds:`.** If the inbound edge carries no `binds:`, `bound_inputs == {}`. Most built-in `*Inputs` models (e.g., `NoopInputs`) have `extra="forbid"` AND no required fields, so `model_validate({}, strict=True)` succeeds. Models with required fields (none today, but future) would raise — that's the correct behavior (the workflow author forgot to bind a required input).

---

## §6.5 Branch value extraction (runtime contract)

`src/workflows/branching.py` (NEW module) ships a single function:

```python
def compute_branch_value(
    node_def: NodeDefUnion,
    captured: dict[str, Any],
) -> bool | None:
    """Phase 3.3 — extract the branchable boolean for edge dispatch.

    Returns ``None`` for non-branchable nodes. Caller filters
    outgoing edges accordingly.
    """
```

Rules:

- `kolsch/nodes/bash` → `bool(captured["exit_code"] == 0)`. The validator guarantees `exit_code` is present in the captured shape (per the typed BashOutputs).
- `<bundle>/nodes/<name>` (authored Python catalog node) → introspect the runner's recorded return-type discrimination:
  - If the runner's last invocation produced `{"value": <bool>}` ONLY (the `bool` return form), `captured["value"]` is the branch value.
  - If the runner produced `{"value": <bool>, **<model>}` (the `tuple` return form), `captured["value"]` is the branch value.
  - If the runner produced a plain BaseModel `model_dump()` (no `value` key, or `value` is not a bool), the node is NOT branchable — return `None`. The validator would have rejected `branch:` edges from this source already; the runtime check is defensive.
- All other node types — `noop`, `agent`, future leaves — return `None`. The validator prevents `branch:` edges from these sources at parse time.

The dispatcher is type-driven via the node's `type` field; no introspection of the node's signature is required (the parser+validator have already committed to a branchability decision via the return-type rules — the runtime trusts it).

---

## §6.6 Edge filtering and pruning cascade (runtime contract)

After the runner returns successfully:

1. **Compute branch value** via `compute_branch_value(node_def, captured)`.
2. **Filter outgoing edges**:
   - `branch_value is None` (non-branchable): every outgoing edge fires (the validator guarantees no `branch:` field on these edges).
   - `branch_value is True`: edges with `edge.branch in (True, None)` fire; edges with `edge.branch is False` are PRUNED.
   - `branch_value is False`: edges with `edge.branch in (False, None)` fire; edges with `edge.branch is True` are PRUNED.
3. **Cascade pruning** via reverse-reachability:
   - Maintain a `_pruned: set[str]` on the executor (node names).
   - For each pruned edge `e`, descend the graph from `e.to`. A node `n` is added to `_pruned` IFF every incoming edge of `n` is either (a) pruned at this iteration OR (b) sourced from a node already in `_pruned`.
   - The cascade stops at any node with at least one inbound edge that is NOT pruned (multi-input partial-fan-in case — see §6.6.1 below).
4. **Emit `workflow.node.pruned`** for each newly pruned node, with payload `{node_name, reason: "branch_not_taken", upstream_source: <name of branchable source that didn't fire>}`. Pruned nodes are added to `_completed_or_skipped` immediately so AND-strict downstream checks treat them as decided.

### §6.6.1 Multi-input partial-fan-in

A receiver `R` with two incoming edges `e1` (from `A`, fired) and `e2` (from `B`, pruned because `B` branched the other way) is NOT pruned. `R` fires when both `A` and `B` have reached terminal decisions (fire-or-prune); the runtime computes `bound_inputs(R) = e1.binds rendered` only (e2's binds are absent). The `WorkflowBindCollision` check at parse time already guaranteed `e1` and `e2` had distinct bind keys, so the merged dict is well-defined.

When the receiver's `Inputs` model requires a field that came from the PRUNED edge, strict-mode validation (§6.4.4) raises `WorkflowBindTypeMismatch` because the required key is missing — `expected_type` is the model's declared type, `actual_type` is `"missing"`. The author's fix is to add a default value to the receiver's input model OR restructure the graph so the required key isn't behind a branch.

### §6.6.2 No race with AND-strict

The AND-strict completion check (already in Phase 3.1) reads `_completed_or_skipped`. Phase 3.3 inserts pruned nodes into the same set at the moment of pruning. Downstream receivers correctly fire when all their inbound sources have reached terminal decisions — completed, skipped, or pruned. No timing window where a downstream is blocked on a node that will never fire.

---

## §8.5.5 Bash success-vs-failure semantics (revised)

Phase 3.1's bash runner aborts the run on non-zero exit (the default failure path). Phase 3.3 splits this:

- **Bash without `branch:` outgoing edges**: existing behavior. Non-zero exit raises (no change). The validator already permits this — non-branching bash usage is a common case (CI scripts, builds).
- **Bash WITH at least one `branch:` outgoing edge**: non-zero exit is SUCCESS. The runner returns the captured `{stdout, stderr, exit_code}` normally; `compute_branch_value` reads `exit_code == 0` (False) and the false-branch edges fire.
- **Determining which case applies**: the executor inspects the graph at fire time — does this bash node's outgoing edge set include any edge with `branch is not None`? If yes, suppress the non-zero-exits-abort behavior for this node and route via branch dispatch. If no, keep Phase 3.1's abort behavior.

The check is `O(outgoing_edges(node_name))` per node and happens once at fire-time; no precomputation needed.

---

## §11.1 Event kinds emitted by the executor (extension)

Phase 3.3 adds ONE new event kind to master spec §11.1: `workflow.node.pruned`.

Payload:

```python
{
    "node_name": str,                    # the pruned node
    "reason": "branch_not_taken",        # string-coded reason
    "upstream_source": str,              # name of the branchable source whose branch value caused this prune
    "branch_value": bool,                # the source's branch value (true/false)
}
```

Common fields per master spec §11.3: `kind`, `workflow_run_id`, `trace_id`, `parent_event_id` (the source's `workflow.node.completed` event), `ts`.

The event ordering rule (master spec §11.2): pruned-node events follow the source's `workflow.node.completed` in the JSONL. Multiple pruned events for one source are emitted contiguously, one per pruned descendant.

`workflow.node.completed` for branchable sources gains an optional `branch_value: bool | None` payload entry (consistent with the pruned events' source).

---

## §12.1 Error classes (no additions)

Phase 3.3 reuses two existing error classes:

- `WorkflowBindTypeMismatch` (`src/workflows/errors.py:490-528`) — raised at runtime from `IterativeExecutor`'s strict-mode bind validation path. Constructor unchanged.
- `WorkflowNodeFailed` (`src/workflows/errors.py:1185`) — the executor's existing failure path raises this for any uncaught exception from a runner. No change.

No new error classes ship in Phase 3.3.

---

## CLI Surface

Phase 3.3 adds NO new CLI commands. The Phase 3.1 `kolsch workflows run` command is unchanged; a branching workflow runs end-to-end via the same surface. `kolsch workflows events <run_id>` displays the workflow JSONL including the new `workflow.node.pruned` events.

A future QoL (not in 3.3 scope): a `--show-pruned` flag on `events` that visually distinguishes pruned events from completed events.

---

## Phase-level Acceptance Criteria

- A bash node with `branch: true` AND `branch: false` outgoing edges: zero exit fires true-branch edges; non-zero exit fires false-branch edges; the run continues either way (no abort on non-zero).
- A bash node with NO `branch:` outgoing edges retains Phase 3.1 behavior — non-zero aborts the run.
- An authored Python `@node` returning `bool` drives branch dispatch — `True` fires `branch: true` edges, `False` fires `branch: false` edges.
- An authored Python `@node` returning `tuple[bool, BaseModel]` drives branch dispatch on the bool AND exposes the model's fields as part of captured output for downstream consumption.
- A multi-input receiver consumes `{{ inputs.<key> }}` in its config strings; the rendering pulls from edge-bound `binds:` first, workflow-level inputs second.
- A receiver whose bound inputs fail Pydantic strict-mode validation raises `WorkflowBindTypeMismatch` with `expected_type` and `actual_type` populated; the run aborts via the existing failure path.
- Pruned nodes emit `workflow.node.pruned` with the documented payload; they do NOT emit `workflow.node.started` or `workflow.node.completed`.
- A pruned subgraph correctly cascades: every node downstream of a never-fired branch edge is pruned, UNLESS the node has another inbound edge that DID fire.
- A multi-input receiver with mixed fired/pruned incoming edges fires with the fired edges' binds only; pruned edges' bind keys are absent from `inputs.*`.
- `workflow.node.completed` for branchable nodes carries `branch_value` in payload.
- Workflow inferred output dict carries `None` for any terminal node that was pruned (per master spec §6.6).
- `when:`-skip and pruning compose: a skipped source cascades a prune-equivalent through ALL of its outgoing edges (the existing Phase 3.1 behavior is structurally identical to the new pruning machinery — confirm via test).
- No regressions: full suite still green; existing non-branching workflows produce byte-identical JSONLs.

---

## Phase-level Integration Tests

### Phase Test 1: Bash exit-code branching
- **Run:** `uv run kolsch --root /tmp/kolsch-branch-test workflows run kolsch/workflows/branching-demo`
- **Expect:** exit 0; workflow JSONL contains the bash node's `workflow.node.completed` with `branch_value=False` (non-zero exit); the false-branch downstream fires; the true-branch downstream is pruned.
- **Fail means:** branch dispatch broken OR pruning cascade broken OR success/abort semantics inverted.

### Phase Test 2: Authored Python bool branching
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_python_bool_branching -v`
- **Expect:** an authored `@node` returning `True` triggers true-branch edges; downstream fires; false-branch is pruned.
- **Fail means:** `compute_branch_value` extracts the wrong key OR `bool` return shape regressed.

### Phase Test 3: Multi-input + inputs.* namespace
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_multi_input_namespace -v`
- **Expect:** a receiver with config `command: "echo {{ inputs.greeting }} from {{ inputs.source }}"` sees both keys from its two inbound edges' `binds:` rendered correctly.
- **Fail means:** `_render_jinja` scope composition broken.

### Phase Test 4: Strict-mode bind validation
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_bind_type_mismatch_raises -v`
- **Expect:** a workflow binding `attempt: "{{ '5' }}"` (a string) into a receiver expecting `attempt: int` raises `WorkflowBindTypeMismatch` with `expected_type` and `actual_type` populated; the run aborts.
- **Fail means:** strict-mode validation not wired OR exception fields wrong.

### Phase Test 5: Pruning cascade with multi-input fan-in
- **Run:** `uv run pytest tests/component/test_workflow_branching.py::test_pruning_with_partial_fan_in -v`
- **Expect:** a 4-node fixture: `A` (branchable, fires `branch: false`) → `B` (pruned). `C` (always fires) → `D`. `B → D` AND `C → D`. Assert `D` fires with only `C`'s binds; `B` is pruned; `D` is NOT pruned.
- **Fail means:** pruning cascade incorrectly marks `D` as pruned despite a fired inbound edge.

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green. Non-branching workflows produce byte-identical JSONLs (modulo timestamps).

---
