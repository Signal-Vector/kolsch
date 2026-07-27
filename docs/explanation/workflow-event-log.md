# Workflow Event Log

This document is the cross-phase event-log schema and conventions for the workflow engine — the canonical list of event kinds, the frozen envelope, the layered ID model, capture rules, and the cross-JSONL correlation story that ties workflow runs to spawned agent sessions. It is extracted verbatim from §11 of the Phase 3.0 workflow specification (`process/phase3.0-workflow-spec.md`); section-number cross-references resolve to the phase that now owns each topic: §5.5 (`NodeContext`) remains in `process/phase3.0-workflow-spec.md`; §8.4 (cancellation / events.jsonl layout) is in `process/phase3.1-executor-spec.md`; §13.5 (configurable policy audit emission) is in `docs/explanation/workflow-policy-model.md` under "Configurable policy audit emission"; §9.5 (fork-point legality) is in `process/phase3.8-fork-spec.md`. New event kinds, fields, or conventions are NOT introduced here — this file is a stable surface for downstream consumers (forensics tooling, fork-point reasoning, agent-layer integrations) to cite.

---

## 11. Event Log

### 11.1 Event kinds

**Naming convention.** Every event kind follows the strict pattern `<owner>.<thing>.<verb-past>` — three dot-segments, the last always a past-tense verb. `workflow.run.completed`, `workflow.node.started`, `policy.decision.made`. This convention is the contract; new event kinds added in Phase 3.x must conform.

| Kind | Meaning |
|---|---|
| `workflow.run.started` | run begins |
| `workflow.run.completed` | entire run finished successfully |
| `workflow.run.failed` | entire run aborted due to node failure |
| `workflow.run.timed_out` | entire run exceeded the workflow-level `timeout_seconds:` at a node boundary |
| `workflow.run.aborted` | user-initiated cancellation via `kolsch workflows cancel` (§8.4) |
| `workflow.run.forked` | emitted on the NEW run with parent + fork_event_id |
| `workflow.run.validation_failed` | run-start gate failed |
| `workflow.node.started` | node begins execution |
| `workflow.node.completed` | node finished successfully; payload includes captured outputs |
| `workflow.node.failed` | node raised; payload includes exception + traceback |
| `workflow.node.timed_out` | a node exceeded its per-node `timeout_seconds:` (§2.6) |
| `workflow.node.logged` | author's `ctx.log` call (was: `node.log`) |
| `workflow.node.emitted` | engine-side wrapper kind family for author `ctx.emit` calls (the actual wire kind on disk is the per-call author kind below) |
| `workflow.node.called_workflow` | sub-workflow invocation start |
| `workflow.node.called_workflow_completed` | sub-workflow invocation end |
| `workflow.node.file_read` / `workflow.node.file_written` / `workflow.node.file_listed` | file-IO audit |
| `workflow.tool.called` | tool invocation from inside a workflow node |
| `policy.decision.made` | emitted by the engine's tool-dispatch wrapper for each policy decision (see `docs/explanation/workflow-policy-model.md`, *Configurable policy audit emission*); payload: `{action, target, effect, rule_id, layer, caller_chain}`. Emission is configurable per `audit_level` (*Configurable policy audit emission*). |
| `model.request.sent` | LLM request fired (inherited from the agent layer) |
| `model.response.received` | LLM response received (inherited from the agent layer) |

**UI consumer of `called_workflow*` (spec-103).** Beyond forensics, the parent-side `workflow.node.called_workflow` / `workflow.node.called_workflow_completed` events now drive a live chat-TUI surface: the chat service (`WorkflowsImpl`) treats `called_workflow` as the signal to open the child sub-workflow's own flat peer card (opening a card + child run record + event drain, keyed on the payload's `child_workflow_run_id`), and `called_workflow_completed` as the signal to wind that lane down. This does not change the events' on-disk shape — it is a new read-side consumer. See `docs/architecture/workflows.md` §"Sub-workflows".

**Author-emitted kinds (`ctx.emit`).** Author calls to `ctx.emit(subkind, payload)` (per §5.5) land in the JSONL with wire kind `node.<bundle>.<subkind>`. The engine auto-prepends `node.<bundle>.` based on `ctx.bundle` (engine-set from the catalog-resolved invocation file's bundle, per S4); the `subkind` portion is the FREEFORM author-supplied suffix (no regex enforcement). Examples: a node in bundle `acme` calling `ctx.emit("ticket_classified", {...})` emits `kind: node.acme.ticket_classified`; the same node calling `ctx.emit("crm_lookup_failed", {...})` emits `kind: node.acme.crm_lookup_failed`. Conformance to `<owner>.<thing>.<verb-past>` is the author's responsibility — engine does not validate the subkind. The bundle segment guarantees no cross-bundle collisions.

**R6-R1: the `author` segment is DROPPED.** The earlier R5 R1 wire prefix `node.author.<bundle>.<subkind>` is amended to `node.<bundle>.<subkind>`. The dropped segment was redundant: the engine's own kinds all start with `workflow.`, `policy.`, `model.`, or `tool.` (never `node.`), so the `node.*` namespace is unambiguously authored. The shorter prefix keeps event-kind strings legible without losing the namespace partition.

**Rename table (R5 → R6 amendment):**

| R5 (deprecated) | R6 (current) |
|---|---|
| `node.author.<bundle>.<subkind>` | `node.<bundle>.<subkind>` |

**Cost telemetry (out-of-scope; agent-layer concern).** The `model.response.received` event already carries `data.usage` (token counts captured from the underlying LLM response) inherited from the agent layer. The `.cost` field on that payload is RESERVED (populated as `0.0` at Phase 3.0); pricing and dollar-cost calculation are agent-layer scope, not workflow-layer scope. When the agent layer eventually ships pricing tables and computes per-call cost, workflow events will inherit that data automatically — no workflow-engine changes required. Phase 3.0 does NOT introduce cost telemetry primitives. See §20 for the corresponding open question.

**Token-usage aggregation (R6-R3).** The engine does NOT aggregate token usage at v1 — no per-run totals, no per-node totals, no rolling counter event kind. Operators who want totals read the `model.response.received` events across the JSONL and sum the `data.usage` payload themselves (jq one-liner, or a follow-up script). Consistent with L7 (no `token_budget:` field) and R4 #16 (no aggregate event kind reserved). If a real operational need emerges, a Phase 3.x QoL can ship `kolsch workflows show <run> --usage` as a read-side aggregation over the existing event stream — no new event kinds required.

**JSONL integrity (S6).** Event log integrity (tamper detection, append-only chain validation, HMAC chaining) is deferred to Phase 3.x. v1 relies on FILESYSTEM-LEVEL access controls — the workspace directory's POSIX permissions are the trust boundary. There is no `kolsch workflows verify <run_id>` command at v1, no per-event HMAC, no chain-prev-hash field. Operators concerned with tamper detection should restrict write access to `runs/` to the engine's process identity.

**Code-side module rename is OUT of Phase 3.0 scope.** The user-facing event-kind rename `authz.decision → policy.decision.made` and the YAML field rename `permissions: → policy:` happen in Phase 3.0. The code-side rename (`src/providers/authz.py → src/providers/policy.py`, `RuleBasedAuthorizationProvider → RuleBasedPolicyProvider`, `PermissionLayer → PolicyLayer`) is a separate follow-up tracked in `process/qol-policy-rename.md`. Module names referenced elsewhere in this spec (e.g., `src/providers/authz.py` in the matcher snippet in `docs/explanation/workflow-policy-model.md`, *`from:` field on Rule (caller-chain scoping)*) intentionally keep the legacy name until that QoL ships.

### 11.2 Common fields

All events carry: `kind`, `workflow_run_id`, `trace_id`, `parent_event_id`, `ts`. Node-scoped events also carry `invocation_id` and `node_name`. The following events ALSO carry a persisted `caller_chain: list[str]` in payload (so "who authorized this?" forensics work post hoc): `workflow.tool.called`, `workflow.node.started`, `model.request.sent`.

- `trace_id` is the chain-spanning ID for the entire causal cascade triggered by one user-initiated entry point — inherited UNCHANGED across sub-workflow calls, spawned agent sessions, sub-agent invocations, and forks. A root run gets a freshly minted `trace_id`; everything downstream inherits.
- `parent_event_id` follows the Phase 1 session convention — references the event that causally produced this one (e.g., a `workflow.node.started` event's `parent_event_id` is the predecessor edge's `workflow.node.completed`, or `workflow.run.started` for the start node).
- Workflow-node-emitted events for sub-workflow calls carry the standard envelope; the child workflow_run's events carry the parent's `trace_id` so the cascade is correlatable across JSONLs.
- Map-node-spawned worker invocation events carry payload fields `parent_invocation_id: <map_invocation_id>` AND `iteration_index: int` (0-based) so consumers can correlate workers to their map node and ordinal (§4.6.2).
- The `session.opened` event for spawn-from-workflow scenarios carries a polymorphic `spawned_by` field in payload — either `{from_workflow_run_id, from_invocation_id, from_workflow_event_id}` (workflow→session) OR `{from_session_id, from_tool_call_id}` (agent→agent).

There is NO `schema_version:` field on events. Earlier drafts proposed one; it's been dropped in favor of an additive-evolution rule:

- The event envelope (`kind`, `workflow_run_id`, `trace_id`, `parent_event_id`, `ts`, `invocation_id`, `node_name`) is FROZEN.
- Per-event-kind payload `*Data` models EVOLVE ADDITIVELY: new optional fields with sensible defaults are allowed without a version bump.
- Renames, type-changes, and field removal in payloads are NOT allowed without a migration plan.
- Triggers to revisit (i.e., introduce a real version field): (a) a second on-disk event-log format appears that isn't an additive evolution of the Phase-1 session JSONL, OR (b) a payload field genuinely needs renaming / re-typing / removal, OR (c) an external consumer of the JSONL emerges that wants explicit version negotiation.

### 11.2.1 ID naming conventions and ownership

The layered ID model — outermost first:

| Scope | ID | Prefix | Owner |
|---|---|---|---|
| Chain-spanning | `TraceId` | `tr_` | CLI / chat entry point |
| Workflow run | `WorkflowRunId` | `wrk_` | workflow engine |
| Agent session | `SessionId` | `ses_` | agent runtime |
| Per-node invocation | `InvocationId` | `inv_` | workflow engine |
| Per-tool call | `ToolCallId` | `tc_` | tool dispatcher |
| Per-agent thread | `ThreadId` | `th_` | agent runtime |
| Per-completion call | `CompletionId` | `cl_` | agent runtime |
| Per-event | `EventId` | `ev_` | event emitter |

All ID types are `PrefixedId` subclasses backed by UUID4. The UUID4 invariant guarantees `EventId` is globally unique across all runs and entities.

**Ownership rules.** `TraceId` is minted at the user-initiated entry point (CLI command, chat thread origin); every downstream entity inherits it unchanged. The `WorkflowRunId` and `InvocationId` are minted by the workflow engine. `SessionId`/`ThreadId`/`CompletionId` are minted by the agent runtime. `ToolCallId` is minted by the tool dispatcher. `EventId` is minted by whichever subsystem appends the event to its JSONL.

`ToolCallId` is TYPED at v1: `ToolCallId(PrefixedId)` with prefix `tc_`. (Earlier drafts treated it as a bare `str`; consolidated to the prefix family.)

### 11.3 Capture rules

- Captured outputs are the source of truth for fork replay (per Phase 1 materialization rule).
- Secret values NEVER appear in captured outputs — captured `bash` commands, agent prompts, and config-field interpolations preserve the LITERAL Jinja form (`{{ secrets.api_key }}`), never the resolved value. Engine substitutes the resolved value ONLY at execution time and never persists it. No post-hoc string-scanning of arbitrary payloads (false-negative-prone); discipline relies on the pre-substitution-form-only rule.
- Authors who deliberately log secret values via `ctx.log("token is " + secret)` defeat this — documented as the author's responsibility (mirrors the discipline tools already have).

### 11.5 Cross-JSONL correlation

A workflow run that invokes an `agent:` node produces events across TWO files:

- The workflow JSONL — `runs/<wrk_id>/events.jsonl` — captures the workflow's own execution: `workflow.run.*`, `workflow.node.*`, `policy.decision.made`, and (for the node's perspective) the `model.request.sent` / `model.response.received` framing events emitted when the engine routes through the agent runtime.
- The agent session JSONL — `sessions/<ses_id>/events.jsonl` — captures the agent's reasoning thread: per-completion call detail, tool invocations the agent makes mid-thread, internal `session.*` lifecycle events.

The two JSONLs are LINKED BY `trace_id` — every event in both files carries the same `trace_id` (chain-spanning per R4 #10 + R5 H1; inherited unchanged across spawned sessions and child workflow runs). Forensic queries that need the full picture (everything caused by one user-initiated entry point) read both JSONLs and JOIN on `trace_id`. A query like "show me every event triggered by the user's `/workflow triage` invocation last Tuesday" reads `runs/<wrk_id>/events.jsonl` AND every `sessions/<ses_id>/events.jsonl` whose first event carries the same `trace_id`.

**Reserved future CLI flag.** A planned Phase 3.x QoL: `kolsch workflows events <run_id> --include-spawned` auto-stitches the workflow's JSONL with the JSONLs of all sessions sharing its `trace_id`, returning a single chronologically-ordered stream. The flag is reserved here so future tooling has a clear name; v1 ships the unstitched per-file view.

This is also why the fork-point legality rule (§9.5) restricts forks to workflow-run-level events: events in agent session JSONLs belong to the agent's lifecycle, not the workflow's, and the join-by-`trace_id` correlation is read-only forensic — it does NOT make agent-session events legal fork points.
