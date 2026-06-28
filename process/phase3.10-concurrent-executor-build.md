# [SPEC-011] Phase 3.10 — Concurrent executor

**Type:** spec
**Doc #:** 011
**Status:** Draft
**Created:** 2026-06-28

---

## Glossary

| Term | Meaning |
| --- | --- |
| concurrency mode | The workflow-level `concurrency:` field. `sequential` (default — Phase 3.1 — 3.7 behavior) or `auto` (Phase 3.10 fan-out under safety invariants). |
| ready set | The nodes whose every predecessor is in `_completed_or_skipped` AND whose state-touch invariants permit firing now. |
| in-flight set | The asyncio tasks currently dispatched but not yet completed. |
| state-write set | The set of `state.*` keys statically declared by a node's transitive descendants' `set_state` / `delete_state` `keys:` lists. |
| state-read set | The set of `state.*` keys statically discovered in a node's Jinja-rendered config strings (transitive over the branch). |
| parallel branch | A connected subgraph that begins at a fan-out point (a node with >1 outgoing edge, where each edge has no `branch:` selector) and ends at the next confluence (a node with >1 incoming edge). |
| state-mutation lock | An executor-private `asyncio.Lock` held across the dispatch of a `set_state` / `delete_state` invocation so the `workflow.state.mutated` event stream is total-ordered. |
| emit lock | An executor-private `asyncio.Lock` held across `emit_event(...)` writes to the JSONL so concurrent emissions don't corrupt line framing. |
| run-id-short | The first 8 chars of `run.run_id` after the `wrk_` prefix; used as the `sys.modules` namespace tag. |
| per-run module prefix | `_kolsch_wrk_<run-id-short>.` — the `sys.modules` key prefix bundle modules load under during this run. |

---

## Background

Phase 3.10 closes the engine's concurrency story. Every dependency
already exists in the codebase per the surface map:

- **`IterativeExecutor`** at `src/workflows/executor.py:261-507` —
  the worklist body Phase 3.10 generalises. The `_continue_worklist`
  loop (line 1061-1500-ish) is the sequential dispatch site; the
  Phase 3.10 surgery wraps each iteration of the topological walk
  in an `asyncio.gather` over ready-set tasks.
- **`MapRunner`** at `src/workflows/builtins/map.py:64-327` — the
  sequential map runtime. Phase 3.10 adds a `_run_parallel` branch
  gated on `node_def.parallel`.
- **`snapshot_sys_modules` / `cleanup_bundle_imports`** at
  `src/workflows/module_isolation.py:42-99` — the global-namespace
  isolation Phase 3.10 supplements with the per-run module prefix.
- **`emit_event`** at `src/workflows/events.py` — the JSONL append
  Phase 3.10 wraps with an `asyncio.Lock`.
- **`_replay.py`** at `src/workflows/_replay.py` — line-by-line
  replay machinery Phase 3.10 verifies is robust under interleaved
  event streams (no change expected; tests confirm).
- **`set_state` / `delete_state` nodes** at
  `src/workflows/builtins/state.py` (Phase 3.5) — the state
  mutators whose static `keys:` lists feed the state-write-set
  analysis. The `workflow.state.mutated` event kind is the
  load-bearing event Phase 3.10's gate inspects via the parser
  (not the JSONL — analysis is static, not runtime).
- **`Workflow` model** at `src/workflows/parser.py:571-624` — the
  top-level Pydantic model Phase 3.10 extends with `concurrency:`.
- **`MapNodeDef`** at `src/workflows/parser.py:345-352` —
  extended with `parallel: bool` + `concurrency: int | None`.
- **`validator.py`** at `src/workflows/validator.py` — run-start
  gate Phase 3.10 extends with the state-conflict analysis.
- **`WorkflowValidationError`** at `src/workflows/errors.py:146` —
  parent class for the new `WorkflowParallelStateConflict` /
  `WorkflowInvalidConcurrency`.
- **`workflow.run.started.payload`** at `src/workflows/events.py` —
  payload dict Phase 3.10 extends with `concurrency_mode`.
- **`workflow.run.completed.payload`** at `src/workflows/events.py` —
  payload dict Phase 3.10 extends with `peak_in_flight`.

What's LEFT for Phase 3.10:

1. **`Workflow.concurrency: Literal["sequential", "auto"] = "sequential"`**
   field on the top-level workflow model.
2. **`MapNodeDef.parallel: bool = false`** + **`MapNodeDef.concurrency: int | None = None`**
   fields.
3. **`WorkflowParallelStateConflict`** error class (master spec
   §16.4 / §16.8).
4. **`WorkflowInvalidConcurrency`** error class (or audit + reuse
   if one already exists for `timeout_seconds`-style negative-int
   checks; Step 0 picks).
5. **Run-start gate static analysis**:
   - For each `auto`-mode workflow: identify parallel branches via
     graph fan-out + confluence walk; compute each branch's
     state-write set + state-read set; reject conflicts with
     `WorkflowParallelStateConflict`.
   - For each `parallel: true` map: walk the worker subgraph; reject
     if it transitively contains `set_state` / `delete_state` with
     `WorkflowParallelStateConflict`.
   - Reject non-positive `concurrency:` integers with
     `WorkflowInvalidConcurrency`.
6. **`IterativeExecutor` concurrent worklist** for `auto`-mode:
   - Replace the sequential `for node_name in topo:` loop body
     with a fixed-point loop over a ready set; dispatch via
     `asyncio.gather` (no `Semaphore` at v1 — workflow-level
     concurrency is unbounded per master spec §16.2).
   - `self._emit_lock: asyncio.Lock` wrapping every `emit_event`
     write.
   - `self._state_mutation_lock: asyncio.Lock` held around any
     `set_state` / `delete_state` invocation so the
     `workflow.state.mutated` stream is total-ordered.
   - Failure handling: first failure cancels in-flight; cancelled
     tasks emit `workflow.node.failed` with `reason="sibling_failure"`.
   - `cancel.flag` polling at the gather loop's top per spec §16.6.
   - Track `peak_in_flight` and emit on `workflow.run.completed`.
7. **`MapRunner` parallel path** for `parallel: true`:
   - `asyncio.gather` over `_run_one_iteration(...)` calls; bounded
     by `asyncio.Semaphore(node_def.concurrency)` when set.
   - Preserve `results` index order (collect into a list keyed by
     index, then sort + flatten).
   - Failure: first failing iteration cancels the rest; emit
     `workflow.node.failed` per cancelled iteration; map's own
     `workflow.node.failed` wraps the first exception.
8. **`RunModuleNamespace`** at `src/workflows/module_isolation.py`:
   - Holds the per-run prefix (`_kolsch_wrk_<run-id-short>.`).
   - `wrap_module_name(canonical_name: str) -> str` returns the
     prefixed key.
   - `cleanup_for_run(prefix: str) -> int` deletes every
     `sys.modules` entry starting with the prefix; returns count.
9. **`CatalogNodeRunner` extension** at
   `src/workflows/catalog_node.py`:
   - On import resolution, register the loaded module under the
     run-prefixed key instead of (or in addition to) the canonical
     key. The Phase 3.1 `cleanup_bundle_imports` continues to
     handle the canonical-key fallback for non-prefix-tagged
     loads (so a sequential run still works exactly like Phase 3.1).
10. **`workflow.run.started.payload.concurrency_mode`** field
    addition.
11. **`workflow.run.completed.payload.peak_in_flight`** field
    addition.
12. **CLI text-envelope row** in `cli/workflows.py` printing
    `concurrency_mode` + `peak_in_flight` after the existing
    output dict.

The verification gate is six scenarios: (1) an `auto`-mode workflow
with three sibling bash nodes runs concurrently (wall-clock < sum);
(2) the same workflow in `sequential` mode runs serially (wall-clock
≈ sum); (3) a `parallel: true` map over 10 elements completes faster
than its sequential variant; (4) a workflow with two concurrent
branches both writing `state.counter` is rejected at the run-start
gate; (5) two concurrent runs of the same workflow loading the same
authored Python node module observe distinct module instances;
(6) a concurrent-fan-out run's JSONL replays into the same final
`_captured` / `_state` as a sequential run of the same workflow.

**This spec breaks the phase into 5 sequential steps (Step 0 — Step 4)**.

Related docs:

- `process/phase3.10-concurrent-executor-spec.md` — master design
  spec (§16.1 — §16.12).
- `process/phase3.1-executor-spec.md` — sequential executor +
  module-isolation discipline Phase 3.10 generalises.
- `process/phase3.5-file-io-state-spec.md` — state mutation event
  semantics (the load-bearing primitive for state-conflict analysis).
- `process/phase3.7-composition-spec.md` — sequential map contract
  Phase 3.10 extends.

---

## Scope

**In scope (delivered across the 5 steps):**

- `Workflow.concurrency: Literal["sequential", "auto"] = "sequential"`.
- `MapNodeDef.parallel: bool = false` + `MapNodeDef.concurrency: int | None`.
- `WorkflowParallelStateConflict` + `WorkflowInvalidConcurrency` error classes.
- Run-start gate static analysis for both auto-mode workflows and
  parallel maps.
- `RunModuleNamespace` per-run sys.modules prefix discipline.
- `CatalogNodeRunner` extension to register modules under the
  prefixed key.
- Per-run cleanup at run end via `RunModuleNamespace.cleanup_for_run`.
- `IterativeExecutor` concurrent worklist for `auto`-mode.
- `IterativeExecutor._emit_lock` + `_state_mutation_lock` discipline.
- `MapRunner` parallel iteration path with semaphore bound.
- `peak_in_flight` tracking + `workflow.run.completed.payload` extension.
- `concurrency_mode` field on `workflow.run.started.payload`.
- CLI envelope rows for `concurrency_mode` + `peak_in_flight`.
- Two starter workflows: `parallel-fan-out-demo.yaml` (auto-mode
  graph) and `parallel-map-demo.yaml` (parallel map).
- One negative-test workflow `parallel-state-conflict.yaml`.
- Integration tests at `tests/component/test_workflow_concurrent_*.py`.
- Goldens at `tests/golden/runs/` for the auto-mode and
  parallel-map happy paths.

**Out of scope (per spec §16.1 + roadmap):**

- Cross-run concurrency (scheduling multiple `workflows run`
  invocations) — `kolsch-server` concern.
- Distributed execution — `LocalController` stays single-host.
- `parallel:` on `filter` — compute recipe covers it.
- Multi-pane chat progress for auto-mode — Phase 3.9 indicator
  shape unchanged.
- Re-rendering `nodes.<X>.<field>` after sibling writes — Jinja
  renders ONCE at fire time per Phase 3.1.
- Mid-node cancellation — `cancel.flag` polls at node boundaries
  per Phase 3.1; concurrent batches cancellable only when the
  slowest in-flight node observes the flag.

**Decisions explicitly locked (do NOT relitigate):**

- **`concurrency:` default is `sequential`.** Back-compat with
  every existing workflow is load-bearing.
- **`auto` mode requires disjoint state writes.** Authors who want
  to mutate the same key from concurrent branches must serialise
  via an explicit edge; the gate rejects otherwise.
- **No `WorkflowRunSettings`-style override at runtime.** The
  `concurrency:` field is declared in the workflow YAML; the CLI
  has no `--concurrency` flag at v1.
- **`parallel: true` maps cannot mutate state.** Mirrors auto-mode's
  invariant. Mixed mutation + parallelism is REJECTED.
- **First-failure-cancels-siblings.** No `on_error: continue`
  variant for concurrent siblings at v1 (matches Phase 3.7's map).
- **`peak_in_flight` is reported per run.** A forensics aid, not a
  control surface. No `max_in_flight:` knob at v1.
- **Per-run module prefix is transparent to authored code.** The
  module object the `@node` decorator runs on is the same — only
  the `sys.modules` key changes. Authors NEVER see the prefix.
- **Event ordering in JSONL is real-time-completion-order under
  concurrency.** No topology-based re-ordering. Replay handles
  interleaving via `invocation_id` correlation (already idempotent
  per Phase 3.4).
- **Replay determinism is INHERITED from JSONL order.** Two runs
  of the same workflow may produce different interleavings; one
  run's replay is deterministic against ITS OWN JSONL.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, asyncio, ruamel.yaml, Jinja2,
pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/executor.py:261-507, 1061-1500` — `IterativeExecutor`
  + `_continue_worklist`. Phase 3.10's concurrent worklist replaces
  the sequential `for node_name in topo:` loop body.
- `src/workflows/executor.py:386-505` — module-isolation snapshot/cleanup
  pair Phase 3.10 supplements with per-run prefix discipline.
- `src/workflows/module_isolation.py:42-99` — Phase 3.1 snapshot/cleanup;
  Phase 3.10 ships `RunModuleNamespace` alongside.
- `src/workflows/builtins/map.py:64-327` — `MapRunner.run`. Gain
  the parallel branch.
- `src/workflows/parser.py:345-352, 571-624` — `MapNodeDef` +
  `Workflow`. Field extensions.
- `src/workflows/validator.py` — run-start gate. Add state-conflict
  analysis pass.
- `src/workflows/events.py` — `WorkflowRunStartedEvent` /
  `WorkflowRunCompletedEvent`. Payload extensions.
- `src/workflows/errors.py:146` — `WorkflowValidationError` parent
  class for the new error classes.
- `src/workflows/catalog_node.py` — `CatalogNodeRunner` import
  resolution.
- `src/cli/workflows.py` — text envelope formatter.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-concurrent-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- `Workflow.concurrency` field accepts `sequential` (default) or
  `auto`. Other strings raise `WorkflowValidationError` at parse.
- `MapNodeDef.parallel` defaults to `false`; `concurrency:` is
  optional but rejected if `<= 0` with `WorkflowInvalidConcurrency`.
- Every existing workflow runs unchanged under default `concurrency:
  sequential`. Goldens for Phase 3.1 — Phase 3.9 byte-identical to
  pre-Phase-3.10 snapshots.
- An `auto`-mode workflow with three independent bash nodes
  (each sleeping 200ms) completes in < 400ms wall-clock; the same
  workflow under `sequential` completes in > 550ms.
- A `parallel: true` map over 10 worker invocations (each sleeping
  100ms) completes in < 200ms with `concurrency: 5` (two batches);
  unbounded `parallel: true` completes in < 150ms.
- The map's `results` list preserves index order regardless of
  completion order (verified with deterministic per-iteration
  outputs that include the index).
- A workflow with two concurrent branches both writing `state.counter`
  is rejected at the run-start gate with `WorkflowParallelStateConflict`;
  the error message names both nodes and the conflicting key.
- A `parallel: true` map whose worker writes state is rejected
  with the same error class.
- Per-run module isolation: two concurrent runs of the same
  workflow that load the same authored Python node module observe
  distinct module instances (`id(mod_a) != id(mod_b)`). Cleanup
  at run end removes only the requesting run's prefix-tagged
  entries.
- JSONL replay parity: a concurrent-fan-out run's events.jsonl,
  replayed via `_replay.py`, reconstructs the same `_captured` /
  `_state` / `_completed_or_skipped` as the sequential variant.
- Fork from a concurrent-fan-out run produces a forked run whose
  initial replay matches the original's exact event ordering.
- `workflow.run.started.payload.concurrency_mode` reflects the
  workflow's declared mode.
- `workflow.run.completed.payload.peak_in_flight` is `1` for
  sequential runs and matches the dispatch high-water mark for
  auto-mode runs.
- `kolsch workflows run` text envelope prints
  `concurrency_mode: auto` and `peak_in_flight: N` after the
  existing output dict.
- First-failure-cancels-siblings: a concurrent branch that raises
  causes the executor to cancel every other in-flight task; the
  cancelled tasks emit `workflow.node.failed` with
  `reason="sibling_failure"` in payload; the run's status is
  `failed`.
- `cancel.flag` polled at the top of each gather iteration: an
  operator who writes the flag mid-fan-out sees the run abort
  cleanly with `workflow.run.aborted` after the in-flight batch
  completes (or is cancelled by the same flag observation).
- No regressions: full suite green; existing workflow flows
  produce byte-identical output.

---

## Phase-level Integration Tests

### Phase Test 1: Auto-mode wall-clock speedup
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_auto.py::test_auto_mode_runs_concurrent -v`
- **Expect:** three-sibling-bash workflow under `concurrency: auto`
  completes in < 400ms; same workflow under `sequential` > 550ms.

### Phase Test 2: Sequential default unchanged
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_auto.py::test_default_remains_sequential -v`
- **Expect:** a workflow with no `concurrency:` field behaves
  byte-identically to a pre-Phase-3.10 snapshot.

### Phase Test 3: Parallel map happy path
- **Run:** `uv run pytest tests/component/test_workflow_parallel_map.py::test_parallel_map_speedup -v`
- **Expect:** `parallel: true, concurrency: 5` map over 10 elements
  completes in < 200ms; `results` index order preserved.

### Phase Test 4: State conflict rejection
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_validation.py::test_state_conflict_rejected -v`
- **Expect:** `WorkflowParallelStateConflict` raised at run-start;
  error names both conflicting nodes + the shared `state.counter` key.

### Phase Test 5: Parallel map state-write rejection
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_validation.py::test_parallel_map_state_write_rejected -v`
- **Expect:** a `parallel: true` map whose worker writes state is
  rejected with `WorkflowParallelStateConflict`.

### Phase Test 6: Per-run module isolation
- **Run:** `uv run pytest tests/component/test_workflow_per_run_module_prefix.py::test_concurrent_runs_distinct_modules -v`
- **Expect:** two concurrent runs load the same module under
  distinct `sys.modules` keys; cleanup removes only this run's keys.

### Phase Test 7: JSONL replay parity
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_replay.py::test_auto_mode_replay_matches_sequential -v`
- **Expect:** auto-mode events.jsonl, replayed, reconstructs the
  same `_captured` / `_state` as a sequential run of the same
  workflow.

### Phase Test 8: Fork-from-concurrent-parent
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_fork.py::test_fork_inherits_event_ordering -v`
- **Expect:** a forked run from a concurrent-fan-out parent
  replays the parent's exact event ordering; downstream nodes
  fire correctly.

### Phase Test 9: First-failure-cancels-siblings
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_failure.py::test_sibling_cancellation -v`
- **Expect:** a concurrent branch that raises causes siblings to
  emit `workflow.node.failed` with `reason="sibling_failure"`;
  run status is `failed`.

### Phase Test 10: Cancel mid-batch
- **Run:** `uv run pytest tests/component/test_workflow_concurrent_failure.py::test_cancel_flag_aborts_batch -v`
- **Expect:** an operator-written `cancel.flag` mid-fan-out causes
  the executor to emit `workflow.run.aborted` after the in-flight
  batch is cancelled.

### Phase Test 11: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green; pre-Phase-3.10 goldens byte-identical.

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 5-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.10 through 5 sequential cycles. This is a **closing
phase** for the engine's concurrency story — the work is real but
the scope is tightly bounded by the safety invariants. Most of the
risk lives in two places: (1) the static state-conflict analysis
(getting the parallel-branch decomposition right), and (2) the
per-run module prefix surgery (avoiding regressions in the Phase
3.1 sequential isolation discipline).

**Authoritative references:**

- THIS spec: `process/phase3.10-concurrent-executor-build.md`.
- Master design spec: `process/phase3.10-concurrent-executor-spec.md`
  — §16.
- Phase 3.1 spec: `process/phase3.1-executor-spec.md` — sequential
  executor + module isolation.
- Phase 3.5 spec: `process/phase3.5-file-io-state-spec.md` — state
  mutation event semantics; load-bearing for the conflict analysis.
- Phase 3.7 build plan: `process/phase3.7-composition-build.md` —
  sequential `MapRunner`.
- `src/workflows/executor.py` — concurrent worklist surgery site.
- `src/workflows/builtins/map.py` — parallel iteration surgery site.
- `src/workflows/module_isolation.py` — `RunModuleNamespace` site.

**The 5 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — parser field extensions (`Workflow.concurrency`, `MapNodeDef.parallel` + `concurrency`); error classes (`WorkflowParallelStateConflict`, `WorkflowInvalidConcurrency`); event-payload field declarations; NO runtime behavior yet. | ~1.5 days |
| **Step 1** | Run-start gate static analysis — parallel-branch decomposition; state-write/read set computation; conflict detection; `parallel: true` map worker walk. Validator rejects bad workflows. NO executor behavior change yet (sequential remains the only path). | ~2.5 days |
| **Step 2** | Per-run module prefix — `RunModuleNamespace`; `CatalogNodeRunner` extension; per-run cleanup at run-end. Sequential behavior unchanged; per-run prefix is transparent. Phase Test 6 wired. | ~2 days |
| **Step 3** | Concurrent worklist for `auto`-mode workflows — `_emit_lock`, `_state_mutation_lock`, `asyncio.gather` over ready set; `peak_in_flight` tracking; first-failure-cancels-siblings; `cancel.flag` polling at gather top. Phase Tests 1, 2, 7, 8, 9, 10 wired. | ~3 days |
| **Step 4** | Parallel `MapRunner` — `parallel: true` branch; `asyncio.Semaphore(concurrency)` bound; index-order-preserving `results`; per-iteration cancellation on first failure. Phase Tests 3, 4, 5, 11 wired. Starter workflows + goldens. Roadmap line annotated. | ~2 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a locked decision (cross-run concurrency,
  distributed execution, `--concurrency` CLI flag, `on_error: continue`
  for siblings, `parallel:` on filter, runtime-overridable
  `concurrency:`) → REJECT.
- Coder proposes silent serialisation behind the author's back (a
  concurrent workflow auto-fallback when state writes are detected
  at runtime) → REJECT. The gate is STATIC; the contract is "opt
  in correctly OR be rejected at validate."
- Coder finds master-spec ambiguity → consult, make a binding
  call, append to THIS spec, re-brief.
- Coder discovers Phase 3.1 — Phase 3.9 contracts conflict with
  concurrent execution (e.g., a Phase 3.5 state runner relies on
  serialised reads) → flag as separate patch; resolve before
  shipping Step 3.
- Scope creep (multi-pane chat, distributed execution, cross-run
  queue) → REJECT.

**Done when:** All 5 steps Approved AND all 11 Phase-level
Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.10 line
annotated with the shipping SHA.

---

# STEP 0 — Parser + error class + event payload field PREREQ

**Goal**: ship the parser field extensions + new error classes +
event-payload field declarations. NO runtime behavior yet. Every
existing workflow continues to parse + run identically.

**Files created/modified:**

- `src/workflows/parser.py` (modified) — `Workflow.concurrency`
  field; `MapNodeDef.parallel` + `MapNodeDef.concurrency` fields.
- `src/workflows/errors.py` (modified) — `WorkflowParallelStateConflict`
  + `WorkflowInvalidConcurrency` error classes.
- `src/workflows/events.py` (modified) — declare optional
  `concurrency_mode` payload field on `WorkflowRunStartedEvent`
  and optional `peak_in_flight` on `WorkflowRunCompletedEvent`
  (typed payload models; no runtime emission yet).
- `tests/component/test_workflow_concurrent_parser.py` (new) —
  parser-level coverage.
- `tests/component/test_workflow_concurrent_errors.py` (new) —
  error class coverage.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the parser extensions + error classes. NO runtime change yet.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.10-concurrent-executor-build.md` — Step 0.
2. `process/phase3.10-concurrent-executor-spec.md` — §16.2, §16.3,
   §16.8.
3. `src/workflows/parser.py:345-352, 571-624` — current
   `MapNodeDef` + `Workflow` shape.
4. `src/workflows/errors.py:146-280` — `WorkflowValidationError`
   parent + existing error-class style.

**What to implement:**

1. **`Workflow.concurrency`** field:

   ```python
   class Workflow(BaseModel):
       ...
       concurrency: Literal["sequential", "auto"] = "sequential"
   ```

   Any other string raises Pydantic `ValidationError` at parse —
   `extra="forbid"` on the model already covers the unknown-value
   case via `Literal` enforcement.

2. **`MapNodeDef.parallel` + `MapNodeDef.concurrency`** fields:

   ```python
   class MapNodeDef(_BaseNodeDef):
       ...
       parallel: bool = False
       concurrency: int | None = None
   ```

   No validator-level positivity check at parse — Step 1's
   run-start gate does that (to keep the error class consistent
   with the other gate-tripping errors).

3. **Error classes** in `src/workflows/errors.py`:

   ```python
   class WorkflowParallelStateConflict(WorkflowValidationError):
       caller_message: ClassVar[str] = (
           "The workflow tries to run two nodes in parallel that "
           "fight over the same state key. Fix: add an explicit "
           "edge to serialise them, set concurrency: sequential, "
           "or remove the state write from one of them."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = (
           "node_a", "node_b", "state_key",
       )

   class WorkflowInvalidConcurrency(WorkflowValidationError):
       caller_message: ClassVar[str] = (
           "The workflow declares a concurrency bound that is "
           "not a positive integer."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = (
           "node", "value",
       )
   ```

   Mirror the construction style of `WorkflowBindCollision` /
   `WorkflowExtendsDepthExceeded` (file/line + identifying names
   + cause when applicable).

4. **Event payload field declarations** in `src/workflows/events.py`:
   - `WorkflowRunStartedEvent.payload` accepts optional
     `concurrency_mode: "sequential" | "auto"`. The Pydantic
     payload model gains the field with a `None` default for
     back-compat with pre-Phase-3.10 events on disk.
   - `WorkflowRunCompletedEvent.payload` accepts optional
     `peak_in_flight: int | None = None`.
   - NO runtime emission yet — Step 3 wires it.

5. **Tests in `tests/component/test_workflow_concurrent_parser.py`**:
   - Default workflow parses with `concurrency == "sequential"`.
   - Explicit `concurrency: auto` parses.
   - `concurrency: foo` raises Pydantic `ValidationError`.
   - `MapNodeDef` defaults: `parallel == False`, `concurrency is None`.
   - `MapNodeDef` with `parallel: true, concurrency: 5` parses.
   - `MapNodeDef` with `parallel: true, concurrency: -1` PARSES
     (Step 1's gate rejects it).

6. **Tests in `tests/component/test_workflow_concurrent_errors.py`**:
   - `WorkflowParallelStateConflict(file=..., line=..., node_a="a", node_b="b", state_key="k")` renders the canonical single-line form with all three keys.
   - `WorkflowInvalidConcurrency(file=..., line=..., node="m", value=-1)` renders correctly.

**Step 0 Acceptance Criteria:**

0.1. `Workflow.concurrency` field shipped; defaults to `sequential`.
0.2. `MapNodeDef.parallel` + `concurrency` fields shipped.
0.3. Error classes shipped with documented `caller_message` and
     `identifying_names_keys`.
0.4. Event-payload fields declared (optional, None default);
     existing JSONLs round-trip unchanged.
0.5. NO runtime behavior change (grep executor.py for `concurrency`
     → no new references; grep map.py for `parallel` → no new
     references).
0.6. All parser + error class tests pass.
0.7. No regressions: full suite green; existing workflows parse
     byte-identically.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] `git diff src/workflows/executor.py src/workflows/builtins/map.py` returns ZERO lines (Step 0 doesn't touch runtime)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.4 — pre-Phase-3.10 events on disk must round-trip cleanly
  through the updated payload models. Run a Phase 3.4 / 3.7
  golden test that loads an old JSONL and assert the load
  succeeds with both new fields absent.
- AC 0.5 — Step 0 is parser + errors ONLY. Any runtime code
  reference to the new fields is a scope violation.

---

# STEP 1 — Run-start gate static analysis

**Goal**: ship the validator passes that reject unsafe `auto`-mode
workflows and `parallel: true` maps. NO executor change; the
sequential path remains the only execution path.

**Files created/modified:**

- `src/workflows/validator.py` (modified) — new analysis passes:
  - `_collect_parallel_branches(workflow) -> list[set[NodeName]]`
    — branch decomposition.
  - `_branch_state_write_set(workflow, branch) -> set[StateKey]`
    — transitive `set_state` / `delete_state` `keys:` collection
    over the branch.
  - `_branch_state_read_set(workflow, branch) -> set[StateKey]`
    — transitive Jinja `state.*` scan over the branch's node
    config strings.
  - `_check_concurrency_invariants(workflow)` — runs both passes,
    raises `WorkflowParallelStateConflict` / `WorkflowInvalidConcurrency`.
- `src/workflows/validator.py` (modified) — the existing run-start
  gate calls `_check_concurrency_invariants` AFTER the existing
  shape + reachability checks.
- `tests/component/test_workflow_concurrent_validation.py` (new) —
  Phase Tests 4, 5 wired (gate rejections).
- `tests/golden/workflows/parallel-state-conflict.yaml` (new) —
  negative-test fixture.

**Effort**: ~2.5 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the run-start gate static analysis.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.10-concurrent-executor-build.md` — Step 1.
2. `process/phase3.10-concurrent-executor-spec.md` — §16.2,
   §16.2.1, §16.3.
3. `src/workflows/validator.py` — current run-start gate; the new
   pass attaches at the same dispatch site as bind-collision /
   branching checks.
4. `src/workflows/parser.py:296-310` — `SetStateNodeDef` /
   `DeleteStateNodeDef` (the `keys:` shape feeds the write-set
   computation).

**What to implement:**

1. **Parallel-branch decomposition**:

   - Walk the workflow's `graph.edges` to identify fan-out points
     (a node with >1 outgoing edge whose edges DO NOT carry
     `branch:` selectors — branch-edges are exclusive, not
     concurrent).
   - For each fan-out point, BFS forward along each outgoing edge
     until reaching a confluence (a node with >1 incoming edge).
     The nodes visited along one edge constitute one parallel
     branch.
   - A branch ends at the confluence node (exclusive); nodes
     downstream of the confluence belong to the post-fan-in
     subgraph and are analysed by their own enclosing fan-outs (if
     any).

2. **State-write set**:

   - For each branch, walk the branch's nodes; for any
     `SetStateNodeDef` / `DeleteStateNodeDef`, add its `keys:`
     entries to the write set.
   - For sub-workflow (`workflow:`) nodes inside a branch:
     Phase 3.10 v1 treats sub-workflow nodes as OPAQUE — their
     internal state writes don't leak to the parent because child
     state is isolated per Phase 3.7. The branch's effective
     write set excludes child internal writes.
   - For map (`map:`) nodes inside a branch with `parallel: false`:
     transitively collect the worker's state writes.
   - For map nodes with `parallel: true`: Step 1 rejects them
     immediately if the worker writes state (per spec §16.3) —
     these are inspected separately from the branch decomposition.

3. **State-read set**:

   - For each branch's nodes, scan the rendered + non-rendered
     Jinja config strings for `state.<key>` references. A simple
     regex pass at v1: `r"state\.([a-zA-Z_][a-zA-Z_0-9]*)"` — this
     is conservative (it might over-match a literal string that
     happens to contain the pattern), and that's fine because
     the gate's job is to REJECT possibly-unsafe workflows.

4. **Conflict detection**:

   - For each pair of branches `(A, B)` originating at the same
     fan-out point:
     - If `writes(A) ∩ writes(B)` non-empty → raise
       `WorkflowParallelStateConflict(node_a, node_b, state_key)`.
     - If `writes(A) ∩ reads(B)` non-empty OR
       `writes(B) ∩ reads(A)` non-empty → same error class.
   - For `auto`-mode workflows ONLY. `sequential`-mode workflows
     skip the analysis entirely.

5. **Map worker analysis**:

   - For every map node with `parallel: true`:
     - Walk `apply` → worker `NodeDef` → transitive descendant
       set (treating the worker as a sub-graph).
     - If any node in the descendant set is `set_state` /
       `delete_state` → raise `WorkflowParallelStateConflict(node_a=<map>, node_b=<worker>, state_key=<first>)`.
   - For `concurrency:` value: if not None and `<= 0`, raise
     `WorkflowInvalidConcurrency(node=<map>, value=<n>)`.

6. **Tests in `tests/component/test_workflow_concurrent_validation.py`**:
   - **Phase Test 4** — two branches both writing `state.counter`:
     gate raises with `node_a` / `node_b` / `state_key="counter"`.
   - **Phase Test 5** — parallel map worker writes state: gate
     raises.
   - **Read-after-write rejection** — branch A writes
     `state.config`; branch B reads it via `{{ state.config }}`:
     gate raises.
   - **Disjoint writes allowed** — branch A writes `state.a`;
     branch B writes `state.b`: gate passes.
   - **`concurrency: -1` rejected** — `parallel: true,
     concurrency: -1` raises `WorkflowInvalidConcurrency`.
   - **Sequential mode skips analysis** — a workflow with two
     branches writing the same key, but `concurrency: sequential`,
     passes the gate (because sequential = no concurrency).
   - **Sub-workflow opacity** — a branch containing a sub-workflow
     whose internal nodes write state DOES NOT contribute to the
     parent branch's write set.

7. **Fixture** `tests/golden/workflows/parallel-state-conflict.yaml`
   — the canonical negative-test workflow used by Phase Test 4.

**Step 1 Acceptance Criteria:**

1.1. `_collect_parallel_branches(workflow)` correctly decomposes
     fan-out points into branch node sets.
1.2. `_branch_state_write_set` collects keys from `set_state` /
     `delete_state` nodes and (recursively) from sequential map
     workers.
1.3. `_branch_state_read_set` collects `state.<key>` references
     from Jinja config strings.
1.4. `_check_concurrency_invariants` raises
     `WorkflowParallelStateConflict` on write-write, write-read,
     or read-write conflicts between any two parallel branches.
1.5. `parallel: true` maps whose workers write state are rejected.
1.6. `concurrency: <= 0` rejected with `WorkflowInvalidConcurrency`.
1.7. Sequential-mode workflows skip the analysis entirely.
1.8. Sub-workflow nodes inside a branch are OPAQUE (their internal
     writes don't leak into the parent's write set).
1.9. No executor change: grep `executor.py` for `concurrency` →
     no new references; grep `map.py` for `parallel` → no new
     references.
1.10. No regressions: full suite green.

**Self-check before handoff:**

- [ ] All 10 ACs met
- [ ] `git diff src/workflows/executor.py src/workflows/builtins/map.py` returns ZERO lines
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.1 — branch decomposition. Read the BFS code; trace through
  a 3-branch fan-out + a nested fan-out (one branch contains its
  own fan-out). Confirm the decomposition is recursive-correct.
- AC 1.3 — Jinja `state.*` regex. A literal string in a YAML
  containing the substring `state.foo` (e.g., `"my state.foo data"`)
  WILL trigger a false positive. Document this as a known
  conservative-match limitation; the spec calls for rejection on
  any possible conflict, so over-matching is on-contract.
- AC 1.8 — sub-workflow opacity. The branch decomposition must
  NOT recurse into a child workflow's nodes. Confirm by test.

---

# STEP 2 — Per-run module prefix (parallel-safe imports)

**Goal**: ship the per-run module-namespace discipline.
Sequential execution is unchanged in observable behavior;
internally, bundle modules now load under run-id-tagged
`sys.modules` keys. Two concurrent runs no longer race.

**Files created/modified:**

- `src/workflows/module_isolation.py` (modified) — add
  `RunModuleNamespace` class.
- `src/workflows/catalog_node.py` (modified) — `CatalogNodeRunner`
  consults `RunModuleNamespace` when resolving an import.
- `src/workflows/executor.py` (modified) — `__init__` constructs
  `self._module_ns: RunModuleNamespace`; `finally:` arm calls
  `self._module_ns.cleanup_for_run()`.
- `tests/component/test_workflow_per_run_module_prefix.py` (new) —
  Phase Test 6 wired.

**Effort**: ~2 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship per-run module isolation.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.10-concurrent-executor-build.md` — Step 2.
2. `process/phase3.10-concurrent-executor-spec.md` — §16.4.
3. `src/workflows/module_isolation.py:1-99` — Phase 3.1
   snapshot/cleanup discipline (kept as fallback).
4. `src/workflows/catalog_node.py` — import resolution site.
5. `src/workflows/executor.py:386-505` — Phase 3.1 module
   isolation call sites (3 of them — run/resume/start_from_fork).

**What to implement:**

1. **`RunModuleNamespace`** class:

   ```python
   class RunModuleNamespace:
       """Per-run sys.modules prefix (master spec §16.4)."""

       def __init__(self, *, run_id: str) -> None:
           # Run id format: "wrk_<uuid>"; short tag is "wrk_<first 8>".
           assert run_id.startswith("wrk_")
           self._prefix = f"_kolsch_{run_id[:12]}."   # "_kolsch_wrk_<short>."

       @property
       def prefix(self) -> str:
           return self._prefix

       def wrap_module_name(self, canonical: str) -> str:
           """Map a canonical module name ('bundles.kolsch.nodes.transform')
           to this run's prefixed key ('_kolsch_wrk_xyz.bundles.kolsch.nodes.transform').
           """
           return f"{self._prefix}{canonical}"

       def cleanup_for_run(self) -> int:
           """Delete every sys.modules entry under this run's prefix.
           Returns the count removed."""
           import sys
           removed = 0
           for name in list(sys.modules):
               if name.startswith(self._prefix):
                   del sys.modules[name]
                   removed += 1
           return removed
   ```

2. **`CatalogNodeRunner` extension**:

   - On a fresh import of a bundle module (when resolving a 3-seg
     ref `<bundle>/nodes/<name>`), import the module under its
     canonical name, then ALSO register it under the run-prefixed
     key via `sys.modules[ns.wrap_module_name(canonical)] = module`.
   - On subsequent imports during the SAME run, prefer the
     prefixed key (the runner caches the runner-instance under
     the prefixed key so it stays distinct across runs).
   - The canonical-name `sys.modules` entry remains for back-compat
     with the Phase 3.1 cleanup; both paths coexist.

3. **`IterativeExecutor.__init__` extension**:

   ```python
   self._module_ns = RunModuleNamespace(run_id=str(run.run_id))
   ```

4. **Per-run cleanup in `finally:` arms**:

   - `run` / `resume` / `start_from_fork` each gain
     `self._module_ns.cleanup_for_run()` in the `finally:` arm,
     IN ADDITION TO the existing `cleanup_bundle_imports(...)`
     call. Both run — the Phase 3.10 cleanup removes prefixed
     entries; the Phase 3.1 cleanup remains for back-compat with
     code paths that haven't migrated yet.

5. **`CatalogNodeRunner` API addition** (if needed):

   - `runner.resolve_module(canonical_name, *, ns: RunModuleNamespace)`
     — the seam Phase 3.10 uses. Returns the module object
     registered under the prefixed key for this run.

6. **Tests in `tests/component/test_workflow_per_run_module_prefix.py`**:
   - **Phase Test 6** — two concurrent runs of the same workflow
     loading the same bundle node module: each run sees a distinct
     module instance under a distinct `sys.modules` key; after
     cleanup, neither run's keys remain.
   - **Sequential run still works** — a single run loads + cleans
     up correctly under both the prefixed and canonical keys.
   - **Cleanup removes only this run's keys** — load run A's
     module; load run B's module; cleanup A → A's keys gone, B's
     keys remain.
   - **`sys.modules` doesn't grow unbounded** — run 1000 runs in
     sequence; assert `sys.modules` size at the end equals the
     pre-run size + the third-party imports (no per-run leakage).

**Step 2 Acceptance Criteria:**

2.1. `RunModuleNamespace` class shipped with documented prefix
     format.
2.2. `CatalogNodeRunner` registers loaded modules under the
     prefixed key.
2.3. `IterativeExecutor.__init__` constructs `_module_ns`.
2.4. `run` / `resume` / `start_from_fork` `finally:` arms call
     `cleanup_for_run()`.
2.5. Phase 3.1 `cleanup_bundle_imports(...)` continues to fire —
     both cleanups run during the migration window.
2.6. Phase Test 6 passes: two concurrent runs see distinct module
     instances.
2.7. No regressions: existing module-isolation tests pass
     unchanged; sequential goldens byte-identical.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] Manual smoke: run a sequential workflow with an authored Python node; verify the node fires correctly
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.2 — `CatalogNodeRunner` extension. Read the import
  resolution code; confirm the same module object is registered
  under BOTH the canonical and prefixed keys. A run that imports
  module M sees `sys.modules[canonical]` AND
  `sys.modules[ns.wrap_module_name(canonical)]` point at the
  same `M` instance (during the run).
- AC 2.5 — both cleanups fire. Read `run` / `resume` /
  `start_from_fork` `finally:` arms; confirm both cleanup calls
  are present and the order doesn't matter.

---

# STEP 3 — `auto`-mode concurrent worklist

**Goal**: ship the `auto`-mode concurrent worklist — the load-bearing
runtime surgery. `sequential`-mode is unchanged byte-for-byte;
`auto`-mode dispatches ready nodes concurrently via
`asyncio.gather`, tracks `peak_in_flight`, cancels siblings on
first failure, and emits all events through the per-executor lock.

**Files created/modified:**

- `src/workflows/executor.py` (modified) — concurrent worklist
  branch; `_emit_lock`; `_state_mutation_lock`; `peak_in_flight`
  tracking; failure cancellation; `cancel.flag` polling.
- `src/workflows/events.py` (modified) — runtime emission of
  `concurrency_mode` on `workflow.run.started.payload`;
  `peak_in_flight` on `workflow.run.completed.payload`.
- `src/cli/workflows.py` (modified) — text envelope rows.
- `tests/component/test_workflow_concurrent_auto.py` (new) —
  Phase Tests 1, 2 wired.
- `tests/component/test_workflow_concurrent_replay.py` (new) —
  Phase Test 7 wired.
- `tests/component/test_workflow_concurrent_fork.py` (new) —
  Phase Test 8 wired.
- `tests/component/test_workflow_concurrent_failure.py` (new) —
  Phase Tests 9, 10 wired.
- `tests/golden/runs/parallel-fan-out-demo.jsonl` (new) — golden
  for the canonical example.

**Effort**: ~3 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the concurrent worklist for `auto`-mode workflows.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.10-concurrent-executor-build.md` — Step 3.
2. `process/phase3.10-concurrent-executor-spec.md` — §16.2,
   §16.2.1, §16.2.2, §16.5, §16.6, §16.7.
3. `src/workflows/executor.py:1061-1500` — sequential
   `_continue_worklist` body (the surgery site).
4. `src/workflows/events.py` — `emit_event` (the wrap site for
   `_emit_lock`).

**What to implement:**

1. **`IterativeExecutor.__init__` extensions**:

   ```python
   self._emit_lock = asyncio.Lock()
   self._state_mutation_lock = asyncio.Lock()
   self._peak_in_flight = 0
   ```

2. **`_continue_worklist` dispatch fork**:

   ```python
   if self._workflow.concurrency == "auto":
       return await self._continue_worklist_concurrent(...)
   # else fall through to the existing sequential body
   ```

3. **`_continue_worklist_concurrent`** — new method:

   - Loop while ready set ∪ in-flight set is non-empty.
   - Dispatch every ready node into the in-flight set via
     `asyncio.create_task`; track `peak_in_flight` as the max
     size seen.
   - Await the first completion via `asyncio.wait(..., return_when=FIRST_COMPLETED)`.
   - For each completed task: update `_captured` / `_state` /
     `_completed_or_skipped` per Phase 3.1; remove from in-flight.
   - Recompute ready set; loop.
   - `cancel.flag` check at the TOP of each iteration (before
     dispatching new tasks): if present, cancel every in-flight
     task; emit `workflow.run.aborted`; return `ABORTED`.

4. **Failure handling**:

   - First in-flight task to raise → cancel every other in-flight
     task via `task.cancel()`.
   - Cancelled tasks emit `workflow.node.failed` with
     `error_class="asyncio.CancelledError"` and
     `reason="sibling_failure"` in payload.
   - The aborting node's own `workflow.node.failed` carries the
     original exception per Phase 3.1's contract.
   - Emit `workflow.run.failed` after all in-flight have settled.

5. **State-mutation lock**:

   - When dispatching a `set_state` / `delete_state` invocation,
     acquire `self._state_mutation_lock` BEFORE the runner runs;
     release after the `workflow.state.mutated` event is emitted.
   - This SERIALISES state mutations within the run — the gate
     already guarantees no two concurrent siblings touch the same
     key, but the lock keeps the JSONL stream total-ordered for
     downstream replay.

6. **`_emit_lock` wrap**:

   - Wrap every `emit_event(self._store, ...)` call site in
     `async with self._emit_lock:`. Sequential paths still work
     (the lock is uncontended).
   - The lock holds across the LINE WRITE only — not across any
     downstream node dispatch.

7. **`concurrency_mode` + `peak_in_flight` emissions**:

   - `workflow.run.started.payload` carries `concurrency_mode`.
   - `workflow.run.completed.payload` carries `peak_in_flight`.

8. **CLI envelope rows** in `cli/workflows.py`:

   ```
   concurrency_mode: auto
   peak_in_flight: 4
   ```

   Printed after the existing output dict. For `sequential` runs,
   `concurrency_mode: sequential` and `peak_in_flight: 1`.

9. **Tests**:

   - **Phase Test 1** — `parallel-fan-out-demo.yaml` (three
     sibling bash nodes each sleeping 200ms) completes in
     < 400ms under `auto`; > 550ms under `sequential`.
   - **Phase Test 2** — a workflow with no `concurrency:` field
     produces byte-identical JSONL to its pre-Phase-3.10 golden.
   - **Phase Test 7** — JSONL replay parity: the auto-mode run's
     JSONL replays into the same `_captured` / `_state` as the
     sequential variant.
   - **Phase Test 8** — fork from a concurrent-fan-out parent at
     a `workflow.node.completed` event: the forked run replays
     the parent's exact event ordering.
   - **Phase Test 9** — first-failure-cancels-siblings: the failing
     node's event has its original `error_class`; siblings have
     `error_class="asyncio.CancelledError"` and
     `reason="sibling_failure"`.
   - **Phase Test 10** — `cancel.flag` mid-fan-out aborts.

**Step 3 Acceptance Criteria:**

3.1. `_continue_worklist_concurrent` shipped; gated on
     `self._workflow.concurrency == "auto"`.
3.2. `_emit_lock` wraps every `emit_event` call site.
3.3. `_state_mutation_lock` held around state mutator dispatch.
3.4. `peak_in_flight` tracked + emitted on `workflow.run.completed`.
3.5. `concurrency_mode` emitted on `workflow.run.started`.
3.6. CLI envelope prints both fields.
3.7. First-failure-cancels-siblings + sibling `workflow.node.failed`
     events with `reason="sibling_failure"`.
3.8. `cancel.flag` polled at gather-loop top; aborts cleanly.
3.9. Sequential-mode runs byte-identical to pre-Phase-3.10 (the
     dispatch fork ensures sequential code-path is unchanged
     except for the `_emit_lock` wrap, which is uncontended).
3.10. Phase Tests 1, 2, 7, 8, 9, 10 pass.
3.11. No regressions: full suite green.

**Self-check before handoff:**

- [ ] All 11 ACs met
- [ ] Manual smoke: `kolsch workflows run kolsch/workflows/parallel-fan-out-demo`; observe < 400ms wall-clock
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.2 — `_emit_lock` coverage. Grep `executor.py` for
  `emit_event(self._store` — every call MUST be wrapped in
  `async with self._emit_lock:`. A missed call site is a JSONL
  corruption bug.
- AC 3.7 — sibling cancellation events. Read the failure path;
  confirm each cancelled task EMITS its own
  `workflow.node.failed` event (not just silently disappears).
  The forensics value of the JSONL depends on the cancellation
  being visible.
- AC 3.9 — sequential byte-identity. Run the Phase 3.1 — 3.9
  goldens and assert byte-identical output. The dispatch fork
  must not introduce a single character of drift in
  `sequential`-mode runs.

---

# STEP 4 — Parallel `MapRunner` + starter workflows

**Goal**: ship `parallel: true` in the map runtime + the canonical
starter workflows + goldens + Phase Tests 3, 4, 5, 11. Roadmap line
annotated.

**Files created/modified:**

- `src/workflows/builtins/map.py` (modified) — `_run_parallel`
  branch.
- `bundles/kolsch/workflows/parallel-fan-out-demo.yaml` (new) —
  auto-mode example.
- `bundles/kolsch/workflows/parallel-map-demo.yaml` (new) —
  parallel-map example.
- `bundles/kolsch/workflows/parallel-state-conflict.yaml` (new
  if Step 1 didn't already ship it) — negative example for the
  validator gate.
- `tests/component/test_workflow_parallel_map.py` (new) — Phase
  Test 3 wired.
- `tests/golden/runs/parallel-map-demo.jsonl` (new) — golden.
- `process/phase3.x-roadmap.md` (modified) — Phase 3.10 line
  annotated.

**Effort**: ~2 days.

## Step 4 — Coder Prompt

> Reply "Step 4 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the parallel map + starter workflows.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.10-concurrent-executor-build.md` — Step 4.
2. `process/phase3.10-concurrent-executor-spec.md` — §16.3,
   §16.3.1.
3. `src/workflows/builtins/map.py:64-327` — sequential map
   runtime; the surgery site.

**What to implement:**

1. **`MapRunner._run_parallel`** branch:

   - Dispatched when `node_def.parallel is True`.
   - Build the iteration overlay list (one per element, as Phase
     3.7's sequential path does up to the dispatch line).
   - Create one `asyncio.Task` per iteration via
     `_run_one_iteration(...)`.
   - If `node_def.concurrency is not None`, gate dispatch on
     `asyncio.Semaphore(node_def.concurrency)`.
   - `await asyncio.gather(*tasks)`; results dict keyed by index,
     then flattened into `[results[i] for i in range(len(over))]`.
   - First failing iteration cancels the rest (via
     `asyncio.gather(..., return_exceptions=True)` + explicit
     cancellation pass over remaining tasks).
   - Per-iteration `workflow.node.started` / `workflow.node.completed`
     events emit per Phase 3.7 contract.
   - Cancelled iterations emit `workflow.node.failed` with
     `error_class="asyncio.CancelledError"` +
     `reason="sibling_failure"`.

2. **`_run_one_iteration`** — extracted shared helper for
   sequential + parallel paths. Builds the iter ctx + overlay,
   validates strictly, emits started, dispatches, emits
   completed. Both `_run_sequential` and `_run_parallel` invoke
   this for one iteration.

3. **`results` index ordering**:

   - `_run_parallel` stores per-task results in a dict keyed by
     iteration index.
   - After `gather`, flatten via `[results[i] for i in range(total)]`.
   - Verified by Phase Test 3 with deterministic per-iteration
     outputs containing the index.

4. **Starter workflows**:

   - **`parallel-fan-out-demo.yaml`** — `concurrency: auto`;
     three sibling bash nodes (`alpha`, `beta`, `gamma`) each
     running `sleep 0.2 && echo`; a downstream `noop` confluence
     that joins them.
   - **`parallel-map-demo.yaml`** — single `map` node with
     `parallel: true, concurrency: 5` over a 10-element list;
     worker is a bash node that prints the index after a 100ms
     sleep.
   - **`parallel-state-conflict.yaml`** — `concurrency: auto`;
     two sibling `set_state` nodes both writing to
     `state.counter`; used as the negative test fixture.

5. **Goldens** at `tests/golden/runs/parallel-map-demo.jsonl` —
   the canonical event stream; canonicalised through the same
   `Canonicaliser` infrastructure other Phase 3.x goldens use.

6. **Roadmap annotation**:

   - Update `process/phase3.x-roadmap.md` Phase 3.10 line to
     read `### 3.10 — Concurrent executor — ✅ Done (\`<SHA>\`)`
     with shipping SHA annotated post-merge per the existing
     convention.

7. **Tests**:

   - **Phase Test 3** — `parallel-map-demo.yaml` completes in
     < 200ms under `concurrency: 5`; unbounded `parallel: true`
     completes in < 150ms.
   - **Index-order preservation** — a 10-element map with
     deliberately-shuffled completion times produces
     `results[i].index == i` for every i.
   - **Phase Test 11** — full regression `uv run pytest tests/ -q`.

**Step 4 Acceptance Criteria:**

4.1. `MapRunner._run_parallel` shipped; gated on
     `node_def.parallel is True`.
4.2. `asyncio.Semaphore` enforces `concurrency:` bound.
4.3. `results` index order preserved regardless of completion
     order.
4.4. First-failure-cancels-siblings + cancelled iteration events.
4.5. Three starter workflows shipped; validate cleanly (or
     reject cleanly for the negative example).
4.6. Goldens pin the JSONL shape for the parallel-map example.
4.7. Phase Tests 3, 11 pass.
4.8. Roadmap line annotated.
4.9. No regressions.

**Self-check before handoff:**

- [ ] All 9 ACs met
- [ ] Manual smoke: `kolsch workflows run kolsch/workflows/parallel-map-demo`; observe < 200ms wall-clock
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 4 Complete — [summary]".

## Step 4 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 4.3 — index-order preservation. The map's `results: list`
  MUST be index-ordered (results[0] is iteration 0's output,
  regardless of which iteration finished first). Read
  `_run_parallel`; confirm the dict-keyed-by-index approach.
- AC 4.4 — cancellation events. Each cancelled iteration MUST
  emit its own `workflow.node.failed` event (not silently
  disappear). The forensics value of the JSONL depends on
  cancellation being visible.

---

## Done when

- All 5 steps Approved by Reviewer.
- All 11 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.10 line annotated with
  the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  # Sequential default — back-compat smoke:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/hello
  # Auto-mode fan-out:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/parallel-fan-out-demo
  # observe "concurrency_mode: auto" + "peak_in_flight: 3" in envelope
  # observe wall-clock < 400ms
  # Parallel map:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/parallel-map-demo
  # observe wall-clock < 200ms; results.length == 10; results[i].index == i
  # Negative — gate rejects:
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/parallel-state-conflict
  # observe WorkflowParallelStateConflict envelope with node_a + node_b + state_key
  ```
