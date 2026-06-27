# Phase 3.x Roadmap — kolsch-engine

This roadmap covers Phase 3.0 through Phase 3.10+ of kolsch-engine. Each phase has its own spec file (`process/phase3.X-*.md`) where applicable.

## 18.1 Overview

Eleven phases of `kolsch-engine`. Each ships independently and carries an explicit verification gate — a concrete artifact that proves the phase is correct and complete. Early phases are small (1-2 weeks) to validate architecture decisions quickly; later phases are larger because they tackle genuinely complex subsystems (fork, concurrency).

**Scope boundary: `kolsch-engine` vs `kolsch-server`.** All eleven Phase 3.x sub-phases below ship `kolsch-engine` — parser, validator, executor, node types, agent integration, fork, chat surface, concurrent executor, `author_workflow` meta-agent. NO sub-phase ships triggers (cron / scheduled / webhook), HTTP entry points, or scheduler daemons. Those are a SEPARATE concern, `kolsch-server`, targeted for Phase 4+ or as a companion project. The pattern matches Argo Workflows + Argo Events: a workflow engine and an events/triggers surface are independent components. The engine's only invocation entry points across all eleven sub-phases are CLI (`kolsch workflows run`) and chat (`/workflow`).

## 18.2 Phases

### 3.0 — Parser & validator (no execution) — ✅ Complete

**Status:** Shipped on branch `fix/agent-id-derive-from-ref` across 7
sequential commits (Step 0 → Step 7). Verification gate met: 41 YAML
fixtures discovered + asserted by `tests/golden/workflows_test.py`;
~1,600 tests passing across the suite.

Spec: `process/phase3.0-workflow-build.md` (see also `process/phase3.0-workflow-spec.md` for the master Phase 3.0 workflow design).

**Scope**: (Step 0) migrate kolsch from PyYAML to ruamel.yaml via a new helper module `src/_yaml.py` exporting `load(text)` and `dump(data) -> str` backed by `ruamel.yaml.YAML(typ='safe')`. Replace `yaml.safe_load` / `yaml.safe_dump` call sites across ~10 src/ files (`templating/inheritance.py`, `cli/_setup.py`, `cli/skills.py`, `cli/llm_commands.py`, `cli/permissions.py`, `cli/chat.py`, `assets/manifest.py`, `assets/assembler.py`, `assets/platform_config.py`, `assets/skill_metadata.py`). `pyproject.toml`: remove `pyyaml` + `types-pyyaml`; add `ruamel.yaml` + `ruamel.yaml.clib`. ~1-2 days; prereq to Step 1. Test golden refresh wave expected (the Norway-bug fix may shift outputs). (Step 1+) asset registration with auto-discovery from `bundles/<bundle>/workflows/*.yaml` and `bundles/<bundle>/nodes/*.py`; YAML parser; `extends:` chain resolution with cycle detection + depth-16 cap; full run-start validation gate (structural checks, bind type checks, Python node introspection via `@node` decorator). CLI: `list`, `show`, `validate`.

**Verification gate**: a fixture of ~20 workflow YAML files covering every node type's config, every validator rule, every error class. CI passes when every fixture parses correctly OR raises the expected exception with the expected attributes.

**Why first**: validates the WHOLE architecture (asset model, YAML schema, error hierarchy, ref shapes) without writing execution code. If the schema is wrong, we discover it here with low cost.

**Effort**: ~1-2 weeks.

### 3.1 — Minimum executor (bash + authored Python + noop) — ✅ Done (`6fb91ad`)

**Status:** Shipped across Steps 0–6 of
`process/phase3.1-executor-build.md`. The 5 Phase-level Integration
Tests pass under `uv run pytest tests/`; the manual smoke
(`kolsch setup --non-interactive` → `workflows run
kolsch/workflows/hello`) succeeds end-to-end on a fresh workspace.
Shipping SHA `6fb91ad` covers the post-Step-6 close-out (synthetic
bash-id UUIDization + unified golden canonicaliser); the executor +
JSONL + cancel + orphan-sweep + event-envelope + CLI wire-up that
satisfy the verification gate live in the chain ending at that SHA.

Spec: `process/phase3.1-executor-spec.md`. Build plan:
`process/phase3.1-executor-build.md`.

**Scope**: sequential executor (no modifiers, no branching, no condition gates). Walks the graph topologically and invokes nodes. Node types: `bash` (routing through existing `kolsch/bash` tool), `noop`, AND the catalog-ref dispatcher for authored Python nodes (`type: <bundle>/nodes/<name>` — full `@node` introspection + signature dispatch). Module-loading discipline (`sys.modules` cleanup filtered by `bundles/` prefix). Iterative graph traversal (no Python recursion). The `kolsch-engine.LocalController` per-host coordination concerns (PID tracking, `cancel.flag` poll loop, JSONL append, orphan sweep) land in this phase — the Phase 3.1 designer picks the LocalController module layout. CLI: `run`, `events`.

**Verification gate**: a linear three-node pipeline (`bash → <bundle>/nodes/transform → noop`) runs end-to-end. JSONL event stream is complete and well-formed. Module isolation provably cleans up between runs (test: run twice, verify `sys.modules` is unchanged for `bundles/*`). The authored-Python `@node`'s three return-type forms (BaseModel, bool, tuple) all dispatch correctly.

**Why this slice**: proves the executor architecture without LLM/agent complexity. Authors can write real (if simple) workflows. Architecture validated by running code.

**Effort**: ~2 weeks.

### 3.2 — Agent integration ✅ Done (commit pending)

Spec: `process/phase3.2-agent-integration-spec.md`. Build plan: `process/phase3.2-agent-integration-build.md`.

**Scope**: `agent` node type. Threads through existing CrewAI engine via the asset catalog's agent invocation path. Per-invocation `outputs:` override using existing `IOSpec` grammar from `src/schemas/config.py:58`. Child-session creation with `from_workflow_run:` pointer.

**Verification gate**: workflow invokes an agent and consumes its structured output. Session is created and carries the `from_workflow_run:` pointer. Output validation works for both the agent-declared schema AND a workflow-overridden schema. Agent run failures (max iterations, tool failures) surface as `WorkflowNodeFailed`.

**Why separate**: agent integration is substantial plumbing (CrewAI hooks, session lifecycle, structured-output dispatching). Wants its own milestone so a regression in agent handling doesn't block leaf-type work.

**Effort**: ~1-2 weeks.

### 3.3 — Branching runtime + multi-input bind validation — ✅ Done (`e35169c`)

**Status:** Shipped across Steps 0–3 of
`process/phase3.3-modifiers-branching-build.md` (Step 0 `89101db`,
Step 1 `36b1cfd`, Step 2 `6248899`, Step 3 `e35169c`). The 6
Phase-level Integration Tests pass under `uv run pytest tests/`; the
manual smoke (`kolsch setup --non-interactive` → `workflows run
kolsch/workflows/branching-demo --input check_cmd={true,false}`)
succeeds end-to-end with the expected JSONL shape (branch_value
payload + workflow.node.pruned events + pruned-terminal null outputs).

Spec: `process/phase3.3-modifiers-branching-spec.md`. Build plan: `process/phase3.3-modifiers-branching-build.md`.

**Note on scope reconciliation:** Phase 3.1 shipped more of this entry's original scope than initially planned — `when:` gate evaluation (`src/workflows/executor.py:617`), `retry:` modifier via a bare loop (`src/workflows/builtins/retry.py`, tenacity intentionally NOT introduced), multi-input edge STRUCTURE + AND-strict semantics (executor.py:369-375), parse-time `WorkflowBindCollision` (validator.py:516-530), and validator branching invariants (validator.py:540-590) all landed with the executor. Phase 3.3's remaining surface is purely the RUNTIME wiring those pieces left unfinished.

**Scope**: runtime branch dispatch — the executor reads `edge.branch` at fire time, computes the source's branch value (bash `exit_code == 0`; authored Python `bool` or `tuple[bool, BaseModel]`), and filters outgoing edges. Pruning cascade via reverse-reachability + the new `workflow.node.pruned` event (the SINGLE addition to master spec §11.1). Bash success-vs-failure revision: non-zero exit is success when at least one outgoing edge carries `branch:`, abort-as-before otherwise. The `inputs.*` Jinja namespace at receivers — `_render_jinja(..., inputs_overlay=)` so a receiver's config strings see edge-bound `binds:` keys. Runtime strict-mode bind validation raising `WorkflowBindTypeMismatch` (class already exists at `src/workflows/errors.py:490-528`; never previously raised).

**Verification gate**: bash branch-on-non-zero fires the false-branch edges instead of aborting; authored Python `bool` / `tuple[bool, BaseModel]` returns drive edge dispatch; multi-input receiver's config interpolates `{{ inputs.<bind_key> }}` correctly; type mismatches at fire time surface as `WorkflowBindTypeMismatch`; pruned nodes emit `workflow.node.pruned`; partial fan-in (one fired + one pruned inbound edge) fires the receiver with the fired edge's binds only.

**Why separate**: branching has subtle rules (dynamic abort behavior for bash, cascade semantics for partial fan-in, multi-input bind validation under strict mode). The runtime-only nature makes the scope tight and the test surface focused.

**Effort**: ~1 week (runtime wiring only; no parser/validator/error-class changes).

### 3.4 — User_input with CLI pause/resume — ✅ Done (`1cdaec9`)

**Status:** Shipped across Steps 0–3 of
`process/phase3.4-user-input-build.md`. The 7 Phase-level Integration
Tests pass under `uv run pytest tests/` (`tests/component/test_workflow_user_input_e2e.py`
covers Phase Tests 1–6 end-to-end; Phase Test 7 is the full-suite
regression). Three pause/resume goldens
(`tests/golden/runs/user-input-{single-pause,sequential-pauses,rejected-resume}.workflow.jsonl`)
pin the JSONL shape across the cross-process pause boundary. Manual
smoke (`kolsch setup --non-interactive` → `workflows run
kolsch/workflows/hello-paused` pauses → `workflows resume <run_id>
--input response=world` succeeds) succeeds end-to-end with the
expected JSONL continuity (one `workflow.run.resumed` event between
the user_input's `started` and `completed`; `waiting_at` sidecar
cleaned up at terminal).

Spec: `process/phase3.4-user-input-spec.md`. Build plan: `process/phase3.4-user-input-build.md`.

**Scope**: `user_input` node type. Pause mechanism: status flips to `waiting_for_input`; JSONL ends with the user_input node's `workflow.node.started` event but no completion event. CLI `resume` command completes the open invocation with the user-supplied response.

**Verification gate**: a workflow with a user_input node pauses cleanly; the process can exit and resume in a separate command invocation. Multiple sequential pauses in one workflow work. Resume rejects when run isn't in `waiting_for_input` state.

**Why separate**: pause/resume is a real entity-lifecycle feature with persistence implications. Worth its own milestone.

**Effort**: ~1 week.

### 3.5 — File IO + state mutators — ✅ Done (`bf66125`)

**Status:** Shipped across Steps 0–3 of
`process/phase3.5-file-io-state-build.md` (Step 0 `502638f`,
Step 1 `af61314`, Step 2 `434339a`, Step 3 `bf66125`). The 6
Phase-level Integration Tests pass under `uv run pytest tests/`
(`tests/component/test_workflow_state_e2e.py` covers Phase Tests 3–5;
`tests/component/test_workflow_file_io.py` covers Phase Tests 1–2;
Phase Test 6 is the full-suite regression). Five new goldens
(`tests/golden/runs/{state-evolves,delete-state-idempotent,file-roundtrip,file-access-denied}.jsonl`
+ `state-resume.workflow.jsonl`) pin the JSONL shape — including
the master spec §11.3 chain rule (state-mutating nodes'
`workflow.node.completed` chains to the LAST `workflow.state.mutated`
event) and the output-narrowing contract (`{content}` /
`{bytes_written}` / `{entries: list[str]}`).

Bug surfaced during Step 3 wire-up: `IterativeExecutor.resume` was
overwriting `_state = dict(self._run.inputs)` AFTER `replay_into(...)`,
silently wiping every pre-pause state mutation. Fixed in Step 3 by
initialising state from inputs FIRST so the replay's set/delete ops
overlay correctly. Without the fix, resumed workflows would render
`state.*` against the bare inputs dict.

Spec: `process/phase3.5-file-io-state-spec.md`. Build plan: `process/phase3.5-file-io-state-build.md`.

**Scope**: 3 file-IO leaves (`read_file`, `write_file`, `list_files` — each routing through `dispatch_tool(...)` against the kolsch starter-bundle tool of the same name, inheriting the existing `check_path_allowed` discipline from `src/files/__init__.py`); 2 state leaves (`set_state`, `delete_state`). NEW event kind `workflow.state.mutated` (the source of truth for resume + fork replay; payload `{key, op: "set"|"delete", value}`). Phase 3.4 replay extension to apply state events on resume.

**Verification gate**: workflows read/write files within the workspace; `file_access` denials are enforced and surface as `WorkflowFileAccessDenied`. State mutations are captured events that replay correctly (on fork; tested in 3.8). Jinja `state.*` scope works for any node's config reading state.

**Why grouped**: small leaves that share the "thin wrappers around existing subsystems" pattern. Implementing them together exercises the same auth/permission integration plumbing.

**Effort**: ~1 week.

### 3.6 — Branch node + filter + compute + NativeEnvironment — ✅ Done (`37554b4`)

**Status:** Shipped across Steps 0–2 of
`process/phase3.6-branch-filter-compute-build.md` (Step 0 `cc1fd87`,
Step 1 `c9dadf6`, Step 2 `37554b4`). The 6 Phase-level Integration
Tests pass under `uv run pytest tests/`
(`tests/component/test_workflow_compute_recipes.py` covers Phase
Test 2 — 8 aggregation recipes each asserting both value AND type;
`tests/component/test_workflow_native_env.py` covers Phase Test 4 —
NativeEnvironment typed-vs-string regression including the bash
command-string non-regression and the filter → compute typed chain;
`tests/component/test_workflow_filter.py` covers Phase Tests 3 + 5;
`tests/component/test_workflow_branching_e2e.py` covers Phase Test 1
end-to-end; Phase Test 6 is the full-suite regression). Two new
goldens (`tests/golden/runs/triage-{urgent,normal}.jsonl`) pin the
JSONL shape across both branch arms — `branch_value` on
`is_urgent.completed`, `workflow.node.pruned` on the un-fired
handler, terminal-keyed final outputs with one arm `null`.

Decision logged during Step 0: the build doc's filter example used
bare `{{ item > 5 }}` for per-element bindings, but Phase 3.3's
`inputs_overlay` mechanism puts bind values under `inputs.<key>`.
Tests use `{{ inputs.item }}` / `{{ inputs.ticket.urgent }}` to match
the existing namespace contract. Same goes for the master spec
§6.0.1 sketch (`{{ label == 'urgent' }}`) — real workflows use
`inputs.<key>`, and the shipped `triage-tickets.yaml` follows that
convention.

Spec: `process/phase3.6-branch-filter-compute-spec.md`. Build plan: `process/phase3.6-branch-filter-compute-build.md`.

**Note on scope reconciliation:** Phase 3.3 shipped `WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment)` with `NativeCodeGenerator` + `native_concat` at `src/workflows/jinja_env.py:56-66` — NativeEnvironment is already universal. Parser models (`BranchNodeDef`/`FilterNodeDef`/`ComputeNodeDef`), Inputs models, `_BUILTIN_TO_INPUTS` entries, `BUILTIN_PHASE_MAP`, and validator branching invariants for the dedicated branch node all landed earlier. Phase 3.6 is runtime-only: three small Jinja-driven runners + a one-case extension to `compute_branch_value` for the `kolsch/nodes/branch` node type.

**Scope**: `branch` runtime (`BranchRunner` — renders `condition:` Jinja, returns `{value: bool}`); `filter` runtime (`FilterRunner` — renders `over:` to list, iterates with `item_as` scope, returns `{result: list[T]}`); `compute` runtime (`ComputeRunner` — renders `value:` Jinja, returns `{value: <any>}`). One-line `compute_branch_value` extension for the branch node type. Triage walkthrough (`bundles/kolsch/workflows/triage-tickets.yaml`) end-to-end. Verification of all 8 aggregation recipes as `compute.value:` expressions returning correctly-typed results. NativeEnvironment string-field non-regression coverage.

**Verification gate**: workflows that branch + filter + compute work end-to-end with NativeEnvironment preserving Python types through Jinja expressions (no JSON roundtripping in `over:` expressions). The end-to-end "triage tickets" pipeline example from the spec walkthrough runs and produces correct results. The 8 aggregation recipes (sum/count/min/max/sort/flatten/any/all) all expressible as `compute.value:` Jinja and pass tests.

**Why grouped**: `filter` needs list sources (from authored Python nodes returning lists or `list_files`); `compute` reuses the same Jinja substrate to cover everything `filter` doesn't; `branch` is similar leaf shape; NativeEnvironment is the substrate that makes typed values flow correctly. All four validate the same underlying design choice.

**Effort**: ~1-2 weeks.

### 3.7 — Composition (map + workflow)

Spec: `process/phase3.7-composition-spec.md`. Build plan: `process/phase3.7-composition-build.md`.

**Scope**: `map` node type — sequential iteration; worker resolved by name in same workflow via `apply:`; per-iteration events carry `parent_invocation_id` + `iteration_index`; rendered `with:` constants overlay; output `{results: list[<worker output dict>]}`. `workflow` node type — sub-workflow nesting; mirrors Phase 3.2's parent-pointer pattern (new `WorkflowRun.parent: ParentRunPointer | None`); child runs inherit `trace_id` unchanged; child state isolated (NO inheritance from parent — LOCKS the previously-deferred Phase 3.5 question); cycle detection via `WorkflowCallCycle` (already extant); depth-16 cap shared with `extends:` (same `WorkflowExtendsDepthExceeded` class). Two new event kinds `workflow.node.called_workflow` + `_completed` for parent-side forensics. `workflow.node.completed.child_run_id` payload extension. NEW `ChildWorkflowFailed` error class. Pause cascade — child pause raises `WorkflowPausedForInput` which bubbles through the workflow runner so the parent pauses too; operator-driven resume cascade (`resume <leaf>` then `resume <parent>`). `_replay.py` extension for new event kinds + nested resume detection. Orphan-sweep distinction (`reason="orphaned_child"` for children whose parent crashed).

**Verification gate**: map iterates a worker over a list and produces `results: list[T]`. `filter` and `compute` consume map outputs cleanly. Sub-workflow runs created with `parent: { workflow_run_id, invocation_id }` pointer. Cycle detected and rejected at run-start gate. Workflow-call exceeding depth 16 fails clearly.

**Why separate**: composites are genuinely complex engine work — recursion handling, child-run lifecycle, parent linking. Different from leaf-type implementation.

**Effort**: ~2 weeks.

### 3.8 — Fork mechanism

Spec: `process/phase3.8-fork-spec.md`. Build plan: `process/phase3.8-fork-build.md`.

**Scope**: fork-from-event implementation, structurally "replay + new entry point" — reuses Phase 3.4's `_replay.py` (+ Phase 3.5/3.7 extensions) for parent-event replay and Phase 3.4's `_continue_worklist` for execution. NEW `WorkflowRun.forked_from: ForkedFrom | None` field (symmetric with Phase 3.7's `parent` pointer and Phase 3.2's `from_workflow_run` on sessions). NEW event kind `workflow.run.forked_from` (mirrors Phase 3.4's `workflow.run.resumed` pattern) emitted as first event of forked JSONL. NEW `IterativeExecutor.start_from_fork(*, fork_event_id, input_overrides)` entry point. NEW CLI commands: `kolsch workflows fork <run_id> --at <event_id> [--input k=v]...` AND the prerequisite `kolsch workflows events <run_id>` (operators need it to discover event_ids; Phase 3.0 §14.1 mentioned it but it never shipped). Fork-point legality check: event_id must exist in the parent's `events.jsonl`; spawned-session events are rejected with a guidance message. `--input` overrides replace the fork-point node's captured output keys (per spec §9.1). `WorkflowRunNotFound` raised when parent JSONL deleted. Sub-workflow fork handling rides on existing `_captured` replay — fork AFTER a workflow-call node reuses captured child outputs; fork BEFORE spawns a fresh child run.

**Verification gate**: fork a completed run from any event; new run continues from that point with same captured upstream state; original run remains immutable. Fork a paused run (still in `waiting_for_input`). Fork across a sub-workflow boundary correctly resolves the child's captured outputs.

**Why separate**: fork is genuinely hard. Cross-JSONL reads, deterministic state reconstruction, parent-pointer chain integrity. Its own milestone with its own test suite.

**Effort**: ~3-4 weeks.

### 3.9 — Chat surface

Spec: `process/phase3.9-chat-surface-spec.md`. Build plan: `process/phase3.9-chat-surface-build.md`.

**Scope**: `/workflow <ref> with k=v ...` and `/fork <event_id> with k=v ...` slashes (new `SlashCommand` entries in `src/cli/_slash.py:COMMAND_REGISTRY`). Chat capsule rendering for node invocations via NEW `src/cli/_workflow_capsules.py:WorkflowCapsuleRenderer` — header from `display_label(node_def, type_label)` (the SOLE call site per spec §15.4); body shows captured output at `workflow.node.completed`; failure body shows `caller_message` (NOT `str(exc)`); `workflow.node.pruned` events render muted. Sub-workflow nesting via cross-JSONL trace_id correlation — child renderer spawned at `workflow.node.called_workflow` events. user_input pause/resume in chat: post-pause inline-prompt capsule + `ChatSessionState.expecting_user_input_response` flag + next non-slash message → `executor.resume(response={"response": text})`; slash escape preserves pause. NEW `ChatSessionState.pending_workflow_run_id` field tracks the active run; `/fork` gates on it. Shared `parse_input_pairs` factored to `src/workflows/_args.py` so slash + CLI share. Modal callback integration is transitive (the existing modal wires through authz subsystem); no new code. NO new event kinds; NO new runtime behavior — surface phase only.

**Verification gate**: workflow runnable from `/workflow` slash. Capsules render correctly per node type. user_input chat reply resumes the workflow on the SAME turn (user types response → engine advances → next capsule appears). Modal fires for `prompt:` policy rules on actions originating from a workflow node.

**Effort**: ~2-3 weeks.

### 3.10 — Concurrent executor

Spec: `process/phase3.10-concurrent-executor-spec.md` (to be written).

**Scope**: parallel fan-out execution for branches that don't share state writes. Switch from `sys.modules` cleanup to per-run module prefix (parallel-safe imports). Map gains optional parallel iteration. Determinism rules documented: event-ordering policy, write-conflict resolution policy for state mutations.

**Verification gate**: parallel-eligible workflows execute concurrently (provable via timing on parallel-fan-out test cases). Sequential-required workflows (those with state writes) still run sequentially. Per-run module isolation works under concurrent loads (multi-threaded test: load same Python node module in two parallel runs, verify no cross-contamination).

**Effort**: ~3-4 weeks.

### 3.11 — `author_workflow` meta-agent

Spec: `process/phase3.11-author-workflow-meta-agent-spec.md` (to be written).

**Scope**: asset that generates workflow YAML + Python node files from natural-language requests. Validates the produced workflow against the run-start gate during authoring (iterative refinement loop: agent generates → engine validates → agent revises). Author iteration via chat.

**Verification gate**: agent produces a workflow that validates against the run-start gate. Author can request a modification ("add a step that emails the result") and the agent produces a valid updated workflow. The generated workflow runs end-to-end.

**Effort**: ~2 weeks.

## 18.3 Dependency graph

```
3.0 → 3.1 ─┬─→ 3.2 (agent)
           ├─→ 3.3 (modifiers + branching)
           ├─→ 3.4 (user_input)
           ├─→ 3.5 (file IO + state)
           └─→ 3.6 (branch + filter + compute)

3.2 + 3.3 + 3.4 + 3.5 + 3.6 → 3.7 (composition)
3.7 → 3.8 (fork)
3.7 → 3.9 (chat)
3.7 → 3.10 (concurrent)
3.8 + 3.9 + 3.10 → 3.11 (author_workflow)
```

**Strict dependencies**: 3.0 → 3.1 (parser before executor). 3.1 → everything (executor is foundational).

**Parallel opportunities**: after 3.1 lands, phases 3.2-3.6 can be developed independently (different subsystems). 3.7 needs them all complete. 3.8/3.9/3.10 are independent after 3.7.

A solo developer takes the serial path: ~18-26 weeks total. A small team can compress by parallelizing 3.2-3.6 (~14-20 weeks total) and 3.8/3.9/3.10.

## 18.4 Cumulative user value at each gate

| After phase | What an author can build |
|---|---|
| 3.0 | (nothing executable yet) — but can write and validate workflow YAML |
| 3.1 | Linear bash + authored-Python pipelines |
| 3.2 | + LLM-classified pipelines |
| 3.3 | + conditional branching, retries |
| 3.4 | + interactive workflows with pauses |
| 3.5 | + persistent file IO and state |
| 3.6 | + branching logic, list filter + general-purpose compute (Jinja-expression node) |
| 3.7 | + iteration over lists, sub-workflow composition (full v1 catalog) |
| 3.8 | + fork-from-event ("what-if" runs) |
| 3.9 | + workflows from chat |
| 3.10 | + parallel execution |
| 3.11 | + AI-generated workflows |
