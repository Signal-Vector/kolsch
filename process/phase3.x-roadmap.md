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

### 3.1 — Minimum executor (bash + authored Python + noop) — 🟡 Next

**Status:** Spec exists at `process/phase3.1-executor-spec.md`; the
step-by-step build plan (`process/phase3.1-executor-build.md`,
mirroring `phase3.0-workflow-build.md`'s shape) is NOT yet written.
Step 0 of Phase 3.1 is to author that build plan.

Spec: `process/phase3.1-executor-spec.md`.

**Scope**: sequential executor (no modifiers, no branching, no condition gates). Walks the graph topologically and invokes nodes. Node types: `bash` (routing through existing `kolsch/bash` tool), `noop`, AND the catalog-ref dispatcher for authored Python nodes (`type: <bundle>/nodes/<name>` — full `@node` introspection + signature dispatch). Module-loading discipline (`sys.modules` cleanup filtered by `bundles/` prefix). Iterative graph traversal (no Python recursion). The `kolsch-engine.LocalController` per-host coordination concerns (PID tracking, `cancel.flag` poll loop, JSONL append, orphan sweep) land in this phase — the Phase 3.1 designer picks the LocalController module layout. CLI: `run`, `events`.

**Verification gate**: a linear three-node pipeline (`bash → <bundle>/nodes/transform → noop`) runs end-to-end. JSONL event stream is complete and well-formed. Module isolation provably cleans up between runs (test: run twice, verify `sys.modules` is unchanged for `bundles/*`). The authored-Python `@node`'s three return-type forms (BaseModel, bool, tuple) all dispatch correctly.

**Why this slice**: proves the executor architecture without LLM/agent complexity. Authors can write real (if simple) workflows. Architecture validated by running code.

**Effort**: ~2 weeks.

### 3.2 — Agent integration

Spec: `process/phase3.2-agent-integration-spec.md` (to be written).

**Scope**: `agent` node type. Threads through existing CrewAI engine via the asset catalog's agent invocation path. Per-invocation `outputs:` override using existing `IOSpec` grammar from `src/schemas/config.py:58`. Child-session creation with `from_workflow_run:` pointer.

**Verification gate**: workflow invokes an agent and consumes its structured output. Session is created and carries the `from_workflow_run:` pointer. Output validation works for both the agent-declared schema AND a workflow-overridden schema. Agent run failures (max iterations, tool failures) surface as `WorkflowNodeFailed`.

**Why separate**: agent integration is substantial plumbing (CrewAI hooks, session lifecycle, structured-output dispatching). Wants its own milestone so a regression in agent handling doesn't block leaf-type work.

**Effort**: ~1-2 weeks.

### 3.3 — Modifiers + branching execution

Spec: `process/phase3.3-modifiers-branching-spec.md` (to be written).

**Scope**: node-level `when:` gate modifier (Jinja gate); node-level `retry:` (with `tenacity` adopted as the retry library); MULTI-INPUT edge handling — AND-strict trigger semantics + `inputs.<key>` namespace at receivers + `WorkflowBindCollision` parse-time check + `WorkflowBindTypeMismatch` runtime check (Pydantic strict mode); edge-level `branch:` field. Bash branches on `exit_code`; authored Python nodes branch when return type is `bool` or `tuple[bool, BaseModel]`. Validator rules for branching invariants (both true/false branches required when branching used; no branch on non-branchable nodes).

**Verification gate**: workflows with `when:` gates skip nodes correctly. `retry:` retries the configured number of times then propagates failure. Multi-input fan-in: a receiver with two incoming edges fires only after both sources complete; bind-key collisions surface as `WorkflowBindCollision`; type mismatches at fire time surface as `WorkflowBindTypeMismatch`. Cross-graph references to non-ancestors fire `WorkflowJinjaRefUnknown(reason="not_predecessor")`. Bash branch-on-non-zero fires the false-branch edges instead of aborting. Validator errors fire for every branching invariant violation.

**Why separate**: modifiers genuinely change executor behavior. Branching has subtle rules (dynamic abort behavior for bash). Bundle them so the executor's "extended" behavior arrives coherently.

**Effort**: ~1-2 weeks.

### 3.4 — User_input with CLI pause/resume

Spec: `process/phase3.4-user-input-pause-resume-spec.md` (to be written).

**Scope**: `user_input` node type. Pause mechanism: status flips to `waiting_for_input`; JSONL ends with the user_input node's `workflow.node.started` event but no completion event. CLI `resume` command completes the open invocation with the user-supplied response.

**Verification gate**: a workflow with a user_input node pauses cleanly; the process can exit and resume in a separate command invocation. Multiple sequential pauses in one workflow work. Resume rejects when run isn't in `waiting_for_input` state.

**Why separate**: pause/resume is a real entity-lifecycle feature with persistence implications. Worth its own milestone.

**Effort**: ~1 week.

### 3.5 — File IO + state mutators

Spec: `process/phase3.5-file-io-state-spec.md` (to be written).

**Scope**: 3 file-IO leaves (`read_file`, `write_file`, `list_files` — each 1:1 with the existing tool surfaces, routing through the `file_access` subsystem); 2 state leaves (`set_state`, `delete_state`). State dict initialized from workflow inputs at run start; mutations captured as events.

**Verification gate**: workflows read/write files within the workspace; `file_access` denials are enforced and surface as `WorkflowFileAccessDenied`. State mutations are captured events that replay correctly (on fork; tested in 3.8). Jinja `state.*` scope works for any node's config reading state.

**Why grouped**: small leaves that share the "thin wrappers around existing subsystems" pattern. Implementing them together exercises the same auth/permission integration plumbing.

**Effort**: ~1 week.

### 3.6 — Branch node + filter + compute + NativeEnvironment

Spec: `process/phase3.6-branch-filter-compute-spec.md` (to be written).

**Scope**: `branch` node type. `filter` (the sole leaf list-reducer). `compute` (general-purpose Jinja expression; subsumes the 8 dropped aggregations via stock Jinja filters). Adopt `jinja2.nativetypes.NativeEnvironment` for all config-field interpolation.

**Verification gate**: workflows that branch + filter + compute work end-to-end with NativeEnvironment preserving Python types through Jinja expressions (no JSON roundtripping in `over:` expressions). The end-to-end "triage tickets" pipeline example from the spec walkthrough runs and produces correct results. The 8 aggregation recipes (sum/count/min/max/sort/flatten/any/all) all expressible as `compute.value:` Jinja and pass tests.

**Why grouped**: `filter` needs list sources (from authored Python nodes returning lists or `list_files`); `compute` reuses the same Jinja substrate to cover everything `filter` doesn't; `branch` is similar leaf shape; NativeEnvironment is the substrate that makes typed values flow correctly. All four validate the same underlying design choice.

**Effort**: ~1-2 weeks.

### 3.7 — Composition (map + workflow)

Spec: `process/phase3.7-composition-spec.md` (to be written).

**Scope**: `map` node type with iteration machinery, worker-by-name resolution, and `item_as:` binding. `workflow` node type with sub-workflow nesting, cycle detection (transitive self-call → `WorkflowCallCycle`), depth-16 cap, child-run lifecycle, parent-pointer tracking.

**Verification gate**: map iterates a worker over a list and produces `results: list[T]`. `filter` and `compute` consume map outputs cleanly. Sub-workflow runs created with `parent: { workflow_run_id, invocation_id }` pointer. Cycle detected and rejected at run-start gate. Workflow-call exceeding depth 16 fails clearly.

**Why separate**: composites are genuinely complex engine work — recursion handling, child-run lifecycle, parent linking. Different from leaf-type implementation.

**Effort**: ~2 weeks.

### 3.8 — Fork mechanism

Spec: `process/phase3.8-fork-spec.md` (to be written).

**Scope**: fork-from-event implementation. Engine: load parent run's event log up to fork_event_id; replay captured outputs into new run's state; apply `--input` overrides; begin execution from fork point. New `workflow_run` with `forked_from: { run_id, fork_event_id }`. State mutations replay deterministically. Cross-JSONL navigation when fork spans sub-workflow boundaries. CLI: `fork`.

**Verification gate**: fork a completed run from any event; new run continues from that point with same captured upstream state; original run remains immutable. Fork a paused run (still in `waiting_for_input`). Fork across a sub-workflow boundary correctly resolves the child's captured outputs.

**Why separate**: fork is genuinely hard. Cross-JSONL reads, deterministic state reconstruction, parent-pointer chain integrity. Its own milestone with its own test suite.

**Effort**: ~3-4 weeks.

### 3.9 — Chat surface

Spec: `process/phase3.9-chat-surface-spec.md` (to be written).

**Scope**: `/workflow <ref> with k=v ...` and `/fork <event_id> with k=v ...` slashes. Chat capsule rendering for node invocations + sub-workflow nesting (collapsible capsules) + branch-not-taken visual mute. user_input pause/resume in chat (next message becomes the response). Modal callback integration so policy prompts triggered by workflow-invoked actions fire the existing chat modal.

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
