# Workflow Foundation — Locked Cross-Phase Decisions

This document is the cross-phase foundation for the Phase 3.x workflow subsystem: the decisions that apply uniformly across every Phase 3.x sub-phase (parser, validator, executor, fork, chat surface, policy). These decisions were finalized before spec drafting and are the contract every other workflow document honors. They are extracted verbatim from §2 of `process/phase3.0-workflow-spec.md`; the round/item codes (R3–R7, L/S/J/H/M/C/CV/R6-R#) are preserved exactly because they are referenced from the decision-log decoder.

---

## 2. Locked Foundation Decisions

This section enumerates every decision finalized before spec drafting. It is the contract every other section honors.

### 2.0 Phase-3.0 cross-cutting locks (decisions 1, 2, 3, 5, 6, 7, 8, 9, 10)

These are the cross-cutting decisions that shape multiple sections:

- **3-segment refs canonical EVERYWHERE.** Tools, agents, workflows, nodes — all use `<bundle>/<asset_type>/<name>`. The 2-segment form is deprecated across the board (migration shim continues to accept it on read). One builder: `src/assets/refs.py:build_ref`. See `docs/explanation/workflow-policy-model.md`, *Node-type refs* and *Refs unification (single grammar across agents, tools, workflows, nodes)*.
- **16 built-in node types (no `python` literal).** Authored Python nodes are referenced as `type: <bundle>/nodes/<name>` (a 3-segment catalog ref). See §4 and §4.0.1 of `process/phase3.0-workflow-spec.md`.
- **Multi-input edges as the fan-in mechanism.** A node may have any number of incoming edges; the engine fires it once ALL incoming edges' sources have completed (AND-strict). Edge shape: `{from, to, binds, branch}` — no `ref:` field. `binds:` keys flow into `inputs.<key>` at the receiver; collisions across edges are a parse error (`WorkflowBindCollision`). See §6.4 and §7.2 of `process/phase3.0-workflow-spec.md`.
- **`WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`)** is the structural "this YAML name doesn't exist in `nodes:`" error. Distinct from `WorkflowJinjaRefUnknown` (Jinja predecessor / schema). See §7.1 and §12.1 of `process/phase3.0-workflow-spec.md`.
- **ruamel.yaml** replaces PyYAML across kolsch via a new `src/kolsch/_yamlio.py` helper module. Phase-3.0 Step 0. See §12.3 of `process/phase3.0-workflow-spec.md` and Phase 3.0 in §18.2 of `process/phase3.x-roadmap.md`.
- **`NodeContext` is `@dataclass(frozen=True, slots=True)`** with `secrets: Mapping[str, str]` and `state: Mapping[str, Any]` (both wrapped in `MappingProxyType` at runtime — read-only). The `bundle: str` and `emit_fn: Callable` fields are PUBLIC test seams (renamed from `_bundle` / `_emit_fn` per L7-3); `ctx.emit(subkind, payload)` is a real method that calls through `emit_fn`. A `NodeContext.for_test(**overrides)` classmethod provides a one-line construction shortcut for unit tests. See §5.5 of `process/phase3.0-workflow-spec.md`.
- **stdlib `ExceptionGroup` for multi-error reporting.** No custom group class. Filter via Python 3.11+'s `except* <SubClass>:`. See §7.2 and §12.1 of `process/phase3.0-workflow-spec.md`.
- **Strict single-line error messages with literal `Fix: ` token.** Format: `<ClassName>: <file>:<line> <names>: <summary>. Fix: <action>.` No `\n`, no embedded YAML, no snippet block. See §12.3 of `process/phase3.0-workflow-spec.md`.
- **Type-level label on node types.** Authored Python nodes set it via `@node(label="...")`; the chat-capsule fallback resolves through `display_label` (`src/workflows/labels.py`): per-instance `description:` → the type-level label → a cleaned function/class name (§15.4 of `process/phase3.9-chat-surface-spec.md`).
- **§6.0 minimal example + §6.0.1 common patterns** added at the head of §6 (pattern reference: linear, branching, fan-in via multi-input edges, sub-workflow).

### 2.1 Authoring shape

- YAML-first with `nodes:` (mapping) + `graph: edges:` (list) at the top level.
- Function-call semantics for node reuse: every reference is a fresh invocation.
- Explicit edge data binding via `binds: { target_input: "{{ nodes.<source>.<output_field> }}" }`.
- Workflow asset inheritance via top-level `extends: [<refs>]` — strict union; no dupes (any duplicate node or edge name across the chain is an error). **`extends:` imports `nodes:` and `graph.edges:` ONLY; it NEVER imports `policy:`.** An extended workflow's `policy:` block applies when that workflow is invoked DIRECTLY, but it is NOT inherited by extenders. If an extended workflow declares any `policy:` rules, the extending workflow's run-start gate raises `WorkflowExtendsPolicyForbidden` (hard error). For shared policy patterns across many workflows, place the rules in `bundle.yaml` or `platform.yaml` — those layers are walked naturally by the proximate-caller rule (see `docs/explanation/workflow-policy-model.md`, "Engine-side layer selection (escalation prevention; proximate-caller rule)"). Cross-reference: `docs/explanation/workflow-policy-model.md` (policy model).
- Node-level `copy_of: <other_node_name>` creates a FRESH local instance of another node defined in the same workflow (copy-with-overrides semantics for node reuse). DISTINCT keyword from workflow-level `extends:` to avoid confusion. The `copy_of:` source MUST itself be graph-reachable — there is no "template-only" node status. For pure config-sharing without a graph-resident source, use YAML anchors.
- `binds:` RHS values are either YAML literals accepted as-is OR Jinja expressions wrapped in `{{ }}` (e.g., `{{ nodes.fetch.stdout }}` or `{{ nodes.fetch.stdout | upper }}`). See §6.4 of `process/phase3.0-workflow-spec.md` for the full rule. The legacy bare `source.field` shorthand is NOT supported.

### 2.2 Graph topology

- A node may have ANY number of incoming edges. Fan-in is STRUCTURAL — just add another edge. The engine waits for ALL incoming edges' source nodes to complete before firing the target (AND-strict trigger semantics). For OR semantics, the author hoists the choice into an upstream `branch` node and routes a single edge to the consumer.
- Each node may have multiple outgoing edges (fan-out; branching).
- Edges carry data via `binds:`. At the receiving node, every bound key is addressable as `inputs.<key>` in any of the node's Jinja-evaluated config fields and (for authored Python nodes) is populated into the Pydantic input model. `binds:` keys colliding across multiple edges into the same node (two edges binding the same name) is a parse error → `WorkflowBindCollision`.
- Authors may STILL use `nodes.<X>.<field>` directly for cross-graph references (both forms coexist). `inputs.<key>` is the short-form when the data is already an edge-bound input on this node.
- Start node is implicit (single node with no incoming edges). Ambiguity → `WorkflowAmbiguousStart`.
- End is implicit (any node with no outgoing edges). Multiple end nodes are normal (per-branch terminals).
- Workflow's input shape = start node's input shape (inferred; not declared).
- Workflow's output shape = inferred from terminal nodes (nodes with no outgoing edges). Dict keyed by terminal node name; each value is that terminal node's output schema. At runtime: terminals that fired have populated values; terminals on untaken branches have `null`.

The single-input rule from earlier drafts is GONE; the `WorkflowMultiInputForbidden` error class is REMOVED. Predecessor validation (§7.2 of `process/phase3.0-workflow-spec.md`) is now graph-reachability-based: any transitive ancestor of a node via the edge graph is a legitimate predecessor for `nodes.<X>.<field>` references.

### 2.3 Execution model

- Sequential at v1; topology must support fan-out/fan-in for v2+.
- Eager-at-run-start gate: all reachable nodes loaded + all binds validated BEFORE execution begins.
- Per-run module isolation: `sys.modules` cleanup before and after each run. (Phase 3.10 switches to per-run module prefix when parallelism lands.)
- Forever retention of `workflow_run` JSONLs (matches existing session retention). Concretely: workflow run event logs are plain files on disk at `runs/<run_id>/events.jsonl` (per the layout in §8.4 of `process/phase3.1-executor-spec.md`). "Forever retention" means the engine WILL NOT auto-prune. Operators retain full control — delete, archive, or back up via standard filesystem tools. If a parent run's JSONL is deleted, fork-from-event attempts against that run raise `WorkflowRunNotFound`; resuming or forking a *child* run whose ancestor run is gone raises its subclass `WorkflowLineageUnavailable` (the child's authz caller chain can't be rebuilt, so it fails closed). SaaS/multi-tenant cost concerns (per-tenant quotas, automated retention policies) are explicitly deferred to Phase 4+ multi-tenancy work; v1 ships single-tenant filesystem semantics.
- **Operator-controlled retention tooling — Phase 3.x deliverable.** `kolsch workflows gc --older-than <Nd> [--dry-run] [--keep-forked]` is planned as a Phase 3.x deliverable (operator-controlled retention with safety against fork-chain breakage — deleting a parent that has live forks would orphan those forks). See `process/qol-workflow-gc.md`. Until that ships, retention is purely manual filesystem hygiene.
- **JSONL durability at v1 is best-effort.** The engine does NOT guarantee fsync-per-event or atomic line writes. Partial writes may occur on crash (mid-write process kill, fs flush ordering, kernel panic); recovery is OPERATOR responsibility — typically by trimming the JSONL's last partial line manually and reconciling the run's terminal status. Future durability primitives — fsync-per-event policy, atomic line writes via temp-file rename, tail-skip rules for the reader, orphan sweep last-line validation — are explicitly deferred to Phase 3.x QoL. The Phase 3.0 contract is "events.jsonl is append-only, one JSON object per line, last line MAY be truncated on crash."
- Workflow-level `timeout_seconds:` (top-level peer field) enforced as a next-node-boundary cooperative deadline. Per-node `timeout_seconds:` modifier (§2.6) fails the individual node; retry policy applies. Workflow-level deadline pauses during `waiting_for_input` status. Sub-workflows have independent budgets (no parent-budget propagation).
- Single failure mode: `abort` on any node error. `on_error: skip|continue|route_to` deferred to Phase 3.x.

### 2.4 Entity model

- `workflow_run` is a ROOT entity (its own JSONL file). NOT a child of session.
- Workflow runs may reference a triggering session (`triggered_by: session_id | None`) when invoked from chat.
- When an `agent` node fires inside a workflow run, the engine creates a new session for that agent's reasoning thread (`from_workflow_run: <wrk_id>` on the session).
- Sub-workflow invocations produce child workflow_runs with `parent: { workflow_run_id, invocation_id }`. Child sub-workflows inherit the parent's `trace_id` unchanged.
- Forks produce new workflow_runs with `forked_from: { run_id, fork_event_id }`. Original is immutable. Forked runs inherit `trace_id` from the source unchanged.
- Every `workflow_run` carries a `trace_id` — inherited from its parent at creation when launched from another workflow run, agent session, or chat thread; a root run (one with no parent entity) gets a freshly minted `trace_id`. See `docs/explanation/workflow-event-log.md` for the chain-spanning ID model.

### 2.4.1 workflow_run status states

Enumerated. Surfaces in `kolsch workflows show`, `kolsch workflows events`, event payloads, and CLI exit-code translation.

| Status | Meaning |
|---|---|
| `running` | Run-start gate passed; nodes are executing. |
| `waiting_for_input` | A `user_input` node fired and is awaiting completion. Status is sticky until `kolsch workflows resume` (or chat reply) supplies the response, at which point the user_input node's invocation completes and execution continues. |
| `completed` | All reachable nodes finished successfully; output dict assembled and persisted. |
| `failed` | A node raised; run halted per the abort-on-failure rule. |
| `timed_out` | Run exceeded its workflow-level `timeout_seconds:` at a node boundary; engine halted cleanly before dispatching the next node. |
| `aborted` | User-initiated cancellation via `kolsch workflows cancel <run_id>`. Phase 3.1 executor polls a sidecar `cancel.flag` between node boundaries; on detection, emits `workflow.run.aborted` and exits cleanly. Cancel does NOT rescue hung nodes — operator must `kill -9` for those, then `cancel` reconciles the JSONL status. |

CLI exit code: 0 for `completed`; non-zero for `failed`, `timed_out`, and `aborted`.

### 2.5 Branching

- Edge-level: outgoing edges from a branchable node carry `branch: true` or `branch: false`.
- `bash` nodes are branchable; branch value = `(exit_code == 0)`.
- Authored Python nodes (referenced via `type: <bundle>/nodes/<name>`) are branchable when their `run` function's return type is `bool` or `tuple[bool, BaseModel]`.
- `branch` nodes are always branchable; branch value = their `condition:` field's evaluation result.
- Nodes whose outgoing edges don't use `branch:` are non-branching; their natural output flows unconditionally.
- For branchable `bash` nodes: non-zero exit fires the false-branch edges instead of aborting.

### 2.6 Modifiers

Orthogonal to node type:
- `when:` (node-level) — execution gate; node fires only when the Jinja expression is truthy. This is the renamed modifier (was `condition:` in earlier drafts) chosen to leave the word `condition:` exclusively for the PRIMARY config field on `branch` and `filter` node types. One word, one role: `when:` = gate; `condition:` = the node's actual computation.
- `retry:` (node-level) — integer count of retry attempts on failure; engine wraps invocation via the chosen retry library (proposed: `tenacity`). Workflow-level adoption now creates the seam to expand to other parts of kolsch later (tools, agent calls, etc.) without each subsystem hand-rolling its own. **`retry: int` is intentionally simple at v1; LLM-shaped retry semantics (exception-class filtering, exponential backoff, jitter, per-error-class policies) are deferred to Phase 3.x.**
- `timeout_seconds:` (node-level) — optional integer; positive seconds the node may run before the engine fails it with `NodeTimeoutExceeded`. Failure is per-node; retry policy applies. Distinct from the workflow-level `timeout_seconds:` (§6.1 of `process/phase3.0-workflow-spec.md`) which fails the whole run at a node boundary.
- `description:` (node-level) — optional free-text string used by chat capsules and the `show` CLI to label what this node does. Available on every node type. `description:` is a USER AID for display only; it is NEVER used to identify, reference, or address a node. The node NAME (the `nodes:` mapping key) is the only identifier. When `description:` is absent, surfaces fall back to the 3-step rule in §15.4 of `process/phase3.9-chat-surface-spec.md` (description → type `label:` → class/function name).
- `branch: true|false` (edge-level) — only fire this edge when the source node's branch value matches. The word `when:` is reserved for the node-level gate; the dedicated edge modifier is `branch:` (see §6.4 of `process/phase3.0-workflow-spec.md`).

### 2.6.1 Type-level `label:` property

Every node TYPE declares a `label: str` — a short human-readable name describing the kind of thing it does. This is distinct from the per-instance `description:` modifier above.

- Labels resolve through `display_label` (`src/workflows/labels.py`), the single source of truth — a three-step fallback: per-instance `description:` → a type-level label → a cleaned class/function name.
- For **authored Python node types**, the `@node` decorator accepts an optional `label="..."` keyword: `@node(label="Enrich lead with CRM data", secrets_needed=[...])`, supplying that type-level label. When omitted, the label falls back to the cleaned function/class name (snake_case → title-case).

The label is what chat capsules and `show` CLI fall back to when the per-node `description:` is absent (§15.4 of `process/phase3.9-chat-surface-spec.md` algorithm). `description:` is OPTIONAL universally; the type-level `label:` guarantees there's always something readable to show.

**Canonical built-in labels:**

| Built-in type | `label:` |
|---|---|
| `kolsch/nodes/bash` | Shell |
| `kolsch/nodes/agent` | Agent |
| `kolsch/nodes/workflow` | Sub-workflow |
| `kolsch/nodes/noop` | Pass through |
| `kolsch/nodes/user_input` | Wait for input |
| `kolsch/nodes/read_file` | Read file |
| `kolsch/nodes/write_file` | Write file |
| `kolsch/nodes/list_files` | List files |
| `kolsch/nodes/set_state` | Set state |
| `kolsch/nodes/delete_state` | Delete state |
| `kolsch/nodes/filter` | Filter |
| `kolsch/nodes/map` | Map |
| `kolsch/nodes/branch` | Branch |
| `kolsch/nodes/compute` | Compute |

### 2.7 Jinja

- Universal in every string config field on every node type. **For Jinja evaluation, use `{{ }}`. Otherwise YAML literals are accepted. Filters are core-only — bundles, plugins, and pip-installable packages CANNOT register custom filters. The filter set is kolsch's responsibility, kept small and audited.** Used in `binds:` RHS, every config string, `when:`, `branch.condition:`, `filter.condition:`, `compute.value:`, `map.over:`, and so on — every Jinja-evaluated field follows the same rule: a bare YAML literal (string, int, float, list, bool, null) is accepted as-is; a `"{{ ... }}"` template is evaluated under NativeEnvironment. The legacy `source.field` shorthand stays removed.
- Scopes in evaluation context: `inputs.*` (per-node, populated by incoming edges' `binds:` keys at the receiver), `state.*` (workflow-wide), `secrets.*` (workspace), `nodes.<name>.<field>` (cross-graph reference; legitimate names = transitive ancestors of the receiving node in the edge graph).
- **Graph-reachability rule.** For any Jinja-evaluated field on a node `N`: `nodes.<X>.<field>` is allowed iff `X` is a transitive ancestor of `N` via the edge graph (or `X == N` for self-reference patterns where applicable — currently disallowed at v1). The validator computes the ancestor closure at the run-start gate (§7.2 of `process/phase3.0-workflow-spec.md`). `binds:` RHS may always reference any immediate parent's `nodes.<parent>.<field>` (immediate parents ARE transitive ancestors, trivially).
- `nodes.<X>` is Jinja `Undefined` until node `<X>` completes successfully. Skipped nodes (gate fired false) and failed nodes leave `nodes.<X>` undefined. Idiom for "may not have run": `{{ nodes.X is defined }}` or `{{ nodes.X.field | default(...) }}`.
- `nodes.*` references are evaluated LAZILY at expression-render time (when the node fires), not snapshotted at edge-bind time. Engine looks up the referenced node's captured-output event from the run's JSONL on each access. Trade-off: marginal perf cost vs. clean "always current" semantics; v1 picks clean.
- Pre-validated at run-start gate: see §7.2 of `process/phase3.0-workflow-spec.md` for the AST walk that resolves every `nodes.<X>.<field>` reference against the declared output schemas and the transitive-ancestor predecessor set.
- Standard Jinja2 filters and control structures; v1 ships exactly one core-only kolsch-authored filter (`from_json`, see §2.7.3). Bundles cannot register additional filters.
- `{% include %}` and `{% extends %}` NOT supported (template inheritance is out of scope).

### 2.7.1 Native-type evaluation (`NativeEnvironment`)

The engine uses `jinja2.nativetypes.NativeEnvironment` for ALL config-field interpolation, not default `Environment`. The behavior:

- **Single-expression templates preserve Python types.** `{{ tickets }}` returns the actual `list[Ticket]`, not its `str()` repr. Same for `{{ score }}` returning int/float, `{{ ticket.urgent }}` returning bool, `{{ items | selectattr('urgent') | list }}` returning a filtered list.
- **Multi-segment templates fall back to strings.** `"curl {{ url }} | tee out"` returns the joined string (correct for shell commands, prompts, file paths, etc.).

Why this matters: list-consuming nodes (`filter` via `over:`, `compute` via `value:`, `map` via `over:`) need real Python lists. Default Jinja would render `{{ tickets }}` as a string repr and force the engine to `ast.literal_eval` it back to a list — lossy (Decimal, datetime), slow, and surprising. NativeEnvironment makes this Just Work.

Prior art: Ansible variable resolution, Airflow's `render_template_as_native_obj`, Home Assistant template states — all use this pattern for the same reason.

Engine behavior on type mismatch: if a config field is typed as `str` but its Jinja expression renders to a non-string (e.g., `write_file` node's `content:` receiving a dict), the engine raises `WorkflowNodeFailed` with a clear message. Author fix is one filter (`| tojson` typically).

### 2.7.2 Jinja sandbox contract (Python-RCE mitigation)

Kolsch never evaluates author Jinja under a bare `NativeEnvironment`. All workflow Jinja runs under a SANDBOXED environment — a subclass of `jinja2.sandbox.ImmutableSandboxedEnvironment` that ALSO preserves native-type returns (either by composing `jinja2.nativetypes.native_concat` into the concat path or overriding `_parse` to wrap rendering in the native-types form). The sandbox blocks the canonical Jinja-to-Python escape vectors: `__class__`, `__mro__`, `__subclasses__`, `__globals__`, `__builtins__`, and the recurring `lipsum.__globals__` / `cycler.__init_subclass__` / `joiner` payloads that have surfaced in CVEs against permissive Jinja deployments.

- **Module home.** The singleton sandboxed environment lives at `src/workflows/jinja_env.py`. All workflow Jinja rendering (parser, validator AST walk, runtime config-field interpolation, `binds:` RHS rendering) goes through this module.
- **Sandbox class.** A subclass of `jinja2.sandbox.ImmutableSandboxedEnvironment` (NOT bare `SandboxedEnvironment` — the immutable variant ALSO blocks mutation of objects through Jinja, which matters for `state.*` discipline). Native-type returns are preserved via composition with `jinja2.nativetypes` machinery.
- **Failure mode.** When the sandbox blocks an unsafe attribute access or callable invocation, the engine raises a new exception class `WorkflowJinjaSecurityError` (subclass of `WorkflowRuntimeError`) with the offending construct and the blocked operation in structured fields. The error class is end-user-facing per the §12.4 of `process/phase3.0-workflow-spec.md` two-channel rendering rules.
- **Test fixtures.** `tests/security/test_jinja_sandbox_escape.py` covers ~10 canonical Jinja-to-Python escape payloads: bare `__class__`, the `__mro__` walk, `__subclasses__` enumeration, `lipsum.__globals__`, `cycler.__init_subclass__`, `joiner.__init__`, `''.__class__.__base__.__subclasses__()`, format-string `__globals__` access, `attr` filter sneaking through `__class__`, and `request.application.__globals__` (Flask-style payload). Each fixture asserts the sandbox raises `WorkflowJinjaSecurityError`.

The trade-off: native-type return paths in stock `NativeEnvironment` rely on a literal-eval over the concatenated render. The subclass keeps that semantic for the safe shapes (lists, dicts, ints, bools) while routing attribute/callable access through the sandbox's `is_safe_attribute` and `is_safe_callable` checks. Authors writing pure data-shape Jinja (`{{ tickets | selectattr('urgent') | list }}`) see no change; authors attempting introspection (`{{ ''.__class__ }}`) hit a hard error.

### 2.7.3 v1 filter set

The v1 filter inventory is short by design — the principle from §2.7 is "filters are core-only; bundles cannot register their own." Kolsch ships exactly ONE kolsch-authored filter at v1 alongside the stock Jinja2 filters that work under the sandboxed environment.

**`from_json` (kolsch-authored).** Parses a JSON string into the corresponding Python value (`json.loads`). The expected idiomatic use is bridging the bash → Jinja boundary: an upstream `bash` node emits JSON on stdout, and the downstream consumer reads it as typed Python via `{{ nodes.fetch.stdout | from_json }}`. The filter is implemented in `src/workflows/jinja_env.py` via module-level registration in `_register_builtin_filters()` and is composable with NativeEnvironment's type-preserving returns — `{{ '{"x": 1}' | from_json }}` returns the dict `{"x": 1}`, not the str repr. Failure modes: invalid JSON raises `WorkflowNodeFailed` (the underlying `json.JSONDecodeError` lands in `__cause__`); `null` returns Python `None`. Closes §20.4's open question on JSON-parse filter (in `process/phase3.0-workflow-spec.md`).

**Stock Jinja2 filters.** The standard Jinja2 filter set is available unchanged where the sandbox permits — `length`, `upper`, `lower`, `default`, `selectattr`, `rejectattr`, `map`, `sort`, `sum`, `min`, `max`, `tojson`, `replace`, `trim`, `join`, `split`, etc. The aggregation recipes in §4.0 of `process/phase3.0-workflow-spec.md` all use stock filters. The sandbox blocks the introspection paths (`__class__`, `__mro__`, etc.); data-shape filters are untouched.

**Adding new core filters.** The contribution pattern for kolsch maintainers (NOT bundles) to propose new core filters is documented in `docs/explanation/jinja-extensions.md` — covering criteria for inclusion, security review under the sandbox, naming conventions, and the spec-amendment process. The doc is explicitly addressed to kolsch maintainers; bundle authors who need a non-standard filter compute the value upstream via an authored Python node returning a typed model and consume that.

### 2.8 Python node API

- Bare `@node` marker decorator; no type args on the decorator (only `secrets_needed=[...]`).
- Function MUST be named `run`. One `@node`-decorated `run` per file.
- Signature-driven types via standard Python annotations:
  - Param 1: input model (Pydantic BaseModel subclass).
  - Param 2: must be `NodeContext`.
  - Param 3 (optional): config model (Pydantic BaseModel subclass).
- Return type determines the node's contract:
  - `BaseModel` subclass → non-branching with structured output.
  - `bool` → branching only.
  - `tuple[bool, BaseModel subclass]` → branching with structured output.

### 2.9 Policy

- Nodes are TARGETS in policy (not principals). They follow the existing tool pattern.
- Rules `what:` form: **three-segment qualified ref** `<bundle>/<asset_type>/<name>` (e.g., `kolsch/nodes/parse_classification`, `kolsch/tools/bash`). The asset-type segment disambiguates tools from nodes when a bundle contains both with the same name.
- Internal catalog form is the same three-segment shape.
- Action namespace: `node.<name>` (matches `tool.<name>`).
- `@node(secrets_needed=[...])` mirrors `@tool(secrets_needed=[...])`. NO `policy=` on the decorator.
- New `workflow_policy` layer added to the layer walk (sourced from workflow YAML's top-level `policy:` block).
- Layer order: workflow → agent → bundle → platform → default deny.
- Existing tool rules using the legacy two-segment shape (`<bundle>/<name>`) continue to load through the existing migration shim. Phase 3.0 ships node refs in the three-segment shape; Phase 3.x may migrate tool refs for symmetry (separate effort).

### 2.10 Errors

- Pythonic exception hierarchy under `kolsch.workflows.errors` (no error codes).
- Single `workflow.node.failed` event emitted on failure; the workflow_run's status flips to `failed`. NO separate `aborted` event at v1.
