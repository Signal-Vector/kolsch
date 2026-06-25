# Phase 3.1 — Sequential Executor Spec

**Intro.** This spec covers Phase 3.1 — the sequential executor. Depends on Phase 3.0 (parser + validator) having shipped.

Related docs:
- `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-parser-validator-spec.md` — the Phase 3.0 parser + run-start validator that this executor builds on.
- `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md` — the master design spec (Phase 3.0 locked decisions across 21 sections).
- `docs/explanation/workflow-event-log.md` — author-facing explanation of the event-log conventions referenced below (event kinds, common fields, capture rules, cross-JSONL correlation).

---

## 8. Sequential Executor

### 8.1 Algorithm

1. Run-start gate (§7).
2. Initialize state from workflow inputs.
3. Topological iteration:
   - For each node in topological order, evaluate the node's `when:` gate modifier; skip if false.
   - Compute bound inputs from upstream captured outputs (or `nodes.*` references).
   - Jinja-interpolate config strings (NativeEnvironment per §2.7.1).
   - Invoke the node (leaf execution or composite recursion via the iterative worklist).
   - Capture outputs as a `workflow.node.completed` event.
   - For branchable nodes, compute branch value; mark only branch-matching outgoing edges as fireable.
4. On any node failure: emit `workflow.node.failed`, set status to `failed`, halt.
5. On completion: assemble the inferred output dict from captured terminal-node outputs (terminals that didn't fire contribute `null`); set status to `completed`; emit `workflow.run.completed` with the output dict in the payload.

**When does AND-strict fire?** (A7-7) Multi-input edges require the engine to know when a target node's sources have "completed" — and the answer is more nuanced than "every source has emitted `workflow.node.completed`." Three rules govern the answer:

1. **Skip counts as completion.** A source counts as completed for AND-strict trigger purposes when EITHER its `workflow.node.completed` event has been emitted OR its `when:` gate fired false (the node was skipped). Skipping IS a kind of completion — the source had its decision made, the engine moved on, and downstream consumers should not block waiting for a node that will never fire. Concretely: a target with two upstream sources, one of which has `when: "{{ state.feature_a_enabled }}"`, fires as soon as both (a) the gated source's gate evaluated AND either skipped or completed, AND (b) the other source completed. If the gate evaluated false (skip), the target's `inputs.*` for that edge's binds are `Undefined` in Jinja — authors using `default()` filters handle this idiomatically.
2. **Failure aborts BEFORE AND-strict is re-evaluated.** A source that failed (raised) PREVENTS the target from firing — the failure causes the whole run to abort per the abort-on-failure rule in §2.3, and AND-strict semantics never get a chance to consider the failed source as "completed." `on_error: continue|skip|route_to` is deferred to Phase 3.x (per §1.2); at v1 a failed source HALTS the run, full stop.
3. **Retries are consumed BEFORE the completion event.** A source with `retry: N` (per §2.6) is considered completed only when its Nth attempt SUCCEEDS (emitting `workflow.node.completed`) OR when all N retry attempts have been exhausted in failure (per rule 2, which then aborts the run). The intermediate retry attempts do NOT emit `workflow.node.completed`; the engine emits `workflow.node.failed` only AFTER the final retry has failed. Downstream targets see a clean "the source completed (eventually)" or "the run aborted" — never a partial state where retries are in progress.

These rules together mean AND-strict is well-defined for every combination of `when:` gates, `retry:` counts, and failure modes: the source is "done" when the engine has reached a decision about it (completed, skipped, or fail-after-retries), and at that point the target either fires (all sources done, no failures) or the run aborts (any source failed).

### 8.2 Module loading (per-run isolation)

- At run start: snapshot `sys.modules` keys.
- Track imports performed during the run by filter: any new `sys.modules` entry whose loaded `__file__` lies under a `bundles/` path is "ours" and goes in the per-run import set.
- Third-party imports (httpx, pandas, etc.) — even if the node code imports them — are NOT tracked, so they don't get cleaned up between runs. Re-importing pandas every workflow run would be catastrophic.
- At run end (any status), remove only the per-run import set from `sys.modules`. Third-party state stays.
- Next run re-imports the bundle-namespace modules fresh.

### 8.2.1 Graph traversal: iterative, not recursive

The executor walks the graph iteratively (worklist / queue), not via Python recursion. Reasons:
- Sub-workflow nesting depth (up to 16) plus map iteration could blow the Python call stack with recursion.
- Iterative traversal makes pause/resume cleaner — the executor can save and restore its worklist state to support `waiting_for_input` durably.
- The engine becomes reentrant for sub-workflows without recursion: each sub-workflow gets its own executor instance + its own worklist.

### 8.3 State management

- State dict initialized to a copy of workflow inputs at start.
- `set_state` / `delete_state` nodes mutate the dict.
- All other nodes see the current state via Jinja `{{ state.* }}`.

### 8.4 Cancellation and orphan recovery

**Sidecar cancel protocol.** `kolsch workflows cancel <run_id>` writes a `cancel.flag` sentinel file into the run's directory (alongside the JSONL). The Phase 3.1 executor polls for `cancel.flag` AT NODE BOUNDARIES ONLY (never mid-node). On detection: the executor emits a `workflow.run.aborted` event with `{reason: "user_cancel"}` in the payload, sets the run's status to `aborted`, performs the same module-isolation cleanup it would on `completed`/`failed`, and exits.

The cancel mechanism is COOPERATIVE — it does NOT rescue a hung node. If a node has wedged (e.g., an infinite loop, a stuck subprocess, an unresponsive HTTP call without a timeout), the operator must `kill -9` the process. After the kill, running `kolsch workflows cancel <run_id>` again reconciles the on-disk JSONL status (see orphan sweep below).

Phase 3.0 ships the CLI command and the sidecar-write contract; Phase 3.1 ships the executor poll loop and orphan sweep.

**Orphan sweep.** On engine startup (any `kolsch workflows ...` invocation that loads the run index), every `workflow_run` with `status=running` is checked against its recorded PID. If the PID is no longer live (or the recorded PID is absent — pre-Phase-3.1 runs), the run is reconciled to `status=failed` with `reason: "orphaned"` and a synthetic `workflow.run.failed` event is appended to the JSONL. This guarantees that crashed/killed runs eventually reach a terminal status without manual intervention.

**Sidecar layout** (per run directory):

```
runs/<wrk_id>/
├── events.jsonl              # the canonical event log
├── pid                       # recorded executor PID (Phase 3.1)
└── cancel.flag               # written by `kolsch workflows cancel`
```

The `cancel.flag` file's CONTENTS are unused at v1 (empty file is fine); presence is the signal. Future versions may carry a reason/operator-identity payload.

---

## Pause / Resume

Pause/resume mechanism is documented in `process/phase3.4-user-input-spec.md` §10. The executor's role is to honor the pause marker and emit no completion event for the open user_input invocation until resume.

---

## 11. Event Emission (executor-relevant subset)

The executor is responsible for emitting the `workflow.*` family of events at well-defined points in the algorithm above. The full event-log conventions live in `docs/explanation/workflow-event-log.md`; the rules below are the executor-emission contract.

### 11.1 Event kinds emitted by the executor

The executor emits these kinds (see `docs/explanation/workflow-event-log.md` §11.1 for the full table): `workflow.run.started`, `workflow.run.completed`, `workflow.run.failed`, `workflow.run.timed_out`, `workflow.run.aborted`, `workflow.node.started`, `workflow.node.completed`, `workflow.node.failed`, `workflow.node.timed_out`, `workflow.node.logged`, `workflow.node.emitted`, `workflow.tool.called`, `policy.decision.made` (when `audit_level: all`).

### 11.2 Emission ordering and timing (executor contract)

The executor guarantees the following ordering for the algorithm in §8.1:

- `workflow.run.started` is the FIRST event in the JSONL (preceding any node events).
- Per node invocation: `workflow.node.started` precedes any author-emitted events (`workflow.node.logged`, `node.<bundle>.<subkind>`), which precede the terminal `workflow.node.completed` OR `workflow.node.failed` for that invocation. Retries do not emit intermediate `workflow.node.completed` / `workflow.node.failed` events; only the final attempt is reported (per §8.1 AND-strict rule 3).
- A `when:` gate firing false produces NO `workflow.node.started` and NO `workflow.node.completed` for the skipped node — the skip is implicit (per §8.1 AND-strict rule 1).
- `workflow.run.completed` / `workflow.run.failed` / `workflow.run.timed_out` / `workflow.run.aborted` is the LAST event in the JSONL for a terminal run. The orphan sweep (§8.4) appends a synthetic `workflow.run.failed` to JSONLs that never received a terminal event.
- Sub-workflow invocation: the parent emits `workflow.node.called_workflow` before invoking the child executor, and `workflow.node.called_workflow_completed` after the child returns. Child events land in the child's own JSONL — see cross-JSONL correlation below.
- `workflow.run.aborted` is emitted only after the executor observes the `cancel.flag` sentinel at a node boundary (§8.4) — it is NEVER emitted mid-node.

### 11.3 Common fields

All events carry: `kind`, `workflow_run_id`, `trace_id`, `parent_event_id`, `ts`. Node-scoped events also carry `invocation_id` and `node_name`. The following events ALSO carry a persisted `caller_chain: list[str]` in payload (so "who authorized this?" forensics work post hoc): `workflow.tool.called`, `workflow.node.started`, `model.request.sent`.

- `trace_id` is the chain-spanning ID for the entire causal cascade triggered by one user-initiated entry point — inherited UNCHANGED across sub-workflow calls, spawned agent sessions, sub-agent invocations, and forks. A root run gets a freshly minted `trace_id`; everything downstream inherits.
- `parent_event_id` follows the Phase 1 session convention — references the event that causally produced this one (e.g., a `workflow.node.started` event's `parent_event_id` is the predecessor edge's `workflow.node.completed`, or `workflow.run.started` for the start node).
- Workflow-node-emitted events for sub-workflow calls carry the standard envelope; the child workflow_run's events carry the parent's `trace_id` so the cascade is correlatable across JSONLs.
- Map-node-spawned worker invocation events carry payload fields `parent_invocation_id: <map_invocation_id>` AND `iteration_index: int` (0-based) so consumers can correlate workers to their map node and ordinal (§4.6.2).
- The `session.opened` event for spawn-from-workflow scenarios carries a polymorphic `spawned_by` field in payload — either `{from_workflow_run_id, from_invocation_id, from_workflow_event_id}` (workflow→session) OR `{from_session_id, from_tool_call_id}` (agent→agent).

### 11.4 Capture rules (executor-relevant)

- Captured outputs are the source of truth for fork replay (per Phase 1 materialization rule).
- Secret values NEVER appear in captured outputs — captured `bash` commands, agent prompts, and config-field interpolations preserve the LITERAL Jinja form (`{{ secrets.api_key }}`), never the resolved value. Engine substitutes the resolved value ONLY at execution time and never persists it. No post-hoc string-scanning of arbitrary payloads (false-negative-prone); discipline relies on the pre-substitution-form-only rule.
- Authors who deliberately log secret values via `ctx.log("token is " + secret)` defeat this — documented as the author's responsibility (mirrors the discipline tools already have).

### 11.5 Cross-JSONL correlation

A workflow run that invokes an `agent:` node produces events across TWO files:

- The workflow JSONL — `runs/<wrk_id>/events.jsonl` — captures the workflow's own execution: `workflow.run.*`, `workflow.node.*`, `policy.decision.made`, and (for the node's perspective) the `model.request.sent` / `model.response.received` framing events emitted when the engine routes through the agent runtime.
- The agent session JSONL — `sessions/<ses_id>/events.jsonl` — captures the agent's reasoning thread: per-completion call detail, tool invocations the agent makes mid-thread, internal `session.*` lifecycle events.

The two JSONLs are LINKED BY `trace_id` — every event in both files carries the same `trace_id` (chain-spanning per R4 #10 + R5 H1; inherited unchanged across spawned sessions and child workflow runs).

See `docs/explanation/workflow-event-log.md` for the full author-facing treatment of cross-JSONL correlation, including the planned `--include-spawned` flag.

---

## 14. CLI Surface (executor-shipped commands)

Phase 3.0 already shipped the workflow CLI surface; Phase 3.1 WIRES UP the run-time commands (`run`, `resume`, `cancel`) that the 3.0 phase stubbed out with "executor not implemented (Phase 3.1)" rejections.

### 14.1 Commands (executor subset)

```
kolsch [--format text|json] workflows run <ref> [--input k=v]... [--dry-run] [--follow]
kolsch [--format text|json] workflows resume <run_id> [--input k=v]...
kolsch [--format text|json] workflows cancel <run_id>
```

`workflows cancel <run_id>` writes a `cancel.flag` sidecar file into the run's directory (§8.4). Phase 3.0 already shipped the CLI and sidecar-write contract; Phase 3.1 ships the executor polling cooperation and orphan sweep that consume the sentinel.

(`--format` can be specified before or after the subcommand path; Click handles both naturally.)

### 14.2 Behavior notes

- `--input key=value` repeatable — assembled into a dict and validated against the workflow's derived input schema (run / fork) or the user_input node's output schema (resume; key is typically `response`). Matches the existing `kolsch run <agent> --input key=value` convention exactly.
- Complex inputs: value is treated as a string and Pydantic coerces. For lists/dicts, pass JSON literals (`--input 'tags=["a","b"]'`). A future `--inputs-file <path>` flag may accept a JSON file for very large payloads.
- `--dry-run`: prints the resolved graph + would-execute plan without side effects.
- `--follow`: tails the workflow_run's JSONL while it executes; useful for live observability.
- Default behavior of `run` (no `--follow`, no `--dry-run`): blocks until completion. Prints final outputs on success (JSON) or error info on failure. Exit code reflects success / failure.
- Non-interactive by default; missing required inputs hard-fail with the list of missing keys.

### 14.3 CLI output format convention (executor commands)

The `kolsch` CLI supports a global `--format text|json` flag that applies to every command. Default is `text` (current human-friendly behavior unchanged); `json` produces a structured envelope for machine consumption. The envelope shape (`command`, `status`, `data`, `errors`) is defined in §14.3 of the master spec; the per-command data schemas for the executor-shipped commands are:

- `workflows run <ref>` → execution result schema TBD (Phase 3.1)
- `workflows resume <run_id>` → execution result schema TBD (Phase 3.1)
- `workflows fork <run_id>` → `{ new_run_id, parent_run_id, fork_event_id }` (Phase 3.1)

Exit codes are unchanged: 0 on success, non-zero on failure, regardless of `--format`.
