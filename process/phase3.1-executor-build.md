# [SPEC-002] Phase 3.1 — Sequential executor (bash + authored Python + noop)

**Type:** spec
**Doc #:** 002
**Status:** Draft
**Created:** 2026-06-25

---

## Glossary

| Term | Meaning |
| --- | --- |
| executor | The runtime that walks the validated graph and invokes nodes. Lives at `src/workflows/executor.py`. |
| invocation | One runtime fire of a node within a workflow run. Owns `InvocationId`. |
| worklist | The executor's iterative scheduling queue. Replaces Python-recursion-based traversal. |
| sidecar | Files in `runs/<wrk_id>/` that flank `events.jsonl` — today `pid` + `cancel.flag`. |
| caller chain | Ordered tuple of refs naming the agent / workflow frames the action's call arrived through. Proximate first. |

---

## Background

Phase 3.0 shipped the **foundation layer** — parser, catalog, refs unification, policy/authz refactor, run-start validator, CLI commands (`workflows` + `nodes`), and the fixture suite. The `kolsch workflows run` command is wired but stubbed; `kolsch workflows cancel` writes the sidecar but no executor reads it.

Phase 3.1 ships the **minimum executor** — sequential, no modifiers, no branching, no condition gates beyond `when:` evaluation, three runtime node types only (`bash`, `noop`, and authored Python `@node` dispatch). It is the architecture-validation milestone for execution: if the worklist + iterative-traversal + module-isolation + cancellation protocol is wrong, we discover it here with the smallest possible runtime surface.

The verification gate is a linear three-node pipeline (`bash → <bundle>/nodes/transform → noop`) running end-to-end. The JSONL event stream is complete and well-formed; module isolation provably cleans up between runs (verified by a multi-run test asserting `sys.modules` is unchanged for `bundles/*` entries). All three `@node` return-type forms (`BaseModel`, `bool`, `tuple[bool, BaseModel]`) dispatch correctly.

**This spec breaks the phase into 7 sequential steps (Step 0 — Step 6), each with its own Coder and Reviewer cycle.** Each step is independently shippable (passes its own tests) and reviewable (small enough to read end-to-end in one sitting). The Coordinator drives the multi-step flow; the Coder implements one step at a time; the Reviewer signs off step-by-step.

Related docs:

- `process/phase3.1-executor-spec.md` — the master design spec (§8 Algorithm, §11 Event Emission, §14 CLI).
- `process/phase3.0-workflow-build.md` — the predecessor build plan, particularly Steps 1, 2, 5, 6 (this phase consumes what they shipped).
- `process/phase3.0-workflow-spec.md` — the master Phase 3.0 design spec (§2 locked decisions still apply).
- `docs/explanation/workflow-event-log.md` — author-facing event log conventions referenced by §11.

---

## Scope

**In scope (delivered across the 7 steps):**

- New module `src/workflows/run.py` — `WorkflowRun` Pydantic model + per-run sidecar layout (`runs/<wrk_id>/{events.jsonl, pid, cancel.flag}`) + status enum (`pending`/`running`/`waiting_for_input`/`succeeded`/`failed`/`timed_out`/`aborted`).
- New module `src/workflows/executor.py` — `IterativeExecutor` with iterative (worklist-based) topological traversal; state-dict initialization from workflow inputs; per-node `when:` gate evaluation via `JINJA_ENV`; bind rendering; output capture; failure → abort.
- New module `src/workflows/builtins/` — Python implementations for the 3 runtime node types in Phase 3.1's scope:
  - `bash.py` routes through the existing `kolsch/bash` tool (file_access + sandbox + authz pipeline).
  - `noop.py` returns `{}`.
  - `catalog_node_runner.py` dispatches `type: <bundle>/nodes/<name>` to the imported `@node`-decorated `run` per master spec §5.3 / §5.4 (BaseModel / bool / tuple return types).
- Per-run module isolation — at run start, snapshot `sys.modules`; at run end (any status), remove only entries whose `__file__` lies under `bundles/`. Third-party packages stay imported.
- Cancellation cooperation — `IterativeExecutor` polls the sidecar `cancel.flag` at node boundaries; on detection emits `workflow.run.aborted` and exits cleanly.
- Orphan sweep — on engine startup (any `kolsch workflows ...` invocation that loads the run index), every `workflow_run` with `status=running` is checked against its recorded PID. Missing/dead PID → reconcile to `status=failed` with `reason=orphaned`; append synthetic `workflow.run.failed` event.
- Full event-emission contract per master spec §11.1 + §11.2 + §11.3: the 13 executor-emitted event kinds (`workflow.run.{started,completed,failed,timed_out,aborted}`, `workflow.node.{started,completed,failed,timed_out,logged,emitted}`, `workflow.tool.called`, `policy.decision.made` when `audit_level: all`). `trace_id` inheritance + `caller_chain` persistence in the three named event kinds (`workflow.tool.called`, `workflow.node.started`, `model.request.sent` — the model events come from the agent runtime; Phase 3.1 stamps the workflow side).
- CLI wire-up — `src/cli/workflows.py` `run` subcommand graduates from the Phase 3.0 stub to a live executor invocation: `kolsch workflows run <ref> [--input k=v]... [--dry-run] [--follow]`. `--dry-run` prints the resolved topological plan without side effects. `--follow` tails the JSONL while it executes. Default blocks until terminal status; exit code reflects success / failure.
- Per-node `timeout_seconds` cooperative deadline — fails the individual node only with `NodeTimeoutExceeded`; the workflow's `retry` policy applies; the run continues unless retries exhaust.
- Top-level `Workflow.timeout_seconds` cooperative deadline — observed at next-node boundary; fires `WorkflowTimeoutExceeded` and ends the run with `status=timed_out`.
- `retry:` integer modifier — thin facade over `tenacity` (or equivalent); attempts the configured count + 1 (initial + retries); only the final attempt emits `workflow.node.completed` / `workflow.node.failed`.
- Golden trace fixtures at `tests/golden/runs/` documenting the canonical JSONL shape for three reference workflows (linear bash pipeline; authored Python returning a BaseModel; cancel-mid-run).
- Starter workflow demo — the Phase 3.0 starter `kolsch/workflows/hello.yaml` now runs end-to-end via the executor; the Phase-level integration test invokes it on a fresh workspace.

**Out of scope (deferred to later sub-phases per `phase3.x-roadmap.md`):**

- `agent` node type (Phase 3.2).
- `branch` / `filter` / `compute` runtime + modifiers other than `when:`, `retry:`, `timeout_seconds:` (Phase 3.3, 3.6).
- `user_input` pause/resume (Phase 3.4).
- `read_file` / `write_file` / `list_files` / `set_state` / `delete_state` runtime (Phase 3.5).
- `map` / `workflow` (sub-workflow) runtime (Phase 3.7).
- `kolsch workflows fork` (Phase 3.8).
- Chat slash `/workflow` (Phase 3.9).
- Concurrent executor (Phase 3.10).

For every deferred runtime node type, the executor dispatches to a typed `NodeTypeNotImplemented` raise that surfaces in `workflow.node.failed` with a clear "ships in Phase 3.X" caller_message. The 11 deferred types fail loudly; the 3 in-scope types run.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml (via `src/_yamlio.py`), Jinja2 (`JINJA_ENV` singleton at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py` — Step 2 ships the typed `Workflow` model. The executor consumes it directly.
- `src/workflows/validator.py` — Step 5's `validate_workflow_or_raise` runs as the first thing the executor does on a new run; failure aborts before any sys.modules snapshot.
- `src/workflows/jinja_env.py` — `JINJA_ENV` is the SOLE Jinja substrate. `binds:` rendering, `when:` evaluation, config-string interpolation all route through it.
- `src/workflows/errors.py` — runtime exception classes (`WorkflowNodeFailed`, `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded`, `WorkflowJinjaSecurityError`, etc.) ship in Step 1 of Phase 3.0. Phase 3.1 raises them; does not add new ones unless a contract gap surfaces.
- `src/workflows/catalog.py` — Step 3 exposes the asset entries for nodes; the catalog-node runner uses `catalog.lookup(ref, expected_type="node")` to resolve the `.py` path.
- `src/tools/dispatch.py` — `dispatch_tool(...)` is the existing tool-invocation pipeline. The bash builtin routes through it.
- `src/providers/authz.py` — the executor's bash + future tool-invoking node types call `allow(layers=(PermissionLayer(name="workflow", rules=...), ...))` per master spec §13.2.
- `src/store/jsonl.py` — existing JSONL append helper. The executor reuses it for the workflow run's events file.
- `src/schemas/identifiers.py` — typed prefixed IDs (`WorkflowRunId`, `InvocationId`, `TraceId`) ship in Phase 3.0 Step 1. Phase 3.1 mints + propagates.
- `src/cli/workflows.py` — Phase 3.0 Step 6 wired the subcommand tree. Phase 3.1 graduates `_cmd_run_stub` to a live `_cmd_run` handler.
- `src/cli/_format.py` — `render()` is the SINGLE text/json branching point; `render_exception()` is the generic JSON formatter. Phase 3.1's CLI output routes through both.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
git status

# Bootstrap a fresh workspace for smoke testing
uv run kolsch --root /tmp/kolsch-exec-test setup --non-interactive

# Run the test suite
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

**Decisions already locked in master spec §8 + §11 + §14 (do NOT relitigate):**

- **Sequential — no parallelism in Phase 3.1.** Concurrent execution lands in Phase 3.10.
- **AND-strict trigger semantics** for multi-input edges. A target fires only when ALL incoming edge sources have reached a terminal state (completed, skipped via `when: false`, or fail-after-retries). Skip counts as completion. Retries are consumed before the completion event.
- **Failure aborts the whole run.** No `on_error: continue|skip|route_to` at v1.
- **Module isolation by bundle prefix.** Snapshot `sys.modules` at run start; remove only entries whose `__file__` lies under a `bundles/` path at run end. Third-party packages survive.
- **Iterative graph traversal** via worklist. No Python recursion — sub-workflow nesting (Phase 3.7) and map iteration would blow the call stack.
- **State dict** initialized from workflow inputs; `set_state` / `delete_state` mutate (Phase 3.5 runtime); all other nodes read via `{{ state.* }}`.
- **Cancellation is cooperative.** `cancel.flag` polled at node boundaries only. Hung nodes require `kill -9`; the orphan sweep then reconciles JSONL status on the next engine invocation.
- **`workflow.run.aborted`** is emitted ONLY after the executor observes `cancel.flag` at a node boundary. Never mid-node.
- **Event ordering rules per master spec §11.2.** `workflow.run.started` first; node-scoped events nested between `workflow.node.started` and the terminal `workflow.node.completed` / `workflow.node.failed` for that invocation; `workflow.run.completed`/`failed`/`timed_out`/`aborted` last (or appended synthetically by the orphan sweep).
- **Secret values NEVER persist in captured outputs.** The pre-substitution Jinja form (`{{ secrets.api_key }}`) is captured; the resolved value is supplied only at execution time. No post-hoc scanning.
- **`trace_id`** is chain-spanning and inherited unchanged across sub-workflow calls + spawned agent sessions + forks. Phase 3.1 mints a fresh `tr_` for root runs.
- **`caller_chain`** is persisted in `workflow.tool.called`, `workflow.node.started`, `model.request.sent` event payloads. Proximate first.

---

## Phase-level Acceptance Criteria

- A linear `bash → <bundle>/nodes/transform → noop` workflow runs end-to-end on a fresh workspace.
- The resulting `runs/<wrk_id>/events.jsonl` is complete and well-formed: `workflow.run.started` first; per-node `started` / `completed` (or `failed`) pairs in topological order; `workflow.run.completed` last.
- All three `@node` return-type forms (`BaseModel`, `bool`, `tuple[bool, BaseModel]`) dispatch correctly. Tests construct fixtures for each.
- Module isolation: running the same authored-Python workflow twice leaves `sys.modules` unchanged for `bundles/*` entries. Third-party module imports survive.
- `kolsch workflows cancel <run_id>` mid-run causes the executor to emit `workflow.run.aborted` at the next node boundary and exit with status `aborted`. The CLI command continues to write the sidecar (Phase 3.0 contract); the executor now reads it.
- Engine startup orphan sweep reconciles a `status=running` JSONL whose recorded PID is dead: status flips to `failed` with `reason=orphaned`; synthetic `workflow.run.failed` appended.
- `kolsch workflows run <ref> --dry-run` prints the topological plan to stdout and exits 0 without side effects (no JSONL written, no module imports performed).
- `kolsch workflows run <ref> --follow` tails the JSONL while the run executes.
- `--format json` envelope for `run` follows master spec §14.3 + the executor's per-command schema (final-outputs dict on success; error envelope on failure).
- Per-node `timeout_seconds: N` fails the individual node after N seconds with `NodeTimeoutExceeded`; the workflow's `retry:` policy applies; if retries exhaust, the run aborts per the abort-on-failure rule.
- Workflow-level `timeout_seconds: N` ends the run with `status=timed_out` at the next node boundary after N seconds elapsed; emits `workflow.run.timed_out`.
- A deferred runtime node type (e.g., `kolsch/nodes/branch`) raises a typed `NodeTypeNotImplemented` that surfaces as `workflow.node.failed` with `caller_message` naming the Phase that ships it.
- No regressions: full suite still green.

---

## Phase-level Integration Tests

### Phase Test 1: Linear pipeline end-to-end
- **Run:** `uv run kolsch --root /tmp/kolsch-exec-test workflows run kolsch/workflows/hello`
- **Expect:** exit 0; final outputs JSON printed to stdout; `runs/<wrk_id>/events.jsonl` contains `workflow.run.started`, `workflow.node.started` + `completed` for each node, `workflow.run.completed` (in that order).
- **Fail means:** executor algorithm broken OR event ordering violated.

### Phase Test 2: Cancellation mid-run
- **Run:** start a long-running bash workflow (`command: "sleep 5 && echo done"`); in a separate shell, `uv run kolsch --root /tmp/kolsch-exec-test workflows cancel <run_id>`.
- **Expect:** executor exits within ~1s of the in-flight node finishing; JSONL contains `workflow.run.aborted` with `{reason: "user_cancel"}`; status is `aborted`.
- **Fail means:** poll loop missing OR sidecar protocol broken.

### Phase Test 3: Module isolation across runs
- **Run:** `uv run pytest tests/component/test_workflow_executor.py::test_module_isolation_across_runs -v`
- **Expect:** test asserts `sys.modules` keys whose `__file__` is under `bundles/` are removed at run end; third-party keys (e.g., `pydantic`) survive.
- **Fail means:** snapshot/cleanup discipline broken.

### Phase Test 4: Orphan sweep
- **Run:** `uv run pytest tests/component/test_workflow_executor.py::test_orphan_sweep_reconciles_dead_pid -v`
- **Expect:** test seeds a `runs/<wrk_id>/` directory with `status=running` JSONL + dead PID; engine invocation (`kolsch workflows list`) appends `workflow.run.failed` with `reason=orphaned`.
- **Fail means:** orphan sweep not wired into engine startup.

### Phase Test 5: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green.

---

## Coordinator Prompt

> You are the strategic decision-maker for this task. You do not write or review code.
> Your job is to drive the 7-step Coder/Reviewer flow, make calls when there is ambiguity
> or disagreement, and unblock the work when it stalls. You make binding decisions when
> the master design spec has an answer; you escalate scope creep.

**Your task:**

Ship Phase 3.1 of the kolsch workflow system through 7 sequential Coder/Reviewer cycles. This is the **first execution-bearing** sub-phase. Its purpose is architecture validation for the executor — if the worklist + module-isolation + cancellation protocol is wrong, we discover it here with the smallest possible runtime surface (`bash`, `noop`, authored Python).

**Authoritative references:**

- THIS spec: `process/phase3.1-executor-build.md` — the multi-step plan you're driving.
- Master design spec: `process/phase3.1-executor-spec.md` — §8 Algorithm, §11 Event Emission, §14 CLI.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` — §2 locked decisions still apply.

**The 7 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `WorkflowRun` Pydantic model + per-run sidecar layout (`runs/<wrk_id>/{events.jsonl, pid, cancel.flag}`) + status enum + module-loading snapshot helpers + `WorkflowRunStore` JSONL append/read API. No executor wiring yet. | ~1-1.5 days |
| **Step 1** | `IterativeExecutor` skeleton — worklist + topological scheduler + state dict + `when:` gate via `JINJA_ENV` + bind rendering + output capture loop + `workflow.run.started`/`workflow.run.completed` framing events. Stub node dispatch raising `NodeTypeNotImplemented` for any type. Tests construct fixture nodes via a `RunnerRegistry` injection point. | ~1.5-2 days |
| **Step 2** | Built-in node runners for `bash` (routes through `dispatch_tool` on the `kolsch/bash` tool) + `noop` (returns `{}`). Per-node `timeout_seconds` via cooperative cancellation. `retry:` int facade backed by tenacity. Tests cover single-node + linear pipeline. | ~1.5 days |
| **Step 3** | Authored Python `@node` dispatch — `CatalogNodeRunner` resolves the catalog entry, imports via `importlib.util`, validates the `@node` decorator + signature, dispatches inputs/ctx/optional-state into the run function, parses the three return shapes (`BaseModel`, `bool`, `tuple[bool, BaseModel]`). Module-isolation snapshot/cleanup via `sys.modules` filtered by `bundles/` prefix. | ~2 days |
| **Step 4** | Cancellation + orphan recovery — executor writes its PID to `runs/<wrk_id>/pid` at run start; polls `cancel.flag` at each node boundary; emits `workflow.run.aborted` on detection; performs the same module-isolation cleanup as `completed`/`failed`. Engine startup orphan sweep: scan `runs/`; for any `status=running` JSONL with a dead PID, reconcile to `status=failed` + synthetic event. | ~1.5 days |
| **Step 5** | Full event emission contract — the 13 executor-emitted kinds per master spec §11.1; ordering rules per §11.2; `caller_chain` persistence in `workflow.tool.called` + `workflow.node.started` + `model.request.sent`; `trace_id` minting at root + inheritance for sub-workflow calls (the inheritance plumbing lands here; Phase 3.7 ships the actual sub-workflow runtime). `policy.decision.made` event emission gated by `audit_level: "all"`. CLI wire-up: graduate `_cmd_run_stub` to `_cmd_run` accepting `--input k=v`, `--dry-run`, `--follow`. | ~1.5 days |
| **Step 6** | Golden trace fixtures at `tests/golden/runs/` (linear bash pipeline; BaseModel-returning @node; cancel-mid-run) + starter workflow `kolsch/workflows/hello.yaml` runs end-to-end. Test patterns: semantic equality modulo volatile fields (timestamps, all `*Id` prefix-typed values) per Round 7 A7-3. | ~1.5 days |

**Flow per step:**

1. Hand the Coder the section titled **"Step N — Coder Prompt"** from this spec verbatim. Include the spec file path: `process/phase3.1-executor-build.md`. Coder reads it + the master spec sections it references.
2. Coder implements + replies "Step N Complete — [summary]", "Blocked — ...", or "Decision needed — ...".
3. On Complete: hand the Reviewer the section titled **"Step N — Reviewer Prompt"** verbatim. Reviewer verifies + replies "Step N Approved" or "Changes requested — ...".
4. On Approved: advance to Step N+1. On Changes requested: send the change list back to the Coder.
5. After Step 6 is Approved: run the **Phase-level Integration Tests** above. If all pass, mark this spec Status: Done and annotate `phase3.x-roadmap.md`'s Phase 3.1 line with shipping commit SHA.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision → REJECT, cite the master spec section, restate the decision.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append the call as a note to THIS spec, re-brief.
- Coder discovers Phase 3.0 contract was wrong and needs amendment → flag as a Phase 3.0 patch (separate commit on this branch); do NOT widen Phase 3.1's scope.
- Scope creep (Phase 3.2+ node types sneaking in, fork hooks, concurrent executor work) → REJECT and pull back to listed scope.

**Done when:** All 7 steps Approved by Reviewer AND all 5 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.1 line annotated with the shipping SHA.

---

# STEP 0 — Run lifecycle + sidecar layout (PREREQ)

**Goal**: ship the `WorkflowRun` Pydantic model + per-run sidecar layout + module-loading snapshot helpers + JSONL append/read API. Pure data + filesystem plumbing; no executor wiring yet. This is the structural prereq for Step 1's executor skeleton.

**Files created/modified:**

- `src/workflows/run.py` (new) — `WorkflowRun` model + `WorkflowRunStatus` StrEnum + `RunDirectory` value object (`events_path`, `pid_path`, `cancel_flag_path`).
- `src/workflows/run_store.py` (new) — `WorkflowRunStore` API: `create(workflow_ref, inputs, trace_id) -> WorkflowRun`; `read(run_id) -> WorkflowRun`; `append_event(run_id, event)`; `read_events(run_id) -> Iterator[dict]`; `list() -> list[WorkflowRun]`; `tail(run_id) -> Iterator[dict]` (for `--follow`).
- `src/workflows/module_isolation.py` (new) — `snapshot_sys_modules() -> frozenset[str]`; `cleanup_bundle_imports(snapshot: frozenset[str])` (removes only entries whose `__file__` lies under a `bundles/` path).
- `tests/component/test_workflow_run.py` (new) — CRUD + JSONL round-trip + module-isolation snapshot/cleanup.

**Effort**: ~1-1.5 days.

## Step 0 — Coder Prompt

> You are implementing Step 0 of a 7-step phase. This is the PREREQ that gates Steps 1-6.
> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Build the Phase 3.1 run-lifecycle plumbing: the `WorkflowRun` Pydantic model, the per-run sidecar directory layout (`runs/<wrk_id>/`), the JSONL append/read API, and the module-isolation snapshot helpers. NO executor logic at this step — just the data model and filesystem surfaces.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.1-executor-build.md` — THIS spec. Step 0 + the §"Phase Test 3" reference.
2. `process/phase3.1-executor-spec.md` — §8.2 (Module loading), §8.4 (Sidecar layout + orphan sweep), §11.3 (Common event fields).
3. `src/store/jsonl.py` — existing JSONL append helper. Reuse where the shape matches; wrap if not.
4. `src/schemas/identifiers.py` — `WorkflowRunId` typed prefixed ID. Mint via `WorkflowRunId.new()` for fresh runs.

**What to implement:**

1. **`src/workflows/run.py`**:
   - `WorkflowRunStatus` StrEnum: `PENDING`, `RUNNING`, `WAITING_FOR_INPUT`, `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `ABORTED`. Strings are lowercase (matches the `status` field convention).
   - `WorkflowRun` Pydantic model: `run_id: WorkflowRunId`, `trace_id: TraceId`, `workflow_ref: str`, `status: WorkflowRunStatus`, `inputs: dict[str, Any]`, `created_at: datetime`, `pid: int | None`, `reason: str | None` (populated when status is `failed` / `aborted` / `timed_out`).
   - `RunDirectory` `@dataclass(frozen=True, slots=True)` value object: `root: Path`; computed properties `events_path` (= `root / "events.jsonl"`), `pid_path` (= `root / "pid"`), `cancel_flag_path` (= `root / "cancel.flag"`).

2. **`src/workflows/run_store.py`**:
   - `WorkflowRunStore(workspace_root: Path)` class. The `runs/` directory under the workspace is the root of the store.
   - `create(*, workflow_ref: str, inputs: dict[str, Any], trace_id: TraceId | None = None) -> WorkflowRun` — mints a fresh `WorkflowRunId`; creates `runs/<wrk_id>/`; writes nothing to `events.jsonl` yet (Step 1 will emit `workflow.run.started`). Returns the model.
   - `read(run_id: WorkflowRunId) -> WorkflowRun` — reconstructs the model from the run directory's metadata. Reads the JSONL tail to determine current status (or fall back to a `meta.json` sidecar — your call; document the choice).
   - `append_event(run_id: WorkflowRunId, event: dict[str, Any]) -> None` — append-only JSONL write with the trailing newline.
   - `read_events(run_id: WorkflowRunId) -> Iterator[dict[str, Any]]` — yields one parsed dict per line.
   - `list() -> list[WorkflowRun]` — enumerates runs in `runs/`; sorted by `created_at` descending.
   - `tail(run_id: WorkflowRunId) -> Iterator[dict[str, Any]]` — like `tail -f`. Polls every ~100ms until the run reaches a terminal status; yields events as they arrive. Used by `--follow`.

3. **`src/workflows/module_isolation.py`**:
   - `snapshot_sys_modules() -> frozenset[str]` — captures the current set of `sys.modules` keys.
   - `cleanup_bundle_imports(*, before: frozenset[str], workspace_root: Path) -> int` — iterates `sys.modules`; removes only entries whose `module.__file__` exists AND lies under `workspace_root / "bundles" /`; returns the count removed. Third-party packages (no `__file__`, or `__file__` outside the bundles tree) survive.
   - Document the per-run discipline: snapshot at run start; cleanup at run end (any terminal status).

4. **`tests/component/test_workflow_run.py`**:
   - Round-trip: create a run, append 3 events, read them back; assert events come out in append order and parse cleanly.
   - `list()` ordering: 2 runs with different `created_at`; assert descending order.
   - Module isolation: write a `bundles/demo/nodes/echo.py` under a tmp workspace; `importlib.import_module` it; snapshot pre-import; cleanup; assert the bundle module is gone; assert at least one third-party module (`pydantic`) survives.
   - `RunDirectory` computed paths return the expected children.

**Step 0 Acceptance Criteria:**

0.1. `WorkflowRunStatus` enum exposes the full 7-value landscape.
0.2. `WorkflowRun` model carries `run_id` + `trace_id` + `workflow_ref` + `status` + `inputs` + `created_at` + `pid` + `reason`.
0.3. `WorkflowRunStore.create` mints a fresh `wrk_*` id + creates the run directory.
0.4. `append_event` + `read_events` round-trip cleanly.
0.5. `tail` polls until terminal status; yields new events as they arrive.
0.6. `snapshot_sys_modules` + `cleanup_bundle_imports` honor the bundles-only filter — third-party packages survive cleanup.
0.7. No executor logic in any module shipped here.
0.8. Existing tests still green.

**Step 0 Tests:**

- **Run:** `uv run pytest tests/component/test_workflow_run.py -v`
- **Expect:** all tests pass.

**Self-check before handoff:**

- [ ] All 8 Step 0 acceptance criteria met
- [ ] `WorkflowRunStore` API stable enough that Step 1 can consume it
- [ ] Module-isolation discipline documented in `module_isolation.py`'s docstring
- [ ] No regressions: `uv run pytest tests/ -q` green

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.6 — module isolation MUST exclude third-party packages; the `__file__` check is the discriminator.
- AC 0.7 — no executor logic. Grep `src/workflows/run.py` + `run_store.py` + `module_isolation.py` for any `for node in workflow.nodes` walks or `JINJA_ENV.render` calls; these belong in Step 1.

---

# STEP 1 — IterativeExecutor skeleton (no node dispatch)

**Goal**: ship the iterative worklist + topological scheduler + state dict + `when:` gate + bind rendering + output capture loop. No real node-type runtime yet — every node type raises a typed `NodeTypeNotImplemented`. Tests inject a fake runner registry to verify the algorithm.

**Files created/modified:**

- `src/workflows/executor.py` (new) — `IterativeExecutor` + `RunnerRegistry` + `NodeRunner` protocol.
- `src/workflows/errors.py` (modified) — add `NodeTypeNotImplemented(WorkflowRuntimeError)` with a `phase: str` identifying name ("Phase 3.2" / "3.3" / etc.).
- `tests/component/test_workflow_executor.py` (new) — algorithm coverage via injected fake runners.

**Effort**: ~1.5-2 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Build the executor skeleton. Worklist-based topological iteration; state dict initialized from workflow inputs; per-node `when:` gate evaluated via `JINJA_ENV`; bind rendering; output capture into a `captured: dict[str, dict[str, Any]]` map keyed by node name; failure → abort. Every node-type lookup goes through a `RunnerRegistry` that defaults to `NodeTypeNotImplemented` for any unmapped type. Steps 2 + 3 register real runners.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.1-executor-spec.md` — §8.1 Algorithm (the SEVEN steps + AND-strict rules), §8.2.1 (iterative), §8.3 (state), §11.2 (event ordering).
2. `src/workflows/parser.py` — typed `Workflow` model. The executor consumes it directly; never touches raw YAML.
3. `src/workflows/jinja_env.py` — `JINJA_ENV.from_string(...).render(**ctx)` is the SOLE rendering surface.

**What to implement:**

1. **`NodeRunner` Protocol** in `src/workflows/executor.py`:

   ```python
   class NodeRunner(Protocol):
       async def run(
           self,
           *,
           node_name: str,
           node_def: NodeDefUnion,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           ...
   ```

   The returned dict is the node's captured output (per master spec §4.0's output-shape table).

2. **`RunnerRegistry`** — maps `<bundle>/nodes/<name>` → `NodeRunner`. `get(type_ref) -> NodeRunner` raises `NodeTypeNotImplemented(phase=<from_table>)` for any unmapped built-in. Steps 2 + 3 mutate the default registry at module load. For Phase 3.1 deferred types (every built-in except `bash` / `noop` / `agent`), the registry's `NodeTypeNotImplemented` carries the right Phase number per `phase3.x-roadmap.md` §18.2.

3. **`IterativeExecutor`** in `src/workflows/executor.py`:
   - `IterativeExecutor(*, workflow: Workflow, run: WorkflowRun, store: WorkflowRunStore, registry: RunnerRegistry, catalog: AssetCatalog)` constructor.
   - `async run(self) -> WorkflowRunStatus` method:
     - Validate via `validate_workflow_or_raise` first; ANY failure aborts before the executor touches `sys.modules`.
     - Initialize `state = dict(run.inputs)`.
     - Compute topological order; build the per-node predecessor closure (re-using `_check_jinja_refs`'s helper if available).
     - Emit `workflow.run.started` with payload `{workflow_ref, inputs, trace_id}`.
     - Walk the worklist:
       - For each node, evaluate `when:` (if set) via `_render_when(value, ctx)`; on `False`, mark the node "skipped" (counts as completion per AND-strict rule 1) and continue.
       - Compute `bound_inputs` from inbound edges' `binds:` — each RHS is rendered via `JINJA_ENV` with `nodes=captured, state=state, inputs=run.inputs` in scope. Pure YAML literals pass through.
       - Mint a fresh `InvocationId`; emit `workflow.node.started` with `{node_name, invocation_id, type, inputs: bound_inputs}`.
       - Dispatch via `registry.get(node_def.type).run(...)`. On exception → emit `workflow.node.failed` with `{node_name, invocation_id, error_class, message}`; halt the run; emit `workflow.run.failed`.
       - On success → emit `workflow.node.completed` with `{node_name, invocation_id, output}`. Store in `captured[node_name]`.
     - On complete → emit `workflow.run.completed` with `{outputs: <terminal-keyed dict>}`.
   - **DO NOT** poll `cancel.flag` yet — Step 4 wires that in. Add a `TODO(step-4)` comment at the per-node-boundary location.
   - **DO NOT** mutate `sys.modules` yet — Step 3 wires the snapshot/cleanup in. The catalog-node runner lives in Step 3.

4. **Bind rendering helper** (`_render_bind`) handles the Round 6 J5/β rule: strings containing `"{{"` route through `JINJA_ENV.from_string(value).render(...)`; everything else is passed through verbatim.

5. **`tests/component/test_workflow_executor.py`**:
   - Construct fake runners (each returns a static dict). Inject into the registry. Run a 3-node linear workflow; assert all 3 fire in topological order; assert `captured` contains the right outputs.
   - `when: false` test: gated node is SKIPPED — no `workflow.node.started` / `completed` events; subsequent edges still fire per AND-strict rule 1.
   - Failure test: one runner raises; assert subsequent nodes do NOT fire; assert `workflow.run.failed` is the LAST event; status is `failed`.
   - `NodeTypeNotImplemented`: a node with `type: kolsch/nodes/branch` raises with `phase="Phase 3.3"` (or whichever).

**Step 1 Acceptance Criteria:**

1.1. `IterativeExecutor` consumes the typed `Workflow` directly; never touches raw YAML.
1.2. Worklist + topological order; no Python recursion (grep the executor source for `await self.run(`-style self-recursion — ZERO matches).
1.3. `when:` gate via `JINJA_ENV`; skip on `False` per AND-strict rule 1.
1.4. Bind rendering follows Round 6 J5/β; literal vs Jinja-marked dispatched correctly.
1.5. `workflow.run.started` is the FIRST event; per-node `started` / `completed` (or `failed`) in topological order; `workflow.run.completed` (or `failed`) is the LAST event.
1.6. `RunnerRegistry` raises `NodeTypeNotImplemented` for unmapped types with the correct `phase` ClassVar value.
1.7. Tests cover linear pipeline + when-skip + node failure + deferred-type rejection.
1.8. No `cancel.flag` polling and no `sys.modules` cleanup yet (Steps 4 + 3 own those).

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] `IterativeExecutor` is purely iterative (no recursion)
- [ ] All test cases pass
- [ ] No `sys.modules` mutation in executor source (grep — Step 3 owns it)
- [ ] No `cancel.flag` reads (grep — Step 4 owns it)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.2 — iterative ONLY. Grep for `await self.run(` self-recursion patterns; ZERO matches.
- AC 1.5 — event ordering. Run the test suite under `-v` and inspect a sample JSONL.
- AC 1.8 — Step 3 + 4 ownership. Grep the executor source for `sys.modules` and `cancel.flag` — ZERO matches at this step.

---

# STEP 2 — Built-in runners: bash + noop

**Goal**: implement the two simplest runtime node types — `bash` (routes through the existing `kolsch/bash` tool dispatch pipeline; honors `cwd:` / `env:` / file_access / authz) and `noop` (returns `{}`). Per-node `timeout_seconds` cooperative deadline + `retry:` int facade.

**Files created/modified:**

- `src/workflows/builtins/__init__.py` (new) — registry wiring.
- `src/workflows/builtins/bash.py` (new) — `BashRunner` implements `NodeRunner`; routes through `tools.dispatch.dispatch_tool` against the `kolsch/bash` tool definition.
- `src/workflows/builtins/noop.py` (new) — `NoopRunner` returns `{}`.
- `src/workflows/builtins/retry.py` (new) — `with_retry(runner, retries: int)` decorator using `tenacity` (or equivalent).
- `src/workflows/builtins/timeout.py` (new) — `with_timeout(runner, timeout_seconds: int)` decorator using `asyncio.wait_for`; on timeout raises `NodeTimeoutExceeded`.
- `tests/component/test_workflow_builtins.py` (new) — cover bash + noop + timeout + retry.

**Effort**: ~1.5 days.

## Step 2 — Coder Prompt

**Your task:**

Implement `BashRunner` + `NoopRunner` and register them in the default `RunnerRegistry`. Wrap every runner with the `with_timeout` + `with_retry` decorators so the per-node modifiers fire uniformly across runner types.

**Authoritative references:**

1. `src/tools/dispatch.py:dispatch_tool` — the existing pipeline. The bash runner constructs a `ToolContext` + `ToolDefinition` from the bash node's config and invokes `dispatch_tool`.
2. `src/cli/_starter_bundle/kolsch/tools/bash.py` — the `@tool`-decorated bash tool definition.
3. `process/phase3.1-executor-spec.md` §8.1 step 3 (Jinja-interpolate config strings).

**What to implement:**

1. **`BashRunner.run`**:
   - Render `node_def.command`, optional `cwd`, optional `env` via `JINJA_ENV` using `inputs=bound_inputs, state=state, nodes=ctx.captured` in scope.
   - Construct a synthetic `ToolContext` carrying `principal="workflow.<wrk_id>"`, `caller_chain=ctx.caller_chain`, `agent_id=None`.
   - Invoke `dispatch_tool(defn=<kolsch/bash tool>, args={"command": rendered_command, ...}, context=ctx, authz=ctx.authz, secrets=ctx.secrets, sandbox=ctx.sandbox)`.
   - Catch `ToolUnauthorized` → re-raise as `WorkflowNodeFailed`.
   - Return `{stdout, stderr, exit_code}` from the tool result.

2. **`NoopRunner.run`**: returns `{}`.

3. **`with_retry`**: wraps a runner so that `retries: N` attempts the runner up to `N + 1` times. Only the FINAL attempt's success or failure event is emitted (per master spec §8.1 AND-strict rule 3 + §11.2). Intermediate exceptions are swallowed; the next attempt fires after a 100ms backoff (configurable).

4. **`with_timeout`**: wraps a runner with `asyncio.wait_for(runner.run(...), timeout=node_def.timeout_seconds)`. On `asyncio.TimeoutError`, raises `NodeTimeoutExceeded(node=node_name, invocation_id=ctx.invocation_id, limit=node_def.timeout_seconds)`.

5. **Registry wiring** — `src/workflows/builtins/__init__.py` registers `BashRunner` at `kolsch/nodes/bash` and `NoopRunner` at `kolsch/nodes/noop` via the default `RunnerRegistry`. Every other built-in stays mapped to `NodeTypeNotImplemented` with the correct Phase number.

6. **Tests**:
   - Single bash node: `command: "echo hello"` → `stdout = "hello\n"`, `exit_code = 0`.
   - Linear bash pipeline: 2 nodes; second consumes `{{ nodes.first.stdout }}` via `binds:`.
   - Noop end-to-end.
   - Timeout: a bash node with `timeout_seconds: 1` and `command: "sleep 5"` raises `NodeTimeoutExceeded`.
   - Retry: a runner that fails twice then succeeds with `retries: 3` produces ONE `workflow.node.completed` event (no intermediate failure events).

**Step 2 Acceptance Criteria:**

2.1. `BashRunner` routes through `dispatch_tool`; honors file_access + sandbox + authz.
2.2. `NoopRunner` returns `{}`.
2.3. `with_timeout` enforces cooperative deadline; raises `NodeTimeoutExceeded` cleanly.
2.4. `with_retry` consumes intermediate failures before emitting the final terminal event (rule 3).
2.5. Linear bash pipeline runs end-to-end.
2.6. Other built-in types still raise `NodeTypeNotImplemented` with the correct Phase number.
2.7. No regressions.

**Handoff trigger:** All ACs met → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.1 — bash routes through `dispatch_tool`. Read the diff: NO direct `subprocess.run` in `BashRunner`. The whole point of routing through the tool is to inherit the existing file_access + sandbox + authz pipeline.
- AC 2.4 — retry rule 3. Only the final attempt emits events. Mid-attempt failures must NOT produce `workflow.node.failed`.

---

# STEP 3 — Authored Python @node dispatch + module isolation

**Goal**: ship the `CatalogNodeRunner` that handles `type: <bundle>/nodes/<name>` — catalog lookup, importlib import, `@node` decoration check, signature dispatch, three return-type forms. Per-run `sys.modules` snapshot/cleanup discipline lands here.

**Files created/modified:**

- `src/workflows/builtins/catalog_node_runner.py` (new) — implements `NodeRunner`; routes catalog-resolved `<bundle>/nodes/<name>` refs to imported `@node`-decorated `run` functions.
- `src/workflows/executor.py` (modified) — wire snapshot/cleanup around the worklist walk.
- `tests/component/test_workflow_catalog_node_runner.py` (new) — fixtures for each return shape + module isolation.

**Effort**: ~2 days.

## Step 3 — Coder Prompt

**Your task:**

Wire authored Python node dispatch + module-isolation discipline.

**What to implement:**

1. **`CatalogNodeRunner.run`**:
   - Look up `node_def.type` via `catalog.lookup(ref, expected_type="node")`. If `metadata.builtin is True`, raise `NodeTypeNotImplemented` (the built-in dispatch already happened upstream).
   - `importlib.util.spec_from_file_location(...)` + `module_from_spec(...)` + `loader.exec_module(...)` against `entry.path`.
   - Resolve `run` attribute; verify the `@node` sentinel (`getattr(run, "_kolsch_node", None) is not None`).
   - Introspect the signature via `inspect.get_type_hints` — extract the `inputs` parameter's Pydantic class.
   - Validate `bound_inputs` against the Pydantic class.
   - Build a `NodeContext` for this invocation via `NodeContext.for_test`-style construction (real version — the executor mints the IDs + frames the caller_chain).
   - Invoke `await run(inputs=<model>, ctx=<NodeContext>)` (or sync if `run` is sync — handle both).
   - Parse the return:
     - `BaseModel` → return `result.model_dump()`.
     - `bool` → return `{"value": result}`.
     - `tuple[bool, BaseModel]` → return `{"value": result[0], **result[1].model_dump()}`.
   - Any other return shape raises `WorkflowNodeReturnTypeInvalid` (the type was supposed to be caught at Step 5 validation, but defend at runtime too).

2. **`IterativeExecutor` modifications**:
   - At run start (just after `validate_workflow_or_raise` succeeds), `snapshot = snapshot_sys_modules()`. Persist on the executor instance.
   - Wrap the worklist walk in `try/finally`:
     - `finally:` calls `cleanup_bundle_imports(before=snapshot, workspace_root=self.run.workspace_root)`. Runs on completed, failed, timed_out, aborted — every terminal state.

3. **Tests**:
   - BaseModel return: `EchoOutputs(message="hi")` → captured `{"message": "hi"}`.
   - bool return: `True` → captured `{"value": True}`.
   - tuple return: `(True, EchoOutputs(message="hi"))` → captured `{"value": True, "message": "hi"}`.
   - Module isolation: run the same authored-Python workflow TWICE. Assert `sys.modules` snapshot before run 1 equals state after run 2. Specifically: an authored-node module under `tests/fixtures/bundles/demo/nodes/echo.py` is removed at run end; `pydantic` survives.
   - Failure to import surfaces as `WorkflowNodeImportFailed` (Step 5 validator already covers parse-time; runtime defends defensively).

**Step 3 Acceptance Criteria:**

3.1. `CatalogNodeRunner` dispatches to imported `@node`-decorated `run` functions.
3.2. All 3 return shapes (`BaseModel`, `bool`, `tuple[bool, BaseModel]`) parse correctly into captured outputs.
3.3. Module-isolation snapshot/cleanup runs on every terminal state; `sys.modules` is unchanged across runs for `bundles/*` entries.
3.4. Third-party packages survive cleanup (`pydantic`, `jinja2`, etc.).
3.5. Built-in dispatch (`BashRunner`, `NoopRunner`) still runs unchanged.
3.6. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.3 — module isolation discipline. The cleanup MUST run on EVERY terminal state, including `aborted` (which Step 4 wires). The `try/finally` is the load-bearing construct.
- AC 3.4 — `pydantic` survives. Test must explicitly assert a third-party module is still importable post-cleanup.

---

# STEP 4 — Cancellation + orphan recovery

**Goal**: ship the executor poll loop for `cancel.flag` + the engine startup orphan sweep. The CLI command was wired in Phase 3.0 Step 6; this step makes the executor cooperate.

**Files created/modified:**

- `src/workflows/executor.py` (modified) — write PID file at run start; poll `cancel.flag` at node boundaries; emit `workflow.run.aborted` on detection.
- `src/workflows/orphan_sweep.py` (new) — `sweep(store: WorkflowRunStore) -> list[WorkflowRunId]`; called from engine startup hooks.
- `src/cli/main.py` (modified) — invoke `orphan_sweep.sweep(...)` on `cli.main.main()` entry for `workflows ...` subcommands.
- `tests/component/test_workflow_cancel.py` (new) — cancel-mid-run.
- `tests/component/test_workflow_orphan_sweep.py` (new) — dead-PID reconciliation.

**Effort**: ~1.5 days.

## Step 4 — Coder Prompt

**Your task:**

Wire the executor's cancellation cooperation + the orphan sweep.

**What to implement:**

1. **PID file write** in `IterativeExecutor.run`:
   - At run start (after the snapshot + before the first node), write `os.getpid()` to `RunDirectory.pid_path`. Atomic write (`tmp + rename`) so a partial write never surfaces.

2. **`cancel.flag` polling**:
   - Between every node iteration, `RunDirectory.cancel_flag_path.exists()` check. On True:
     - Emit `workflow.run.aborted` with payload `{reason: "user_cancel"}`.
     - Trip the same `finally:` module-isolation cleanup as on success.
     - Return `WorkflowRunStatus.ABORTED`.

3. **`orphan_sweep.sweep`**:
   - Enumerate `runs/<wrk_id>/` directories.
   - For each, read the JSONL tail to determine current status.
   - If `status == "running"` AND (`pid` file missing OR `os.kill(pid, 0)` raises `ProcessLookupError`):
     - Reconcile: append a synthetic `workflow.run.failed` event with `{reason: "orphaned"}` to the JSONL.
     - Return the run_id in the reconciled list.

4. **Engine startup hook**:
   - In `src/cli/main.py:main`, after `parser.parse_args` and before dispatch, if `args.command in {"workflows", "nodes"}` AND the workspace is initialized, invoke `orphan_sweep.sweep(store)`. Log reconciled IDs at INFO.

5. **Tests**:
   - Cancel-mid-run: spawn an executor running a `command: "sleep 5"` bash node; in the same test, after the executor has emitted `workflow.node.started`, write `cancel.flag`; assert the executor exits at the node boundary with status `aborted`; assert `workflow.run.aborted` is the last event.
   - Cancel-before-start: write `cancel.flag` BEFORE the executor runs; assert it aborts on first poll without firing any nodes.
   - Orphan sweep: seed a `runs/<wrk_id>/` with `status=running` JSONL + a `pid` file containing `99999` (a guaranteed-dead PID); invoke `sweep(...)`; assert the synthetic `workflow.run.failed` is appended; assert subsequent `read(run_id).status == FAILED`.

**Step 4 Acceptance Criteria:**

4.1. Executor writes its PID at run start.
4.2. Executor polls `cancel.flag` at each node boundary; on detection, emits `workflow.run.aborted` and exits cleanly via the same `finally:` cleanup.
4.3. Orphan sweep runs on engine startup for `workflows`/`nodes` commands.
4.4. Dead PID → `status=failed` + synthetic event with `reason=orphaned`.
4.5. Live PID → no reconciliation.
4.6. Cancel-mid-run never mid-node; the in-flight node completes before the abort observation.
4.7. No regressions.

**Handoff trigger:** All ACs met → "Step 4 Complete — [summary]".

## Step 4 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 4.2 — the `workflow.run.aborted` event is emitted at the NODE BOUNDARY, never mid-node. The poll happens AFTER the current node's terminal event (`completed` or `failed`) but BEFORE the next node fires.
- AC 4.3 — orphan sweep wired into engine startup. Read the `cli/main.py` diff: the sweep MUST run before the dispatch handler for `workflows` / `nodes`.

---

# STEP 5 — Full event emission contract + CLI wire-up

**Goal**: ship the 13 executor-emitted event kinds; persist `caller_chain` in the named three; mint + propagate `trace_id`; emit `policy.decision.made` when `audit_level: all`; graduate `_cmd_run_stub` to a live `_cmd_run` handler.

**Files created/modified:**

- `src/workflows/events.py` (new) — Pydantic envelopes for the 13 executor-emitted kinds; common-field helpers.
- `src/workflows/executor.py` (modified) — emit events per master spec §11.2.
- `src/providers/authz.py` (modified) — emit `policy.decision.made` event when `audit_level == "all"`.
- `src/cli/workflows.py` (modified) — graduate `_cmd_run_stub` → `_cmd_run`: `--input k=v`, `--dry-run`, `--follow`.
- `tests/component/test_workflow_events.py` (new) — event ordering + envelope shape.
- `tests/component/test_workflow_cli.py` (modified) — `kolsch workflows run` live tests.

**Effort**: ~1.5 days.

## Step 5 — Coder Prompt

**Your task:**

Ship the full event emission contract + the CLI `run` wire-up.

**What to implement:**

1. **`events.py`** — Pydantic envelopes for the 13 executor-emitted kinds. Each carries the common fields (`kind`, `workflow_run_id`, `trace_id`, `parent_event_id`, `ts`); node-scoped events also carry `invocation_id` + `node_name`. `workflow.tool.called`, `workflow.node.started`, `model.request.sent` also carry `caller_chain: tuple[str, ...]` in payload.

2. **`IterativeExecutor` event emission**:
   - At run start: emit `workflow.run.started` with `{workflow_ref, inputs, trace_id}`.
   - Per node: emit `workflow.node.started` BEFORE the dispatch; `workflow.node.completed` (or `.failed` / `.timed_out`) AFTER. NO intermediate events for retries (rule 3).
   - At run end: emit `workflow.run.completed` (or `.failed` / `.timed_out` / `.aborted`).
   - `parent_event_id` follows the Phase 1 convention — references the event that causally produced this one.

3. **`policy.decision.made` emission**:
   - In `providers/authz.py` `RuleBasedAuthorizationProvider.allow`, after the layer walk resolves, if `self.audit_level == "all"`, append a `policy.decision.made` event to the current workflow run's JSONL via a thread-local current-run handle (set by the executor at run start; cleared at run end). When `audit_level == "rule_matched"`, only emit when an explicit rule fired (not the default-deny tail).

4. **`_cmd_run` graduation**:
   - Replace `_cmd_run_stub` with `_cmd_run` taking `args.ref`, `args.input` (repeatable), `args.dry_run`, `args.follow`.
   - `--input k=v` parses into a dict; values are coerced via the workflow's derived input schema.
   - `--dry-run`: print the resolved topological plan + per-node bound-inputs preview to stdout; exit 0; no executor invocation.
   - `--follow`: invoke the executor in a background coroutine; tail the run's JSONL via `WorkflowRunStore.tail` and print one line per event until terminal.
   - Default (no flags): invoke the executor synchronously; block until terminal; print final outputs JSON on success or error envelope on failure; exit code reflects status.
   - `--format json` honors master spec §14.3 envelope.

5. **Tests**:
   - Ordering: a 3-node linear pipeline's JSONL has exactly `run.started`, `node.started` x3, `node.completed` x3, `run.completed` — in topological order.
   - `caller_chain`: a `workflow.node.started` event for a node inside a workflow whose run has a non-empty `caller_chain` (simulated) persists the chain in payload.
   - `--dry-run`: no `events.jsonl` written; no `sys.modules` mutated.
   - `--follow`: stream produces one line per event; terminates on terminal.
   - `--format json`: success envelope contains `data.outputs` keyed by terminal node names.

**Step 5 Acceptance Criteria:**

5.1. All 13 executor-emitted kinds have Pydantic envelopes.
5.2. Event ordering per master spec §11.2 holds in tests.
5.3. `caller_chain` persists in `workflow.tool.called` + `workflow.node.started` event payloads.
5.4. `policy.decision.made` emits when `audit_level == "all"`.
5.5. `kolsch workflows run <ref>` graduates from stub to live; `--input`, `--dry-run`, `--follow` work.
5.6. `--format json` envelope follows master spec §14.3.
5.7. No regressions.

**Handoff trigger:** All ACs met → "Step 5 Complete — [summary]".

## Step 5 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 5.2 — event ordering is the LOAD-BEARING contract. Run a fixture workflow and inspect `events.jsonl` manually.
- AC 5.5 — `--dry-run` must NOT touch `sys.modules`. Read the source: the dry-run path returns BEFORE the snapshot.

---

# STEP 6 — Golden trace fixtures + starter workflow demo

**Goal**: ship reference JSONL goldens that document the canonical event ordering + the starter `kolsch/workflows/hello.yaml` runs end-to-end. Final integration tests.

**Files created/modified:**

- `tests/golden/runs/` (new) — three reference JSONLs:
  - `linear-bash-pipeline.jsonl` — 3 bash nodes; documents the success path.
  - `authored-python-basemodel.jsonl` — 1 catalog-node returning a `BaseModel`.
  - `cancel-mid-run.jsonl` — bash node interrupted by `cancel.flag` at the boundary.
- `tests/golden/runs_test.py` (new) — runs each scenario and asserts semantic equality against the golden (modulo volatile fields per Round 7 A7-3).
- `tests/component/test_workflow_executor_e2e.py` (new) — Phase Test 1 (linear pipeline) + Phase Test 2 (cancel).

**Effort**: ~1.5 days.

## Step 6 — Coder Prompt

**Your task:**

Ship the golden trace suite + the end-to-end starter demo.

**What to implement:**

1. **Three golden JSONL fixtures** under `tests/golden/runs/`. Each is the expected output of running a corresponding fixture workflow. Volatile fields (`ts`, all `*Id` prefix-typed values) are CANONICALIZED — replaced with sentinels (`<ts>`, `<wrk_id>`, `<inv_0>`, etc.) — so the test can compare semantically.

2. **`runs_test.py`** — runs each fixture workflow, captures the produced JSONL, normalizes volatile fields, asserts equality against the golden. Each fixture becomes its own parametrize case.

3. **Starter demo wiring** — confirm `kolsch/workflows/hello.yaml` from Phase 3.0 Step 7 runs cleanly via `_cmd_run`. Update the YAML if needed (e.g., to match the Phase 3.1 input schema).

4. **`test_workflow_executor_e2e.py`**:
   - Phase Test 1: `setup --non-interactive` a fresh workspace; `kolsch workflows run kolsch/workflows/hello`; assert exit 0; assert JSONL has the expected events in the expected order.
   - Phase Test 2: same setup; start a bash workflow with `command: "sleep 5"`; from the test thread, write `cancel.flag` after `workflow.node.started`; assert the executor exits with status `aborted`; assert `workflow.run.aborted` is the last event.

**Step 6 Acceptance Criteria:**

6.1. Three golden JSONL fixtures shipped at `tests/golden/runs/`.
6.2. `runs_test.py` asserts semantic equality with canonical volatile-field substitution.
6.3. Starter workflow runs end-to-end.
6.4. `test_workflow_executor_e2e.py` covers Phase Test 1 + Phase Test 2.
6.5. No regressions.

**Handoff trigger:** All ACs met → "Step 6 Complete — [summary]".

## Step 6 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 6.2 — semantic equality. The volatile-field substitution must be principled (no per-test ad-hoc replacements). Document the canonical substitution table in `runs_test.py`'s docstring.
- AC 6.3 — the starter workflow's input schema must match the Phase 3.1 executor's expectations. If `--input name=Maya` is required, the YAML's start node must declare the input.

---

## Done when

- All 7 steps Approved by Reviewer.
- All 5 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.1 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/hello
  ls /tmp/kolsch-fresh/runs/
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl
  ```

- Cumulative user value at the gate (per `phase3.x-roadmap.md` §18.4): authors can build linear bash + authored-Python pipelines end-to-end.
