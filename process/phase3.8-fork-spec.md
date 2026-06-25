# Phase 3.8 — Fork Mechanism

This spec covers Phase 3.8 — fork-from-event. Depends on Phase 3.1 executor and Phase 3.7 composition. Replay-with-modification.

Cross-reference: event log structure and event-kind taxonomy live in the event-log doc (§11 of `docs/explanation/workflow-event-log.md`) — fork-point legality (§9.5 below) is defined relative to the `workflow.*` family enumerated in §11.1 of `docs/explanation/workflow-event-log.md`.

## 9. Fork Mechanism

### 9.1 Operation

`kolsch workflows fork <run_id> --at <event_id> [--input '<json>']`:

1. Load parent run's event log up to and including `<event_id>`.
2. Create new `workflow_run` with `forked_from: { run_id: <parent>, fork_event_id: <event_id> }`. The new run inherits the parent's `trace_id` unchanged — both original and fork belong to the same causal cascade.
3. Replay the captured-output events from the parent into the new run's state (idempotent — no node executes; captured outputs become canonical for nodes up to the fork point).
4. Apply optional `--input` overrides (replaces captured outputs at the fork point).
5. Begin execution from the fork point in the new run.

### 9.2 Side-effect discipline

Upstream nodes are NOT re-executed; their captured outputs are reused. Upstream side effects from the parent run (bash commands sent, agent calls made, file writes performed) are NOT undone by the fork — the new run begins with the world in whatever state the parent left it. This is assumed; no separate acknowledgment or warning gates the fork operation.

### 9.3 Sub-workflow forking

Forking a child workflow_run forks only that child; the parent run is unaffected (parent will use the child's captured output when re-evaluating the calling node).

Forking a parent at an event BEFORE a workflow-call node drops the child entirely. Forking AFTER reuses the child's captured outputs.

### 9.4 Picking the fork event

Chat capsules display event IDs. CLI command `kolsch workflows events <run_id>` prints the event stream with IDs. The user picks an `<event_id>` from there.

### 9.5 Fork point legality

Fork-from-event is legal ONLY at WORKFLOW_RUN-level events — the events written to the workflow run's own `runs/<wrk_id>/events.jsonl`. The full list of legal fork-point kinds is the §11.1 `workflow.*` family (`workflow.run.*`, `workflow.node.*`, `workflow.tool.called`, `policy.decision.made` when emitted in the workflow context, etc.) — see `docs/explanation/workflow-event-log.md` §11.1. (R6 L3.)

**Mid-agent-session events are NOT legal fork points.** Events written to a spawned agent's session JSONL (`sessions/<ses_id>/events.jsonl`) — `model.request.sent`, `model.response.received`, `session.*` — belong to the agent's reasoning thread, NOT the workflow's execution model. Forking at a mid-agent event would land the new workflow run inside someone else's session lifecycle with no defined replay semantics. The engine rejects such attempts at fork time with `WorkflowRuntimeError` (the fork-point event_id either doesn't exist in the workflow run's JSONL, or is on the wrong kind family).

Authors who want to "re-roll" an agent invocation fork at the workflow-level event immediately preceding the agent node — typically the `workflow.node.started` for the `agent:` node — and supply alternative inputs via `--input` overrides. The fork re-fires the entire agent invocation from scratch (a new session, a new model call); the original session is left intact and immutable.

## 14.1 Fork CLI command

The fork command is part of the broader `kolsch workflows` CLI surface (§14.1 of `phase3.0-workflow-spec.md`); reproduced here for self-containment:

```
kolsch [--format text|json] workflows fork <run_id> --at <event_id> [--input k=v]...
```

Behavior notes (from §14.2):

- `--input key=value` repeatable — assembled into a dict and validated against the workflow's derived input schema (run / fork) or the user_input node's output schema (resume; key is typically `response`). Matches the existing `kolsch run <agent> --input key=value` convention exactly.
- Complex inputs: value is treated as a string and Pydantic coerces. For lists/dicts, pass JSON literals (`--input 'tags=["a","b"]'`). A future `--inputs-file <path>` flag may accept a JSON file for very large payloads.

JSON output schema (from §14.3):

- `workflows fork` → `{ new_run_id, parent_run_id, fork_event_id }`

## WorkflowRunNotFound handling

When fork-from-event targets a parent run whose JSONL has been deleted, the engine raises `WorkflowRunNotFound` (R4 #14). The error class is anchored in §12.1 of `phase3.0-workflow-spec.md`:

> `WorkflowRunNotFound` — raised on fork-from-event when the parent run's JSONL has been deleted (§2.3, §9).

Retention context (per §1.2 forever-retention lock): workflow run event logs are plain files on disk at `runs/<run_id>/events.jsonl`. "Forever retention" means the engine WILL NOT auto-prune. Operators retain full control — delete, archive, or back up via standard filesystem tools. If a parent run's JSONL is deleted, fork-from-event attempts against that run raise `WorkflowRunNotFound`. SaaS/multi-tenant cost concerns (per-tenant quotas, automated retention policies) are explicitly deferred to Phase 4+ multi-tenancy work; v1 ships single-tenant filesystem semantics.

## Cross-references

- Event log structure, event kinds, and the `workflow.*` family enumeration: §11 of `docs/explanation/workflow-event-log.md`.
- Cross-JSONL correlation by `trace_id` and the read-only nature of agent-session events under join-by-`trace_id` queries: §11.5 of `docs/explanation/workflow-event-log.md` (the fork-point legality rule restricts forks to workflow-run-level events; agent-session events are forensic-only, not legal fork points).
- Sequential executor and run lifecycle: §8 of `process/phase3.1-executor-spec.md` (depends on Phase 3.1 executor).
- Workflow composition (`extends:`, sub-workflow calls): depends on Phase 3.7 composition spec.
- Error hierarchy and `WorkflowRunNotFound` placement: §12.1 of `phase3.0-workflow-spec.md`.

## Non-goals

This spec introduces NO new fork modes. The single fork operation defined is `kolsch workflows fork <run_id> --at <event_id> [--input k=v]...` — replay parent events up to the fork point, optionally override inputs at that point, then execute forward. No "fork-and-merge", no "fork-multiple", no automatic re-execution of upstream side effects. The side-effect discipline (§9.2) is normative: side effects are NOT undone.
