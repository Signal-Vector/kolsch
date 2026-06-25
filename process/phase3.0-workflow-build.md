# [SPEC-001] Phase 3.0 — Workflow parser & run-start validator (no execution)

**Type:** spec
**Doc #:** 001
**Status:** Draft
**Created:** 2026-06-09

---

## Glossary

Default vocabulary for this spec — use these meanings unless qualified otherwise (per Round 4 item #8).

| Term | Meaning |
| --- | --- |
| node | An entry in a workflow's `nodes:` mapping. The default reading of "node" everywhere unless qualified. |
| node type | The catalog citizen, addressable via 3-seg ref (e.g., `kolsch/nodes/bash`). |
| captured output | The value addressable via `nodes.<name>` in Jinja expressions. |
| invocation | The runtime fire of a node; owns `InvocationId`. |

Where prose disambiguation is needed (e.g., §4.0, §11, §13.1, §15.4 of the master spec), the word "node" is prefixed (e.g., "node type", "node instance"). The Pydantic `*NodeDef` class names are unchanged.

---

## Background

Phase 3.0 ships the **foundation layer** of the kolsch workflow system: the YAML parser, the asset catalog integration for the new `workflows/` and `nodes/` directories, the run-start validation gate, and the `kolsch workflows list/show/validate` CLI commands. **No execution machinery lands in 3.0** — `kolsch workflows run` is wired to reject with a clear "executor not implemented (Phase 3.1)" message.

The phase's purpose is **architecture validation**. Every design decision in the master spec (`process/phase3.0-workflow-spec.md`) — the YAML schema, the 14-node-type catalog shape, the `@node` decorator API, the three-segment authz ref form (canonical for ALL asset types: tools, agents, workflows, nodes), the unified refs module shared across agents/tools/workflows, the Pythonic exception hierarchy — gets exercised by writing real workflow YAML files and running them through the validator. If the schema is wrong, we discover it here with low cost (no executor work wasted).

The verification gate is a fixture suite: ~20 workflow YAML files covering every node type's config shape, every validator rule, and every error class. The fixtures are valuable independently as living documentation for authors.

**This spec breaks the phase into 7 sequential steps (preceded by a prereq Step 0 — the ruamel.yaml migration), each with its own Coder and Reviewer cycle.** Each step is independently shippable (passes its own tests) and reviewable (small enough to read end-to-end in one sitting). The Coordinator drives the multi-step flow; the Coder implements one step at a time; the Reviewer signs off step-by-step.

Related docs:
- `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md` — the full design spec (locked decisions across 21 sections).
- `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-prior-art-memo.md` — research synthesis.
- `docs/explanation/tools-subsystem.md` — the `@tool` pattern that `@node` mirrors.
- `docs/explanation/asset-management-contract.md` — the asset catalog mechanics being extended.

---

## Scope

**In scope (delivered across the 7 steps):**
- New module `src/workflows/` with: `parser.py`, `catalog.py`, `validator.py`, `decorator.py`, `errors.py`, `context.py`.
- Public API surface in `src/workflows/__init__.py`: `node` decorator, `NodeContext` dataclass, full exception hierarchy.
- Asset catalog auto-discovery of `bundles/<bundle>/workflows/*.yaml` and `bundles/<bundle>/nodes/*.py`.
- YAML parser → typed `Workflow` Pydantic model per master spec §6.
- `extends:` chain resolution: recursive, cycle detection, depth-16 cap, strict-union semantics.
- Run-start validation gate covering every check in master spec §7.1–§7.7.
- `@node` decorator + signature introspection per master spec §5.
- Exception hierarchy per master spec §12.
- New `workflow` authz layer (per master spec §13.2; surfaced via `RuleBasedAuthorizationProvider.allow()` taking `layers: tuple[PermissionLayer, ...]` per Round 4 item #7 — `PermissionLayer(name="workflow", rules=...)` is the v1 mechanism).
- Three-segment node refs (`<bundle>/<asset_type>/<name>`) per master spec §13.1.
- CLI commands `kolsch workflows list/show/validate/cancel`; `kolsch workflows run` registered but stubbed (Phase 3.1 message). **Round 7 L7-12 — symmetric `kolsch nodes list/show`** ship in Phase 3.0 (master spec §14.1 commands list expanded; §14.3 per-command data schemas include `nodes list → {nodes: [{ref, kind, label, secrets_needed}, ...]}` and `nodes show <ref> → {node: {...full structure...}}`); cross-asset rationalization for `agents`/`tools`/`skills`/`prompts` is queued at `process/qol-asset-cli-symmetric.md` as a follow-up QoL.
- Fixture suite: ~25-35 workflow YAML files at `tests/golden/workflows/`.
- Component tests at `tests/component/test_workflow_*.py`.
- Starter bundle additions: `bundles/kolsch/workflows/hello.yaml`, `bundles/kolsch/nodes/echo.py`.
- **R3 #12 — `docs/workflows.md`** (NOT deferred — Phase 3.0 deliverable). Initial anchor coverage (~16 anchors): `#binds-rhs`, `#ref`, `#extends`, `#fan-in`, `#single-input`, `#node-types`, `#copy-of`, `#map`, `#branch`, `#authz-from`, `#timeout`, `#cancel`, `#trace-id`, `#error-format`, `#secrets`, `#chat-capsules`. Each anchor: 200-500 words explaining concept, syntax, examples, common mistakes. Created OUTSIDE this workflow (separate deliverable from the 7-step Coder/Reviewer flow); Step 7 reviewer verifies the file exists with all referenced anchors.
- **R3 #1 — Cancellation protocol contract**: CLI `kolsch workflows cancel <run_id>` + sidecar `cancel.flag` write contract (Phase 3.0); executor polling + orphan sweep (Phase 3.1).
- **R3 #2 — Timeout primitive**: top-level `Workflow.timeout_seconds` + per-node common `timeout_seconds` modifier; validator enforcement.
- **R3 #3 — `audit_level` config field** on Rule-based authz; event-emission contract documented (Phase 3.1 implements).
- **R3 #10 — Full ID model**: typed prefixed IDs (`TraceId`/`WorkflowRunId`/`InvocationId`/`ToolCallId`); UUID4 invariant; layered ID ownership map; `trace_id` inheritance.

**Out of scope (deferred to later sub-phases per master spec §18.2):**
- Sequential executor (Phase 3.1).
- All built-in node-type RUNTIME implementations (Phase 3.1+).
- `kolsch workflows run/events/resume/fork` execution behavior (3.1, 3.4, 3.8).
- Chat slash commands (Phase 3.9).
- `author_workflow` meta-agent (Phase 3.11).

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml (via the new `src/_yaml.py` helper module), Jinja2 (`jinja2.nativetypes.NativeEnvironment`), Click (CLI), pytest. Package manager: `uv`. Container: `podman-compose` (see `docker-compose.yml`).

**Key existing modules to integrate with:**

- `src/assets/` — asset catalog. `catalog.py` shows how `tools`, `agents`, `skills` register. Workflows + nodes are new asset types added here.
- `src/schemas/config.py` — `IOSpec` at line 58 (used by agents for declared inputs/outputs). Reuse for the agent node's per-invocation `outputs:` override grammar per master spec §4.1.3.
- `src/tools/decorator.py` — the `@tool` decorator pattern. `@node` mirrors this exactly: signature `@node(secrets_needed=[...])`. Read `decorator.py` + `validator.py` + `subsystem.py` in `src/tools/`.
- `src/providers/authz.py` — `RuleBasedAuthorizationProvider`. Per Round 4 item #7 the `allow()` signature is refactored to take a single `layers: tuple[PermissionLayer, ...]` kwarg (the prior per-layer kwargs `agent_permissions=`/`bundle_permissions=`/etc. at lines 633-693 are REMOVED). A new `PermissionLayer` value object (`@dataclass(frozen=True, slots=True)`) carries `name: str` + `rules: tuple[Rule, ...]`. NO `WorkflowCatalogProtocol` introduced.
- `src/providers/secrets.py` — `SecretsProvider` protocol; `DotenvSecretsProvider`. `@node(secrets_needed=[...])` verified via `secrets.exists(name)` (pattern at `src/tools/subsystem.py:215`).
- `src/errors/` — module structure pattern for new `kolsch.workflows.errors` exceptions.
- `src/cli/` — CLI command structure. Add `src/cli/workflows.py` for the four subcommands.
- `src/assets/platform_config.py` — `default_platform_payload()`. No changes needed (workflows are new; no migration required).

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
git status

# Bootstrap a test workspace for smoke testing
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test setup --non-interactive

# Run the test suite
podman-compose exec dev uv run pytest tests/component/ -q
podman-compose exec dev uv run pytest tests/golden/ -q
```

**Decisions already locked in master spec §2 (do NOT relitigate):**

- 14 node types per master spec §4 (catalog DECLARATIONS only in 3.0; no runtime implementations). Final built-in catalog: **bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input** (R3 #9 renames: `mapper` → `map`, `state_set` → `set_state`, `state_delete` → `delete_state`). The `python` discriminator is REMOVED — authored Python nodes are referenced directly by their three-segment catalog ref (`type: <bundle>/nodes/<name>`), NOT by `type: python` with a separate `body:`/`ref:` field. The 8 aggregations (sum, count, min, max, sort, flatten, any, all) are REMOVED — replaced by the new general-purpose `compute` node that evaluates a Jinja expression and emits `{value: <result>}`. The 8 aggregation idioms become stock Jinja-filter recipes (`{{ items | sum(attribute='score') }}`, `{{ items | length }}`, etc.). Built-ins live at `kolsch/nodes/<name>` (3-seg). The 14 specific catalog refs `kolsch/nodes/{bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input}` are RESERVED catalog slots — user authoring at `bundles/kolsch/nodes/bash.py` raises `WorkflowBuiltinShadowed`. A bundle NAMED `bash`/`agent`/etc. is fine (bundle-name reservation is REMOVED per R3 #5); `core` is no longer reserved.
- **Multi-input edges as the fan-in mechanism (Round 4 item #11 — REVERSES the prior single-input rule)**: a node can have ANY NUMBER of incoming edges; the prior single-input rule is DROPPED. `WorkflowMultiInputForbidden` is REMOVED. Edges are now `{from, to, binds, branch, when}` — the prior edge-level `ref:` field is DROPPED. The engine waits for ALL incoming edges' source nodes to complete before firing the target (AND-strict trigger semantics; for OR-fan-in, the author hoists into an upstream `branch` node). Bind keys flow into an **`inputs.*`** YAML namespace at the receiver (Round 5 H4 — renamed from `input.*` to plural `inputs.*` to match the Python `@node` param convention `inputs: ModelType` which is already plural; the YAML and Python sides are now consistent): for authored Python nodes, `binds:` keys populate the Pydantic input model (as before); for built-in nodes, `binds:` keys are accessible via `inputs.<key>` in the node's Jinja config fields. Authors may STILL use `nodes.<X>.<field>` directly for cross-graph refs (both forms coexist). Single-start rule preserved: every workflow has exactly one start node. New error classes: `WorkflowBindCollision` (parse-time — two edges to the same target node bind the same key) and `WorkflowBindTypeMismatch` (runtime — rendered bind value fails Pydantic strict-mode validation against receiver's input model; e.g., 'int' rejects '"5"'). `state.*` is typed as `Any` at v1 (per-key typing deferred to a later phase). Auto-list-binding (collapsing matching-type binds into a list) is deferred to a later QoL. Validator predecessor closure is computed from graph reachability (transitive ancestors), NOT from `ref:` declarations.

- **Three-segment refs are REQUIRED everywhere (R3 #5)**: 1-seg shorthand (`type: bash`) is REMOVED. All YAML `type:` values are full 3-seg refs: built-ins use `kolsch/nodes/<name>` (e.g., `type: kolsch/nodes/bash`); authored Python nodes use `<bundle>/nodes/<name>`. Non-3-seg `type:` values raise `WorkflowInvalidRef`. `resolve_shorthand` is REMOVED from `src/assets/refs.py`; `parse_three_segment` is the sole ref parser. (See `process/qol-refs-1seg-removal.md` for the migration codemod.)

- **Cancel/abort primitive (R3 #1, Option A)**: `kolsch workflows cancel <run_id>` (CLI) writes a sidecar `cancel.flag` file into the run directory. Phase 3.1 executor polls the flag between node boundaries; on detection, emits `workflow.aborted` and exits cleanly. Cancel does NOT rescue hung nodes — operator must `kill -9` for those, then `cancel` reconciles the JSONL status. On engine startup, an **orphan sweep** marks any `status=running` run without a live PID as `failed` with reason `orphaned` and appends a synthetic `workflow.failed` event. Phase 3.0 ships CLI + sidecar write contract; Phase 3.1 ships executor cooperation + orphan sweep.

- **Workflow + per-node timeouts (R3 #2)**: Workflow-level `timeout_seconds:` (optional top-level peer field) enforced as a next-node-boundary cooperative deadline (NOT mid-node interrupt). Per-node `timeout_seconds:` modifier (§2.6) fails the individual node only; retry policy applies; workflow continues. Composition rules: cooperative stop at next node boundary; `waiting_for_input` PAUSES the workflow timer; sub-workflows have independent budgets (no parent-budget propagation). Validator: `WorkflowInvalidTimeout` (error, non-positive); `WorkflowTimeoutShadowsNode` (warning, per-node > workflow). Runtime exceptions: `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded`. Status enum: new `timed_out`. Events: `workflow.timed_out`, `workflow.node_timed_out`.

- **Configurable authz audit_level (R3 #3)**: Rule-based authz config gains `audit_level: str` (per Round 4 item #9 — typed as `str`, NOT `Literal`; the value table is documented at master spec §13.5). Engine emits `authz.decision` event during the workflow's tool-dispatch wrapper (Phase 3.1 implementation; spec is here for contract). Documented value table: `all` (persist every authz decision) and `rule_matched` (persist only decisions where an explicit rule fired — allow or deny). Payload: `{action, target, effect, rule_id, layer, caller_chain}` (caller_chain persisted per R3 #10).

- **`description:` field on every node (R3 #7)**: every node type gains an OPTIONAL common `description: str | None = None` field (`min_length=1`; empty strings rejected). Renamed from compute's required `summary:` — now optional on all 14 types. Chat capsules render `description` when present; type-specific defaults otherwise (`bash: <first 40 chars of command>`, `agent: <agent name>`, `read_file: <path>`, etc.).

- **Two-channel exception rendering — `caller_message` ClassVar (R3 #8)**: every `WorkflowError` subclass declares `caller_message: ClassVar[str]` — a 1-3-sentence plain-English statement for non-engineer end users. The strict single-line `str(exc)` remains engineer-targeted; `caller_message` is the parallel end-user channel. Rendered by: chat capsule surfaces (per §15.x), `--format json` envelope (per §14.4). Style rules: 1-3 sentences; NO class names; NO `file:line` tokens; NO Jinja syntax; NO technical jargon; recommends an action ("try again", "share the run ID with your engineer", "contact your engineer").

- **Per-class `identifying_names_keys` ClassVar (R3 #11)**: every exception class declares `identifying_names_keys: ClassVar[tuple[str, ...]]` listing its expected identifying-name fields. The `src/cli/_format.py:render_exception(exc)` generic JSON formatter introspects dataclass fields + reads the ClassVar to auto-build `{identifying_names: {...}, attrs: {...}}` per master spec §14.4. No per-class formatter functions; per-class JSON shape is contract-validated by the reviewer (every listed key must be a real dataclass field).

- **Full ID model (R3 #10)**: typed prefixed IDs — `TraceId(PrefixedId, prefix="tr_")`, `WorkflowRunId(PrefixedId, prefix="wrk_")`, `InvocationId(PrefixedId, prefix="inv_")`, `ToolCallId(PrefixedId, prefix="tc_")`. UUID4 backing guarantees global uniqueness for `EventId`. `trace_id` is the chain-spanning ID inherited unchanged across sub-workflow calls, spawned agent sessions, sub-agent invocations, and forks; root entry point gets a fresh `tr_`. `parent: {run_id, invocation_id}` is RENAMED to `parent: {workflow_run_id, invocation_id}` (no unqualified `run_id` survives). Map-spawned worker invocation events carry `parent_invocation_id` + `iteration_index: int` (0-based). Workflow events carry `parent_event_id` (Phase 1 convention). `session.opened` for spawn-from-workflow carries polymorphic `spawned_by`: either `{from_workflow_run_id, from_invocation_id, from_workflow_event_id}` OR `{from_session_id, from_tool_call_id}`. Events `tool.called`, `workflow.node_started`, `model.request` carry persisted `caller_chain: list[str]` in payload (was in-memory only). ID ownership map: CLI/chat owns `trace_id`; workflow engine owns `wrk_`/`inv_`; agent runtime owns `ses_`/`th_`/`cl_`; tool dispatcher owns `tc_`; event emitter owns `ev_`.

- **`validate_workflow()` returns ValidationResult + thin `_or_raise` wrapper (R3 #6)**: signature is `validate_workflow(workflow: Workflow, catalog: AssetCatalog) -> ValidationResult` where `ValidationResult` is a dataclass `{errors: tuple[WorkflowValidationError, ...], warnings: tuple[WorkflowValidationWarning, ...]}` with `is_valid: bool` property. Thin wrapper `validate_workflow_or_raise(workflow, catalog) -> None` preserves the raise-on-failure pattern for callers that want it (warnings silent in raise path). `WorkflowValidationWarning` base class is RESTORED (still hosts `WorkflowTimeoutShadowsNode`). Per Round 4 item #13, `WorkflowFromShapeWarning` is REMOVED; `from:` shape problems surface as HARD ERRORS (`WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`).

- **R3 #9 naming renames applied everywhere**: node types `mapper` → `map`, `state_set` → `set_state`, `state_delete` → `delete_state` (rename Pydantic model class names, dispatch entries, module files: `map.py`/`set_state.py`/`delete_state.py`); exception classes `WorkflowBindNotJinja` → `WorkflowBindMustBeJinja` (positive form), `WorkflowBranchDeadSide` → `WorkflowBranchMissingArm`, `WorkflowNodeRefUnknown` → `WorkflowJinjaRefUnknown` (disambiguates from `WorkflowNodeNotDefined`). KEEP `ref:` edge field name (polysemy with catalog "ref" accepted). KEEP `WorkflowMapperWorkerNotAddressable` (verbose but accurate; rename to map-worker would be vaguer).

- **`docs/workflows.md` is a Phase 3.0 deliverable (R3 #12)**: NOT deferred. Initial anchor coverage (~16 anchors): `#binds-rhs`, `#ref`, `#extends`, `#fan-in`, `#single-input`, `#node-types`, `#copy-of`, `#map`, `#branch`, `#authz-from`, `#timeout`, `#cancel`, `#trace-id`, `#error-format`, `#secrets`, `#chat-capsules`. Each anchor: 200-500 words explaining concept, syntax, examples, common mistakes. The docs file is created OUTSIDE this workflow (separate deliverable); the spec just commits to it. Step 7 reviewer verifies each `docs_anchor` referenced in error messages resolves to an actual anchor in `docs/workflows.md`.
- **Edge `ref:` field DROPPED (Round 4 item #11)**: edges are `{from, to, binds, branch, when}` only — no `ref:` list. Multi-input edges express fan-in directly (just add another edge). Ordering-only edges (no `binds:`) remain legal — they just sequence nodes. `state.*` remains for workflow-wide values. The old node-level `collect:` field is also REMOVED. Predecessor relationship for cross-graph `nodes.<X>.<field>` refs is computed from graph reachability (transitive ancestors) at validation time — not from any `ref:` declaration. SPECIAL CASE for `binds:` RHS: `nodes.<edge.from>.<field>` is always a valid source (binds: is literally for this case).
- Inferred workflow inputs (start node's schema) and outputs (dict keyed by terminal node names) per master spec §6.5, §6.6.
- `extends:` strict union; max depth 16 (cycles raise `WorkflowExtendsCycle`; depth overflow raises `WorkflowExtendsDepthExceeded` — split into two distinct exceptions).
- `copy_of:` (RENAMED from `instance_of:`) for in-workflow node copies with overrides. Source MUST be graph-reachable — no "template-only" status. YAML anchors handle pure config-sharing.
- `when:` (RENAMED from `condition:` modifier on EDGES). `condition:` STAYS as the primary config field on `branch` and `filter` node types. Distinction: `when:` on an edge gates traversal; `condition:` on a node is its computed predicate.
- Access syntax: ONE scope (`nodes.*`), ONE syntax (`{{ ... }}`). Used everywhere — `binds:` RHS, config strings, `when:`, `branch.condition`, `filter.condition`, `compute.value`, `mapper.over`. The legacy bare `source.field` shorthand on `binds:` RHS is DROPPED — RHS MUST be a Jinja expression wrapped in `{{ }}`. `nodes.<X>` is Jinja `Undefined` until X completes successfully (skipped/failed → undefined). Idiom for "may not have run": `{{ nodes.X is defined }}` or `{{ nodes.X.field | default(...) }}`.
- Node identity / reuse: every entry in `nodes:` is a single invocation site, producing at most one captured output per workflow run addressable as `nodes.<name>`. Reuse via `copy_of:` re-declaration OR callable invocation (`mapper.apply:`, `workflow.ref:`). Mapper workers are NOT addressable from outside the mapper (`WorkflowMapperWorkerNotAddressable`); only `nodes.<mapper>.results` works. Sub-workflow internals are opaque (only `nodes.<workflow_node>.<terminal>` is visible). Shared upstream consumed by two siblings (A → B, A → C; both read `nodes.A`) is canonical — A runs once. Multiple `state_set` writes to the same key are the canonical pattern for evolving values; semantics are last-write-wins in topological order. No collision exception, no lint warning — state is by design mutable across nodes.
- Pythonic exception hierarchy, NO error codes. Each class carries structured attributes; `__str__()` is STRICT single-line (no `\n` ever). Format: `<ClassName>: <file>:<line> <identifying-names>: <problem summary>. Fix: <one-line action>.`. Every YAML-origin error message contains the literal `"Fix: "` token (capital F, colon, space). Fix-path is PROSE — a single-line action sentence. The Fix portion is FORBIDDEN to contain: Jinja delimiters (`{{`, `}}`, `{%`, `%}`); YAML literal fragments showing syntax (e.g., `ref: [foo]`); backticks or fenced code spans; multi-line content. ALLOWED in the Fix portion: single-quoted identifiers naming workflow elements (`'fetch_a'`, `'merge'`), technical vocabulary as prose (`binds-edge`, `extends chain`, `Jinja delimiters`), and cross-references to docs (`see docs/workflows#anchor`). The Fix tells the author WHAT to do; the docs link carries the exact syntax. **R3 #8 two-channel addition**: every exception class declares `caller_message: ClassVar[str]` for non-engineer end users (chat capsule + JSON envelope); `str(exc)` remains engineer-targeted. NO `exc.format()` method. Multi-error reporting uses stdlib `ExceptionGroup[WorkflowValidationError]` (Python 3.11+; kolsch is on 3.13); NO custom `WorkflowValidationErrorGroup` class. Tests access sub-exceptions via `exc.exceptions`; the native `except* WorkflowBindMustBeJinja` syntax filters by sub-type. Validator collects per-layer findings into a `ValidationResult` (errors + warnings tuples); `validate_workflow_or_raise` raises `ExceptionGroup(...)` when N > 1 errors, or the single exception when N == 1.
- CLI `--format text|json` is a cross-cutting flag accepted by every new `kolsch workflows ...` command (and is the contract for any new command authored in Phase 3.0+). Default `text` preserves current human output unchanged. `json` produces a structured envelope `{command, status, data, errors}` per master spec §14.3, with per-error objects per master spec §14.4. A single shared helper `src/cli/_format.py:render(...)` is the ONLY place where text vs json branching happens.
- `secrets_needed=[...]` on `@node` — mirrors `@tool`. NO `permissions=` on the decorator.
- Three-segment ref `<bundle>/<asset_type>/<name>` is canonical for ALL asset types: tools, agents, workflows, nodes. The 2-segment form (`<bundle>/<name>`) is DEPRECATED across the board (no longer canonical for tools). The existing QoL doc `qol-prefix-defaults-and-unified-refs.md` is REVOKED / superseded. `parse_ref` is REMOVED from `src/assets/refs.py` and replaced by `parse_three_segment`. Three new utilities are the single public surface: `build_ref(bundle, asset_type, name) -> str` (the ONE canonical builder), `parse_three_segment(ref) -> (bundle, plural_segment, name)` (pure shape check), `is_three_segment(ref) -> bool` (cheap predicate). `_TYPE_TO_SEGMENT` gains `"workflow": "workflows"` and `"node": "nodes"`. `AssetCatalog.lookup(ref, expected_type=None)` wraps `parse_three_segment + dict get`. ALL inline f-string ref construction in `src/assets/catalog.py` scanners, `src/providers/authz.py` validators, etc. routes through `build_ref`. Existing 2-seg tool refs continue to work via a migration shim for authored rules; new authoring uses 3-seg.
- ALL `nodes.<X>.<field>` references in Jinja-evaluated string fields ARE pre-validated at the gate (Jinja AST walk + graph-reachability predecessor check + output-schema field check). The prior "NOT pre-validated" carve-out is DROPPED. Per Round 4 item #11: predecessor closure is computed from graph reachability (transitive ancestors), NOT from edge `ref:` declarations (which no longer exist). `binds:` RHS has a special case: `nodes.<edge.from>.<field>` is always allowed.
- Output shapes are flat per-type dicts per the table in master spec §4 — including the explicit `{value: bool}` for `branch` and `{}` for `state_delete` (fixing prior under-specs), plus `{value: <jinja-result>}` for `compute`.
- Permissions (per Round 4 item #7): `RuleBasedAuthorizationProvider.allow()` takes a single `layers: tuple[PermissionLayer, ...]` kwarg (replaces the per-layer kwargs). `PermissionLayer` is a `@dataclass(frozen=True, slots=True)` value object with fields `name: str` (`"workflow" | "agent" | "bundle" | "platform"`) and `rules: tuple[Rule, ...]`. The engine assembles the tuple in proximate-caller order: workflow first when the proximate caller is a workflow node firing the action; workflow layer OMITTED when an embedded agent fires (structural escalation-prevention guarantee). NO `WorkflowCatalogProtocol` is introduced; the provider keeps its concrete `AssetCatalog` dependency (library extraction dropped as YAGNI at v1). Rules gain optional `from:` 3-seg ref over the caller chain. **Round 4 item #13**: `from:` is STRICTLY validated at parse/validate time — must be 3-seg (else `WorkflowFromShapeError`); asset_type must be `agents`/`workflows` (else `WorkflowFromAssetTypeError`); target must exist in catalog (else `WorkflowFromNotFound`). The R3 #6 `WorkflowFromShapeWarning` is REMOVED. Empty chain + `from:` set → fail-closed (no match). Transitive — fires if ANY frame matches. `core/principals/{user,system}` is NOT added in v1; principal strings (`user.local`, `system`) stay as TELEMETRY only.
- Caller chain ordering: proximate first (`chain[0]` closest to tool call), distal last. Push frames on agent/workflow execution start (use `build_ref(bundle, "agent"|"workflow", name)`); pop on return/error. Engine bootstrap → `chain = ()`. Synthetic skill read during agent X → `chain = [<X-ref>]` (`principal="system"` is telemetry only).
- Workflow run status enum: `aborted` is now user-initiated cancellation per R3 #1 (NOT a reserved-for-future slot). New status `timed_out` added per R3 #2. Full status enum landscape: `pending`, `running`, `waiting_for_input`, `succeeded`, `failed`, `timed_out`, `aborted`.
- No `schema_version` field on events. Event envelope is FROZEN. Payload `*Data` models evolve ADDITIVELY (new optional fields with defaults allowed; rename / re-type / remove requires a migration plan).

---

## Phase-level Acceptance Criteria

These criteria must ALL be met when ALL 7 steps (plus prereq Step 0) are complete. Each step has its own narrower acceptance criteria listed within the step.

1. **Refs unification (3-seg canonical for ALL asset types)**: `src/assets/refs.py` exports `build_ref(bundle, asset_type, name) -> str`, `parse_three_segment(ref) -> (bundle, plural_segment, name)`, `is_three_segment(ref) -> bool`. The old `parse_ref` is REMOVED entirely (no compatibility alias). `_TYPE_TO_SEGMENT` gains `"workflow": "workflows"` and `"node": "nodes"`. `AssetCatalog.lookup(ref, expected_type=None) -> AssetEntry | None` wraps parse + dict get. ALL inline `f"{bundle}/{plural}/{name}"` constructions in `src/assets/catalog.py` scanners, `src/providers/authz.py:_validate_what` / `_build_action_to_tool_ref`, `src/workflows/catalog.py`, and `src/engine/observers.py:221` are replaced with `build_ref(...)` calls. Three-segment is canonical for tools, agents, workflows, AND nodes; 2-segment tool refs survive only via the authored-rule migration shim.

2. **Asset registration**: bundle scan picks up workflow YAMLs and Python node files; catalog entries with three-segment refs (`<bundle>/workflows/<name>`, `<bundle>/nodes/<name>`) built via `build_ref`; `bundle.yaml` `exclude:` suppresses files.

3. **YAML parser**: every workflow YAML in the fixture suite parses into a typed `Workflow` Pydantic model. Malformed YAML raises clear error with file path + line number. Unknown top-level keys raise `WorkflowValidationError`. **Round 6 J5/β — `binds:` RHS accepts YAML literals OR Jinja-marked strings**: bare YAML literals (strings, ints, floats, lists, bools, null) pass through as-is; only `"{{ ... }}"`-marked values trigger Jinja evaluation. The legacy `source.field` shorthand stays REMOVED. The `WorkflowBindMustBeJinja` exception class is REMOVED — bare RHS values no longer raise. Same rule applies to `set_state.keys` values, `compute.value`, `filter.condition`, `branch.condition`, `map.over`, and node-level + edge-level `when:`. Pydantic strict-mode validation against the receiver's `Inputs` model applies AFTER rendering (rendered literal values are still validated; `WorkflowBindTypeMismatch` fires at runtime if strict validation fails).

4. **`extends:` chain resolution**: recursive; cycles → `WorkflowExtendsCycle`; unresolved refs → `WorkflowExtendsUnresolved`; depth >16 → `WorkflowExtendsDepthExceeded` (split from cycle); duplicate node names → `WorkflowDuplicateNode`; duplicate edge `(from, to)` → `WorkflowDuplicateEdge`; diamond resolution idempotent.

5. **Run-start gate — structural** (master spec §7.1): edge endpoints, DAG, unambiguous start, reachability, unreachable nodes (`WorkflowUnreachableNode` — defined but no incoming edge from start and not a mapper `apply:` target), `copy_of:` source reachability, bind-key collision check across multiple edges to the same target (`WorkflowBindCollision` per Round 4 item #11). Multi-input edges ARE permitted — the prior single-input rule is DROPPED. Each raises its specific exception.

6. **Run-start gate — schema** (master spec §7.2): **Round 6 J5/β — only Jinja-marked values are walked**: a value is considered Jinja iff it is a `str` containing `"{{"`; pure YAML literals (non-string OR string without `"{{"`) skip Jinja parsing entirely. ALL `nodes.<X>.<field>` references in Jinja-marked config fields (binds, config, `when:`, `condition:`, `compute.value`, `map.over`, `set_state.keys` values, etc.) are pre-validated via Jinja AST walk through the sandboxed singleton (Round 6 S1 — `src/workflows/jinja_env.py`'s `JINJA_ENV`). Each ref must resolve to (a) a predecessor — defined as a graph-reachable transitive ancestor of the consuming node (per Round 4 item #11; computed from edge structure, NOT from any `ref:` declaration — edges no longer carry `ref:`); `binds:` RHS has an implicit pass for `edge.from`, and (b) a field declared in the source node's output schema. Failures raise `WorkflowJinjaRefUnknown` (with reason discriminator typed as `str` per Round 4 item #9 — value table at master spec §7.2: `not_predecessor` / `unknown_field` / `not_declared`). Validator check order: existence in `workflow.nodes` (`WorkflowNodeNotDefined`) → predecessor relationship via graph reachability (`not_predecessor`) → output schema known (`not_declared`) → field in schema (`unknown_field`). Mapper workers referenced from outside their mapper raise `WorkflowMapperWorkerNotAddressable`. Jinja-AST walk that triggers the sandbox raises `WorkflowJinjaSecurityError` (Round 6 S1).

7. **Run-start gate — branching** (master spec §7.3): edges with `when:` modifier; edges with `branch:` from branchable nodes only; both true/false required; non-branching nodes have no `branch:` on edges. (Note: `condition:` REMAINS as primary config on `branch` and `filter` node types — it is `branch.condition` / `filter.condition`, distinct from the edge-level `when:` modifier.)

8. **Run-start gate — type-specific** (master spec §7.4): mapper, `compute`, workflow node, branch node, filter checks. The 8 dropped aggregations (sum/count/min/max/sort/flatten/any/all) are NOT in the catalog — replaced by `compute`. The `python` discriminator is REMOVED — authored Python nodes are invoked via `type: <bundle>/nodes/<name>` (a `CatalogNodeDef` generic model in the parser per Round 4 item #3, hosted in `src/workflows/catalog_node.py` per Round 4 item #2), and validator introspection is keyed off the catalog entry, not a `body:`/`ref:` field.

9. **Run-start gate — extends + composition** (master spec §7.5): depth, strict union, ref resolution; cycle vs depth-exceeded are DISTINCT exceptions.

10. **Run-start gate — authored Python node module** (master spec §7.6): for each `CatalogNodeDef` (Round 4 item #3 renamed; `type: <bundle>/nodes/<name>`) referenced in `nodes:`, the validator resolves the catalog entry, imports, checks decoration, signature, return type (per output-shape table in master spec §4), and secrets exist.

11. **Run-start gate — authz** (master spec §7.7): permissions parse; refs resolve via `parse_three_segment` + `catalog.lookup`. **Round 4 item #13 — `from:` STRICT validation (HARD ERRORS)**: `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`. Invocation passes layer walk via `allow(action, target, *, layers: tuple[PermissionLayer, ...], caller_chain=...)` (Round 4 item #7 refactored signature).

12. **Validator accumulation**: within each L3 structural-independent layer, findings are collected into a list. If N == 1, the single specific exception is raised. If N > 1, a stdlib `ExceptionGroup[WorkflowValidationError]` (Python 3.11+; kolsch on 3.13) wraps all findings. NO custom `WorkflowValidationErrorGroup` class. Tests access sub-exceptions via `exc.exceptions`; `except* WorkflowBindNotJinja` syntax filters by sub-type. Across layers, FAIL FAST — but the failing exception's message includes a best-effort "Related issues" tail listing sibling-independent findings from later layers. Pretty-printer groups findings by location (stdlib ExceptionGroup formatting renders as a tree; no custom `__str__` on the group).

13. **`@node` decorator**: importable from `kolsch.workflows`. `secrets_needed: list[str] | None = None` only. Signature/return-type introspection works at validation time. Mirrors `@tool`.

14. **Exception hierarchy** (master spec §12.1): full hierarchy declared; structured attributes; `__str__()` is STRICT single line (no `\n` ever); no codes. Format `<ClassName>: <file>:<line> <identifying-names>: <problem summary>. Fix: <one-line action>.`. Every YAML-origin error message contains the literal `"Fix: "` token (capital F, colon, space). The Fix portion is STRICT-prose: NO `{{`, `{%`, backticks, or YAML literal fragments; single-quoted identifiers and prose vocabulary only; for exact syntax, reference a `docs/workflows#anchor`. Reviewer asserts `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)` for every YAML-origin error class. **R3 #8**: every exception class declares `caller_message: ClassVar[str]` (1-3 sentences, plain English, no class names, no file:line tokens, no Jinja syntax). **R3 #11**: every exception class declares `identifying_names_keys: ClassVar[tuple[str, ...]]` listing identifying-name fields (every listed key must be a real dataclass field). **R3 #9 renames**: `WorkflowBranchDeadSide` → `WorkflowBranchMissingArm`, `WorkflowNodeRefUnknown` → `WorkflowJinjaRefUnknown`. New + renamed classes present: `WorkflowJinjaRefUnknown` (with reason field typed as `str` per Round 4 item #9 — documented values `not_predecessor` / `unknown_field` / `not_declared`), `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef` — fires for ANY YAML field naming a non-existent node: `edge.from`/`edge.to`, `copy_of:` source, `map.apply:` target; the prior `edge.ref` trigger is GONE per Round 4 item #11), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable` (kept), `WorkflowBranchMissingArm`, `WorkflowExtendsCycle` (split), `WorkflowExtendsDepthExceeded` (split). **Round 4 item #11 multi-input redesign**: `WorkflowMultiInputForbidden` is REMOVED; ADDED: `WorkflowBindCollision` (parse-time — two edges to the same target binding the same key) and `WorkflowBindTypeMismatch` (runtime — rendered bind value fails receiver Pydantic strict validation). **Round 4 item #13 from: tightness**: ADDED THREE error classes (all parse/validate time): `WorkflowFromShapeError` (value is not a 3-seg ref), `WorkflowFromAssetTypeError` (asset_type segment is not `agents` or `workflows`), `WorkflowFromNotFound` (target doesn't exist in the catalog). REMOVED: `WorkflowFromShapeWarning` (R3 #6 warning is reversed — now a hard error). **R3 #2 timeout classes**: `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded` (runtime), `WorkflowInvalidTimeout` (validation error), `WorkflowTimeoutShadowsNode` (warning). **R3 #5 ref classes**: `WorkflowInvalidRef` (1-seg or malformed), `WorkflowBuiltinShadowed` (user authoring at reserved catalog slot). **R3 #6**: `WorkflowValidationWarning` base class RESTORED (still used for `WorkflowTimeoutShadowsNode`). `WorkflowStateKeyCollision` remains DELETED. **Round 6 NEW**: `WorkflowJinjaSecurityError` (S1 — sandboxed Jinja env blocked an unsafe attribute access or callable; raised by `src/workflows/jinja_env.py` wrapping `jinja2.exceptions.SecurityError`), `WorkflowExtendsPolicyForbidden` (S5 — extended workflow has non-empty `policy:` rules; `policy:` is NOT mergeable across `extends:`). **Round 6 REMOVED**: `WorkflowBindMustBeJinja` (J5/β — `binds:` now accepts YAML literals OR Jinja-marked strings; bare RHS values no longer raise; the legacy `source.field` shorthand stays REMOVED). Multi-error reporting uses stdlib `ExceptionGroup`; NO custom `WorkflowValidationErrorGroup` class.

15. **CLI — `kolsch workflows list`**: table output (text default) OR JSON envelope (`--format json`); exit 0.

16. **CLI — `kolsch workflows show <ref>`**: full structure + derived I/O (text default) OR JSON envelope (`--format json`); exit 0 on found, 1 on unknown.

17. **CLI — `kolsch workflows validate <ref>`**: runs gate without execution via `validate_workflow_or_raise`. Success exits 0; failure exits 1 with class name + attrs. Text default OR JSON envelope (`--format json`) where the envelope includes BOTH `errors` AND `warnings` arrays (per master spec §14.4), one object per sub-exception of any wrapping `ExceptionGroup`. Each error JSON object includes the per-class `caller_message` field (R3 #8). Per-class `identifying_names` extracted via the generic `src/cli/_format.py:render_exception(exc)` formatter using dataclass introspection + ClassVar lookup (R3 #11).

18. **CLI — `kolsch workflows run <ref>` stub**: "Workflow execution is not implemented in Phase 3.0. Ships in Phase 3.1." to stderr; exit 1. Text default OR JSON envelope (`--format json`) carrying a single-element `errors` array with `class: "NotImplemented"`.

18a. **CLI `--format text|json` convention**: every new `kolsch workflows ...` command accepts `--format text|json` at the group level OR the subcommand level (Click handles both). Default `text` keeps current human output unchanged. `json` emits the envelope `{command, status, data, errors}` per master spec §14.3 + §14.4. A single shared helper `src/cli/_format.py:render(...)` is the ONLY place where text vs json branching happens; commands MUST route all terminal output through it.

19. **New `workflow_permissions` authz layer + `layers` refactor (Round 4 item #7)**: `RuleBasedAuthorizationProvider.allow()` signature is refactored from per-layer keyword-only kwargs (`workflow_permissions=...`, `agent_permissions=...`, `bundle_permissions=...`, `platform_permissions=...`) to a single ordered tuple kwarg: `allow(action, target, *, layers: tuple[PermissionLayer, ...], caller_chain=())`. A new value object `@dataclass(frozen=True, slots=True) class PermissionLayer { name: str; rules: tuple[Rule, ...] }` is introduced; `name` is one of `"workflow" | "agent" | "bundle" | "platform"`. The engine constructs the `layers` tuple in proximate-caller order (workflow first when proximate frame is a workflow node; embedded agent fires → workflow layer OMITTED). NO `WorkflowCatalogProtocol` is introduced (per Round 4 item #7 — YAGNI dropped); the validator stays on concrete `AssetCatalog` because tests can construct `AssetCatalog` in-memory. Library extraction is dropped as non-goal at v1.

20. **`Rule.from_` field tightness (Round 4 item #13)**: optional 3-seg ref over caller chain. Strict parse/validate-time checks: must be a 3-seg ref (else `WorkflowFromShapeError`); the asset_type segment must be `agents` or `workflows` (else `WorkflowFromAssetTypeError`); the target must exist in the catalog (else `WorkflowFromNotFound`). `WorkflowFromShapeWarning` is REMOVED (the R3 #6 warning is reversed — these are hard errors now). Empty chain + `from:` set → fail-closed (no match at runtime). Transitive — fires if ANY frame matches. Matcher addition is the FIRST check inside `_rule_matches`.

21. **Three-segment refs (canonical for ALL asset types)**: `kolsch/nodes/<name>`, `kolsch/workflows/<name>`, `kolsch/agents/<name>`, `kolsch/tools/<name>` all validate against catalog via `parse_three_segment` + `catalog.lookup`; `node.<name>` / `tool.<name>` actions route through the action map built via `parse_three_segment`; two-segment authored tool rules (`kolsch/bash`) continue via migration shim unchanged.

22. **Fixture suite ~20 YAMLs**: every fixture parses/validates per declared expected outcome; covers every validator rule + every error class (including the new ones above) + the `from:` field shape + the refs unification (workflow node refs both ends).

23. **Starter bundle**: `bundles/kolsch/workflows/hello.yaml` + `bundles/kolsch/nodes/echo.py` both validate; visible via `kolsch workflows list`.

24. **No regressions**: full existing test suite passes (setup, agent run, chat unchanged).

25. **Cancel command (R3 #1)**: `kolsch workflows cancel <run_id>` writes a sidecar `cancel.flag` file into the run directory. Text default OR JSON envelope (`--format json`). Phase 3.0 ships the CLI + sidecar write contract; Phase 3.1 wires executor polling + orphan sweep. The cancellation protocol is documented in §10.x (sidecar location, polling discipline, orphan sweep on engine startup).

26. **Timeouts (R3 #2)**: top-level `Workflow.timeout_seconds: int | None = None` parses (positive int validation); per-node `timeout_seconds: int` modifier on common `BaseNodeDef` parses. Validator emits `WorkflowInvalidTimeout` (error, non-positive) and `WorkflowTimeoutShadowsNode` (warning, per-node > workflow). Runtime exceptions `WorkflowTimeoutExceeded` / `NodeTimeoutExceeded` declared. Status enum includes `timed_out`. Event kinds `workflow.timed_out` + `workflow.node_timed_out` declared.

27. **Authz `audit_level` config (R3 #3, refined by Round 4 item #9)**: Rule-based authz config Pydantic model gains `audit_level: str = "rule_matched"` (typed as `str`, NOT `Literal`; documented value table at master spec §13.5: `all` persists every authz decision, `rule_matched` persists only decisions where an explicit rule fired). Engine emits `authz.decision` event during workflow's tool-dispatch wrapper subject to setting (Phase 3.1 implementation; spec is for contract). Payload includes `{action, target, effect, rule_id, layer, caller_chain}`.

28. **3-seg-only refs (R3 #5)**: `resolve_shorthand` is REMOVED from `src/assets/refs.py`; `parse_three_segment` is the sole ref parser. Parser rejects non-3-seg `type:` values with `WorkflowInvalidRef`. Catalog detects user authoring at reserved built-in slot (e.g., `bundles/kolsch/nodes/bash.py`) → `WorkflowBuiltinShadowed` at scan time. `core` bundle reservation REMOVED. Bundle-name reservation (14 type names) REMOVED. Built-ins live at `kolsch/nodes/<name>` (3-seg).

29. **`description:` field (R3 #7)**: every node type accepts optional `description: str | None = None` (`min_length=1`; empty strings rejected) on common `BaseNodeDef`. All examples + fixtures renamed `summary:` → `description:`. Chat capsule rendering uses `description` when present; falls back to type-specific defaults.

30. **`caller_message` ClassVar on every exception (R3 #8)**: every `WorkflowError` subclass declares `caller_message: ClassVar[str]` non-empty. Style: 1-3 sentences; NO class names; NO file:line tokens; NO Jinja syntax; recommends an action. Reviewer asserts `"{{" not in caller_message and ":" not in caller_message_with_path_token and ClassName not in caller_message`. Rendered in chat capsule + `--format json` envelope.

31. **`identifying_names_keys` ClassVar (R3 #11)**: every exception class declares `identifying_names_keys: ClassVar[tuple[str, ...]]`; every listed key is a real dataclass field. `src/cli/_format.py:render_exception(exc)` is the single generic JSON formatter using dataclass introspection + ClassVar lookup. Per-class JSON shape contract-validated by reviewer (golden JSON files per exception class trigger; assert identifying_names keys match ClassVar declaration).

32. **Full ID model (R3 #10)**: `src/workflows/ids.py` (or refs module) declares `TraceId(PrefixedId)` with prefix `tr_`, `WorkflowRunId(PrefixedId)` with prefix `wrk_`, `InvocationId(PrefixedId)` with prefix `inv_`, `ToolCallId(PrefixedId)` with prefix `tc_` (was bare `str`). Documents UUID4 backing for global uniqueness of `EventId`. Workflow_run carries `trace_id` inherited from parent at creation (root run gets fresh `tr_`); fork inherits unchanged; sub-workflow inherits unchanged. `parent: {workflow_run_id, invocation_id}` (renamed from `run_id`). Workflow events carry `parent_event_id`. Map-spawned worker invocation events carry `parent_invocation_id` + `iteration_index: int` (0-based). Events `tool.called`, `workflow.node_started`, `model.request` carry persisted `caller_chain: list[str]` in payload.

33. **ValidationResult API (R3 #6)**: `validate_workflow(workflow, catalog) -> ValidationResult` is the structured API; `validate_workflow_or_raise(workflow, catalog) -> None` is the thin wrapper (warnings silent in raise path). `WorkflowValidationWarning` base class RESTORED (still hosts `WorkflowTimeoutShadowsNode`). Per Round 4 item #13, `WorkflowFromShapeWarning` is REMOVED; `from:` shape/type/existence problems now surface as HARD ERRORS (`WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`) in `result.errors`.

34. **`docs/workflows.md` deliverable (R3 #12)**: `docs/workflows.md` is a Phase 3.0 deliverable (NOT deferred). Initial anchor coverage (~16 anchors): `#binds-rhs`, `#ref`, `#extends`, `#fan-in`, `#single-input`, `#node-types`, `#copy-of`, `#map`, `#branch`, `#authz-from`, `#timeout`, `#cancel`, `#trace-id`, `#error-format`, `#secrets`, `#chat-capsules`. Each anchor: 200-500 words. Step 7 reviewer verifies each `docs_anchor` referenced in error messages resolves to an actual anchor in `docs/workflows.md`.

35. **`from_json` Jinja filter (Round 7 CV1)**: registered in `src/workflows/jinja_env.py` via a module-level `_register_builtin_filters()` helper (~5 LoC); the v1 core filter set (per master spec §2.7.3) is `from_json` plus stock Jinja2 filters; filters are CORE-ONLY (bundles, plugins, and pip-installable packages CANNOT register custom filters); contribution chokepoint for kolsch maintainers documented at `docs/explanation/jinja-extensions.md`; one fixture (`valid-from-json-filter.yaml`) demonstrates end-to-end use.

36. **`display_label()` public helper (Round 7 L7-8)**: `kolsch.workflows.display_label(node, type_label=None) -> str` implements the master-spec §15.4 3-step fallback (per-instance `description:` → type-level `label:` → cleaned class/function name); ships in `src/workflows/labels.py`; listed in `__all__`; the single source of truth — chat capsules, CLI tables (`kolsch workflows show`, `kolsch nodes list`), error-message label formatters all call this helper.

37. **`NodeContext` public test seams + `for_test()` + `state` field (Round 7 L7-3 + A7-1 + A7-6)**: `NodeContext.bundle` and `NodeContext.emit_fn` are PUBLIC fields (underscore prefixes from R5/R6 dropped per L7-3 — both are documented public test seams); `NodeContext.state: Mapping[str, Any]` field (A7-6) is wrapped in `MappingProxyType` by the engine and read-only from inside @node (`ctx.state["foo"]`); `NodeContext.for_test(**overrides)` classmethod (A7-1) provides sensible defaults for all 7 fields and is listed in `__all__`. `NodeContext.emit()` internally calls `self.emit_fn(self.bundle, subkind, payload)` (or similar wrap — engine's bound wrapper does the namespacing).

38. **Symmetric asset-discovery CLI (Round 7 L7-12)**: `kolsch nodes list [--bundle <name>] [--format text|json]` and `kolsch nodes show <ref> [--format text|json]` ship symmetric with `kolsch workflows list`/`show`; master spec §14.1 commands list expanded; §14.3 per-command data schemas include `nodes list → {nodes: [{ref, kind, label, secrets_needed}, ...]}` and `nodes show <ref> → {node: {...full structure...}}`; cross-asset rationalization for `agents`/`tools`/`skills`/`prompts` queued at `process/qol-asset-cli-symmetric.md`.

39. **Testing strategy reword (Round 7 A7-3)**: master spec §19.3 reads "Capture full event streams; assert semantic equality modulo volatile fields (timestamps, all *Id prefix-typed fields)" (was: "byte-equality on replay"). Phase 3.1's executor designer chooses the normalization mechanism. No canonicalization tool committed in Phase 3.0; no fixture asserts byte-equality of event JSON.

---

## Phase-level Integration Tests

Run AFTER all 7 steps are complete to verify the phase is shippable. Per-step tests live within each step.

### Phase Test 1: Full fixture suite green
- **Run:** `podman-compose exec dev uv run pytest tests/golden/workflows_test.py -v`
- **Expect:** all ~22-30 fixtures pass per their declared expected outcomes. Coverage includes every new/renamed exception class (`WorkflowJinjaRefUnknown`, `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, `WorkflowInvalidRef`, `WorkflowBuiltinShadowed`, `WorkflowInvalidTimeout`, `WorkflowBranchMissingArm`, **`WorkflowJinjaSecurityError` (Round 6 S1)**, **`WorkflowExtendsPolicyForbidden` (Round 6 S5)**), the multi-error-accumulation case (a single workflow surfacing N independent failures in one report), the `from:` field shape warning (via `result.warnings`), the refs-unification (workflow + node refs both ends), the timeout fixtures (valid + invalid + shadowing warning), the cancel command, the 1-seg rejection, and the **Round 6 J5/β literals-and-Jinja fixture** (`valid-binds-literals-and-jinja.yaml` — `WorkflowBindMustBeJinja` is no longer triggered).
- **Fail means:** at least one validator rule is broken or a fixture is mis-annotated.

### Phase Test 2: Every component test passes
- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_*.py -v`
- **Expect:** all workflow component tests pass.
- **Fail means:** subsystem-level regression in parser, validator, catalog, authz, or CLI.

### Phase Test 3: Starter bundle visible end-to-end
- **Run:** in a fresh workspace, `podman-compose exec dev uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive && podman-compose exec dev uv run kolsch --root /tmp/kolsch-fresh workflows list`
- **Expect:** output includes `kolsch/workflows/hello`. Followed by `kolsch workflows show kolsch/workflows/hello` showing structure + I/O. Followed by `kolsch workflows validate kolsch/workflows/hello` returning `valid`. Followed by `kolsch workflows run kolsch/workflows/hello` returning the Phase 3.1 stub message.
- **Fail means:** end-to-end discoverability broken.

### Phase Test 4: No regressions
- **Run:** `podman-compose exec dev uv run pytest tests/ -q`
- **Expect:** full suite green.
- **Fail means:** workflow-layer additions broke an existing subsystem.

---

## Coordinator Prompt

> You are the strategic decision-maker for this task. You do not write or review code.
> Your job is to drive the 7-step Coder/Reviewer flow, make calls when there is ambiguity
> or disagreement, and unblock the work when it stalls. You make binding decisions when
> the master design spec has an answer; you escalate scope creep.

**Your task:**

Ship Phase 3.0 of the kolsch workflow system through 7 sequential Coder/Reviewer cycles. This is the FOUNDATION layer of an 11-phase workflow system. Its purpose is architecture validation — if the YAML schema or validator rules are wrong, we discover it here without writing an executor.

**Authoritative references:**

- THIS spec: `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md` — the multi-step plan you're driving. Contains the 7 Step sections.
- Master design spec: `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md` — 21 sections of locked decisions. Cite by section number when making calls.

**The 7 steps (preceded by prereq Step 0):**

| Step | Focus | Effort |
|---|---|---|
| **Step 0** | **PREREQ** — Migrate kolsch to ruamel.yaml via `src/_yaml.py` helper module (9 src/ call sites); replace `pyyaml` + `types-pyyaml` with `ruamel.yaml` + `ruamel.yaml.clib` in `pyproject.toml`; refresh test goldens (Norway-bug fix may shift outputs) | ~1-2 days |
| **Step 1** | Scaffolding: package, full exception hierarchy (RESTORED `WorkflowValidationWarning`; new `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded`, `WorkflowInvalidTimeout`, `WorkflowInvalidRef`, `WorkflowBuiltinShadowed`; R3 #9 renames `WorkflowBranchMissingArm`/`WorkflowJinjaRefUnknown`; **Round 4 NEW: `WorkflowBindCollision` + `WorkflowBindTypeMismatch` (item #11); `WorkflowFromShapeError` + `WorkflowFromAssetTypeError` + `WorkflowFromNotFound` (item #13)**; **Round 6 NEW: `WorkflowJinjaSecurityError` (S1); `WorkflowExtendsPolicyForbidden` (S5)**; **REMOVED: `WorkflowMultiInputForbidden` (item #11); `WorkflowFromShapeWarning` (item #13); `WorkflowBindMustBeJinja` (Round 6 J5/β — binds: now accepts YAML literals OR Jinja-marked strings)**); every exception class has `caller_message: ClassVar[str]` + `identifying_names_keys: ClassVar[tuple[str, ...]]`; typed prefixed IDs (`TraceId`/`WorkflowRunId`/`InvocationId`/`ToolCallId`); UUID4 invariant for `EventId`; frozen+slotted `NodeContext` with `secrets: Mapping[str, str]` + **`emit` as a typed method backed by private `_emit_fn` (Round 4 item #10)** + **`_bundle: str` field + public `ctx.bundle` property (Round 6 S4)**; **new `src/workflows/jinja_env.py` module (Round 6 S1) hosting the sandboxed Jinja singleton + `WorkflowJinjaSecurityError`**; `@node` decorator + refs unification (`build_ref`, `parse_three_segment`, `is_three_segment`, `AssetCatalog.lookup`; `parse_ref` REMOVED — no compatibility alias; `resolve_shorthand` REMOVED); **explicit `__all__` in `src/workflows/__init__.py` + ruff parity rule (Round 4 item #5)** | ~1 day |
| **Step 2** | YAML parser: Pydantic models for 14 RENAMED built-in node types (`map`/`set_state`/`delete_state` per R3 #9) + generic `CatalogNodeDef` (Round 4 item #3 renamed from `CatalogNodeRef`) for authored Python nodes (`type: <bundle>/nodes/<name>`) hosted in standalone `src/workflows/catalog_node.py` (Round 4 item #2) + `Workflow.timeout_seconds` (top-level) + `Workflow` shape + `copy_of:` + `EdgeDef` shape `{from, to, binds, branch, when}` per Round 4 item #11 (edge `ref:` field DROPPED; multi-input edges now LEGAL) + `when:` edge modifier + Jinja-only `binds:` + common `description: str \| None` + common `timeout_seconds: int \| None` modifier + parser rejects non-3-seg `type:` (`WorkflowInvalidRef`) + ruamel.yaml load via `src/_yaml.py` | ~1 day |
| **Step 3** | Catalog integration: auto-discovery of `workflows/*.yaml` + `nodes/*.py` using `build_ref` from day one; virtual `kolsch` bundle exposes 14 built-in nodes at `kolsch/nodes/<name>`; catalog detects user authoring at reserved built-in slot → `WorkflowBuiltinShadowed`; node files migrated to renamed names (`map.py`/`set_state.py`/`delete_state.py`) | ~1 day |
| **Step 4** | Authz integration: `allow()` refactored to `layers: tuple[PermissionLayer, ...]` (Round 4 item #7 — per-layer kwargs REMOVED; `PermissionLayer` value object introduced; NO `WorkflowCatalogProtocol`) + three-segment refs (canonical for all types) via `parse_three_segment` + `Rule.from_` STRICT validation (Round 4 item #13 — `WorkflowFromShapeError`/`WorkflowFromAssetTypeError`/`WorkflowFromNotFound` as HARD ERRORS; `WorkflowFromShapeWarning` REMOVED) + `caller_chain` kwarg + `audit_level: str = "rule_matched"` (Round 4 items #9 + #12 — typed as `str`; default `rule_matched`) + `authz.decision` event contract | ~0.75 day |
| **Step 5** | Run-start validator: `validate_workflow -> ValidationResult` + thin `validate_workflow_or_raise` wrapper; every check in master spec §7.1–§7.7 + Jinja-AST `nodes.*` pre-validation via **graph-reachability predecessor closure** (Round 4 item #11 — replaces edge-`ref:`-based check; edge `ref:` field is GONE) + accumulation via stdlib `ExceptionGroup` + `WorkflowNodeNotDefined` for unresolved YAML node-name refs + timeout validation (`WorkflowInvalidTimeout` error, `WorkflowTimeoutShadowsNode` warning) + **bind-key collision check (`WorkflowBindCollision` per Round 4 item #11)** + **`from:` STRICT HARD ERRORS (`WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` per Round 4 item #13)** + R3 #9 renamed classes used throughout. NO `WorkflowMultiInputForbidden` (Round 4 item #11 REMOVED). NO `WorkflowFromShapeWarning` (Round 4 item #13 REMOVED). | ~2-3 days |
| **Step 6** | CLI: `list`, `show`, `validate`, `cancel` (writes sidecar `cancel.flag`), `run`-stub. `validate` uses `validate_workflow_or_raise`; `--format json` envelope includes BOTH `errors` AND `warnings` arrays + per-error `caller_message`; `src/cli/_format.py:render_exception(exc)` is the single generic JSON formatter using dataclass introspection + ClassVar lookup | ~1 day |
| **Step 7** | Fixture suite (~22-30 YAMLs) + starter bundle (`hello.yaml`, `echo.py`). ALL YAMLs use full 3-seg `type:` refs (no 1-seg shorthand); `description:` (renamed from `summary:`); RENAMED node types (`map`/`set_state`/`delete_state`); cancel command tests; timeout fixtures (valid + invalid + shadowing warning); `invalid-1seg-rejected.yaml`; reviewer verifies each `docs_anchor` referenced in error messages resolves to an actual anchor in `docs/workflows.md` | ~2 days |

**Flow per step:**

1. Hand the Coder the section titled **"Step N — Coder Prompt"** from this spec verbatim. Include the spec file path: `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`. Coder reads it + the master spec sections it references.
2. Coder implements + replies "Step N Complete — [summary]", "Blocked — ...", or "Decision needed — ...".
3. On Complete: hand the Reviewer the section titled **"Step N — Reviewer Prompt"** verbatim. Reviewer verifies + replies "Step N Approved" or "Changes requested — ...".
4. On Approved: advance to Step N+1. On Changes requested: send the change list back to the Coder.
5. After Step 7 is Approved: run the **Phase-level Integration Tests** above. If all pass, mark this spec Status: Done and annotate master spec `phase3.0-workflow-spec.md` §18.2's Phase 3.0 line with shipping commit SHA.

Note: Step 0 (ruamel.yaml migration) is a PREREQ — it must be Approved before Step 1 begins, because Step 2's parser imports the new `src/_yaml.py` helper.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision → REJECT, cite the master spec section, restate the decision.
- Coder finds master-spec ambiguity → consult master spec, make a binding call grounded in the spirit of §2 (Locked Foundation Decisions), append the call as a note to THIS spec doc, re-brief.
- Coder discovers master spec is internally inconsistent → flag as spec bug, choose the resolution keeping Phase 3.0 the smallest viable slice, document in a note, re-brief.
- Coder and Reviewer disagree on whether a step's deliverables are complete → require Reviewer to enumerate missing items; if real, send back to Coder; if Coder believes coverage is complete, ask Reviewer to cite the specific acceptance criterion or master-spec section being violated.
- Scope creep (new subsystem touched, executor logic sneaking in) → REJECT and pull back to listed scope.

**Handoff triggers:**

| Situation | Action |
|---|---|
| Step N: Coder signals "Complete" | Hand Step N Reviewer Prompt to Reviewer |
| Step N: Coder signals "Blocked — [blocker]" | Resolve or make the call (consult master spec), re-brief Coder |
| Step N: Coder signals "Decision needed — [options]" | Consult master spec; make the call; re-brief Coder |
| Step N: Reviewer signals "Approved" | Advance to Step N+1; hand its Coder Prompt to Coder |
| Step N: Reviewer signals "Changes requested" | Send change list to Coder; remain on Step N |
| All 7 steps Approved | Run Phase-level Integration Tests; on green, mark spec Done |

**Done when:** All 7 steps Approved by Reviewer AND all 4 Phase-level Integration Tests pass AND master spec §18.2 Phase 3.0 line annotated.

---

# STEP 0 — ruamel.yaml migration (PREREQ)

**Goal**: replace `PyYAML` with `ruamel.yaml` across the kolsch src/ codebase via a new `src/_yaml.py` helper module. This is the structural prereq for Step 2's parser, which needs line-info-aware YAML loading and round-trip-safe handling (Norway-bug fix). Migration is mechanical — every existing `yaml.safe_load` / `yaml.safe_dump` call routes through the helper.

**Files created/modified:**
- `src/_yaml.py` (new) — exports `load(text) -> Any` and `dump(data) -> str` using `ruamel.yaml.YAML(typ='safe')`. Single source of truth for YAML I/O across the codebase.
- 9 src/ files migrated from `import yaml` to `from _yaml import load, dump` (or module path equivalent):
  - `src/templating/inheritance.py`
  - `src/cli/_setup.py`
  - `src/cli/skills.py`
  - `src/cli/llm_commands.py`
  - `src/cli/permissions.py`
  - `src/cli/chat.py`
  - `src/assets/manifest.py`
  - `src/assets/assembler.py`
  - `src/assets/platform_config.py`
  - `src/assets/skill_metadata.py`
- `pyproject.toml` (modified) — remove `pyyaml` and `types-pyyaml`; add `ruamel.yaml` and `ruamel.yaml.clib`.
- Test goldens refreshed where YAML formatting shifted (Norway-bug fix may rewrite specific values that PyYAML previously coerced).

**Effort:** ~1-2 days.

## Step 0 — Coder Prompt

> You are implementing the PREREQ Step 0 of the phase. Read everything below. Do not ask for
> context that is already here. When done, reply "Step 0 Complete — [summary]",
> "Blocked — [specific blocker]", or "Decision needed — [options + tradeoff]".

**Your task:**

Migrate kolsch from `PyYAML` to `ruamel.yaml` via a new central helper module `src/_yaml.py`. This unblocks Step 2's parser (which uses ruamel's line-info-aware loader) and fixes the Norway bug (PyYAML's implicit-bool coercion of unquoted `no`/`yes`/`on`/`off`). All 10 existing src/ call sites are migrated to import from the helper; the test suite stays green; goldens are refreshed where formatting shifted.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — THIS spec. Step 0 prereq context.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — master design spec §12.3 (mention of ruamel.yaml).
3. **ruamel.yaml docs** — `YAML(typ='safe')` is the closest analog to `yaml.safe_load` / `yaml.safe_dump`.

**What to implement:**

1. **`src/_yaml.py`** — single helper module exporting two functions:
   ```python
   from io import StringIO
   from typing import Any
   from ruamel.yaml import YAML

   _yaml = YAML(typ='safe')
   _yaml.default_flow_style = False

   def load(text: str) -> Any:
       return _yaml.load(text)

   def dump(data: Any) -> str:
       buf = StringIO()
       _yaml.dump(data, buf)
       return buf.getvalue()
   ```
   - `typ='safe'` mirrors PyYAML's safe loader (no arbitrary Python object construction).
   - `default_flow_style = False` keeps block-style output (matches PyYAML's default).
   - Single private module-level `YAML` instance is cheap and thread-safe-enough for our use.

2. **Migrate 9 src/ files** (one PR-or-commit per file, or a single batch — either works for Step 0):
   - Replace `import yaml` with `from _yaml import load, dump` (use the appropriate relative or absolute import path for the file's package).
   - Replace `yaml.safe_load(text)` with `load(text)`.
   - Replace `yaml.safe_dump(data)` with `dump(data)`.
   - Replace `yaml.safe_dump(data, stream)` patterns with `stream.write(dump(data))` (or refactor the call site to use the string-returning form).
   - Files: `src/templating/inheritance.py`, `src/cli/_setup.py`, `src/cli/skills.py`, `src/cli/llm_commands.py`, `src/cli/permissions.py`, `src/cli/chat.py`, `src/assets/manifest.py`, `src/assets/assembler.py`, `src/assets/platform_config.py`, `src/assets/skill_metadata.py`.

3. **`pyproject.toml`** modifications:
   - Remove `pyyaml` from dependencies.
   - Remove `types-pyyaml` from dev/optional dependencies (if present).
   - Add `ruamel.yaml` to dependencies.
   - Add `ruamel.yaml.clib` to dependencies (C extension for performance).

4. **Test golden refresh**: after migration, run `podman-compose exec dev uv run pytest tests/ -q`. Failures likely concentrate in golden tests where YAML round-trip output shifted (e.g., a `no:` value PyYAML previously read as `False` is now read as the string `"no"` — the Norway-bug fix). Refresh affected goldens via the project's standard update mechanism. Document any test that needed a semantic change (not just whitespace).

**Step 0 Acceptance Criteria:**

0.1. `src/_yaml.py` exists and exports `load` + `dump`. Both functions are documented (one-line docstring each).
0.2. `grep -rn 'import yaml' src/` returns ZERO matches OUTSIDE `src/_yaml.py`. All other `import yaml` lines have been replaced.
0.3. `grep -rn 'yaml.safe_load\|yaml.safe_dump' src/` returns ZERO matches OUTSIDE `src/_yaml.py`.
0.4. `pyproject.toml` does NOT list `pyyaml` or `types-pyyaml`; DOES list `ruamel.yaml` and `ruamel.yaml.clib`.
0.5. Full test suite green: `podman-compose exec dev uv run pytest tests/ -q`. Any golden refreshes are committed with a clear "ruamel.yaml migration — golden refresh" message.
0.6. CLI smoke test: `podman-compose exec dev uv run kolsch --root /tmp/kolsch-yaml-smoke setup --non-interactive` succeeds; `kolsch agents list` returns expected output.

**Step 0 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/ -q`
- **Expect:** all tests pass (after golden refresh for the Norway-bug shifts).
- **Fail means:** a call site was missed; ruamel.yaml's typ='safe' doesn't actually mirror PyYAML's safe loader on some edge case (investigate; document; resolve).

**Self-check before handoff:**

- [ ] All 6 Step 0 acceptance criteria met
- [ ] `src/_yaml.py` is the ONLY src/ file importing `yaml` / `ruamel.yaml`
- [ ] All 10 migration targets in scope have been touched (check each off explicitly)
- [ ] Tests green; goldens refreshed where needed; refresh diffs reviewed for semantic correctness (not just whitespace)
- [ ] CLI smoke test passes
- [ ] `pyproject.toml` is clean (no dead PyYAML entries)

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]". Otherwise → "Blocked" / "Decision needed".

## Step 0 — Reviewer Prompt

> You are reviewing Step 0 (PREREQ). Reply "Step 0 Approved" or "Changes requested — [list]".

**What was built in Step 0:** central `src/_yaml.py` helper; 9 src/ files migrated from PyYAML to ruamel.yaml; pyproject.toml dependency swap; test goldens refreshed.

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — re-read Step 0.
2. **ruamel.yaml docs** — confirm `YAML(typ='safe')` is the safe-loader equivalent.

**Acceptance criteria to verify (Step 0):** Verify each of 0.1–0.6.

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/ -q` — full suite
- Manual smoke: fresh workspace `kolsch setup` + `kolsch agents list`

**Review checklist:**
- [ ] All 6 Step 0 acceptance criteria demonstrably met
- [ ] `grep -rn 'import yaml' src/` returns ONE line (in `src/_yaml.py`) — no other import survivors
- [ ] `grep -rn 'yaml\.safe_' src/` returns ZERO matches outside `src/_yaml.py`
- [ ] `pyproject.toml` swap clean: no `pyyaml`/`types-pyyaml`; `ruamel.yaml` + `ruamel.yaml.clib` present
- [ ] Test goldens refreshed for Norway-bug shifts; refresh diffs are semantic-clean (only-whitespace OK; semantic-change diffs flagged + explained)
- [ ] CLI smoke test passes (setup + at least one read path)
- [ ] No regressions

**Handoff trigger:** All boxes → "Step 0 Approved". Step 1 begins. Failures → "Changes requested — [list]".

---

# STEP 1 — Foundation scaffolding + refs unification

**Goal**: stand up the `src/workflows/` package skeleton — exception hierarchy, `NodeContext` dataclass, `@node` decorator — AND ship the refs unification utility shared across agents/tools/workflows. Nothing parses, nothing validates yet. This step proves the public API surface compiles and imports cleanly, and lands the single source of truth for three-segment ref construction, parsing, and lookup that all later steps depend on.

**Files created/modified:**
- `src/workflows/__init__.py` (new) — public API re-exports
- `src/workflows/errors.py` (new) — full exception hierarchy per master spec §12.1
- `src/workflows/context.py` (new) — `NodeContext` dataclass per master spec §5.5 (Round 5 R1 / Round 6 R6-R1: `emit(subkind, payload)` signature + documented engine-side auto-prepend of `node.<bundle>.` — the "author" segment was DROPPED per R6-R1; Round 6 S4 + Round 7 L7-3: gains public `bundle: str` field set by engine + public `emit_fn` test seam — both renamed from underscore-prefixed per L7-3; Round 7 A7-1: gains `for_test(**overrides)` classmethod; Round 7 A7-6: gains `state: Mapping[str, Any]` field wrapped in `MappingProxyType`)
- `src/workflows/decorator.py` (new) — `@node` decorator per master spec §5.2
- `src/workflows/jinja_env.py` (new — Round 6 S1) — singleton sandboxed Jinja environment. Hosts a `ImmutableSandboxedEnvironment` subclass that preserves native-type returns (via `jinja2.nativetypes.native_concat` override or `_parse` override). The single source of truth for ALL Jinja evaluation in the workflows layer — replaces ad-hoc `NativeEnvironment()` constructions. Raises `WorkflowJinjaSecurityError` when the sandbox blocks an unsafe attribute access or callable. **Round 7 CV1** — module-level `_register_builtin_filters()` registers the v1 core filter `from_json` (~5 LoC); filter set is kolsch's responsibility (core-only — bundles, plugins, packages cannot add filters); contribution chokepoint documented at `docs/explanation/jinja-extensions.md`.
- `src/workflows/labels.py` (new — Round 7 L7-8) — `display_label(node: NodeDef, type_label: str | None = None) -> str` public helper implementing the 3-step fallback per master spec §15.4: per-instance `description:` → type-level `label:` → cleaned class/function name. Re-exported from `src/workflows/__init__.py` and listed in `__all__`. The single source of truth for human-readable node labels (chat capsules, CLI tables, error messages).
- `src/assets/refs.py` (modified) — add `build_ref`, `parse_three_segment`, `is_three_segment`; extend `_TYPE_TO_SEGMENT` with `"workflow": "workflows"` and `"node": "nodes"`
- `src/assets/catalog.py` (modified) — Round 5 H3: add `AssetCatalog.lookup(ref, expected_type=None)` as the documented chokepoint; refactor existing `_scan_*` functions to call `build_ref` (drop inline f-strings); add planned custom ruff lint rule documentation (flags `catalog.assets[` / `catalog._<private>[` outside this file)
- `src/assets/asset_manager.py` (modified, line 188) — Round 5 H3: migrate from direct `catalog.assets[...]` subscript to `catalog.lookup(ref, expected_type=...)`. Reference call site demonstrating the chokepoint discipline.
- `src/providers/authz.py` (modified) — refactor `_validate_what` and `_build_action_to_tool_ref` to route through `parse_three_segment` + `catalog.lookup` (no inline ref construction). NOTE: filename + class names (`RuleBasedAuthorizationProvider`, `PermissionLayer`) remain UNCHANGED at Phase 3.0 per Round 5 H1; the user-facing vocabulary rename to "policy" is YAML-field + event-kind only, with the code-side rename queued at `process/qol-policy-rename.md`.
- `src/engine/observers.py` (modified, line ~221) — replace inline ref construction with `build_ref(bundle, "agent", name)` for chain frame push

**Effort:** ~0.75 day.

## Step 1 — Coder Prompt

> You are implementing Step 1 of a 7-step phase. Read everything below. Do not ask for
> context that is already here. When done, reply "Step 1 Complete — [summary]",
> "Blocked — [specific blocker]", or "Decision needed — [options + tradeoff]".

**Your task:**

Stand up the `src/workflows/` package skeleton (exception hierarchy, `NodeContext` dataclass, `@node` decorator) AND land the refs unification utility shared across agents, tools, and workflows. Nothing in this step parses YAML or validates anything — that comes in later steps. Goal: prove the public API surface compiles and imports cleanly, and that there is ONE canonical builder, ONE shape-parser, ONE catalog lookup for three-segment refs that every later step uses.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — THIS spec. Read the Background, Scope, Technical Context, and Phase-level Acceptance Criteria sections for context. Then read this Step 1 section end-to-end.

2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — master design spec. For Step 1 you need:
   - **§5.2 Decorator surface** — the `@node` signature
   - **§5.5 `NodeContext` surface** — the dataclass fields
   - **§12.1 Errors hierarchy** — every exception class to declare
   - **§12.2 Conventions** — structured attributes; single-line `__str__()`; no error codes
   - **§13.1 Node refs** — the three-segment shape (informs nothing in Step 1 directly but you'll need to know it later)

3. **`src/tools/decorator.py`** — the `@tool` decorator pattern. `@node` mirrors it. Read end-to-end before writing the decorator.

**What to implement:**

1. **`src/workflows/errors.py`** — full exception hierarchy per master spec §12.1 (with R3 #9 renames applied):
   ```
   WorkflowError                         (base)
   ├── WorkflowValidationError
   │   ├── WorkflowDuplicateNode
   │   ├── WorkflowDuplicateEdge
   │   ├── WorkflowExtendsCycle               # cycle in extends chain
   │   ├── WorkflowExtendsDepthExceeded       # split from cycle; chain > 16 deep
   │   ├── WorkflowExtendsUnresolved
   │   ├── WorkflowNodeNotDefined             # RENAMED from WorkflowUnknownNodeRef — fires for ANY YAML field naming a non-existent node (edge.from/edge.to, copy_of source, map.apply target). NOTE (Round 4 item #11): the edge.ref trigger is GONE — the field is dropped.
   │   ├── WorkflowAmbiguousStart
   │   ├── WorkflowNoStartNode
   │   ├── WorkflowUnreachableNode            # defined but no incoming edge & not a map apply: target
   │   │                                       # NOTE (Round 6 J5/β): WorkflowBindMustBeJinja is REMOVED — binds: RHS now accepts YAML literals OR Jinja-marked strings; only "{{ ... }}" triggers Jinja evaluation, so a bare literal is no longer an error
   │   ├── WorkflowBindCollision              # Round 4 item #11 NEW — two edges to the same target binding the same key (parse time)
   │   ├── WorkflowBindTypeMismatch           # Round 4 item #11 — rendered bind value fails Pydantic strict validation against receiver's input model (runtime, node-fire time)
   │   ├── WorkflowJinjaRefUnknown            # R3 #9 RENAMED from WorkflowNodeRefUnknown — Jinja nodes.X.Y validation; carries reason: str (typed as str per Round 4 item #9; documented values: not_predecessor / unknown_field / not_declared); DISTINCT from WorkflowNodeNotDefined (YAML node-name refs)
   │   ├── WorkflowMapperWorkerNotAddressable # KEPT verbose name (R3 #9 — alternatives vaguer); outside-map reference to a map worker
   │   ├── WorkflowBranchOnNonBranchable
   │   ├── WorkflowBranchMissingArm           # R3 #9 RENAMED from WorkflowBranchDeadSide (clearer)
   │   ├── WorkflowMapperWorkerMissingField
   │   ├── WorkflowCallCycle
   │   ├── WorkflowNodeImportFailed
   │   ├── WorkflowNodeMissingRun
   │   ├── WorkflowNodeNotDecorated
   │   ├── WorkflowNodeSignatureInvalid
   │   ├── WorkflowNodeReturnTypeInvalid
   │   ├── WorkflowInvalidTimeout             # R3 #2 NEW — non-positive timeout_seconds
   │   ├── WorkflowInvalidRef                 # R3 #5 NEW — non-3-seg type: value or malformed ref
   │   ├── WorkflowBuiltinShadowed            # R3 #5 NEW — user authoring at reserved catalog slot (e.g., bundles/kolsch/nodes/bash.py)
   │   ├── WorkflowFromShapeError             # Round 4 item #13 NEW — from: value is not a 3-seg ref
   │   ├── WorkflowFromAssetTypeError         # Round 4 item #13 NEW — from: ref's asset_type segment is not "agents" or "workflows"
   │   ├── WorkflowFromNotFound               # Round 4 item #13 NEW — from: ref's target doesn't exist in the catalog
   │   ├── WorkflowJinjaSecurityError         # Round 6 S1 NEW — sandboxed Jinja env blocked an unsafe attribute access or callable (e.g., mro, __subclasses__, lipsum.__globals__, cycler, joiner escape payloads). Raised at Jinja-evaluation time inside src/workflows/jinja_env.py wrapping jinja2.exceptions.SecurityError.
   │   └── WorkflowExtendsPolicyForbidden     # Round 6 S5 NEW — a workflow with extends: refs cannot inherit policy: rules; if any extended workflow declares policy: rules, the validator raises this HARD error. policy: applies only when the extended workflow is invoked DIRECTLY.
   ├── WorkflowValidationWarning              # R3 #6 RESTORED base class (still hosts WorkflowTimeoutShadowsNode)
   │   └── WorkflowTimeoutShadowsNode         # R3 #2 NEW — per-node timeout > workflow timeout
   │                                          # NOTE (Round 4 item #13): WorkflowFromShapeWarning is REMOVED — from: shape problems are now hard errors (see WorkflowFromShapeError / WorkflowFromAssetTypeError / WorkflowFromNotFound above)
   │                                          # NOTE (Round 4 item #11): WorkflowMultiInputForbidden is REMOVED — multi-input edges are the v1 fan-in mechanism
   └── WorkflowRuntimeError
       ├── WorkflowNotRunnable
       ├── WorkflowInputInvalid
       ├── WorkflowNodeFailed
       ├── WorkflowSecretUnresolved
       ├── WorkflowFileAccessDenied
       ├── WorkflowTimeoutExceeded            # R3 #2 NEW — workflow-level deadline hit at next node boundary
       └── NodeTimeoutExceeded                # R3 #2 NEW — per-node timeout fires; retry policy applies
   ```
   - Each class carries structured attributes via `__init__` (the relevant identifiers — node names, refs, types).
   - `__str__()` is STRICT single line — NO `\n` allowed anywhere. Format: `<ClassName>: <file>:<line> <identifying-names>: <problem summary>. Fix: <one-line action>.`. Fix-path is PROSE — a single-line action sentence — and is STRICT-no-code: NO Jinja delimiters (`{{`, `}}`, `{%`, `%}`), NO backticks or fenced code spans, NO YAML literal fragments showing syntax. ALLOWED: single-quoted identifiers naming workflow elements (`'fetch_a'`), technical vocabulary as prose (`binds-edge`, `Jinja delimiters`), and cross-references to docs (`see docs/workflows#anchor`). NO multi-line repr.
   - **R3 #8 — `caller_message: ClassVar[str]` on EVERY exception class**: declares a 1-3-sentence plain-English statement for non-engineer end users. Style rules: NO class names; NO `file:line` tokens; NO Jinja syntax (`{{`, `}}`, `{%`, `%}`); NO technical jargon; recommends an action ("try again", "share the run ID with your engineer", "contact your engineer"). Base `WorkflowError` declares a generic default; every subclass overrides with specific text. Reviewer assertion (per class): `caller_message` non-empty AND `"{{" not in caller_message and "{%" not in caller_message` AND class name does NOT appear in `caller_message` AND no `<file>:<line>` token in `caller_message`.
   - **R3 #11 — `identifying_names_keys: ClassVar[tuple[str, ...]]` on EVERY exception class**: declares expected identifying-name field names; every listed key MUST be a real dataclass field on the class. Generic JSON renderer (`src/cli/_format.py:render_exception(exc)`, lands Step 6) auto-extracts those keys into `identifying_names: {...}` and places all remaining instance fields into `attrs: {...}`. Reviewer assertion: every key in `identifying_names_keys` resolves to an actual dataclass field on the class.
   - YAML-origin classes' `__str__()` MUST contain: the literal `<file>:<line>` form, the identifying names, AND the literal `"Fix: "` token (capital F, colon, space — lock the token). Reviewer asserts each YAML-origin class's message contains these tokens AND asserts `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`.
   - `WorkflowJinjaRefUnknown` (R3 #9 renamed from `WorkflowNodeRefUnknown`) carries `reason: str` (typed as `str` per Round 4 item #9, NOT `Literal`; value table documented at master spec §7.2) as a structured attribute and surfaces the reason in `__str__()`. Documented values: `not_predecessor` (X is NOT a graph-reachable transitive ancestor of the consuming node — predecessor closure is now graph-reachability-based per Round 4 item #11; `binds:` RHS still implicitly allows `edge.from`); `unknown_field` (X is a valid predecessor with a known output schema, but Y is NOT in that schema); `not_declared` (X is a valid predecessor but X's output schema is UNKNOWN — e.g., authored Python node failed to import or introspect).
   - `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`) is DISTINCT from `WorkflowJinjaRefUnknown` — it fires for unresolved YAML node-name references (the name doesn't exist in `workflow.nodes`); `WorkflowJinjaRefUnknown` is exclusively for Jinja `nodes.X.Y` validation.
   - NO error codes (no `code` attribute, no enum lookup).
   - **NO custom `WorkflowValidationErrorGroup` class** — multi-error reporting uses stdlib `ExceptionGroup[WorkflowValidationError]` directly (Python 3.11+; kolsch on 3.13).
   - Wrapped causes go in `__cause__` via `raise X(...) from original`.

2. **`src/workflows/context.py`** — `NodeContext` dataclass per master spec §5.5 (Round 4 item #10 — `emit` is now a method, not a bare `Callable`; Round 5 R1 + Round 6 R6-R1 — signature is `emit(subkind, payload)` and the engine auto-prepends `node.<bundle>.` for the wire kind; the "author" segment from R5 R1 was DROPPED per R6-R1; Round 6 S4 — `bundle: str` field set by engine at construction time (publicly named per Round 7 L7-3); Round 7 L7-3 — rename `_bundle` → `bundle` and `_emit_fn` → `emit_fn` (drop underscore prefixes; both are public test seams); Round 7 A7-1 — `for_test(**overrides)` classmethod; Round 7 A7-6 — `state: Mapping[str, Any]` field wrapped in `MappingProxyType`):
   ```python
   from dataclasses import dataclass, field
   from pathlib import Path
   from types import MappingProxyType
   from typing import Any, Callable, Mapping
   # NOTE: runtime (Phase 3.1+ executor) wraps the resolved secrets dict AND
   # the resolved state dict in types.MappingProxyType before constructing
   # NodeContext. Tests can pass plain dicts — dict IS a Mapping; runtime
   # wraps for production read-only enforcement.

   @dataclass(frozen=True, slots=True)
   class NodeContext:
       workflow_run_id: str
       invocation_id: str
       workspace_root: Path
       log: StructLogger              # placeholder — use existing logger pattern from src/tools/
       secrets: Mapping[str, str]     # read-only protocol; runtime wraps in MappingProxyType
       # Round 7 A7-6: workflow state, read-only from inside @node. Engine wraps
       # in types.MappingProxyType before constructing. Authored @node code reads
       # via ctx.state["foo"]; mutating from inside @node is FORBIDDEN — only
       # set_state / delete_state nodes mutate state. §5.5 of the master spec
       # locks this read-only contract.
       state: Mapping[str, Any]
       # Round 6 S4 + Round 7 L7-3: bundle owning the currently-firing @node.
       # Set by the ENGINE at NodeContext construction time, sourced from the
       # catalog-resolved invocation file's bundle — NOT from inspect of the
       # calling @node's __module__ frame. This makes bundle attribution
       # SPOOF-RESISTANT: re-exporting a @node from another bundle cannot fake
       # its bundle of origin because the engine tracks the resolution path
       # that led to this invocation. Round 7 L7-3 promoted from `_bundle` to
       # public `bundle` — this is the public test seam (set by engine at
       # construction; authors read it; nobody mutates because the dataclass
       # is frozen).
       bundle: str
       # Round 7 L7-3: public test seam, renamed from `_emit_fn`. Bound at
       # construction time by the engine. repr=False keeps the callable out
       # of NodeContext reprs / logs. The engine wraps the caller's emit so
       # that calls like
       #   ctx.emit("custom.metric", {...})
       # land on the wire as the FULL kind
       #   node.<bundle>.custom.metric
       # where <bundle> is ctx.bundle (engine-tracked, per S4 + L7-3 above).
       # Per Round 6 R6-R1: the wire kind is node.<bundle>.<subkind> — the
       # "author" segment from R5 R1 was DROPPED. Engine event kinds all start
       # with workflow., policy., model., or tool. so `node.*` is unambiguously
       # the author namespace; no disambiguating "author" segment is needed.
       emit_fn: Callable[[str, Mapping[str, Any]], None] = field(repr=False)

       def emit(self, subkind: str, payload: Mapping[str, Any]) -> None:
           """Emit a custom workflow event. `subkind` is a freeform SUFFIX (no regex
           enforcement); the engine auto-prepends `node.<bundle>.` based on the
           engine-tracked invocation bundle (ctx.bundle, per Round 6 S4 +
           Round 7 L7-3), so the wire kind in JSONL becomes
           `node.<bundle>.<subkind>` (per Round 6 R6-R1 — the "author" segment
           from R5 R1 was DROPPED). Internally this method delegates to
           self.emit_fn(self.bundle, subkind, payload) — the engine's wrapper
           reads ctx.bundle from the bound NodeContext and prepends the
           namespace prefix on the wire."""
           self.emit_fn(self.bundle, subkind, payload)

       @classmethod
       def for_test(
           cls,
           *,
           workflow_run_id: str = "wrk_test",
           invocation_id: str = "inv_test",
           bundle: str = "test",
           workspace_root: Path | None = None,     # defaults to tempfile.mkdtemp()
           log: "StructLogger | None" = None,      # defaults to a no-op StructLogger
           emit_fn: Callable | None = None,        # defaults to lambda *_: None
           secrets: Mapping[str, str] | None = None,   # defaults to {}
           state: Mapping[str, Any] | None = None,     # defaults to {} (per A7-6)
       ) -> "NodeContext":
           """Round 7 A7-1 — sensible-defaults test constructor for NodeContext.
           Tests construct via NodeContext.for_test(...) instead of hand-rolling
           all 7 fields. Override any field via keyword. Amends R6 J3 D-decision
           (which was 'no helper at all')."""
           ...  # implementation: ~30 LoC; fills defaults, calls cls(...)
   ```
   - `frozen=True` prevents top-level field reassignment.
   - `slots=True` for memory efficiency (matches the `PermissionsBinding` pattern at `src/tools/permissions.py:70-71`).
   - `secrets` typed as `Mapping[str, str]` (NOT `dict`) — read-only protocol; runtime is responsible for wrapping its resolved dict in `types.MappingProxyType` before constructing the context.
   - **Round 7 A7-6 — `state: Mapping[str, Any]`**: read-only access to workflow state from inside @node. Engine wraps the live state dict in `types.MappingProxyType` before constructing. Authored @node code reads via `ctx.state["foo"]`. Mutating state from inside @node remains FORBIDDEN — only `set_state` / `delete_state` nodes mutate state. §5.5 of the master spec locks this read-only contract.
   - **Round 4 item #10**: `emit` is a real method with a typed signature. IDEs surface the signature on autocomplete. Backed by a public `emit_fn` field (renamed from `_emit_fn` per Round 7 L7-3 — public test seam) bound at construction time by the engine (with `repr=False` to keep it out of `NodeContext` reprs). The prior bare `emit: Callable` field (no signature) is REPLACED.
   - **Round 7 L7-3 — public test seam names**: both `bundle` and `emit_fn` are PUBLIC fields (underscore prefixes from R5/R6 dropped). The dataclass is `frozen=True` so authors cannot mutate them; they are public so tests can construct `NodeContext` directly without underscore-prefixed kwargs, and so the `for_test(**overrides)` classmethod (A7-1) has a clean public surface. Documented as test seams.
   - **Round 5 R1 + Round 6 R6-R1 — `emit(subkind, payload)` with engine-side auto-prepend**: the first positional is named `subkind` (not `kind`) to signal that the author supplies the SUFFIX, not the full wire kind. Subkind is FREEFORM (no regex enforcement). The engine constructs the `emit_fn` bound to a wrapper that prepends `node.<bundle>.` based on `ctx.bundle` (engine-tracked per S4), so the wire kind in the JSONL stream is `node.<bundle>.<subkind>`. **R6-R1 lock**: the "author" segment from R5 R1's `node.author.<bundle>.<subkind>` is DROPPED — engine event kinds all start with `workflow.`, `policy.`, `model.`, or `tool.`, so the `node.*` namespace is unambiguously the author namespace and no disambiguating segment is needed. Authors NEVER write the `node.<bundle>.` prefix themselves — this is the bundle-namespacing guarantee. `NodeContext.emit()` internally calls `self.emit_fn(self.bundle, subkind, payload)` (the engine's wrapper reads the bundle from the NodeContext to do the namespacing).
   - **Round 6 S4 + Round 7 L7-3 — public `bundle: str` field + spoof-resistance**: the engine sets `bundle` at `NodeContext` construction time from the **catalog-resolved invocation file's bundle**, NOT from `inspect` of the calling @node's `__module__` (or any other call-frame heuristic). This makes bundle attribution structurally spoof-resistant: re-exporting a @node from another bundle cannot fake its bundle of origin, because the engine tracks the resolution path that led to this invocation. The public `ctx.bundle` field exposes the value (read-only via `frozen=True`). §5.5 of the master spec locks this contract.
   - **Round 7 A7-1 — `NodeContext.for_test(**overrides)` classmethod**: keyword-only `for_test` classmethod accepting sensible defaults for ALL 7 fields (`workflow_run_id="wrk_test"`, `invocation_id="inv_test"`, `bundle="test"`, `workspace_root=tempfile.mkdtemp()`, `log=<no-op StructLogger>`, `emit_fn=lambda *_: None`, `secrets={}`, `state={}`). Overrides via keyword args. Implementation ~30 LoC; PUBLIC API; listed in `__all__`. Amends R6 J3 D-decision (which was 'no helper at all'). Tests construct via `NodeContext.for_test(...)` instead of hand-rolling all 7 fields.
   Step 1 only needs the dataclass declared. No runtime hookup — that comes in Phase 3.1 (the engine constructs `NodeContext(..., bundle=<resolved bundle>, state=<live state proxy>, emit_fn=<engine's bundle-namespaced event emitter>)`).

3. **`src/workflows/decorator.py`** — `@node` decorator per master spec §5.2:
   - Signature: `def node(*, secrets_needed: list[str] | None = None) -> Callable: ...`
   - Marks decorated function with a sentinel attribute (e.g. `_kolsch_node = {"secrets_needed": [...]}`).
   - Mirrors `@tool` pattern from `src/tools/decorator.py` — read that file FIRST.
   - At decoration time, validate basic shape (function is callable, `secrets_needed` is list-of-str if present). Deeper signature/return-type introspection happens at the validator (Step 5); do NOT do it here.

3a. **`src/workflows/jinja_env.py`** (NEW per Round 6 S1) — the singleton sandboxed Jinja environment. ALL Jinja evaluation in the workflows layer (binds rendering, config-string rendering, `when:`/`condition:` evaluation, `compute.value`, `map.over`, the validator's AST walk) routes through this module — no other module instantiates `NativeEnvironment` or `Environment` directly.
   - Subclass `jinja2.sandbox.ImmutableSandboxedEnvironment` with native-type returns preserved. Concrete approach: override `_parse` / `_generate` to use `jinja2.nativetypes.native_concat` (the same machinery `NativeEnvironment` uses) so single-segment templates still return Python typed values (int, list, dict, bool, etc.) instead of stringified output. The sandbox layer of the parent class blocks unsafe attribute access (the `__class__`/`__mro__`/`__subclasses__`/`__globals__` family) and unsafe callables (`getattr` on attributes whose name starts with `_`).
   - Expose a module-level singleton: `JINJA_ENV = WorkflowJinjaEnvironment()` (or a `get_env() -> WorkflowJinjaEnvironment` accessor — Coder picks). The validator + the engine (Phase 3.1+) share this single instance.
   - **Round 7 CV1 — built-in filter registration via module-level `_register_builtin_filters()`**: register the v1 `from_json` filter (and any future kolsch-maintained core filters) at module import time via a module-level helper `_register_builtin_filters(env: ImmutableSandboxedEnvironment) -> None` that calls `env.filters["from_json"] = json.loads` (plus a tiny wrapper raising a clean `WorkflowError` subclass on `json.JSONDecodeError`). The helper is called once from the singleton constructor / module top-level after the env is built. Implementation budget: ~5 LoC for the filter registration itself; the surrounding helper is the contribution chokepoint per `docs/explanation/jinja-extensions.md`. Filters are CORE-ONLY — bundles, plugins, and pip-installable packages CANNOT register custom filters. The filter set is kolsch's responsibility, kept small and audited. Per master spec §2.7's amendment, only `{{ }}` triggers Jinja evaluation; YAML literals pass through unchanged; the v1 filter set is `from_json` plus stock Jinja2 filters (see master spec §2.7.3). One fixture demonstrates the filter's use (added in Step 7's fixture suite — see 7.28).
   - **Error contract**: when the sandbox raises `jinja2.exceptions.SecurityError` (e.g., the canonical escape payloads `{{ ''.__class__.__mro__[1].__subclasses__() }}`, `{{ ().__class__.__bases__[0].__subclasses__() }}`, `{{ lipsum.__globals__ }}`, `{{ cycler.__init__.__globals__ }}`, `{{ joiner.__init__.__globals__ }}`, etc.), the module catches the underlying `SecurityError` and re-raises as `WorkflowJinjaSecurityError` (from `src/workflows/errors.py`) with the offending expression text + the @node/edge/config location + a `caller_message` advising the operator to review the bundle's bash commands and Jinja expressions for sandbox-escape attempts.
   - **Step 1 acceptance**: the module is importable; the singleton evaluates a single-segment Jinja template into a native Python type (e.g., `JINJA_ENV.from_string("{{ 1 + 1 }}").render() == 2`, NOT `"2"`); evaluating each of ~10 canonical escape payloads (see test fixtures below) raises `WorkflowJinjaSecurityError`.
   - **Test fixtures**: `tests/security/test_jinja_sandbox_escape.py` (NEW per S1) covers ~10 canonical Jinja-to-Python escape payloads:
     - `{{ ''.__class__.__mro__[1].__subclasses__() }}` (the `mro` chain)
     - `{{ ().__class__.__bases__[0].__subclasses__() }}` (the `bases` chain)
     - `{{ {}.__class__.__mro__[1].__subclasses__() }}` (dict-rooted)
     - `{{ lipsum.__globals__['__builtins__'] }}` (the `lipsum` global escape)
     - `{{ cycler.__init__.__globals__['os'] }}` (the `cycler` escape)
     - `{{ joiner.__init__.__globals__['os'] }}` (the `joiner` escape)
     - `{{ namespace.__init__.__globals__['os'] }}` (the `namespace` escape)
     - `{{ self.__init__.__globals__['os'] }}` (the `self` escape — common with template inheritance)
     - `{{ config.__class__.__mro__[1].__subclasses__() }}` (the `config` escape — Flask Jinja idiom)
     - `{{ ''.__class__.__mro__[2].__subclasses__()[X]('id', shell=True, ...) }}` (RCE-shaped — a representative compound payload).
     Each fixture asserts `WorkflowJinjaSecurityError` is raised when the singleton tries to render the payload. Reviewer enumerates all 10 + confirms the security test file exists.
   - **Master spec §2.7.2 documents the sandbox contract** (the Round 6 S1 lock pointer): "The workflows layer evaluates Jinja inside `src/workflows/jinja_env.py`'s `ImmutableSandboxedEnvironment` subclass with native-type returns preserved. Author-supplied Jinja CANNOT escape into the Python runtime via attribute traversal."

3b. **`src/workflows/labels.py`** (NEW per Round 7 L7-8) — the single source of truth for human-readable node labels. Exports `display_label(node: NodeDef, type_label: str | None = None) -> str`. Implementation follows the 3-step fallback per master spec §15.4:
   1. If `node.description` (per-instance, R3 #7) is set and non-empty → return it.
   2. Else if `type_label` (caller-supplied — typically the per-type human label, e.g., `"Run a shell command"` for `bash`) is set → return it.
   3. Else clean the class / function name: convert `BashNodeDef` → `"bash"`; convert `MyAuthoredNode` (authored Python `@node` function name) → `"my authored node"` (snake-or-camel-case → space-separated lowercase).
   - Public API; re-exported from `src/workflows/__init__.py` and listed in `__all__`.
   - Spec §15.4 documents that `display_label()` is the single source of truth — chat capsules, CLI table renderers (`kolsch workflows show`, `kolsch nodes list`), and error-message label formatters all call this helper rather than re-implementing the fallback.
   - Step 1 acceptance: import works; 3-step fallback verified by unit test (one test per branch — instance description set; only type_label set; only class name available).

4. **`src/workflows/__init__.py`** — public API re-exports. **Round 4 item #5 lock**: every public name MUST be listed in an explicit `__all__: list[str]` at the bottom of the module; a ruff rule (e.g., RUF022 + a project-local custom check) enforces parity between `__all__` and the imports. Any name imported into `workflows/__init__.py` and not listed in `__all__` is a lint failure; any name listed in `__all__` but not imported is also a lint failure. **Round 7 L7-8 — `display_label(node, type_label=None)` helper is part of the public API**: re-exported here and listed in `__all__`; the helper implements the 3-step fallback per master spec §15.4 (per-instance `description:` → type-level label → cleaned class/function name). Master spec §15.4 documents `display_label()` as the single source of truth.
   ```python
   from workflows.decorator import node
   from workflows.context import NodeContext
   from workflows.labels import display_label  # Round 7 L7-8 — public helper for the §15.4 3-step fallback
   from workflows.errors import (
       WorkflowError,
       WorkflowValidationError,
       WorkflowValidationWarning,             # R3 #6 RESTORED
       WorkflowRuntimeError,
       # ... every class from §12.1 — including R3 #9 renamed + R3 #2/#5/#6 new + Round 4 #11/#13 new + Round 6 S1/S5 new:
       # WorkflowJinjaRefUnknown (renamed),
       # WorkflowBranchMissingArm (renamed), WorkflowUnreachableNode,
       # WorkflowMapperWorkerNotAddressable, WorkflowExtendsDepthExceeded,
       # WorkflowInvalidTimeout, WorkflowInvalidRef, WorkflowBuiltinShadowed,
       # WorkflowBindCollision, WorkflowBindTypeMismatch,  # Round 4 item #11
       # WorkflowFromShapeError, WorkflowFromAssetTypeError, WorkflowFromNotFound,  # Round 4 item #13
       # WorkflowJinjaSecurityError,                                                  # Round 6 S1 NEW
       # WorkflowExtendsPolicyForbidden,                                              # Round 6 S5 NEW
       # WorkflowTimeoutShadowsNode,
       # WorkflowTimeoutExceeded, NodeTimeoutExceeded
       # NOTE: WorkflowMultiInputForbidden REMOVED (Round 4 item #11)
       # NOTE: WorkflowFromShapeWarning REMOVED (Round 4 item #13 — promoted to hard errors)
       # NOTE: WorkflowBindMustBeJinja REMOVED (Round 6 J5/β — binds: now accepts YAML literals OR Jinja-marked strings)
   )
   from workflows.ids import (                # R3 #10 typed prefixed IDs
       TraceId, WorkflowRunId, InvocationId, ToolCallId,
   )

   __all__: list[str] = [
       # decorator + context
       "node",
       "NodeContext",
       # public helpers (Round 7 L7-8)
       "display_label",
       # base exceptions
       "WorkflowError",
       "WorkflowValidationError",
       "WorkflowValidationWarning",
       "WorkflowRuntimeError",
       # ... every other public name listed explicitly (ruff RUF022 + custom check enforce parity)
   ]
   ```

5. **`src/assets/refs.py`** (modify the existing module that owns `_TYPE_TO_SEGMENT`):
   - **REMOVE `parse_ref` entirely.** No compatibility alias — every caller migrates to `parse_three_segment` in this step. The Decision 1 lock REVOKES the prior QoL doc `qol-prefix-defaults-and-unified-refs.md`.
   - **R3 #5: REMOVE `resolve_shorthand` entirely.** 1-seg shorthand is REMOVED universally; `parse_three_segment` is the sole ref parser. Migration codemod available at `process/qol-refs-1seg-removal.md`. (`parse_ref` was already removed in must-fix #1; this step removes `resolve_shorthand` too.)
   - Extend `_TYPE_TO_SEGMENT` with two new entries: `"workflow": "workflows"` and `"node": "nodes"`. The full map now covers all four asset types: `tool`, `agent`, `workflow`, `node` — 3-seg is canonical for ALL of them.
   - Add `build_ref(bundle: str, asset_type: str, name: str) -> str` — the ONE canonical builder. Looks up `_TYPE_TO_SEGMENT[asset_type]` (raises `ValueError` on unknown asset_type) and returns `f"{bundle}/{segment}/{name}"`.
   - Add `parse_three_segment(ref: str) -> tuple[str, str, str]` — pure shape check. Splits on `/`, validates 3 segments, returns `(bundle, plural_segment, name)`. NOTE: returns the plural segment as-is (e.g., `"tools"`, `"agents"`, `"workflows"`, `"nodes"`) — callers can reverse-map via `_TYPE_TO_SEGMENT` if they need the singular. Raises `ValueError` on shape mismatch.
   - Add `is_three_segment(ref: str) -> bool` — cheap predicate (try `parse_three_segment`; return True iff it succeeded).
   - These three functions are the ENTIRE public surface for refs work in the rest of the codebase.

6. **`src/workflows/ids.py`** (NEW per R3 #10) — typed prefixed IDs:
   - `TraceId(PrefixedId)` with prefix `tr_` — chain-spanning ID; CLI/chat owns assignment at the root entry point; inherited unchanged across sub-workflow calls, spawned agent sessions, sub-agent invocations, and forks.
   - `WorkflowRunId(PrefixedId)` with prefix `wrk_` — workflow engine owns assignment.
   - `InvocationId(PrefixedId)` with prefix `inv_` — workflow engine owns assignment.
   - `ToolCallId(PrefixedId)` with prefix `tc_` — tool dispatcher owns assignment (was bare `str`; now typed).
   - All prefixed IDs are UUID4-backed; document the global-uniqueness invariant for `EventId` (prefix `ev_`).
   - Ownership map documented in the module docstring per master spec §11.x.

7. **`src/assets/catalog.py`** modifications (refs unification consumption — Round 5 H3 lands the chokepoint and discipline):
   - **Round 5 H3 — `AssetCatalog.lookup` is the Phase 3.0 catalog chokepoint**: add `AssetCatalog.lookup(ref: str, expected_type: str | None = None) -> AssetEntry | None`. Implementation wraps `parse_three_segment(ref)` + dict-get against the appropriate inner registry; returns None if shape is wrong, ref is unknown, or `expected_type` is set and doesn't match. This method lives in `src/assets/catalog.py` (Phase 3.0 scope — Coder may place it on `AssetCatalog` directly; if the implementation prefers a sibling module, document the rationale in the diff). Master spec §13.2.4 (refs unification) names this as the single read path: "All catalog access in src/workflows/*, src/providers/policy.py (file rename queued — at Phase 3.0 the file is still src/providers/authz.py per H1), and other call sites routes through AssetCatalog.lookup(ref, expected_type). Direct dict access (catalog.assets[ref]) is permitted only inside src/assets/catalog.py itself."
   - **Round 5 H3 — Planned custom ruff lint rule (DOCUMENTED HERE, ENFORCED via the ruff config landed in this step)**: a custom ruff lint flags `catalog.assets[` and `catalog._<anything>[` subscript access OUTSIDE `src/assets/catalog.py`. The lint rule is added under the project's existing custom-lints location (or a project-local ruff plugin); failures point the author at `catalog.lookup(ref, expected_type=...)`. Document the planned rule + its enforcement path in this Step 1 deliverable; if the custom-lint plumbing isn't in place at 3.0 yet, ship a `# noqa`-grep-able convention and queue the actual ruff plugin as a follow-up QoL — but the docstring on `AssetCatalog.lookup` MUST state the discipline.
   - **Round 5 H3 — Migrate `src/assets/asset_manager.py:188`**: that line currently reaches into `catalog.assets[...]` directly; replace with `catalog.lookup(ref, expected_type=<...>)`. The migration is included in Step 1's acceptance — reviewer flags any new `.assets[...]` access introduced elsewhere in the diff. This is the canonical reference call site demonstrating the chokepoint discipline.
   - Refactor ALL 4 existing `_scan_*` functions to construct catalog entry refs via `build_ref(bundle, asset_type, name)` — replace every inline `f"{bundle}/{plural}/{name}"`. Search for the existing inline f-strings and confirm none survive.
   - **R3 #5: DROP the `core` reservation entirely** — `core` is no longer reserved (was speculative for the dropped `core/principals/*` design). DO NOT add `core/principals/user` or `core/principals/system` ref types. Principal strings stay engine telemetry (`engine.py:407`, `engine.py:1047`, `_classify.py:99`); they never feed the matcher.

8. **`src/providers/authz.py`** modifications (refs unification consumption — partial; the layer/`from:` work is Step 4):
   - Refactor `_validate_what` to route three-segment refs through `parse_three_segment` + `catalog.lookup` instead of inline string splits.
   - Refactor `_build_action_to_tool_ref` (or its successor) to route through `parse_three_segment`.
   - Two-segment tool refs (legacy migration shim) MUST continue to work unchanged.

9. **`src/engine/observers.py:221`** modification: replace the inline ref construction for the agent chain frame with `build_ref(bundle, "agent", name)`. (Workflow chain frames land later when the workflow engine ships in Phase 3.1; this step is the agent-side cleanup.)

**Step 1 Acceptance Criteria:**

1.1. `from workflows import node, NodeContext, WorkflowError, WorkflowValidationError, WorkflowValidationWarning, ...` works for every class in the hierarchy, INCLUDING the new/renamed ones (R3 + Round 4 + Round 6): `WorkflowJinjaRefUnknown` (R3 #9 renamed from `WorkflowNodeRefUnknown`), `WorkflowBranchMissingArm` (R3 #9 renamed from `WorkflowBranchDeadSide`), `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, `WorkflowInvalidTimeout` (R3 #2), `WorkflowInvalidRef` (R3 #5), `WorkflowBuiltinShadowed` (R3 #5), `WorkflowTimeoutShadowsNode` (R3 #2), `WorkflowTimeoutExceeded` + `NodeTimeoutExceeded` (R3 #2 runtime), **`WorkflowBindCollision` + `WorkflowBindTypeMismatch`** (Round 4 item #11 NEW), **`WorkflowFromShapeError` + `WorkflowFromAssetTypeError` + `WorkflowFromNotFound`** (Round 4 item #13 NEW), **`WorkflowJinjaSecurityError`** (Round 6 S1 NEW), **`WorkflowExtendsPolicyForbidden`** (Round 6 S5 NEW). `WorkflowValidationWarning` IS in the hierarchy (R3 #6 RESTORED — still hosts `WorkflowTimeoutShadowsNode`). `WorkflowStateKeyCollision` remains DELETED. **Round 4 item #11**: `WorkflowMultiInputForbidden` is NOT importable (REMOVED). **Round 4 item #13**: `WorkflowFromShapeWarning` is NOT importable (REMOVED — promoted to hard errors above). **Round 6 J5/β**: `WorkflowBindMustBeJinja` is NOT importable (REMOVED — binds: now accepts YAML literals OR Jinja-marked strings; only `"{{ ... }}"` triggers Jinja evaluation, so a bare literal is no longer an error).
1.2. Every exception class is instantiable with its expected structured attributes; `repr(exc)` shows the class name; `str(exc)` is STRICT single-line (assertion: `"\n" not in str(exc)`). For YAML-origin classes, `str(exc)` includes the `<file>:<line>` token, identifying names, AND the literal `"Fix: "` token (capital F, colon, space — lock the token). Reviewer asserts each YAML-origin class's `str(exc)` contains all three patterns via string-contains checks. Reviewer ALSO asserts STRICT-no-code in the Fix portion: `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)` for every YAML-origin error class (per master spec §12.3 strict rule).
1.3. `WorkflowNodeFailed("foo", "inv_123", cause=ValueError("x"))` (or equivalent) sets `__cause__` to the wrapped exception.
1.4. `@node(secrets_needed=["api_key"])` decorates a function; the function carries the sentinel attribute with the secrets list.
1.5. `@node` (bare) decorates a function; the sentinel attribute has empty/None secrets.
1.6. Decorating a non-callable raises `TypeError` at decoration time (basic shape check).
1.7. `NodeContext` is `@dataclass(frozen=True, slots=True)` with all fields from master spec §5.5; `secrets` is typed as `Mapping[str, str]` (not `dict`); **Round 7 A7-6**: `state: Mapping[str, Any]` field declared (read-only protocol; engine wraps in `MappingProxyType` at construction; authored @node code reads via `ctx.state["foo"]`; mutating from inside @node is FORBIDDEN — only `set_state`/`delete_state` mutate; §5.5 documents the contract). Mutating any field raises `dataclasses.FrozenInstanceError`. **Round 4 item #10 + Round 5 R1 + Round 6 R6-R1 + Round 7 L7-3**: `NodeContext.emit` is a real method with signature `emit(self, subkind: str, payload: Mapping[str, Any]) -> None` — first positional renamed from `kind` to `subkind` per Round 5 R1 to signal the author supplies a SUFFIX, not the full wire kind. The bare `emit: Callable` field from the prior spec is REPLACED. Backed by a PUBLIC `emit_fn: Callable[[str, Mapping[str, Any]], None] = field(repr=False)` populated at construction by the engine — **Round 7 L7-3 renames `_emit_fn` → `emit_fn` (drop underscore prefix; documented as a public test seam)**. **Engine-side auto-prepend (Round 6 R6-R1 — amends R5 R1)**: the engine binds `emit_fn` to a wrapper that prepends `node.<bundle>.` to the subkind based on `ctx.bundle` (engine-tracked per Round 6 S4 + L7-3 rename), so the wire kind in the JSONL stream is `node.<bundle>.<subkind>` (freeform subkind — no regex enforcement). **R6-R1 drops the "author" segment** from R5 R1's `node.author.<bundle>.<subkind>` — engine event kinds all start with `workflow.`, `policy.`, `model.`, or `tool.`, so the `node.*` namespace is unambiguously the author namespace. **`NodeContext.emit()` internally calls `self.emit_fn(self.bundle, subkind, payload)`** (or similar wrap — the engine's bound wrapper reads `self.bundle` to do the namespacing). Reviewer asserts: `inspect.signature(NodeContext.emit)` returns `(self, subkind: str, payload: Mapping[str, Any]) -> None`; `NodeContext(...)` constructed with `emit_fn=<callable>` calls into it on `ctx.emit(...)`; `emit_fn` does NOT appear in `repr(ctx)`. Reviewer also verifies the engine-side auto-prepend behavior is DOCUMENTED in the `NodeContext.emit` docstring as `node.<bundle>.<subkind>` (NOT `node.author.<bundle>.<subkind>` — the "author" segment is dropped per R6-R1; the actual prepending lives in Phase 3.1's engine — Step 1 only documents the contract). **Round 6 S4 + Round 7 L7-3 — public `bundle: str` field**: `NodeContext` carries a `bundle: str` field set by the engine at construction time from the catalog-resolved invocation file's bundle (NOT from `inspect` of the calling @node's `__module__`). The field is PUBLIC (renamed from `_bundle` per Round 7 L7-3 — documented as a public test seam) and `frozen=True` keeps it read-only via dataclass freeze. Reviewer asserts: `NodeContext(..., bundle="acme")` constructs cleanly; `ctx.bundle == "acme"`; the docstring on `bundle` documents the spoof-resistance contract ("engine uses catalog-resolved invocation file's bundle, NOT inspect of calling frame"). **Round 7 A7-1 — `NodeContext.for_test(**overrides)` classmethod**: keyword-only classmethod with sensible defaults for all 7 fields (`workflow_run_id`, `invocation_id`, `bundle`, `workspace_root`, `log`, `emit_fn`, `secrets`, `state`); ~30 LoC; PUBLIC API; listed in `__all__`. Reviewer asserts: `NodeContext.for_test()` constructs cleanly with no args; `NodeContext.for_test(bundle="acme").bundle == "acme"`; `NodeContext.for_test(state={"k": 1}).state["k"] == 1`.
1.8. `WorkflowJinjaRefUnknown` (R3 #9 renamed) carries `reason: Literal["not_predecessor", "unknown_field", "not_declared"]`; constructing without a reason raises.
1.9. **Refs unification (3-seg canonical for ALL types)**: `from assets.refs import build_ref, parse_three_segment, is_three_segment` succeeds. `parse_ref` is NOT importable (removed entirely). **`resolve_shorthand` is NOT importable** (R3 #5 — removed entirely; 1-seg shorthand gone). `build_ref("acme", "tool", "bash") == "acme/tools/bash"`; `build_ref("acme", "agent", "bot") == "acme/agents/bot"`; `build_ref("acme", "workflow", "triage") == "acme/workflows/triage"`; `build_ref("acme", "node", "echo") == "acme/nodes/echo"`. `parse_three_segment("a/b/c") == ("a", "b", "c")`; `parse_three_segment("a/b")` raises `ValueError`. `is_three_segment("a/b/c") is True`; `is_three_segment("a/b") is False`.
1.10. **Round 5 H3 — `AssetCatalog.lookup` chokepoint**: `catalog.lookup("acme/agents/bot")` returns the entry; `catalog.lookup("acme/agents/missing")` returns `None`; `catalog.lookup("acme/agents/bot", expected_type="workflow")` returns `None`; `catalog.lookup("bad-shape")` returns `None`. Method docstring documents the chokepoint discipline ("Direct dict access — catalog.assets[ref] — is permitted only inside src/assets/catalog.py itself; all external call sites route through .lookup()."). The planned custom ruff lint rule (flags `catalog.assets[` and `catalog._<anything>[` access outside `src/assets/catalog.py`) is documented in Step 1's deliverable list; reviewer verifies the docstring + lint rule plan are present, and that `src/assets/asset_manager.py:188` has been migrated from direct `.assets[...]` access to `.lookup(...)`.
1.11. **No inline ref construction anywhere**: grep across `src/assets/catalog.py`, `src/providers/authz.py`, `src/engine/observers.py` finds ZERO occurrences of `f"{bundle}/{` or equivalent inline three-segment string construction; ALL go through `build_ref`. (Reviewer enforces this with a literal grep in their checklist.) **Round 5 H3 — Catalog access discipline**: grep across `src/workflows/*`, `src/providers/authz.py` (which Round 5 H1 queues for rename to `src/providers/policy.py` in a follow-up QoL), and other consumers finds ZERO occurrences of `catalog.assets[` or `catalog._<private>[` subscript access — ALL reads route through `catalog.lookup(ref, expected_type=...)`. The custom ruff lint rule enforces this on future commits.
1.12. **Migration shim untouched**: existing two-segment tool refs (`kolsch/bash`) continue to resolve via `_validate_what` after the refactor — verified by re-running the existing authz test suite.
1.13. **R3 #8 — `caller_message` ClassVar on every exception**: every class in `WorkflowError`'s tree declares `caller_message: ClassVar[str]` non-empty. Reviewer assertion per class: `caller_message` is 1-3 sentences; class name does NOT appear in `caller_message`; `"{{" not in caller_message and "{%" not in caller_message`; no `<file>:<line>` token in `caller_message`; no backticks.
1.14. **R3 #11 — `identifying_names_keys` ClassVar on every exception**: every class declares `identifying_names_keys: ClassVar[tuple[str, ...]]`; reviewer asserts every listed key is a real dataclass/init field on the class.
1.15. **R3 #10 — Typed prefixed IDs**: `from workflows.ids import TraceId, WorkflowRunId, InvocationId, ToolCallId` succeeds. `TraceId(...)` constructs with `tr_` prefix; `WorkflowRunId(...)` with `wrk_`; `InvocationId(...)` with `inv_`; `ToolCallId(...)` with `tc_`. `ToolCallId` is now a typed class (was bare `str`). Module docstring documents UUID4 invariant for `EventId` global uniqueness AND the layered ID ownership map (trace > root entity > sub-entity > per-event; CLI/chat owns `trace_id`; workflow engine owns `wrk_`/`inv_`; agent runtime owns `ses_`/`th_`/`cl_`; tool dispatcher owns `tc_`; event emitter owns `ev_`).
1.16. **R3 #5 — `core` reservation dropped**: grep `core/principals` across `src/` returns ZERO matches. `core` is not a reserved bundle name anywhere.

1.17. **Round 4 item #5 — Explicit `__all__` in `src/workflows/__init__.py`**: the module declares `__all__: list[str]` listing every public name re-exported from the module. A ruff rule (RUF022 plus a project-local custom check) enforces parity between `__all__` and the imports: any name imported but not listed in `__all__` is a lint failure, and any name listed in `__all__` but not imported is a lint failure. Reviewer verifies (a) `__all__` exists as a list literal, (b) running ruff on the file passes, (c) deliberately desynchronizing `__all__` (test fixture commit) causes ruff to fail.

1.18. **Round 4 item #10 + Round 5 R1 + Round 6 R6-R1 + Round 6 S4 + Round 7 L7-3 + Round 7 A7-1 + Round 7 A7-6 — `NodeContext.emit` is a method (subkind, payload) with engine-side auto-prepend documented; bundle attribution is engine-tracked; public test seams + for_test classmethod + state field**: `inspect.signature(NodeContext.emit)` returns `(self, subkind: str, payload: Mapping[str, Any]) -> None` (first positional named `subkind` per Round 5 R1 — NOT `kind`). The PUBLIC `emit_fn` field is present with `repr=False` — **Round 7 L7-3 renamed from `_emit_fn` to `emit_fn` (drop underscore; documented as a public test seam)**. `ctx.emit("custom.metric", {"k": 1})` delegates to `emit_fn` (internal call shape: `self.emit_fn(self.bundle, subkind, payload)` — engine-side wrapper does the namespacing). `repr(ctx)` does NOT mention `emit_fn`. The method's docstring documents the engine-side auto-prepend: the engine wraps `emit_fn` to prepend `node.<bundle>.` (Round 6 R6-R1 — the "author" segment from R5 R1 is DROPPED) based on `ctx.bundle` (engine-tracked per Round 6 S4 + Round 7 L7-3 rename from the catalog-resolved invocation file's bundle — NOT from `inspect` of the calling @node's `__module__`), so the wire kind in the JSONL stream is `node.<bundle>.<subkind>` (freeform subkind; no regex enforcement on the suffix). Step 1 only declares + documents the contract; actual prepending lives in Phase 3.1's engine. **Round 6 S4 + Round 7 L7-3 spoof-resistance**: `NodeContext` carries PUBLIC `bundle: str` set by the engine at construction (renamed from `_bundle` per L7-3 — `frozen=True` keeps it read-only); the docstring documents that re-exporting a @node from another bundle cannot fake its bundle of origin because the engine tracks the catalog-resolution path, not the call-frame module. **Round 7 A7-6 — `state: Mapping[str, Any]` field**: declared on `NodeContext`; engine wraps in `MappingProxyType` at construction; authored @node code reads via `ctx.state["foo"]`; mutating from inside @node remains FORBIDDEN (only `set_state`/`delete_state` mutate); §5.5 documents the read-only contract. **Round 7 A7-1 — `NodeContext.for_test(**overrides)` classmethod**: keyword-only `for_test` classmethod accepts sensible defaults for ALL 7 fields (`workflow_run_id="wrk_test"`, `invocation_id="inv_test"`, `bundle="test"`, `workspace_root=<tempdir>`, `log=<no-op>`, `emit_fn=lambda *_: None`, `secrets={}`, `state={}`); ~30 LoC; PUBLIC API; listed in `__all__`. Amends R6 J3 D-decision. Reviewer asserts: `NodeContext.for_test()` constructs cleanly; overrides flow through (`NodeContext.for_test(bundle="acme").bundle == "acme"`); `state` override flows through (`NodeContext.for_test(state={"k": 1}).state["k"] == 1`); listed in `kolsch.workflows.__all__`.

1.19. **Round 6 S1 — `src/workflows/jinja_env.py` module + `WorkflowJinjaSecurityError`**: `from workflows.jinja_env import JINJA_ENV` (or `get_env()`) succeeds; the singleton is an `ImmutableSandboxedEnvironment` subclass that preserves native-type returns (`JINJA_ENV.from_string("{{ 1 + 1 }}").render() == 2` — an int, NOT the string `"2"`); evaluating each of ~10 canonical Jinja-to-Python escape payloads raises `WorkflowJinjaSecurityError` (NOT bare `jinja2.exceptions.SecurityError`). `from workflows import WorkflowJinjaSecurityError` is importable. `tests/security/test_jinja_sandbox_escape.py` exists with ~10 canonical payloads (mro, bases, lipsum, cycler, joiner, namespace, self, config, dict-rooted, RCE-shaped compound). Reviewer enumerates the 10 + asserts each raises `WorkflowJinjaSecurityError` from the singleton. Master spec §2.7.2 cross-reference present in the module docstring.

1.20. **Round 6 S5 — `WorkflowExtendsPolicyForbidden` is importable**: `from workflows import WorkflowExtendsPolicyForbidden` succeeds; it is a `WorkflowValidationError` subclass; `caller_message` + `identifying_names_keys` ClassVars present per R3 #8/#11 conventions. Step 1 only declares the class; the validator pass that raises it lands in Step 4.

1.21. **Round 7 L7-8 — `display_label()` public helper**: `from kolsch.workflows import display_label` succeeds. Helper signature is `display_label(node: NodeDef, type_label: str | None = None) -> str`. 3-step fallback per master spec §15.4: (a) per-instance `description:` (R3 #7) wins; (b) else `type_label` arg wins; (c) else cleaned class/function name. `display_label` is listed in `src/workflows/__init__.py:__all__` (Round 4 item #5 — explicit `__all__` parity). Reviewer verifies: importable; 3 unit tests (one per fallback branch) pass; `__all__` includes the name; master spec §15.4 cross-reference present in the helper docstring.

1.22. **Round 7 CV1 — `from_json` Jinja filter registered in `src/workflows/jinja_env.py`**: module-level `_register_builtin_filters(env)` helper registers `env.filters["from_json"] = json.loads` (with a tiny wrapper raising a clean `WorkflowError` subclass on `json.JSONDecodeError`). Called once at module import time (singleton constructor or module top-level after env construction). Reviewer asserts: `JINJA_ENV.filters["from_json"]` is registered; `JINJA_ENV.from_string('{{ \'{"name":"x"}\' | from_json }}').render()` returns the dict `{"name": "x"}` (a real Python dict, NOT a stringified version — sandboxed env preserves native types per S1); filters are CORE-ONLY (bundles/plugins/packages cannot register custom filters — verified by reading the module docstring + the contribution doc `docs/explanation/jinja-extensions.md`). Implementation budget ~5 LoC for the filter registration itself. One fixture in Step 7 demonstrates the filter's use end-to-end (`valid-from-json-filter.yaml`, see 7.28).

**Step 1 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_scaffolding.py tests/component/test_refs_unification.py -v` (create BOTH test files as part of Step 1).
- **Expect:** ~20-25 tests covering: imports (including new exception classes), every exception class instantiates with structured attrs, `__str__` is single line, YAML-origin classes embed file+line+fix-path tokens in their message, `@node` sentinel attribute, `NodeContext` field shape, `build_ref`/`parse_three_segment`/`is_three_segment` round-trips for all three asset types, `AssetCatalog.lookup` shape + expected_type filtering. All pass.
- **Run (regression):** `podman-compose exec dev uv run pytest tests/component/test_authz*.py tests/component/test_catalog*.py -q` — the refs refactor must NOT break existing authz / catalog tests.
- **Fail means:** public API surface is wrong; decorator behavior is wrong; exception hierarchy is wrong; refs unification regressed an existing caller.

**Self-check before handoff:**

- [ ] All 22 Step 1 acceptance criteria met (18 original + 1.19 Round 6 S1 sandbox + 1.20 Round 6 S5 extends-policy-forbidden + 1.21 Round 7 L7-8 display_label + 1.22 Round 7 CV1 from_json filter)
- [ ] `tests/component/test_workflow_scaffolding.py`, `tests/component/test_refs_unification.py`, AND `tests/security/test_jinja_sandbox_escape.py` (Round 6 S1) exist and pass
- [ ] Existing test suite still green (`podman-compose exec dev uv run pytest tests/component/ -q`) — including existing authz/catalog tests
- [ ] Decorator code mirrors `src/tools/decorator.py` patterns; no ad-hoc deviation
- [ ] No error codes, no multi-line `__str__()`, no scope creep into parser/catalog/validator
- [ ] `grep -rn 'f"{[^}]*}/{[^}]*}/{[^}]*}"' src/assets/catalog.py src/providers/authz.py src/engine/observers.py` returns ZERO matches — all three-segment refs go through `build_ref`
- [ ] `core/principals/*` ref types NOT added (principals are telemetry-only)
- [ ] **Round 5 R1 + Round 6 R6-R1 — `NodeContext.emit(subkind, payload)`**: first positional renamed from `kind` to `subkind`; docstring documents engine-side auto-prepend (`node.<bundle>.<subkind>` wire kind — the "author" segment from R5 R1 is DROPPED per R6-R1) based on `ctx.bundle` (engine-tracked per S4 + Round 7 L7-3 rename); subkind is freeform (no regex enforcement); `NodeContext.emit()` internally calls `self.emit_fn(self.bundle, subkind, payload)`
- [ ] **Round 6 S4 + Round 7 L7-3 — `NodeContext.bundle` PUBLIC field** (renamed from `_bundle`): public test seam; `frozen=True` keeps it read-only; docstring documents spoof-resistance contract (engine sets `bundle` from catalog-resolved invocation file's bundle, NOT from `inspect` of calling frame's `__module__`)
- [ ] **Round 7 L7-3 — `NodeContext.emit_fn` PUBLIC field** (renamed from `_emit_fn`): public test seam; `repr=False` keeps it out of reprs
- [ ] **Round 7 A7-6 — `NodeContext.state: Mapping[str, Any]` field**: read-only; engine wraps in `MappingProxyType`; authored @node code reads via `ctx.state["foo"]`; mutating from inside @node FORBIDDEN; `for_test(state=...)` accepts override
- [ ] **Round 7 A7-1 — `NodeContext.for_test(**overrides)` classmethod**: sensible defaults for all 7 fields; ~30 LoC; PUBLIC API; listed in `__all__`; amends R6 J3 D-decision
- [ ] **Round 7 L7-8 — `display_label(node, type_label=None)` public helper** ships in `src/workflows/labels.py`; re-exported from `kolsch.workflows`; listed in `__all__`; 3-step fallback per master spec §15.4; 3 unit tests (one per branch)
- [ ] **Round 7 CV1 — `from_json` Jinja filter** registered in `src/workflows/jinja_env.py` via module-level `_register_builtin_filters()` helper (~5 LoC); singleton evaluates `'{"k":1}' | from_json` to the native dict `{"k": 1}`; filter set is CORE-ONLY (bundles/plugins/packages cannot register filters); contribution chokepoint documented at `docs/explanation/jinja-extensions.md`
- [ ] **Round 6 S1 — `src/workflows/jinja_env.py` module ships**: singleton sandboxed env subclassing `ImmutableSandboxedEnvironment` with native-type returns preserved; `WorkflowJinjaSecurityError` wraps `jinja2.exceptions.SecurityError`; `tests/security/test_jinja_sandbox_escape.py` covers ~10 canonical escape payloads (mro, bases, lipsum, cycler, joiner, namespace, self, config, dict-rooted, compound RCE)
- [ ] **Round 6 S5 — `WorkflowExtendsPolicyForbidden` exception class declared**: importable from `kolsch.workflows`; `WorkflowValidationError` subclass; `caller_message` + `identifying_names_keys` ClassVars present
- [ ] **Round 6 J5/β — `WorkflowBindMustBeJinja` REMOVED**: NOT in `errors.py`; NOT importable from `kolsch.workflows`; grep across `src/workflows/` returns ZERO matches
- [ ] **Round 5 H3 — `AssetCatalog.lookup` chokepoint discipline**: method exists with documented chokepoint discipline; planned custom ruff lint rule (flags `catalog.assets[` and `catalog._<private>[` outside `src/assets/catalog.py`) documented; `src/assets/asset_manager.py:188` migrated from direct `.assets[...]` to `.lookup(...)`; reviewer flags any new `.assets[...]` access elsewhere in the diff

**Handoff trigger:** When all boxes checked, reply "Step 1 Complete — [summary]". Decision needed → cite master spec section.

## Step 1 — Reviewer Prompt

> You are reviewing Step 1 of a 7-step phase. Read everything below. Your job is to
> verify Step 1 meets its acceptance criteria. Be specific. Reply "Step 1 Approved"
> or "Changes requested — [itemized list with spec citations]".

**What was built in Step 1:** the `src/workflows/` package skeleton (exception hierarchy, `NodeContext` dataclass, `@node` decorator) AND the refs unification utility shared across agents/tools/workflows (`build_ref`, `parse_three_segment`, `is_three_segment`, `AssetCatalog.lookup`), with `_TYPE_TO_SEGMENT` extended for `workflow` + `node`. No parsing, no validation, no CLI yet.

**Authoritative references — read BEFORE reviewing:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — THIS spec. Re-read the Step 1 section for the exact acceptance criteria.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — master design spec. Read §5.2 (decorator surface), §5.5 (`NodeContext`), §12.1 (full exception hierarchy), §12.2 (conventions).
3. **`src/tools/decorator.py`** — the `@tool` pattern that `@node` should mirror.

**Acceptance criteria to verify (Step 1):**

1.1 — `from workflows import ...` works for every class. **Verify**: open a Python REPL, import every name from the public API INCLUDING the R3 #9 RENAMED + R3 #2/#5/#6 NEW + Round 4 #11/#13 NEW + Round 6 S1/S5 NEW classes: `WorkflowJinjaRefUnknown`, `WorkflowBranchMissingArm`, `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, `WorkflowInvalidTimeout`, `WorkflowInvalidRef`, `WorkflowBuiltinShadowed`, `WorkflowValidationWarning` (R3 #6 RESTORED), `WorkflowTimeoutShadowsNode`, `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded`, **`WorkflowBindCollision`**, **`WorkflowBindTypeMismatch`** (Round 4 item #11), **`WorkflowFromShapeError`**, **`WorkflowFromAssetTypeError`**, **`WorkflowFromNotFound`** (Round 4 item #13), **`WorkflowJinjaSecurityError`** (Round 6 S1 NEW), **`WorkflowExtendsPolicyForbidden`** (Round 6 S5 NEW). None should raise. Verify ALSO that `WorkflowStateKeyCollision` is NOT importable (still deleted); the OLD names `WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide` are ALSO NOT importable (renamed away — no compatibility aliases per R3 #9). **`WorkflowMultiInputForbidden` is NOT importable** (REMOVED per Round 4 item #11). **`WorkflowFromShapeWarning` is NOT importable** (REMOVED per Round 4 item #13). **`WorkflowBindMustBeJinja` is NOT importable** (REMOVED per Round 6 J5/β — binds: now accepts YAML literals OR Jinja-marked strings).
1.2 — Every exception instantiable; `str(exc)` STRICT single line. **Verify**: instantiate at least 5 exception classes with their attributes; print `str(exc)`; assert `"\n" not in str(exc)`. For at least 2 YAML-origin classes, assert `str(exc)` contains a `<file>:<line>` token AND the literal `"Fix: "` token (capital F, colon, space). ALSO assert STRICT-no-code: `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)` for every YAML-origin class checked (per master spec §12.3 strict rule).
1.3 — Wrapped causes via `__cause__`. **Verify**: raise `WorkflowNodeFailed(...) from ValueError("x")`; confirm `exc.__cause__` is the `ValueError`.
1.4 — `@node(secrets_needed=[...])` sets sentinel. **Verify**: decorate a function; inspect the sentinel attribute; secrets list matches.
1.5 — Bare `@node` works. **Verify**: decorate; sentinel attribute has empty/None secrets.
1.6 — Non-callable raises `TypeError`. **Verify**: `@node(secrets_needed=[])\nclass Foo: pass` should raise `TypeError`.
1.7 — `NodeContext` is `@dataclass(frozen=True, slots=True)`; `secrets: Mapping[str, str]`. **Verify**: instantiate (a plain dict is acceptable as `secrets` because dict IS a Mapping); mutating any field raises `dataclasses.FrozenInstanceError`; field set matches master spec §5.5; confirm `slots=True` via `NodeContext.__slots__` (or attempting attribute addition).
1.8 — `WorkflowJinjaRefUnknown.reason` discriminator (R3 #9 renamed). **Verify**: instantiate with each of `"not_predecessor"`, `"unknown_field"`, `"not_declared"`; assert `str(exc)` differs visibly per reason. Constructing without `reason` raises.
1.9 — Refs unification builders/parsers. **Verify**: `build_ref(...)` round-trips for ALL FOUR asset types (tool/agent/workflow/node) — 3-seg is canonical for all of them; `parse_three_segment` round-trips; `is_three_segment` boolean cases. ALSO: `from assets.refs import parse_ref` should FAIL (`parse_ref` is removed entirely).
1.10 — `AssetCatalog.lookup`. **Verify**: known ref returns entry; unknown returns None; wrong `expected_type` returns None; malformed shape returns None.
1.11 — **No inline ref construction**: run `grep -rn 'f"{[^}]*}/{[^}]*}/{[^}]*}"' src/assets/catalog.py src/providers/authz.py src/engine/observers.py` — must return ZERO matches. Read the diff to confirm all 4 `_scan_*` functions in `catalog.py` use `build_ref`, and `authz.py`'s `_validate_what` + `_build_action_to_tool_ref` route through `parse_three_segment` + `catalog.lookup`.
1.12 — Two-segment tool ref migration shim untouched. **Verify**: run the full existing authz test suite; all passes.

**Tests to run (record actual output):**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_scaffolding.py tests/component/test_refs_unification.py -v`
- **Expect:** all tests pass.

- **Run (regression check):** `podman-compose exec dev uv run pytest tests/component/ -q`
- **Expect:** all existing tests pass — especially `test_authz*.py` and `test_catalog*.py`, which exercise the refactored ref pathways.

**Review checklist:**

- [ ] All 18 Step 1 acceptance criteria demonstrably met
- [ ] Test suite passes (record output)
- [ ] Exception hierarchy matches master spec §12.1 EXACTLY — INCLUDING the R3-renamed/new + Round 4 #11/#13 new + Round 6 S1/S5 new classes (`WorkflowJinjaRefUnknown`, `WorkflowBranchMissingArm`, `WorkflowNodeNotDefined`, `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, `WorkflowInvalidTimeout`, `WorkflowInvalidRef`, `WorkflowBuiltinShadowed`, `WorkflowTimeoutShadowsNode`, `WorkflowTimeoutExceeded`, `NodeTimeoutExceeded`, **`WorkflowBindCollision`**, **`WorkflowBindTypeMismatch`**, **`WorkflowFromShapeError`**, **`WorkflowFromAssetTypeError`**, **`WorkflowFromNotFound`**, **`WorkflowJinjaSecurityError`** (Round 6 S1), **`WorkflowExtendsPolicyForbidden`** (Round 6 S5)). `WorkflowValidationWarning` IS in the hierarchy (R3 #6 RESTORED). `WorkflowStateKeyCollision` is DELETED — verify it's NOT in `src/workflows/errors.py`. **`WorkflowMultiInputForbidden`** is NOT in `errors.py` (Round 4 item #11 REMOVED). **`WorkflowFromShapeWarning`** is NOT in `errors.py` (Round 4 item #13 REMOVED — promoted to hard errors). **`WorkflowBindMustBeJinja`** is NOT in `errors.py` (Round 6 J5/β REMOVED — binds: now accepts YAML literals OR Jinja-marked strings). OLD names (`WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide`) are NOT present (no compatibility aliases per R3 #9)
- [ ] **Round 4 item #5 — Explicit `__all__` in `src/workflows/__init__.py`**: list literal exists; ruff (RUF022 + project-local custom check) passes; deliberate desync causes ruff failure
- [ ] **Round 4 item #10 + Round 5 R1 + Round 7 L7-3 — `NodeContext.emit` method**: `inspect.signature(NodeContext.emit)` returns `(self, subkind: str, payload: Mapping[str, Any]) -> None` (first positional named `subkind` per Round 5 R1, NOT `kind`); PUBLIC `emit_fn` field with `repr=False` (renamed from `_emit_fn` per Round 7 L7-3 — public test seam); `ctx.emit(...)` delegates to `emit_fn` via internal call `self.emit_fn(self.bundle, subkind, payload)`
- [ ] **Round 6 R6-R1 — `NodeContext.emit` docstring documents the `node.<bundle>.<subkind>` wire kind** (the "author" segment from R5 R1 is DROPPED); the engine event kinds all start with `workflow.`/`policy.`/`model.`/`tool.` so `node.*` is unambiguously the author namespace
- [ ] **Round 6 S4 + Round 7 L7-3 — `NodeContext.bundle` PUBLIC field**: `bundle: str` set by engine at construction (renamed from `_bundle` per L7-3 — public test seam); `frozen=True` keeps it read-only; docstring documents spoof-resistance contract
- [ ] **Round 7 A7-1 — `NodeContext.for_test(**overrides)` classmethod**: sensible defaults for all 7 fields; ~30 LoC; PUBLIC API; listed in `__all__`; amends R6 J3 D-decision; reviewer asserts `NodeContext.for_test()` constructs cleanly and overrides flow through
- [ ] **Round 7 A7-6 — `NodeContext.state: Mapping[str, Any]` field**: read-only; engine wraps in `MappingProxyType`; reviewer asserts `ctx.state["foo"]` works and mutating is forbidden; `for_test(state=...)` accepts override
- [ ] **Round 7 L7-8 — `display_label(node, type_label=None)` public helper** ships in `src/workflows/labels.py`; re-exported from `kolsch.workflows`; listed in `__all__`; 3-step fallback per §15.4; 3 unit tests (one per branch)
- [ ] **Round 7 CV1 — `from_json` Jinja filter** registered via `_register_builtin_filters()` in `src/workflows/jinja_env.py` (~5 LoC); singleton round-trips JSON to native Python types; filter set is CORE-ONLY (contribution doc at `docs/explanation/jinja-extensions.md`)
- [ ] **Round 6 S1 — `src/workflows/jinja_env.py` module**: singleton sandboxed env subclassing `ImmutableSandboxedEnvironment` with native-type returns preserved; `WorkflowJinjaSecurityError` raised on sandbox blocks; `tests/security/test_jinja_sandbox_escape.py` covers ~10 canonical escape payloads
- [ ] **Round 6 J5/β — `WorkflowBindMustBeJinja` REMOVED**: NOT in `errors.py`; NOT importable; grep returns ZERO matches
- [ ] **R3 #8 — `caller_message: ClassVar[str]` on every exception class**: assert non-empty; assert NO class name in message; assert `"{{" not in caller_message and "{%" not in caller_message`; assert no `<file>:<line>` token; 1-3 sentences
- [ ] **R3 #11 — `identifying_names_keys: ClassVar[tuple[str, ...]]` on every exception class**: assert every listed key is a real dataclass/init field on the class
- [ ] **R3 #10 — Typed prefixed IDs in `src/workflows/ids.py`**: `TraceId(prefix='tr_')`, `WorkflowRunId(prefix='wrk_')`, `InvocationId(prefix='inv_')`, `ToolCallId(prefix='tc_')` — verify each via `from workflows.ids import ...`; verify module docstring documents UUID4 invariant + layered ID ownership
- [ ] **R3 #5 — `resolve_shorthand` is REMOVED** from `src/assets/refs.py`; grep `resolve_shorthand` returns ZERO matches across `src/`. `core` reservation also removed (grep `'core' in RESERVED` or equivalent returns ZERO)
- [ ] YAML-origin exception messages embed `<file>:<line>` + identifying name + the literal `"Fix: "` token (assert with string contains)
- [ ] **STRICT-no-code in Fix portion** (per master spec §12.3): for every YAML-origin error class, assert `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`. No Jinja delimiters, no backticks, no YAML literal fragments.
- [ ] No `\n` in any `str(exc)` anywhere; NO `exc.format()` method
- [ ] NO custom `WorkflowValidationErrorGroup` class — multi-error uses stdlib `ExceptionGroup`
- [ ] No error codes anywhere (no `code` attribute, no enum, no lookup)
- [ ] `@node` mirrors `src/tools/decorator.py` patterns
- [ ] **Refs unification verified**: `grep -rn 'f"{[^}]*}/{[^}]*}/{[^}]*}"' src/assets/catalog.py src/providers/authz.py src/engine/observers.py` returns ZERO matches. `_TYPE_TO_SEGMENT` includes `"workflow": "workflows"` and `"node": "nodes"`. `AssetCatalog.lookup` exists with the right signature. `parse_ref` is REMOVED from `src/assets/refs.py` (grep `parse_ref` returns ZERO matches — no compatibility alias).
- [ ] No `core/principals/user` or `core/principals/system` ref types added — principals stay engine telemetry
- [ ] No scope creep (no parser models, no validator gate, no CLI code in Step 1 — refs unification is in scope; refs USAGE in workflow catalog/authz LAYER is Step 3 / Step 4)
- [ ] **Round 5 R1 — `NodeContext.emit(subkind, payload)`** signature confirmed; docstring documents engine-side `node.author.<bundle>.<subkind>` auto-prepend; reviewer asserts `inspect.signature(NodeContext.emit)` shows the renamed `subkind` parameter
- [ ] **Round 5 H3 — `AssetCatalog.lookup` chokepoint** present with docstring discipline; planned custom ruff lint rule documented (flags `catalog.assets[` / `catalog._<private>[` access outside `src/assets/catalog.py`); `src/assets/asset_manager.py:188` migrated to `.lookup(...)`; reviewer greps the diff for any newly-introduced `.assets[...]` access OUTSIDE `src/assets/catalog.py` (must be ZERO)
- [ ] No regressions in authz / catalog / engine tests (refactor is structural-only)

**Handoff trigger:**
- All boxes checked → reply "Step 1 Approved"
- Anything fails → reply "Changes requested — [itemized list]". Each item must cite the violated acceptance criterion (1.1–1.12) OR the master spec section (e.g., "master spec §12.1 includes `WorkflowNodeRefUnknown` but it's missing").

---

# STEP 2 — YAML parser

**Goal**: parse workflow YAML into typed Pydantic models. Cover all 14 built-in node types' config shapes per master spec §4 (final built-in catalog with R3 #9 renames applied: **bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input**) PLUS the generic `CatalogNodeDef` (Round 4 item #3 — renamed from `CatalogNodeRef` for *Def symmetry with `BashNodeDef`, `MapNodeDef`, etc.) parser variant for authored Python nodes (`type: <bundle>/nodes/<name>`). Structural validation only — deeper checks (DAG, binds, branching, predecessor-via-reachability) land in the validator (Step 5). YAML is loaded via the `src/_yaml.py` helper (Step 0 prereq). All `type:` values are **full 3-seg refs** (per R3 #5 — 1-seg shorthand is removed); built-ins use `kolsch/nodes/<name>` (e.g., `type: kolsch/nodes/bash`).

**Per Round 4 item #2**: the `CatalogNodeDef` dispatcher lives in its OWN module at `src/workflows/catalog_node.py` (per master spec §3.1), NOT folded into `parser.py`. `parser.py` imports `CatalogNodeDef` from there.

**Per Round 4 item #11 (multi-input redesign)**: the `EdgeDef` Pydantic model is `{from, to, binds, branch, when}` — the prior `ref:` field is DROPPED entirely. Multi-input edges are now the v1 fan-in mechanism (no longer forbidden). Two edges binding the same key into the same target node raise `WorkflowBindCollision` at parse/validate time. Runtime Pydantic strict-mode bind validation raises `WorkflowBindTypeMismatch` at node-fire time (Phase 3.1+).

**Per Round 6 J5/β (Jinja-evaluated fields accept YAML literals OR `{{ }}`-marked strings)**: SUBSTANTIVE DESIGN CHANGE reversing R4 #11's strict-binds rule. Universal rule across ALL Jinja-evaluated config fields:
- Bare YAML literal → the YAML value as-is (string, int, float, list, bool, null) — passed unchanged to Pydantic strict validation (where the receiver's `Inputs` model applies at runtime per Phase 3.1).
- `"{{ ... }}"` → Jinja-evaluated under the sandboxed singleton env (Round 6 S1, `src/workflows/jinja_env.py`).
Applies to: `binds:` RHS values, `set_state.keys` values, `compute.value`, `filter.condition`, `branch.condition`, `node-level when:`, edge-level `when:`, and `map.over:`. The legacy `source.field` shorthand STAYS REMOVED. **`WorkflowBindMustBeJinja` is REMOVED** (R3 #9 renamed exception class is gone in Round 6). Updated principle: "For Jinja evaluation, use `{{ }}`. Otherwise YAML literals are accepted." (was: "ONE syntax everywhere: `{{ }}`".) Parsing distinction:
- `binds: {url: "https://example.com"}` → bare YAML string literal `"https://example.com"`; passed to the receiver as the string.
- `binds: {count: 5}` → bare YAML int literal `5`; passed as int.
- `binds: {url: "{{ nodes.fetch.url }}"}` → Jinja-evaluated at node-fire; the rendered value is what flows.
- `set_state: {keys: {status: pending, attempts: 0}}` → bare literals; `status` gets the string `"pending"`, `attempts` gets the int `0`. No Jinja evaluation.

**Files created/modified:**
- `src/workflows/parser.py` (new) — Pydantic models + `load_workflow(path) -> Workflow`
- `src/workflows/catalog_node.py` (new — Round 4 item #2) — standalone module hosting `CatalogNodeDef` and the 3-seg `type:` dispatcher
- `tests/component/test_workflow_parser.py` (new) — parser unit tests

**Effort:** ~1 day.

## Step 2 — Coder Prompt

> You are implementing Step 2 of a 7-step phase. Step 1 (scaffolding) is complete and
> approved. Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Build the YAML parser. Define Pydantic models for `Workflow` (with top-level `timeout_seconds: int | None`), `NodeDef` (discriminated union over the FINAL 14 BUILT-IN node types — R3 #9 renames applied — PLUS a generic `CatalogNodeDef` variant for authored Python nodes; common base carries `description: str | None` and `timeout_seconds: int | None`), and `EdgeDef` (per Round 4 item #11: `{from, to, binds, branch, when}` — NO `ref:` field). Implement `load_workflow(path: Path) -> Workflow` that parses YAML via `src/_yaml.py` and validates the top-level shape. Step 2 does ONLY shape validation — DAG checks, bind resolution, branching invariants, bind-key collision detection are Step 5's job.

The 14 built-in node types are (R3 #9 renames applied): **bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input**. The `python` discriminator is REMOVED — authored Python nodes are referenced directly by their three-segment catalog ref (`type: <bundle>/nodes/<name>`), parsed into a generic `CatalogNodeDef` Pydantic model (Round 4 item #3 — renamed from `CatalogNodeRef` for *Def symmetry with `BashNodeDef`, `MapNodeDef`, etc.) carrying only common fields (`description`, `timeout_seconds`, `when`, `retry`, `secrets`); type-specific config is validated against the node's declared input model AFTER catalog lookup (Step 5). The 8 aggregations are NOT in the catalog. The new `compute` node carries `{value: str (Jinja expression), description: str | None}` (R3 #7 renamed from `summary:`) and emits `{value: <evaluated>}`. The 8 aggregation idioms are now stock Jinja-filter recipes in author docs.

**Round 4 item #2 — Standalone catalog_node module**: `CatalogNodeDef` and the 3-seg `type:` dispatcher live in `src/workflows/catalog_node.py` (per master spec §3.1), NOT in `parser.py`. `parser.py` imports `from workflows.catalog_node import CatalogNodeDef, dispatch_node_type`. This keeps the dispatcher module small, focused, and independently testable.

**Round 4 item #11 — `EdgeDef` shape**: `{from, to, binds, branch, when}`. The `ref:` field is DROPPED. Multi-input edges (multiple edges pointing at the same `to`) are LEGAL — they are the v1 fan-in mechanism. Engine semantics (Phase 3.1): wait for ALL incoming edges' source nodes to complete (AND-strict); `binds:` keys flow into an **`inputs.*`** YAML namespace at the receiver (Round 5 H4 — renamed from `input.*` to plural for consistency with the Python `inputs: ModelType` param convention). Parse-time: Step 5 validator checks that no two edges to the same target node bind the same key (`WorkflowBindCollision`). Runtime (Phase 3.1): rendered bind values are Pydantic-strict-validated against the receiver's input model (`WorkflowBindTypeMismatch` on failure; e.g., 'int' rejects '"5"').

**Dispatcher rule (R3 #5)**: all `type:` values are full 3-seg refs (no 1-seg shorthand — `resolve_shorthand` is removed). If `type:` matches `kolsch/nodes/<name>` where `<name>` is one of the 14 built-in catalog slots → that built-in's Pydantic model. If 3-seg ref matching `parse_three_segment` but not a built-in slot → `CatalogNodeDef` generic model. Otherwise (non-3-seg, including bare `type: bash`) → `WorkflowInvalidRef` with a fix-path block ("Fix: use a full 3-seg ref such as 'kolsch/nodes/bash'; see docs/workflows#ref").

**RESERVED catalog slots (R3 #5)**: The 14 specific catalog refs `kolsch/nodes/{bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input}` are RESERVED slots. User authoring at `bundles/kolsch/nodes/bash.py` raises `WorkflowBuiltinShadowed` at catalog scan time (Step 3). A bundle NAMED `bash`/`agent`/etc. is fine (R3 #5 dropped bundle-name reservation). `core` is no longer reserved.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — THIS spec. Read Step 2 end-to-end. Note the boundary between parsing (shape) and validation (semantics) — do not cross it.

2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — master design spec. For Step 2 you need:
   - **§6.1 Top-level structure** — the `Workflow` shape
   - **§6.2 Workflow kind inference** — runnable iff the `graph:` key is PRESENT in the parsed YAML (any value of `edges:`, including empty `[]`); node_library iff the `graph:` key is ABSENT entirely. A single-node workflow with `graph: { edges: [] }` IS runnable (per polish-pass Decision P3). Empty `edges:` is NOT a node-library marker.
   - **§6.3 NodeDef shape** — common fields (`type`, `copy_of` [RENAMED from `instance_of`], `condition` [NODE-LEVEL primary config on `branch`/`filter` only], `retry`). The old node-level `collect:` field is REMOVED (moved to edge as `ref:` — see §6.4).
   - **§6.4 Edge shape** — `from`, `to`, `binds`, optional `branch`, optional `when` [RENAMED from edge-level `condition:`]. The prior `ref: list[str]` field is DROPPED (Round 4 item #11 — multi-input edges are now the fan-in mechanism; predecessor closure is computed from graph reachability, not from a `ref:` declaration).
   - **§4 Node Type Catalog** — config fields for all 14 BUILT-IN types (the 8 aggregations are gone; `compute` is new; `python` discriminator is removed in favor of `CatalogNodeDef` per Round 4 item #3)
   - **Output shapes table** in master spec §4 — `compute → {value: <evaluated>}`, `branch → {value: bool}` (EXPLICIT), `state_delete → {}` (EXPLICIT)

3. **`src/schemas/config.py`** — existing Pydantic patterns in kolsch. Reuse `IOSpec` (line 58) for the agent node's `outputs:` override per master spec §4.1.3.

**What to implement:**

1. **`src/workflows/parser.py`**:
   - `Workflow` top-level Pydantic model with fields per master spec §6.1: `description: str = ""`, `extends: list[str] = []`, **`policy: list[Rule] = []`** (Round 5 H1 — renamed from `permissions:` to `policy:` as the unified user-facing term; matches OPA/Cerbos/AWS IAM Policy idiom; SINGULAR per H1 lock), `nodes: dict[str, NodeDef]`, `graph: GraphDef | None`, **`timeout_seconds: int | None = None`** (R3 #2 — top-level workflow-level cooperative deadline; positive int validated; non-positive → `WorkflowInvalidTimeout` at Step 5).
   - `GraphDef` model with `edges: list[Edge]`. **Kind inference rule (per polish-pass Decision P3):** absence of the top-level `graph:` key → `kind="node_library"`; presence of `graph:` (even with `edges: []`) → `kind="workflow"`. Empty `edges:` does NOT mark a library. The parser must distinguish "no `graph:` key" from "`graph: { edges: [] }`" — track this on the `Workflow` model (e.g., `graph: GraphDef | None` where `None` means the key was absent).
   - `NodeDef` as a discriminated union of all 14 BUILT-IN types PLUS a generic `CatalogNodeDef` variant (`BaseNodeDef` + per-type subclasses with a `type:` discriminator). All `type:` values are full 3-seg refs (per R3 #5). **Per Round 4 item #2 + #3**: `CatalogNodeDef` (renamed from `CatalogNodeRef` for *Def symmetry) and the 3-seg dispatcher live in their own module `src/workflows/catalog_node.py` (per master spec §3.1); `parser.py` imports from there. Custom dispatcher: if `type:` matches `kolsch/nodes/<name>` for `<name>` in the 14-name catalog → built-in model; if `type:` is any other 3-seg ref → `CatalogNodeDef`; if `type:` is NOT 3-seg (e.g., bare `type: bash`) → `WorkflowInvalidRef` with a fix-path block ("Fix: use a full 3-seg ref such as 'kolsch/nodes/bash'; see docs/workflows#ref").
   - The 14 BUILT-IN type variants (R3 #9 renamed Pydantic class names): `BashNodeDef`, `AgentNodeDef`, `WorkflowNodeDef`, `NoopNodeDef`, `ReadFileNodeDef`, `WriteFileNodeDef`, `ListFilesNodeDef`, **`SetStateNodeDef`** (R3 #9 renamed from `StateSetNodeDef`), **`DeleteStateNodeDef`** (R3 #9 renamed from `StateDeleteNodeDef`), `FilterNodeDef`, **`MapNodeDef`** (R3 #9 renamed from `MapperNodeDef`), `BranchNodeDef`, `ComputeNodeDef`, `UserInputNodeDef`. NO `PythonNodeDef`. NO aggregation variants.
   - **Round 5 H8 — Per-built-in `Inputs(BaseModel)` models**: each built-in `*NodeDef` class also declares an associated `Inputs(BaseModel)` class (paired 1-to-1; lives alongside the NodeDef in the same module — e.g., `BashInputs` next to `BashNodeDef`). The `Inputs` class declares the Pydantic input model used at runtime (Phase 3.1+) for strict-mode bind validation against rendered bind values (the `WorkflowBindTypeMismatch` runtime contract). Phase 3.0 lands the CLASSES + `extra=` config + strict-typed fields per the table below; the strict validation itself fires at node-fire time in Phase 3.1+. The per-built-in `Inputs` models give Phase 3.1 a typed receiver shape per built-in:

     | Type | Inputs class | `extra=` | Strict-typed fields |
     | --- | --- | --- | --- |
     | bash | `BashInputs` | `"allow"` | none (binds are author-named) |
     | agent | `AgentInputs` | `"allow"` | none |
     | workflow (sub-wf) | `WorkflowSubInputs` (renamed to avoid name collision with the `workflow` ASSET type) | `"allow"` | none |
     | noop | `NoopInputs` | `"forbid"` | none (no inputs accepted) |
     | user_input | `UserInputInputs` | `"allow"` | none |
     | read_file | `ReadFileInputs` | `"forbid"` | (`path: str` already on the NodeDef's config field — Inputs model declares no additional fields) |
     | write_file | `WriteFileInputs` | `"forbid"` | (`path: str`, `content: str \| bytes` on NodeDef — Inputs declares no additional fields) |
     | list_files | `ListFilesInputs` | `"forbid"` | (`path: str`, `pattern: str \| None` on NodeDef — Inputs declares no additional fields) |
     | set_state | `SetStateInputs` | `"allow"` | none |
     | delete_state | `DeleteStateInputs` | `"forbid"` | none |
     | filter | `FilterInputs` | `"allow"` | `items: list[Any]` |
     | map | `MapInputs` | `"allow"` | `over: list[Any]` |
     | branch | `BranchInputs` | `"allow"` | none |
     | compute | `ComputeInputs` | `"allow"` | none |

     Convention: in practice most built-ins use `extra="allow"` with no strict fields because bind keys are author-named — strict mode mostly catches authored Python nodes (whose declared Pydantic input models are tight by design) and the built-ins above with `extra="forbid"`. Per-built-in tightening (adding more strict-typed fields to `BashInputs`, etc.) is queued as a Phase 3.x QoL once real workflow patterns emerge.
   - `CatalogNodeDef` generic model (Round 4 item #3 — renamed from `CatalogNodeRef`): discriminator is the 3-seg ref STRING in `type:`; the model itself carries only common fields (`description`, `timeout_seconds`, `when`, `retry`, `secrets`). Type-specific config validation deferred to Step 5. No `body:` field — the `type:` value IS the catalog ref. Defined in `src/workflows/catalog_node.py` (Round 4 item #2 — standalone module).
   - `ComputeNodeDef` config: `value: Any` (Round 6 J5/β — YAML literal OR Jinja-marked string `"{{ expr }}"`; only the latter triggers Jinja evaluation in the validator + runtime) + `description: str | None = None` (R3 #7 renamed from `summary:` — now optional on compute too, since it's a common-base field). Output schema constant: `{value: <any>}` (validator pin in Step 5).
   - `BranchNodeDef`: primary config field `condition: Any` (Round 6 J5/β — YAML literal OR Jinja-marked string; a literal `true`/`false` is valid and evaluates as-is). Output schema: `{value: bool}` (EXPLICIT — replaces prior "implicit bool").
   - `FilterNodeDef`: primary config field `condition: Any` (Round 6 J5/β — YAML literal OR Jinja-marked string). Output schema: `{result: list[T]}`.
   - `MapNodeDef.over`: `Any` (Round 6 J5/β — YAML literal list OR Jinja-marked string `"{{ ... }}"` rendering to a list).
   - `SetStateNodeDef.keys`: `dict[str, Any]` (Round 6 J5/β — values may be YAML literals OR Jinja-marked strings; literal forms are natural: `set_state: {keys: {status: pending, attempts: 0}}` parses cleanly).
   - Node-level common `when: Any | None` modifier (Round 6 J5/β — YAML literal OR Jinja-marked string).
   - `DeleteStateNodeDef`: output schema constant `{}` (EXPLICIT, fix prior under-spec).
   - Common modifier fields on `BaseNodeDef`: `description: str | None = None` (R3 #7 NEW — optional on ALL 14 types; `min_length=1` validation; empty strings rejected), `timeout_seconds: int | None = None` (R3 #2 NEW — per-node cooperative timeout; positive int validation; failures fail-node-only at runtime with `NodeTimeoutExceeded`; retry policy applies), `condition: str | None` (node-level, used as primary config by `branch`/`filter` only — DO NOT promote to a modifier on all types), `retry: RetryDef | None`, `copy_of: str | None` (RENAMED from `instance_of`). The old `collect: list[str]` field is REMOVED from NodeDef.
   - `EdgeDef` model (per Round 4 item #11 — multi-input redesign + Round 6 J5/β — binds accepts literals OR Jinja): `from: str`, `to: str`, `binds: dict[str, Any] = {}` (Round 6 J5/β — value type is `Any`: YAML literal OR `"{{ ... }}"` Jinja-marked string; NOT `dict[str, str]` requiring Jinja markers), `branch: bool | None = None`, `when: str | Any | None = None` (RENAMED from edge-level `condition:`; per Round 6 J5/β accepts YAML literal OR Jinja-marked string — bare literals evaluate as truthy / falsy unchanged). **The prior `ref: list[str]` field is DROPPED entirely** — multi-input edges are now the v1 fan-in mechanism; predecessor closure is computed from graph reachability (Step 5 validator); the `ref:` validation-hint list is no longer needed and `EdgeDef` rejects it with `extra="forbid"`.
   - **Binds parsing distinction (Round 6 J5/β)**: parser stores every `binds:` value as-is from YAML. Step 5's `_check_jinja_refs` walks ONLY values that look like Jinja expressions — strings containing `"{{"`. Pure YAML literals (any non-string, OR a string with no `"{{"` substring) are passed through unchanged. The runtime (Phase 3.1+) renders the Jinja-marked strings under the sandboxed singleton env (Round 6 S1); literals flow as-is. The legacy `source.field` bare shorthand STAYS REMOVED — bare strings like `"foo.bar"` are now treated as the literal string `"foo.bar"` (NOT a ref shorthand), since J5/β reverses R4 #11's strict-binds rule. **`WorkflowBindMustBeJinja` is REMOVED** — no parse-time error fires for bare values. (`SetStateNodeDef.keys`, `ComputeNodeDef.value`, `FilterNodeDef.condition`, `BranchNodeDef.condition`, `MapNodeDef.over`, and node-level `when:` follow the same rule — see below.)
   - **Non-3-seg `type:` rejected** (R3 #5): any `type:` value that is not a full 3-seg ref raises `WorkflowInvalidRef` (e.g., `type: bash` → "Fix: use 'kolsch/nodes/bash'"). `resolve_shorthand` is NOT called anywhere in the parser (it's been removed from `src/assets/refs.py`).
   - Top-level loader `load_workflow(path: Path) -> Workflow`:
     - Reads YAML via `from _yaml import load` (the Step 0 helper) — `YAML(typ='safe')` under the hood.
     - Surfaces YAML errors with file path + line number from the ruamel.yaml exception type.
     - Validates against `Workflow` model.
     - Unknown top-level keys → `WorkflowValidationError` (Pydantic's `extra="forbid"` on the model).
   - Use `IOSpec` from `src/schemas/config.py:58` for the `agent` node's optional `outputs:` override.

2. **`tests/component/test_workflow_parser.py`**:
   - Parse hand-written valid YAML snippets covering: minimal workflow, all leaf execution types (`kolsch/nodes/bash`/`kolsch/nodes/agent`/`kolsch/nodes/user_input`/`kolsch/nodes/noop`), all I/O types (`kolsch/nodes/read_file`/`kolsch/nodes/write_file`/`kolsch/nodes/list_files`), state mutators (R3 #9 renames: `kolsch/nodes/set_state`/`kolsch/nodes/delete_state`), `kolsch/nodes/filter`, `kolsch/nodes/branch`, **`kolsch/nodes/map`** (R3 #9 renamed) + sub-workflow (`kolsch/nodes/workflow`), `kolsch/nodes/compute` (replaces aggregations), authored Python node via `CatalogNodeDef` (Round 4 item #3 — renamed; `type: acme/nodes/echo` from a non-kolsch bundle), node library (no graph).
   - Each of the 14 BUILT-IN node types appears at least once (all referenced via `kolsch/nodes/<name>`). One additional `CatalogNodeDef` test for an authored Python node.
   - Malformed YAML → clear error with path + line.
   - Unknown top-level key → `WorkflowValidationError`.
   - **Non-3-seg `type:` rejected (R3 #5)**: `type: bash` (bare 1-seg) raises `WorkflowInvalidRef` with fix-path "Fix: use 'kolsch/nodes/bash'". `type: sum` (1-seg, dropped aggregation) raises `WorkflowInvalidRef`. `type: python` (1-seg, dropped discriminator) raises `WorkflowInvalidRef` with fix-path "Fix: reference an authored Python node by its full 3-seg ref such as 'kolsch/nodes/echo'".
   - **Top-level `timeout_seconds: 60` parses (R3 #2)**. `timeout_seconds: -5` parses cleanly but Step 5 validator rejects with `WorkflowInvalidTimeout`. `timeout_seconds: 0` similarly Step 5-rejected.
   - **Per-node `timeout_seconds: 30` parses (R3 #2)** on common `BaseNodeDef` — any node type accepts this modifier.
   - **Common `description:` parses (R3 #7)**: any node type accepts optional `description: "human-readable label"`. `description: ""` (empty) rejected via `min_length=1` validation. `description:` absent works (defaults to None).
   - Node-level `copy_of:` parses as alternative to `type:` (and `instance_of:` is REJECTED — the old keyword is gone).
   - **Edge-level `ref:` is REJECTED** (Round 4 item #11 — the field is gone). `EdgeDef` has `extra="forbid"`; any YAML edge carrying `ref: [...]` raises `WorkflowValidationError` with a fix-path explaining that fan-in is now expressed via multi-input edges (just add another edge to the same target). The OLD node-level `collect:` field is also REJECTED at the NodeDef level — fix-path suggests adding additional edges (or a `branch` for OR-semantics).
   - **Multi-input edges parse cleanly** (Round 4 item #11): two `EdgeDef` entries with the same `to` value and different `from` values are LEGAL at parse time. Bind-key collision detection across these edges is Step 5's job (`WorkflowBindCollision`).
   - Edge-level `when: "{{ ... }}"` parses (old `condition:` on EDGES is REJECTED at the edge level — but node-level `condition:` STILL parses for `branch` and `filter` nodes).
   - `binds: {x: "{{ nodes.foo.bar }}"}` parses (Jinja-marked — will be rendered at runtime). `binds: {x: "foo.bar"}` (Round 6 J5/β) ALSO parses cleanly — the bare string is treated as the literal `"foo.bar"`, NOT as a ref shorthand; **`WorkflowBindMustBeJinja` is NOT raised** (the class is REMOVED per J5/β). `binds: {count: 5}`, `binds: {flag: true}`, `binds: {items: ["a", "b"]}`, `binds: {data: null}` — all bare YAML literals parse and pass through as-is to the receiver.
   - `compute` node config parses (`value: str`, optional `description: str | None` per R3 #7).
   - **`CatalogNodeDef` parses** (Round 4 item #3 renamed): `{type: acme/nodes/echo}` parses into a `CatalogNodeDef` instance carrying the 3-seg ref string. The model carries only common fields (`description`, `timeout_seconds`, `when`, `retry`, `secrets`). Imported from `src/workflows/catalog_node.py` (Round 4 item #2 — standalone module), NOT defined in `parser.py`.
   - `extends:` parses as list of strings.
   - `policy:` (Round 5 H1 — renamed from `permissions:`; singular per the OPA/Cerbos/AWS IAM Policy idiom) parses as list of Rule-like dicts (full policy validation is Step 4). The internal code class names — `RuleBasedAuthorizationProvider`, `PermissionLayer`, the `src/providers/authz.py` module path — STAY UNCHANGED at Phase 3.0 per the H1 lock; the code-side rename is queued as a separate QoL spec at `process/qol-policy-rename.md`. User-facing YAML field, master spec sections, and docs use `policy`; internal Python identifiers continue to use `Permission*`/`authz`/etc.

**Step 2 Acceptance Criteria:**

2.1. `Workflow` model declared with correct top-level shape per master spec §6.1, INCLUDING **`timeout_seconds: int | None = None`** (R3 #2).
2.2. EXACTLY 14 BUILT-IN node types have Pydantic model variants with correct config fields per master spec §4 (R3 #9 renames applied: bash, agent, workflow, noop, read_file, write_file, list_files, **set_state**, **delete_state**, filter, **map**, branch, compute, user_input — Pydantic class names: `SetStateNodeDef`, `DeleteStateNodeDef`, `MapNodeDef` per renames). NO `PythonNodeDef`. NO sum/count/min/max/sort/flatten/any/all variants exist.
2.3. **NodeDef dispatcher (R3 #5; Round 4 items #2 + #3)**: only full 3-seg `type:` values accepted; if `type:` matches `kolsch/nodes/<name>` for one of the 14 built-ins → built-in model; any other 3-seg → `CatalogNodeDef` (Round 4 item #3 — renamed from `CatalogNodeRef`); non-3-seg (including bare `type: bash`) → **`WorkflowInvalidRef`** with fix-path. `type: sum` → `WorkflowInvalidRef` (1-seg). `type: python` → `WorkflowInvalidRef` (1-seg; fix-path mentions full 3-seg authored-node path). **Round 4 item #2**: `CatalogNodeDef` + the dispatcher live in `src/workflows/catalog_node.py` (standalone module), NOT in `parser.py`; `parser.py` imports from there.
2.4. `copy_of:` (RENAMED from `instance_of:`) works as an alternative to `type:`; child fields override parent fields when both present. `instance_of:` is REJECTED with a fix-path block suggesting the rename.
2.5. **`EdgeDef` model** (Round 4 item #11 multi-input redesign): parses `from`, `to`, `binds`, optional `branch`, optional `when` (RENAMED from edge-level `condition:`). The prior `ref: list[str]` field is DROPPED — `EdgeDef` has `extra="forbid"`; any edge with `ref: [...]` raises `WorkflowValidationError` with a fix-path explaining that fan-in is now expressed via multi-input edges (just add another edge to the same target). Edge with bare `condition:` is REJECTED with a fix-path block suggesting `when:`. **Multi-input edges parse cleanly** — two edges with the same `to` are LEGAL at parse time.
2.6. `load_workflow(path)` returns a `Workflow` for valid YAML; raises clear error for malformed YAML. **Loader uses `from _yaml import load`** (the Step 0 helper) — NOT `yaml.safe_load`.
2.7. Workflow kind (runnable vs node_library) is inferable from the presence of the top-level `graph:` key (NOT from `graph.edges` being empty). `kind="workflow"` iff the `graph:` key is present (even with `edges: []`); `kind="node_library"` iff the `graph:` key is absent. A single-node workflow with `graph: { edges: [] }` MUST resolve to `kind="workflow"` per polish-pass Decision P3.
2.8. Unknown top-level keys → `WorkflowValidationError`.
2.9. **Round 6 J5/β — Binds accept YAML literals OR Jinja-marked strings**: every `binds:` RHS may be a bare YAML literal (string, int, float, list, bool, null) OR a string of the form `"{{ <expr> }}"`. Only the Jinja-marked form triggers Jinja evaluation; bare literals pass through as-is. **`WorkflowBindMustBeJinja` is REMOVED** — the parser no longer raises this class for bare RHS values. Pydantic strict-mode validation against the receiver's `Inputs` model applies AFTER rendering (rendered literal values are still validated against the receiver's `Inputs` model — Phase 3.1+ runtime; `WorkflowBindTypeMismatch` fires there if the rendered value fails strict validation).
2.10. **`compute` node parses**: `{type: kolsch/nodes/compute, value: "{{ items | length }}", description: "count of items"}` parses cleanly (R3 #7 `description:` replacing `summary:`). Output schema constant is `{value: <any>}`.
2.11. **Edge-level `ref:` field is REJECTED** (Round 4 item #11 — the field is DROPPED). `EdgeDef` has `extra="forbid"`; YAML edges with `ref: [...]` raise `WorkflowValidationError` with a fix-path explaining that fan-in is now expressed by adding more edges to the same target. The OLD node-level `collect:` field is ALSO REJECTED with a fix-path suggesting additional edges (or a `branch` for OR-semantics).
2.12. Node-level `condition:` (primary config field on `branch`/`filter`) parses correctly; the rename of edge-level `condition:` → `when:` does NOT affect this.
2.13. **`CatalogNodeDef` parses** (Round 4 item #3 renamed; Round 4 item #2 standalone module): `{type: acme/nodes/echo}` parses into a `CatalogNodeDef` (imported from `src/workflows/catalog_node.py`) carrying the 3-seg ref string + common fields (`description`, `timeout_seconds`, `when`, `retry`, `secrets`); type-specific config validation deferred to Step 5. **R3 #5 — `core` reservation and bundle-name reservation are REMOVED**: a bundle named `bash` is fine — `type: bash/nodes/parse` parses into a `CatalogNodeDef`.
2.14. **`description:` field common to all 14 types + `CatalogNodeDef` (R3 #7; Round 4 item #3 renamed)**: optional; `min_length=1` validation; empty strings rejected. Absent → defaults to None.
2.15. **`timeout_seconds:` field common to all 14 types + `CatalogNodeDef` (R3 #2; Round 4 item #3 renamed)**: optional positive int. Parser accepts negative/zero values (deeper validation deferred to Step 5 — `WorkflowInvalidTimeout`).
2.16. **Round 5 H8 — Per-built-in `Inputs(BaseModel)` models declared**: each of the 14 built-in `*NodeDef` modules also declares its paired `Inputs(BaseModel)` class per the locked table (`BashInputs`, `AgentInputs`, `WorkflowSubInputs`, `NoopInputs`, `UserInputInputs`, `ReadFileInputs`, `WriteFileInputs`, `ListFilesInputs`, `SetStateInputs`, `DeleteStateInputs`, `FilterInputs`, `MapInputs`, `BranchInputs`, `ComputeInputs`). Each class declares the locked `model_config = ConfigDict(extra="allow" | "forbid")` per the table, plus any locked strict-typed fields (`FilterInputs.items: list[Any]`, `MapInputs.over: list[Any]`). The classes are importable from the same module as their NodeDef (e.g., `from workflows.builtins.bash import BashNodeDef, BashInputs`). Reviewer asserts: all 14 `*Inputs` classes exist, each has the expected `model_config.extra` value per the table, the two strict-typed fields are present and correctly typed. Phase 3.0 ships ONLY the classes (no strict-mode runtime invocation yet — that fires at node-fire time in Phase 3.1+ via the `WorkflowBindTypeMismatch` runtime contract).
2.17. **Round 5 H1 — Top-level `policy:` field**: `Workflow.policy: list[Rule] = []` (renamed from `permissions:`). Parsing a YAML with the OLD `permissions:` field surfaces a fix-path-block parse error suggesting `policy:` (the alias is NOT silently accepted — author migration is the explicit goal). Internal Python identifiers like `RuleBasedAuthorizationProvider` and `PermissionLayer` remain UNCHANGED at Phase 3.0 per the H1 lock (code rename queued at `process/qol-policy-rename.md`).

**Step 2 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_parser.py -v`
- **Expect:** all parser tests pass (~25-30 cases — includes 14 built-in node-type-coverage tests, 1 `CatalogNodeDef` test per Round 4 item #3, plus rename/Jinja-only/edge-`ref:`-rejected tests).
- **Fail means:** parser is rejecting valid YAML OR accepting invalid shapes, OR one of the renames isn't enforced.

**Self-check before handoff:**

- [ ] All 17 Step 2 acceptance criteria met
- [ ] Parser test file exists and passes
- [ ] EXACTLY 14 BUILT-IN node types covered by at least one valid-parse test (enumerate to confirm; R3 #9 renamed: map/set_state/delete_state)
- [ ] **Round 5 H8 — 14 per-built-in `Inputs(BaseModel)` classes declared** (one per built-in NodeDef); reviewer enumerates all 14 by name; `model_config.extra` matches the locked table; `FilterInputs.items` and `MapInputs.over` strict fields present
- [ ] **Round 5 H1 — `Workflow.policy` field** present (renamed from `permissions:`); YAML with OLD `permissions:` field surfaces a fix-path parse error suggesting the rename
- [ ] Pydantic class names use renames: `MapNodeDef`, `SetStateNodeDef`, `DeleteStateNodeDef` (R3 #9)
- [ ] **Round 4 item #3 — `CatalogNodeDef`** (renamed from `CatalogNodeRef`) parser variant covered by a valid-parse test (`type: acme/nodes/echo`); grep for `CatalogNodeRef` returns ZERO matches in `src/workflows/`
- [ ] **Round 4 item #2 — `CatalogNodeDef` defined in `src/workflows/catalog_node.py`**, NOT in `parser.py`; `parser.py` imports from there
- [ ] **Round 4 item #11 — `EdgeDef` shape is `{from, to, binds, branch, when}`** (no `ref:` field); YAML edge with `ref: [...]` is REJECTED via `extra="forbid"`; multi-input edges (two edges with same `to`) parse cleanly
- [ ] NO `PythonNodeDef` (grep the parser source — no `python` discriminator)
- [ ] NO Pydantic variants exist for sum/count/min/max/sort/flatten/any/all (grep the parser source)
- [ ] **R3 #5 — Non-3-seg `type:` rejected with `WorkflowInvalidRef`**: bare `type: bash` → rejected; `type: sum` → rejected; `type: python` → rejected
- [ ] **R3 #5 — `core` reservation REMOVED**: bundle named `core` is fine (`type: core/nodes/echo` parses)
- [ ] **R3 #5 — Bundle-name reservation REMOVED**: bundle named `bash` is fine (`type: bash/nodes/parse` parses)
- [ ] **R3 #7 — `description:` parses on all 14 node types + `CatalogNodeDef`** (optional, `min_length=1`)
- [ ] **R3 #2 — `timeout_seconds:` parses on all 14 node types + `CatalogNodeDef`** (optional positive int; Step 5 validates)
- [ ] **R3 #2 — Top-level `Workflow.timeout_seconds`** parses (optional positive int)
- [ ] `copy_of:` works; `instance_of:` is rejected with a fix-path block
- [ ] Edge-level `when:` works; edge-level `condition:` is rejected with a fix-path block
- [ ] **Round 4 item #11 — Edge-level `ref:` field DROPPED**; YAML edges with `ref: [...]` are REJECTED with a fix-path explaining multi-input edges. Node-level `collect:` (old field) is also rejected with a fix-path block
- [ ] Node-level `condition:` on `branch`/`filter` STILL works (the rename is edge-only)
- [ ] **Round 6 J5/β — `binds:` RHS accepts YAML literals OR Jinja-marked strings**: bare `binds: {x: "foo.bar"}` parses cleanly as a string literal; `binds: {count: 5}` parses cleanly as int; `binds: {x: "{{ nodes.foo.bar }}"}` parses cleanly as Jinja-marked. `WorkflowBindMustBeJinja` is REMOVED — no parse error for bare RHS values. Same rule applies to `set_state.keys`, `compute.value`, `filter.condition`, `branch.condition`, `map.over`, and node-level + edge-level `when:`
- [ ] `compute` node parses with `value:` + `description:` (R3 #7 renamed from `summary:`)
- [ ] YAML loaded via `from _yaml import load` (Step 0 helper) — NOT `yaml.safe_load`
- [ ] Existing tests still green
- [ ] No semantic validation in parser (DAG checks, bind resolution, graph-reachability predecessor checks, bind-key collision, branching, timeout sign — those are Step 5)
- [ ] `IOSpec` reused from `src/schemas/config.py:58` for agent `outputs:` override (no new grammar invented)

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]". Otherwise → "Blocked" / "Decision needed".

## Step 2 — Reviewer Prompt

> You are reviewing Step 2 of a 7-step phase. Step 1 (scaffolding) was approved.
> Reply "Step 2 Approved" or "Changes requested — [itemized list with citations]".

**What was built in Step 2:** YAML parser with Pydantic models for `Workflow` (with new top-level `timeout_seconds`), `NodeDef` (dispatcher over EXACTLY 14 BUILT-IN types — no aggregations, no `python`; R3 #9 renames: map/set_state/delete_state — plus `CatalogNodeDef` for authored Python nodes, Round 4 item #3 renamed from `CatalogNodeRef`, hosted in standalone `src/workflows/catalog_node.py` per Round 4 item #2), `EdgeDef` (Round 4 item #11: `{from, to, binds, branch, when}` — NO `ref:` field; multi-input edges are now the v1 fan-in mechanism), and `load_workflow(path)` (uses `src/_yaml.py` helper). Renames applied: `instance_of:` → `copy_of:`, edge-level `condition:` → `when:` (node-level `condition:` preserved on `branch`/`filter`). Node-level `collect:` and edge-level `ref:` are BOTH rejected (Round 4 item #11). `binds:` RHS Jinja-only enforced via `WorkflowBindMustBeJinja` (R3 #9). R3 #5: non-3-seg `type:` rejected with `WorkflowInvalidRef`; bundle-name reservation and `core` reservation REMOVED. R3 #7: common optional `description:` on all types. R3 #2: common optional `timeout_seconds:` on all types + top-level workflow. Shape validation only; DAG/binds/branching/predecessor (graph-reachability)/timeout-sign/bind-collision checks deferred to Step 5.

**Authoritative references — read BEFORE reviewing:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — re-read Step 2.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §6.1, §6.2, §6.3, §6.4 (YAML reference), §4 (all 14 built-in node type configs).
3. **`src/schemas/config.py`** — confirm `IOSpec` is reused, not duplicated.

**Acceptance criteria to verify (Step 2):**

2.1 — `Workflow` top-level shape. **Verify**: fields match master spec §6.1 EXACTLY (no extras, no missing). Defaults match.
2.2 — EXACTLY 14 BUILT-IN node types have Pydantic variants. **Verify**: enumerate the 14 (bash, agent, workflow, noop, read_file, write_file, list_files, state_set, state_delete, filter, mapper, branch, compute, user_input). Confirm no `PythonNodeDef` and no Pydantic variants for sum/count/min/max/sort/flatten/any/all (grep source).
2.3 — `NodeDef` dispatcher. **Verify**: parse a YAML with mixed node types; each parses into the correct variant. A 3-seg `type: <bundle>/nodes/<name>` parses into `CatalogNodeDef` (Round 4 item #3 renamed). `type: sum` is rejected; error message references `compute` + Jinja idiom in its fix-path block. `type: python` is rejected; error message suggests `type: <bundle>/nodes/<name>`.
2.4 — `copy_of:` as alternative to `type:`. **Verify**: `{copy_of: foo, command: "bar"}` succeeds and inherits type from `foo`. `instance_of:` is REJECTED with a fix-path block suggesting `copy_of:`.
2.5 — `EdgeDef` model (Round 4 item #11). **Verify**: parse edges with and without `binds:`, with and without `branch:`, with and without `when:`. Edge-level `condition:` (old name) is REJECTED with a fix-path block suggesting `when:`. Edge-level `ref:` is REJECTED via `extra="forbid"` (the field is DROPPED per Round 4 item #11). Multi-input edges (two edges with the same `to`) parse cleanly. Node-level `condition:` on `branch`/`filter` STILL parses.
2.6 — `load_workflow(path)`. **Verify**: parse a malformed YAML; error message includes path AND line number. **Loader uses `from _yaml import load`** — grep the source to confirm no `yaml.safe_load` survivors.
2.7 — Kind inference. **Verify**: a workflow with the top-level `graph:` key (any value of `edges:`, including empty `[]`) is `kind="workflow"`; one without the `graph:` key entirely is `kind="node_library"`. The single-node `graph: { edges: [] }` case MUST resolve to `kind="workflow"` (polish-pass Decision P3).
2.8 — Unknown top-level key. **Verify**: add `foo: bar` at top level; raises `WorkflowValidationError`.
2.9 — **Round 6 J5/β — Binds accept literals OR Jinja-marked strings**. **Verify**: `binds: {x: "{{ nodes.foo.bar }}"}` parses (Jinja-marked); `binds: {x: "foo.bar"}` ALSO parses cleanly (bare literal — does NOT raise `WorkflowBindMustBeJinja`; the exception class is REMOVED per J5/β). `binds: {count: 5}` parses as int; `binds: {flag: true}` parses as bool. The legacy `source.field` shorthand stays removed (the parser does NOT special-case bare strings — `"foo.bar"` is literally the string `"foo.bar"`, not a ref). Pydantic strict-mode validation against the receiver's `Inputs` model applies AFTER rendering (rendered literal values are still validated against the receiver's `Inputs` model at Phase 3.1+ runtime).
2.10 — `compute` node parses. **Verify**: `{type: compute, value: "{{ items | length }}", summary: "count"}` parses; `value` and `summary` carried through.
2.11 — Edge-level `ref:` REJECTED (Round 4 item #11). **Verify**: an Edge with `ref: [a, b]` raises `WorkflowValidationError`; the fix-path explains that fan-in is now expressed via multi-input edges (just add another edge to the same target). The OLD node-level `collect:` field is also REJECTED with a fix-path suggesting additional edges (or a `branch` node for OR-semantics).
2.12 — Node-level `condition:` preserved. **Verify**: `{type: branch, condition: "{{ x > 0 }}"}` and `{type: filter, condition: "{{ item.active }}"}` both parse correctly.
2.13 — `CatalogNodeDef` (Round 4 items #2 + #3). **Verify**: `{type: acme/nodes/echo}` parses into a `CatalogNodeDef` (imported from `src/workflows/catalog_node.py`, NOT defined in `parser.py`) carrying the ref string + common fields (`description`, `timeout_seconds`, `when`, `retry`, `secrets`). Grep for `CatalogNodeRef` in `src/workflows/` returns ZERO matches. R3 #5: `{type: bash/nodes/echo}` is FINE (bundle-name reservation removed); `{type: core/nodes/echo}` is FINE (`core` reservation removed).
2.14 — Common `description:` (R3 #7): optional on all node types; `min_length=1`; empty rejected.
2.15 — Common `timeout_seconds:` (R3 #2): optional positive int on all node types + top-level `Workflow`; non-positive values pass the parser (Step 5 rejects with `WorkflowInvalidTimeout`).

**Tests to run:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_parser.py -v`
- **Expect:** all tests pass.

- **Run (regression):** `podman-compose exec dev uv run pytest tests/component/ -q`
- **Expect:** all existing tests pass.

**Review checklist:**

- [ ] All 17 Step 2 acceptance criteria demonstrably met
- [ ] EXACTLY 14 BUILT-IN node types represented in parser models AND tests with R3 #9 renamed Pydantic class names (`MapNodeDef`, `SetStateNodeDef`, `DeleteStateNodeDef`) — no `PythonNodeDef`, no sum/count/min/max/sort/flatten/any/all
- [ ] **Round 5 H8 — 14 per-built-in `Inputs(BaseModel)` classes** declared per the locked table; `model_config.extra` matches; `FilterInputs.items: list[Any]` + `MapInputs.over: list[Any]` present
- [ ] **Round 5 H1 — Top-level YAML field renamed from `permissions:` to `policy:`** in the `Workflow` model; OLD `permissions:` field rejected with a fix-path block; internal Python identifiers (`RuleBasedAuthorizationProvider`, `PermissionLayer`, `src/providers/authz.py`) UNCHANGED at Phase 3.0 (code rename queued at `process/qol-policy-rename.md`)
- [ ] **Round 4 item #3 — `CatalogNodeDef`** (renamed from `CatalogNodeRef`) parser variant exists and is exercised by tests; grep `CatalogNodeRef` in `src/workflows/` returns ZERO matches
- [ ] **Round 4 item #2 — `CatalogNodeDef` lives in `src/workflows/catalog_node.py`** (standalone module per master spec §3.1), NOT in `parser.py`
- [ ] **Round 4 item #11 — `EdgeDef` shape `{from, to, binds, branch, when}`**; YAML edge with `ref:` is REJECTED; multi-input edges parse cleanly
- [ ] **R3 #5 — Non-3-seg `type:` REJECTED**: bare `type: bash` → `WorkflowInvalidRef`; `type: python` → `WorkflowInvalidRef`; `type: sum` → `WorkflowInvalidRef`
- [ ] **R3 #5 — Bundle-name + `core` reservations REMOVED**: `type: bash/nodes/parse` and `type: core/nodes/foo` both parse
- [ ] **R3 #7 — `description:` common field** on all 14 types + `CatalogNodeRef` (optional, `min_length=1`)
- [ ] **R3 #2 — `timeout_seconds:` common field** on all 14 types + `CatalogNodeRef` + top-level `Workflow`
- [ ] `compute` node carries `value: str` + optional `description: str | None` (R3 #7 renamed from `summary:`); output shape is `{value: <any>}`
- [ ] `IOSpec` is REUSED from `src/schemas/config.py:58` — NOT duplicated/redefined
- [ ] Renames enforced: `copy_of:` accepted / `instance_of:` rejected; edge-level `when:` accepted / edge-level `condition:` rejected; **edge-level `ref:` REJECTED** (Round 4 item #11 — field DROPPED) / node-level `collect:` rejected; node-level `condition:` on `branch`/`filter` still works
- [ ] **Round 6 J5/β — `binds:` RHS accepts YAML literals OR Jinja-marked strings**; `WorkflowBindMustBeJinja` is NOT raised for bare values (the class is REMOVED). Same rule applies to `set_state.keys`, `compute.value`, `filter.condition`, `branch.condition`, `map.over`, and node-level + edge-level `when:`
- [ ] **Round 4 item #11 — Multi-input edges parse** (two edges with same `to` are LEGAL); edge-level `ref:` is REJECTED with a fix-path explaining the new fan-in mechanism
- [ ] YAML loaded via `from _yaml import load` (Step 0 helper); grep confirms no `yaml.safe_load` in `src/workflows/parser.py`
- [ ] No semantic validation (no DAG check, no bind resolution, no graph-reachability predecessor check, no bind-key collision check, no branching rules, no timeout-sign check) in the parser — those belong to Step 5
- [ ] Error messages on malformed YAML include file path AND line number
- [ ] No regressions

**Handoff trigger:**
- All boxes checked → "Step 2 Approved"
- Anything fails → "Changes requested — [itemized list]" with citations to acceptance criteria (2.1–2.12) or master-spec sections.

---

# STEP 3 — Asset catalog integration

**Goal**: auto-discover workflow YAMLs and Python node files from `bundles/<bundle>/workflows/*.yaml` and `bundles/<bundle>/nodes/*.py`. Register as catalog entries with three-segment refs. Honor `bundle.yaml`'s `exclude:` block. **Virtual `kolsch` bundle** exposes the 14 built-in nodes at `kolsch/nodes/<name>` (the RESERVED catalog slots per R3 #5). **`WorkflowBuiltinShadowed`** fires at scan time if a user authors a file at one of those reserved slots (e.g., `bundles/kolsch/nodes/bash.py`). Built-in node files are migrated to the R3 #9 renamed names: `map.py` / `set_state.py` / `delete_state.py`.

**Files created/modified:**
- `src/workflows/catalog.py` (new) — discovery + catalog registration
- `src/assets/catalog.py` (modified) — wire workflow + node asset types into existing loader
- `tests/component/test_workflow_catalog.py` (new) — discovery tests

**Effort:** ~1 day.

## Step 3 — Coder Prompt

> You are implementing Step 3 of a 7-step phase. Steps 1-2 (scaffolding, parser) are approved.
> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Auto-discover and register `workflows/*.yaml` + `nodes/*.py` files in the asset catalog. Three-segment refs: `<bundle>/workflows/<name>`, `<bundle>/nodes/<name>`, **constructed via `build_ref(bundle, "workflow"|"node", name)` from `src/assets/refs.py`** (landed in Step 1). NO inline `f"{bundle}/{plural}/{name}"` anywhere in this module — the refs unification utility is the single source of truth. Mirror how existing asset types (`agents/`, `tools/`, `skills/`) auto-discover today.

**R3 #5 — Built-in slot collision detection**: maintain a registry of 14 reserved catalog slots: `kolsch/nodes/{bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input}`. If the scanner encounters a user-authored `.py` file that would land at one of these slots (e.g., `bundles/kolsch/nodes/bash.py`), raise `WorkflowBuiltinShadowed` at scan time with a fix-path block ("Fix: rename the file — 'kolsch/nodes/bash' is a reserved built-in catalog slot; see docs/workflows#node-types"). Note: a bundle named `bash` is fine (`bundles/bash/nodes/parse.py` → `bash/nodes/parse` is OK; R3 #5 dropped bundle-name reservation). The collision check is per-slot, not per-bundle-name.

**R3 #9 — Built-in node files migrated**: rename `bundles/kolsch/nodes/mapper.py` → `map.py`, `state_set.py` → `set_state.py`, `state_delete.py` → `delete_state.py` (if these existed pre-R3; otherwise they land with the new names from day one).

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 3.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §3.2 (asset-catalog registration), §16 (asset catalog integration).
3. **`src/assets/catalog.py`** — existing asset catalog. Find how `tools` and `agents` register; mirror for workflows and nodes.

**What to implement:**

1. **`src/workflows/catalog.py`**:
   - `discover_workflows(bundle_root: Path) -> list[CatalogEntry]` — scans `<bundle_root>/workflows/*.yaml`.
   - `discover_nodes(bundle_root: Path) -> list[CatalogEntry]` — scans `<bundle_root>/nodes/*.py`.
   - **Use `build_ref` from day one**: import `from assets.refs import build_ref`; construct every catalog entry ref via `build_ref(bundle, "workflow", name)` or `build_ref(bundle, "node", name)`. ZERO inline f-strings for refs in this module.
   - Each entry: `asset_type="workflow"` or `"node"`, `ref=<build_ref(...)>`, `path=<abs path>`, `metadata={"kind": "workflow"|"node_library", "extends": [...], ...}` for workflows (kind inferred from parser); minimal metadata for nodes.
   - **Kind inference (per polish-pass Decision P3):** `metadata["kind"] = "node_library"` IFF the `graph:` key is ABSENT in the parsed YAML. If `graph:` is PRESENT (any value of `edges:`, including empty `[]`), `metadata["kind"] = "workflow"`. The previous shorthand "absent OR empty edges → node_library" is REPLACED: empty `edges:` alone does NOT mark a library — single-node workflows with `graph: { edges: [] }` are runnable.
   - Workflow files are PARSED (Step 2's `load_workflow`) during discovery; metadata extracted from the parsed `Workflow` object.
   - Node files are NOT imported during discovery — just registered by path. Import + introspection happens later (Step 5 validator).
   - Honor `bundle.yaml` `exclude:` block — existing mechanism. Find the existing helper in `src/assets/` and call it.

2. **`src/assets/catalog.py`** modifications:
   - Add `workflow` and `node` to the set of known asset types.
   - Wire `discover_workflows` + `discover_nodes` into the bundle-scan path.
   - Confirm the existing catalog loader's API accepts the new asset types without breaking existing tools/agents/skills.

3. **`tests/component/test_workflow_catalog.py`**:
   - Set up a temp workspace with a fake bundle containing both `workflows/` and `nodes/` subdirs.
   - Assert discovery finds expected entries.
   - Assert refs use three-segment form.
   - Assert `bundle.yaml` `exclude:` suppresses specific files.
   - Assert mixed bundles (only workflows, only nodes, both) work.

**Step 3 Acceptance Criteria:**

3.1. `bundles/<bundle>/workflows/*.yaml` is auto-discovered.
3.2. `bundles/<bundle>/nodes/*.py` is auto-discovered.
3.3. Workflow `CatalogEntry` has `asset_type="workflow"`, ref `<bundle>/workflows/<name>` built via `build_ref(bundle, "workflow", name)`.
3.4. Node `CatalogEntry` has `asset_type="node"`, ref `<bundle>/nodes/<name>` built via `build_ref(bundle, "node", name)`.
3.5. Workflow metadata includes inferred `kind` (`workflow` or `node_library`) and resolved `extends`. `kind="node_library"` IFF the top-level `graph:` key is ABSENT in the parsed YAML; `kind="workflow"` IFF `graph:` is PRESENT (any value of `edges:`, including empty `[]`). The catalog fixture `single-node-runnable.yaml` (one node + `graph: { edges: [] }`) MUST resolve to `kind="workflow"` and pass the validator.
3.6. `bundle.yaml` `exclude:` suppresses listed files from both asset types.
3.7. Node files are NOT imported during discovery (defer to validator).
3.8. Existing tools/agents/skills discovery still works.
3.9. **Zero inline ref construction in `src/workflows/catalog.py`**: grep finds no `f"{bundle}/{` or equivalent. All refs go through `build_ref`. (Reviewer enforces with a literal grep.)
3.10. **Catalog `lookup` works**: `catalog.lookup("acme/workflows/triage")` returns the workflow entry; `catalog.lookup("acme/workflows/missing")` returns None; `catalog.lookup("acme/workflows/triage", expected_type="node")` returns None.
3.11. **R3 #5 — `WorkflowBuiltinShadowed` fires at scan time**: a user `.py` at `bundles/kolsch/nodes/bash.py` (or any of the 14 reserved slots) raises `WorkflowBuiltinShadowed`. A bundle NAMED `bash` (e.g., `bundles/bash/nodes/parse.py`) is FINE — no collision.
3.12. **R3 #5 — Virtual `kolsch` bundle exposes 14 built-in nodes**: `catalog.lookup("kolsch/nodes/bash", expected_type="node")` resolves to the built-in entry. All 14 built-in slots resolve.
3.13. **R3 #9 — Built-in node files use renamed names**: `bundles/kolsch/nodes/map.py`, `set_state.py`, `delete_state.py` exist; no `mapper.py`/`state_set.py`/`state_delete.py` survive (grep returns ZERO matches).

**Step 3 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_catalog.py -v`
- **Expect:** all discovery tests pass.

**Self-check before handoff:**

- [ ] All 13 Step 3 acceptance criteria met
- [ ] Discovery tests pass
- [ ] Existing tests still green
- [ ] Three-segment refs used consistently — built via `build_ref` from `src/assets/refs.py`
- [ ] `grep -n 'f"{' src/workflows/catalog.py` returns no three-segment inline constructions
- [ ] No Python imports during discovery (only at validation time)
- [ ] Existing asset types (tools/agents/skills) untouched
- [ ] **R3 #5 — `WorkflowBuiltinShadowed` fires** for user-authored files at reserved slots (e.g., `bundles/kolsch/nodes/bash.py`)
- [ ] **R3 #5 — Virtual `kolsch` bundle** exposes all 14 built-in nodes at `kolsch/nodes/<name>`
- [ ] **R3 #9 — Built-in node files renamed**: `map.py`/`set_state.py`/`delete_state.py` exist; `mapper.py`/`state_set.py`/`state_delete.py` do NOT

**Handoff trigger:** All boxes checked → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

> You are reviewing Step 3. Reply "Step 3 Approved" or "Changes requested — [list]".

**What was built in Step 3:** asset catalog discovery for `workflows/*.yaml` and `nodes/*.py`, three-segment refs, `bundle.yaml` `exclude:` honored.

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 3.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §3.2, §16.
3. **`src/assets/catalog.py`** — confirm the new asset types are wired without breaking existing patterns.

**Acceptance criteria to verify (Step 3):** Verify each of 3.1–3.13 above.

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/component/test_workflow_catalog.py -v`
- `podman-compose exec dev uv run pytest tests/component/ -q` (regression)

**Review checklist:**
- [ ] All 13 Step 3 acceptance criteria demonstrably met
- [ ] Three-segment refs consistent — built via `build_ref` (verify by reading the diff)
- [ ] `grep -rn 'f"{' src/workflows/catalog.py` returns NO three-segment inline constructions
- [ ] `catalog.lookup(...)` exercised in tests for both hit and miss cases
- [ ] Node `.py` files NOT imported during discovery
- [ ] Existing asset types (tools/agents/skills) untouched (smoke test: `kolsch run kolsch/helper` still works)
- [ ] **R3 #5 — `WorkflowBuiltinShadowed`** triggered by test fixture with user-authored `kolsch/nodes/bash.py`
- [ ] **R3 #5 — Virtual `kolsch` bundle exposes 14 built-ins** verified by test
- [ ] **R3 #9 — Built-in node files use new names**: `bundles/kolsch/nodes/{map,set_state,delete_state}.py` exist; old names (`mapper`/`state_set`/`state_delete`) gone
- [ ] No regressions

**Handoff trigger:** All boxes → "Step 3 Approved". Fails → "Changes requested — [list with citations]".

---

# STEP 4 — Policy integration

**Round 5 H1 vocabulary lock**: this step's USER-FACING vocabulary is "policy" everywhere (the unified term per H1 — matches OPA/Cerbos/AWS IAM Policy idiom). INTERNAL Python identifiers (`RuleBasedAuthorizationProvider`, `PermissionLayer`, `src/providers/authz.py` module path, the `_check_authz` validator function name) STAY UNCHANGED at Phase 3.0 — the code-side rename to `RuleBasedPolicyProvider` / `PolicyLayer` / `src/providers/policy.py` / `_check_policy` is queued as a separate QoL spec at `process/qol-policy-rename.md`. Where this step's text reads "authz" it refers to the internal namespace; where it reads "policy" it refers to the user-facing surface. The wire event kind for policy decisions is `policy.decision.made` (per H1's event-kind table — renamed from `authz.decision`).

**Goal**: refactor `RuleBasedAuthorizationProvider.allow()` to a `layers: tuple[PermissionLayer, ...]` signature (Round 4 item #7 — supersedes the per-layer-kwarg design); introduce the value object `PermissionLayer{name, rules}` (internal code name — see H1 vocabulary lock above). The engine constructs the `layers` tuple in the proximate-caller order (workflow's own node → include `workflow` layer; embedded agent → omit `workflow` layer — structural escalation prevention with no new rule field required). NO `WorkflowCatalogProtocol` is introduced (per Round 4 item #7 — YAGNI dropped); the validator stays on concrete `AssetCatalog` because tests construct `AssetCatalog` in-memory. Library extraction is dropped as a non-goal at v1. Validate three-segment refs (nodes AND workflows) via `parse_three_segment` + `catalog.lookup` (landed in Step 1). Add `Rule.from_` field with STRICT 3-seg + asset-type + catalog-existence validation (Round 4 item #13 — supersedes the warning-on-bad-shape behavior). Add `caller_chain: tuple[str, ...] = ()` keyword-only param to `allow()`. **R3 #3 — `audit_level` config**: Rule-based policy config Pydantic model gains `audit_level: str = "rule_matched"` (Round 4 item #9 — typed as `str`, NOT `Literal`; documented values `all` / `rule_matched` at master spec §13.5). Engine emits `policy.decision.made` event (Round 5 H1 — renamed from `authz.decision`) during workflow's tool-dispatch wrapper subject to setting (Phase 3.1 implementation; Step 4 spec is for contract). Existing two-segment tool refs continue via migration shim.

**Files created/modified:**
- `src/providers/authz.py` (modified) — **Round 4 item #7**: refactor `allow()` to `layers: tuple[PermissionLayer, ...]` (introduce `PermissionLayer` value object); add `caller_chain` keyword-only param. Add `from_` field to `Rule` with STRICT validation (Round 4 item #13). Add caller-chain glob check at the top of `_rule_matches`. `_validate_what` + `_build_action_to_tool_ref` already refactored to use refs unification in Step 1, but EXTEND here for workflow refs.
- `src/schemas/config.py` (modified, if `Rule` lives there) — add `from_: str | None = Field(default=None, alias="from")`.
- `src/providers/authz.py` — also add `audit_level: str = "rule_matched"` to the Rule-based authz config Pydantic model (Round 4 items #9 + #12).
- `tests/component/test_workflow_authz.py` (new) — authz layer wiring tests including `from:` STRICT validation + caller_chain semantics + `layers` tuple + `audit_level` field.

**Effort:** ~0.75 day.

## Step 4 — Coder Prompt

> You are implementing Step 4 of a 7-step phase. Steps 1-3 are approved.
> Reply "Step 4 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Refactor `RuleBasedAuthorizationProvider.allow()` to take a single `layers: tuple[PermissionLayer, ...]` kwarg (Round 4 item #7 — replaces the per-layer `workflow_permissions=`, `agent_permissions=`, `bundle_permissions=`, `platform_permissions=` kwargs). Introduce `PermissionLayer` as a `@dataclass(frozen=True, slots=True)` value object with fields `name: str` (`"workflow" | "agent" | "bundle" | "platform"`) and `rules: tuple[Rule, ...]`. The provider walks the tuple IN ORDER — the engine is responsible for assembling the tuple with the workflow layer FIRST when the proximate caller is a workflow node firing the action (and OMITTED entirely when the proximate caller is an embedded agent — structural escalation prevention). NO `WorkflowCatalogProtocol` is introduced; the provider keeps its concrete `AssetCatalog` dependency (per Round 4 item #7 — library extraction dropped as YAGNI at v1; tests can construct `AssetCatalog` in-memory). Extend rule validation to accept three-segment refs for both nodes AND workflows via the refs unification utility from Step 1. Add the `Rule.from_` field with STRICT validation: must be a 3-seg ref (Round 4 item #13). Add the `caller_chain` kwarg on `allow()`. Engine-side layer-tuple construction lands when the workflow engine ships (Phase 3.1). Document the contract here so 3.1 inherits it cleanly. Do NOT touch the two-segment tool ref migration shim — it must continue working untouched.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 4.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §13.1 (node/workflow refs, three-segment form), §13.2 (layer order, walks first; engine-side layer selection by proximate caller), §13.1.1 (tool-ref migration consideration), §13.3 (`from:` field + caller chain construction).
3. **`src/providers/authz.py`** — entire file. Pay special attention to:
   - Lines 633-693: `allow()` method with existing `agent_permissions` / `bundle_permissions` keyword-only params. **Round 4 item #7**: REPLACE the per-layer kwargs with a single `layers: tuple[PermissionLayer, ...]` kwarg. Introduce `PermissionLayer` value object. Also add `caller_chain: tuple[str, ...] = ()` kwarg.
   - Lines 564-631: `_validate_what` — already refactored in Step 1 to use `parse_three_segment`. EXTEND to accept workflow refs (`<bundle>/workflows/<name>`) in addition to nodes.
   - Lines 811-842: `_rule_matches` matcher logic. ADD a top-of-function check: if `compiled.from_pattern is not None`, fail-closed when `caller_chain` is empty; else iterate frames and call `fnmatch.fnmatchcase` against each — fires if ANY frame matches (transitive).
4. **`src/engine/observers.py`** and **`src/engine/engine.py`** — confirm where caller-chain frames will be pushed in Phase 3.1; document the contract.

**What to implement:**

1. **`src/providers/authz.py`** modifications:
   - **Round 4 item #7 — `allow()` signature refactor**: replace the four per-layer keyword-only params (`workflow_permissions=`, `agent_permissions=`, `bundle_permissions=`, `platform_permissions=`) with a single ordered tuple kwarg `layers: tuple[PermissionLayer, ...]`. Introduce `PermissionLayer` as a `@dataclass(frozen=True, slots=True)` value object with fields `name: str` (`"workflow" | "agent" | "bundle" | "platform"`) and `rules: tuple[Rule, ...]`. The provider walks `layers` IN ORDER — first match wins per layer; engine assembles the tuple with `workflow` FIRST when the proximate caller is a workflow node, OMITTED when the proximate caller is an embedded agent. The layer's `name` is used in audit-event payloads (where the prior code emitted `"layer": "workflow_permissions"`, it now emits `"layer": layer.name`).
   - Add `caller_chain: tuple[str, ...] = ()` keyword-only parameter to `allow()`. Ordering: proximate first (`chain[0]` is closest to the tool call), distal last. Default `()` represents engine bootstrap (no agent/workflow active).
   - Final signature: `allow(action, target, *, layers: tuple[PermissionLayer, ...], caller_chain: tuple[str, ...] = ()) -> AuthzDecision`.
   - Thread `caller_chain` into every `_rule_matches(...)` call.
   - Extend `_validate_what` (already refactored to use `parse_three_segment` in Step 1) to accept three-segment refs for BOTH nodes AND workflows:
     - `<bundle>/nodes/<name>` → validate via `catalog.lookup(ref, expected_type="node")`.
     - `<bundle>/workflows/<name>` → validate via `catalog.lookup(ref, expected_type="workflow")`.
     - `<bundle>/agents/<name>` → validate via `catalog.lookup(ref, expected_type="agent")` (the agent ref form is what `from:` will glob against; the `what` field continues to support it for future agent-invocation permissions).
     - `<bundle>/tools/<name>` → also accept (parallels nodes; sets up future tool migration).
     - `<bundle>/<name>` → existing two-segment tool ref logic (unchanged migration shim).
   - Action-namespace matching: `tool.<name>` continues to map to `<bundle>/<name>` (legacy); add `node.<name>` mapping to `<bundle>/nodes/<name>` via the action map (already refactored in Step 1 to use `parse_three_segment`; extend the type-routing table here).
   - **`from:` field on `Rule`**: add `from_: str | None = Field(default=None, alias="from")`. Preserve legacy `id` and `who` fields during migration window (do NOT remove). Final shape:
     ```python
     class Rule(BaseModel):
         what: str
         pattern: str = "*"
         from_: str | None = Field(default=None, alias="from")
         effect: Literal["allow", "deny", "prompt"]
         # Legacy fields preserved
         id: str = ""
         who: str | None = None
     ```
   - **`from:` matcher addition** — insert as the FIRST check inside `_rule_matches`:
     ```python
     if compiled.from_pattern is not None:
         if not caller_chain:
             return False  # fail-closed when chain empty + from: set
         if not any(fnmatch.fnmatchcase(frame, compiled.from_pattern)
                    for frame in caller_chain):
             return False
     ```
   - **`from:` shape validation — Round 4 item #13 STRICT** at rule-compile time: parse with `parse_three_segment`; emit THREE distinct hard errors (NOT warnings — Round 4 item #13 reverses R3 #6):
     - Not a 3-seg ref → `WorkflowFromShapeError`.
     - 3-seg, but the asset_type segment is not `agents` or `workflows` → `WorkflowFromAssetTypeError`.
     - 3-seg + valid asset_type, but the target doesn't exist in the catalog → `WorkflowFromNotFound`.
     `WorkflowFromShapeWarning` is REMOVED entirely. `core/principals/{user,system}` is NOT supported in v1 (would trip `WorkflowFromAssetTypeError`).
   - Engine-side layer-tuple-construction contract (documented; actual engine wiring lands in Phase 3.1): the engine MUST build the `layers` tuple with `PermissionLayer(name="workflow", rules=...)` FIRST when the proximate caller frame is a workflow node firing the action. When an embedded agent fires, the `workflow` layer is OMITTED from the tuple entirely. Principal strings (`user.local`, `system`) stay engine telemetry — they NEVER feed the matcher.
   - **R3 #3 + Round 4 item #9 + Round 5 H1 — `audit_level` config + `policy.decision.made` wire kind**: the Rule-based policy config Pydantic model gains `audit_level: str = "rule_matched"` (typed as `str`, NOT `Literal`; documented value table at master spec §13.5: `all` persists every policy decision, `rule_matched` persists only decisions where an explicit rule fired). The DEFAULT is `rule_matched` (per Round 4 item #12). Engine emits **`policy.decision.made`** event (Round 5 H1 — renamed from `authz.decision`; follows the locked `<owner>.<thing>.<verb-past>` naming convention) during workflow's tool-dispatch wrapper subject to setting (Phase 3.1 implementation; Step 4 spec is for contract). Payload: `{action, target, effect, rule_id, layer, caller_chain}` (caller_chain persisted per R3 #10; `layer` is the `name` field of the matching `PermissionLayer` — internal class name unchanged at Phase 3.0 per H1's code-side defer to QoL). Step 4 implements the Pydantic field + the contract docstring on `allow()`; Phase 3.1 implements the event emission in the tool-dispatch wrapper.

2. **`tests/component/test_workflow_authz.py`**:
   - Test: `allow(layers=(PermissionLayer(name="workflow", rules=(Rule(what="kolsch/nodes/foo", effect="allow"),)),))` matches an action `node.foo` (or the equivalent ref).
   - Test: layer order — `layers=(PermissionLayer(name="workflow", rules=(<allow>,)), PermissionLayer(name="agent", rules=(<deny>,)))` → workflow allow wins (walks first).
   - Test: omitting the `workflow` layer (`layers=(PermissionLayer(name="agent", rules=...),)`) — embedded-agent simulation — gives no workflow-layer allow.
   - Test: `PermissionLayer` is `@dataclass(frozen=True, slots=True)` — mutating `rules` raises `FrozenInstanceError`.
   - Test: three-segment node ref `kolsch/nodes/parse` validates against the catalog if the node exists.
   - Test: three-segment workflow ref `kolsch/workflows/triage` validates against the catalog if the workflow exists.
   - Test: three-segment node ref for non-existent node → `AuthzToolRefUnknown` (or analogous).
   - Test: two-segment tool ref `kolsch/bash` STILL works (regression).
   - Test: catch-all `*` and pattern globs still work for both old and new shapes.
   - **Test (`from:` field)**: rule with `from: kolsch/agents/triage` + matching caller_chain frame → allow fires.
   - **Test (`from:` field, no chain)**: rule with `from: kolsch/agents/triage` + `caller_chain=()` → rule SKIPS (fail-closed).
   - **Test (`from:` field, transitive)**: caller_chain has 3 frames; rule's `from:` pattern matches the LAST (distal) frame → rule fires (transitive).
   - **Test (`from:` field, glob)**: `from: kolsch/agents/*` matches any agent under `kolsch`.
   - **Test (`from:` field, miss)**: `from: kolsch/agents/foo` + caller_chain has only `kolsch/agents/bar` → rule SKIPS.
   - **Test (`from:` shape ERROR — Round 4 item #13)**: rule with `from: random` (1-seg) → `WorkflowFromShapeError` at compile time (HARD ERROR, not a warning). The R3 #6 warning behavior is REMOVED.
   - **Test (`from:` asset-type ERROR — Round 4 item #13)**: rule with `from: kolsch/nodes/foo` (3-seg but asset_type is `nodes`, not `agents`/`workflows`) → `WorkflowFromAssetTypeError` at compile time.
   - **Test (`from:` not-found ERROR — Round 4 item #13)**: rule with `from: kolsch/agents/nonexistent` (3-seg + valid asset_type, but the agent isn't in the catalog) → `WorkflowFromNotFound` at compile time.
   - **Test (caller_chain kwarg)**: `allow(...)` without `caller_chain=` defaults to `()`; behavior matches default (no `from:` rules can match).
   - **Test (`audit_level` field — Round 4 item #9)**: the Rule-based authz config Pydantic model exposes `audit_level: str` with default `"rule_matched"` (Round 4 item #12). Setting it to `"all"` is valid; setting it to an arbitrary string (e.g., `"verbose"`) is also valid (it's `str`, not `Literal`); semantic interpretation lives at the engine emit site (Phase 3.1).
   - **Test (engine-side layer-tuple construction — DOCUMENTATION-LEVEL CHECK)**: the test file asserts via inline comments that the proximate-caller contract is documented in `allow()`'s docstring (workflow layer FIRST when proximate frame is a workflow node; OMITTED when embedded agent). The actual structural engine wiring lands in Phase 3.1.

**Step 4 Acceptance Criteria:**

4.1. **Round 4 item #7 — `allow()` signature refactor**: `RuleBasedAuthorizationProvider.allow(action, target, *, layers: tuple[PermissionLayer, ...], caller_chain: tuple[str, ...] = ())`. The four prior per-layer kwargs (`workflow_permissions=`, `agent_permissions=`, `bundle_permissions=`, `platform_permissions=`) are REMOVED. `PermissionLayer` is a `@dataclass(frozen=True, slots=True)` value object exposing `name: str` (`"workflow" | "agent" | "bundle" | "platform"`) and `rules: tuple[Rule, ...]`. NO `WorkflowCatalogProtocol` is introduced — provider keeps its concrete `AssetCatalog` dependency (per Round 4 item #7 — YAGNI dropped).
4.2. Provider walks `layers` in order — first match per layer wins; layer-tuple ordering is the engine's responsibility (proximate-caller frame determines whether the `workflow` layer is included and where it goes in the tuple).
4.3. Three-segment node refs (`<bundle>/nodes/<name>`) validate against catalog for asset_type="node" via `parse_three_segment` + `catalog.lookup`.
4.4. Three-segment workflow refs (`<bundle>/workflows/<name>`) validate against catalog for asset_type="workflow" via `parse_three_segment` + `catalog.lookup`.
4.5. Action `node.<name>` matches `<bundle>/nodes/<name>` refs via the action map (routed through `parse_three_segment`).
4.6. Two-segment tool refs (`kolsch/bash`) continue to work unchanged.
4.7. Three-segment tool refs (`<bundle>/tools/<name>`) are accepted (future-proofing).
4.8. PROMPT effect + modal callback continue to work for the new `workflow` layer (per master spec §13.3).
4.9. **`Rule.from_` field**: `Rule(**{"what": "...", "from": "kolsch/agents/triage", "effect": "allow"})` parses (via alias); `rule.from_` is the string. Default `None`.
4.10. **`caller_chain` kwarg**: `allow(..., caller_chain=("a/agents/x",))` accepted; default `()`.
4.11. **`from:` matcher — empty chain fail-closed**: `from:` set + `caller_chain=()` → rule does NOT match (does NOT contribute allow/deny).
4.12. **`from:` matcher — transitive**: rule fires if ANY frame in the chain matches the glob (proximate or distal).
4.13. **Round 4 item #13 — `from:` STRICT validation (HARD ERRORS at compile time)**: rule with `from:` that is not a 3-seg ref → `WorkflowFromShapeError`. Rule with 3-seg `from:` whose asset_type segment is not `agents`/`workflows` → `WorkflowFromAssetTypeError`. Rule with 3-seg `from:` whose target doesn't exist in the catalog → `WorkflowFromNotFound`. The prior `WorkflowFromShapeWarning` (R3 #6) is REMOVED.
4.14. **`core/principals/*` NOT added**: no new ref type for principals; `user.local` / `system` remain telemetry only. (`core/principals/foo` would trip `WorkflowFromAssetTypeError` anyway.)
4.15. **Engine-side layer-tuple-construction contract documented**: `allow()`'s docstring states that the engine builds the `layers` tuple with `PermissionLayer(name="workflow", ...)` FIRST when the proximate caller is a workflow node firing the action; embedded agents → workflow layer OMITTED from the tuple entirely.
4.16. **R3 #3 + Round 4 items #9 + #12 + Round 5 H1 — `audit_level` config field + `policy.decision.made` wire kind**: the Rule-based policy config Pydantic model gains `audit_level: str = "rule_matched"` (typed as `str`, NOT `Literal`; documented values `all` / `rule_matched` at master spec §13.5). DEFAULT is `"rule_matched"` (per Round 4 item #12 — compliance-driven operators set `"all"` explicitly in their `platform.yaml` or `bundle.yaml`). `allow()`'s docstring documents the contract: engine emits **`policy.decision.made`** event (Round 5 H1 — renamed from `authz.decision`; follows the locked `<owner>.<thing>.<verb-past>` event-kind convention) in workflow's tool-dispatch wrapper subject to this setting (Phase 3.1 implementation). Event payload spec: `{action, target, effect, rule_id, layer, caller_chain}` where `layer` is the matching `PermissionLayer.name` (internal class name unchanged at Phase 3.0; H1 code-side rename queued at `process/qol-policy-rename.md`). Step 4 test asserts the Pydantic field exists with the expected type, default, and documented values, AND that the docstring references the new wire kind `policy.decision.made` (not the legacy `authz.decision`).
4.17. No regressions: existing authz tests pass.

**Step 4 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_authz.py -v`
- **Expect:** all tests pass.

- **Run (existing authz):** `podman-compose exec dev uv run pytest tests/component/test_*authz*.py -v` (whatever exists today).
- **Expect:** all existing authz tests pass.

**Self-check before handoff:**

- [ ] All 17 Step 4 acceptance criteria met
- [ ] New authz tests pass (including all `from:` glob cases + caller_chain semantics + `audit_level` field)
- [ ] EXISTING authz tests all pass (no regression — this is critical for the migration shim)
- [ ] Two-segment tool refs continue to work via existing shim — DO NOT modify the shim
- [ ] **Round 4 item #7 — `allow()` signature uses `layers: tuple[PermissionLayer, ...]`** (the per-layer kwargs are REMOVED); `PermissionLayer` is `@dataclass(frozen=True, slots=True)` with `name`/`rules` fields; layer-tuple order is engine's responsibility (provider walks in order)
- [ ] **Round 4 item #7 — NO `WorkflowCatalogProtocol`** introduced; provider keeps concrete `AssetCatalog` dependency
- [ ] PROMPT modal callback still fires for the `workflow` layer
- [ ] `Rule.from_` populates from YAML alias `"from"`; legacy `id` and `who` fields preserved
- [ ] `_rule_matches`'s FIRST check is the `from:` caller-chain glob (fail-closed on empty chain)
- [ ] **Round 4 item #13 — `from:` STRICT validation**: `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` fire as HARD ERRORS at compile time; `WorkflowFromShapeWarning` is REMOVED (grep returns ZERO matches)
- [ ] `core/principals/*` NOT added anywhere
- [ ] Engine-side layer-tuple-construction contract is documented in `allow()`'s docstring
- [ ] **Round 4 items #9 + #12 — `audit_level: str = "rule_matched"`** (NOT `Literal`; default `"rule_matched"`); documented value table at master spec §13.5

**Handoff trigger:** All boxes checked → "Step 4 Complete — [summary]".

## Step 4 — Reviewer Prompt

> You are reviewing Step 4. Reply "Step 4 Approved" or "Changes requested — [list]".

**What was built in Step 4:** `RuleBasedAuthorizationProvider.allow()` refactored to take `layers: tuple[PermissionLayer, ...]` (Round 4 item #7 — replaces per-layer kwargs); `PermissionLayer` value object introduced (`@dataclass(frozen=True, slots=True)` with `name`/`rules` fields); NO `WorkflowCatalogProtocol` introduced (per Round 4 item #7 YAGNI lock). Three-segment node + workflow refs validate via `parse_three_segment` + `catalog.lookup`. `Rule.from_` field with STRICT 3-seg + asset-type + catalog-existence validation (Round 4 item #13 — `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` as HARD ERRORS; the R3 #6 `WorkflowFromShapeWarning` is REMOVED). `caller_chain: tuple[str, ...] = ()` kwarg on `allow()`; `_rule_matches` gains a caller-chain glob check as its FIRST step (fail-closed on empty chain). `audit_level: str = "rule_matched"` (Round 4 items #9 + #12 — typed as `str` not `Literal`; default `rule_matched`). Engine-side layer-tuple-construction contract documented (proximate caller frame determines whether `workflow` layer is included and where). Legacy two-segment tool refs and migration shim untouched.

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 4.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §13.1, §13.1.1, §13.2, §13.3.
3. **`src/providers/authz.py`** before + after — verify the migration shim is unchanged AND that all three-segment ref construction is via `build_ref` / parsing via `parse_three_segment` (refs unification).

**Acceptance criteria to verify (Step 4):** Verify each of 4.1–4.17 above. PAY EXTRA ATTENTION to:
- 4.1 (`allow()` signature uses `layers: tuple[PermissionLayer, ...]` — Round 4 item #7; per-layer kwargs REMOVED; NO `WorkflowCatalogProtocol`) — this is the biggest API change.
- 4.6 (two-segment tool ref regression) — this is the most likely failure mode.
- 4.11 (empty-chain fail-closed for `from:`) — easy to get wrong.
- 4.13 (`from:` STRICT validation — Round 4 item #13 — HARD ERRORS, NOT warnings; `WorkflowFromShapeWarning` REMOVED entirely) — must not regress to the R3 #6 behavior.
- 4.14 (no `core/principals/*`) — grep to confirm nothing was added.
- 4.16 (`audit_level: str` — Round 4 items #9 + #12 — typed as `str` not `Literal`; default `"rule_matched"`).

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/component/test_workflow_authz.py -v`
- `podman-compose exec dev uv run pytest tests/component/ -q` (full regression — especially existing authz tests)

**Review checklist:**
- [ ] All 17 Step 4 acceptance criteria demonstrably met
- [ ] **Round 4 item #7 — `allow()` signature** uses `layers: tuple[PermissionLayer, ...]` (NOT per-layer kwargs); `PermissionLayer` is `@dataclass(frozen=True, slots=True)`; provider walks layers in order; NO `WorkflowCatalogProtocol` introduced
- [ ] **Round 4 items #9 + #12 — `audit_level` field present** on Rule-based authz config (Pydantic `str = "rule_matched"`, NOT `Literal`); `allow()` docstring documents the event-emission contract for Phase 3.1
- [ ] Migration shim for two-segment tool refs UNTOUCHED (read the diff against `src/providers/authz.py` and confirm)
- [ ] Layer-tuple ordering documented as engine's responsibility (workflow first when proximate frame is workflow node; OMITTED when embedded agent)
- [ ] PROMPT modal callback fires for the `workflow` layer
- [ ] `Rule.from_` field present with alias `"from"`; legacy `id`/`who` preserved
- [ ] `_rule_matches`'s FIRST check is the `from:` caller-chain glob; fail-closed on empty chain confirmed
- [ ] `from:` transitive (any frame matches → fires) confirmed
- [ ] **Round 4 item #13 — `from:` STRICT HARD ERRORS** (`WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`); `WorkflowFromShapeWarning` REMOVED (grep ZERO matches across `src/` and `tests/`)
- [ ] `caller_chain` kwarg on `allow()`; default `()`
- [ ] `grep -rn 'core/principals' src/ tests/` returns ZERO matches (principals NOT added)
- [ ] Engine-side layer-tuple-construction contract documented in `allow()`'s docstring
- [ ] All existing authz tests pass
- [ ] No regressions in `kolsch run <agent>` or `kolsch chat` (manual smoke test)

**Handoff trigger:** All boxes → "Step 4 Approved". Fails → "Changes requested — [list with citations]".

---

# STEP 5 — Run-start validator

**Goal**: implement the full run-start validation gate covering every check in master spec §7.1–§7.7 PLUS the new gate-level checks (Jinja-AST `nodes.<X>.<field>` pre-validation via **graph-reachability predecessor closure** per Round 4 item #11 — NOT edge-level `ref:` declarations which no longer exist; `copy_of:` reachability; unreachable-node detection; mapper-worker addressability; multi-error accumulation via stdlib `ExceptionGroup`; `WorkflowNodeNotDefined` for unresolved YAML node-name refs; **R3 #2 timeout validation**; **R3 #6 `ValidationResult` structured API + thin `_or_raise` wrapper**; **R3 #9 renamed exception classes**; **Round 4 item #11 bind-key collision check** (`WorkflowBindCollision` — no two edges to the same target may bind the same key); **Round 4 item #13 strict `from:` validation** (`WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` as hard errors)). Every validator rule has a unit test; every error class — including the new ones — fires for its specific case.

**Files created/modified:**
- `src/workflows/validator.py` (new) — the gate
- `tests/component/test_workflow_validator.py` (new) — exhaustive validator unit tests

**Effort:** ~2-3 days.

## Step 5 — Coder Prompt

> You are implementing Step 5 of a 7-step phase. Steps 1-4 are approved.
> Reply "Step 5 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Implement the full run-start validation gate. **R3 #6 — TWO public surfaces**: (a) `validate_workflow(workflow: Workflow, catalog: AssetCatalog) -> ValidationResult` is the structured API — returns a dataclass `ValidationResult{errors: tuple[WorkflowValidationError, ...], warnings: tuple[WorkflowValidationWarning, ...]}` with `is_valid: bool` property; NEVER raises on validation failure (only on internal contract violations); (b) `validate_workflow_or_raise(workflow: Workflow, catalog: AssetCatalog) -> None` is the thin wrapper — raises on errors (preserves raise-on-failure for callers that want it); warnings are silent in the raise path. Internally: within each L3 structural-independent layer, COLLECT all findings into the result's `errors`/`warnings` tuples. `validate_workflow_or_raise`: if N == 1 error, raises the single specific exception; if N > 1, raises stdlib `ExceptionGroup[WorkflowValidationError]` (Python 3.11+; kolsch on 3.13). **NO custom `WorkflowValidationErrorGroup` class.** Across layers (structural → schema → branching), FAIL FAST in the structured API too — but on the fail-fast point, run subsequent layers in best-effort mode and append to `errors` so callers see the cluster. The raise wrapper appends "Related issues" tail when raising. Step 5 is the largest in the phase.

**Cross-graph `nodes.<X>.<field>` references ARE pre-validated** (the prior "NOT pre-validated" carve-out in master spec §7.2 is DROPPED). The check walks every Jinja-evaluated string field (binds RHS, config strings, `when:`, `branch.condition`, `filter.condition`, `compute.value`, `map.over`, etc.) via `jinja2.Environment().parse()`, extracts `Getattr` chains rooted at `Name("nodes")`, and verifies the predecessor relationship via **graph reachability** (Round 4 item #11 — NOT via edge-level `ref:`; the `ref:` field no longer exists). Failures raise **`WorkflowJinjaRefUnknown`** (R3 #9 renamed from `WorkflowNodeRefUnknown`) with the appropriate `reason: str` value (Round 4 item #9 — typed as `str`, NOT `Literal`; value table at master spec §7.2).

**Validator check order for `nodes.<X>.<field>` references** (locked; semantics shifted per Round 4 item #11):

1. Does X exist in `workflow.nodes`? → if not, raise `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`). This is the structural pre-pass; fires for ANY YAML field naming a non-existent node (`edge.from`/`edge.to`, `copy_of:` source, `map.apply:` target — the `edge.ref` entry source is GONE per Round 4 item #11).
2. Is X a **graph-reachable transitive ancestor** of the consuming node? (Round 4 item #11 — computed from edge structure: walk back from the consuming node via inbound edges; X must be in the closure.) → if not, raise `WorkflowJinjaRefUnknown(reason="not_predecessor")`. SPECIAL CASE: for `binds:` RHS Jinja expressions on a given edge, `nodes.<edge.from>.<field>` is IMPLICITLY ALLOWED (binds: is literally for parent → target wiring).
3. Is X's output schema known? → if not (e.g., authored Python node failed to import or introspect), raise `WorkflowJinjaRefUnknown(reason="not_declared")`.
4. Is Y in X's output schema? → if not, raise `WorkflowJinjaRefUnknown(reason="unknown_field")`.

Note: "predecessor" = any graph-reachable transitive ancestor (Round 4 item #11 — supersedes the prior edge-`ref:`-based definition).

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 5.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §7 (every sub-section, in order):
   - **§7.1 Structural** — DAG, start (single-start rule preserved), reachability, multi-input edges ARE permitted (Round 4 item #11 — single-input rule DROPPED; `WorkflowMultiInputForbidden` REMOVED), **bind-key collision across multiple edges to the same target** (`WorkflowBindCollision` — Round 4 item #11 NEW; no two edges to the same target node may bind the same key), **unreachable nodes** (raise `WorkflowUnreachableNode` — defined but no incoming edge from start AND not a mapper `apply:` target), **`copy_of:` source reachability** (the source MUST be graph-reachable; no "template-only" status). PRE-PASS: walk ALL YAML node-name references (`edge.from`, `edge.to`, `copy_of:` source, `map.apply:` target — the `edge.ref` entry source is GONE per Round 4 item #11); any name not in `workflow.nodes` raises `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`).
   - **§7.2 Schema** — bind sources resolve, type-compat. **`nodes.<X>.<field>` references ARE pre-validated** (the prior carve-out is GONE) via Jinja-AST walk + **graph-reachability predecessor closure** (Round 4 item #11; NOT edge-level `ref:` which no longer exists) + output-schema field check. Raise `WorkflowJinjaRefUnknown` with `reason: str` (Round 4 item #9 — typed as `str`; documented values `not_predecessor` / `unknown_field` / `not_declared`) per the order locked above. Raise `WorkflowMapperWorkerNotAddressable` when an external reference targets a mapper worker.
   - **§7.3 Branching** — edge `branch:` from branchable nodes, both true/false required, non-branching no `branch:`. Edge `when:` (RENAMED from `condition:`) is a Jinja expression — extract any `nodes.<X>.<field>` refs and pre-validate them too (predecessor closure is graph-reachability-based per Round 4 item #11).
   - **§7.4 Type-specific** — mapper, `compute` (the new aggregation replacement), workflow node, branch node, filter. The 8 dropped aggregations are NOT in the catalog. `python` is NOT a discriminator — authored Python nodes are `CatalogNodeRef` instances and are validated via §7.6's introspection path keyed off the catalog entry.
   - **§7.5 Extends + composition** — depth (cap 16 → `WorkflowExtendsDepthExceeded`), strict union, ref resolution, cycle (`WorkflowExtendsCycle`) — cycle and depth-exceeded are DISTINCT exceptions
   - **§7.6 Authored Python node introspection** — for each `CatalogNodeDef` (Round 4 item #3 renamed; `type: <bundle>/nodes/<name>`) in `nodes:`, resolve the catalog entry (path to .py file), import via `importlib.import_module`, check decoration, signature, return type (per the output-shape table in master spec §4), secrets exist. There is NO `body:` field on the node — the `type:` value IS the catalog ref. (The prior `_check_python_modules` logic that imported a `body:` file is REMOVED; the rescoped function `_check_catalog_node_defs` — renamed from `_check_catalog_node_refs` per Round 4 item #3 — handles authored-Python-node introspection via the catalog.)
   - **§7.7 Authz** — permissions parse, refs resolve via `parse_three_segment` + `catalog.lookup`; **`from:` STRICT validation (Round 4 item #13 — HARD ERRORS)**: not a 3-seg ref → `WorkflowFromShapeError`; asset_type segment not `agents`/`workflows` → `WorkflowFromAssetTypeError`; ref's target doesn't exist in catalog → `WorkflowFromNotFound`. The R3 #6 `WorkflowFromShapeWarning` is REMOVED entirely. Invocation passes layer walk via `allow(action, target, *, layers: tuple[PermissionLayer, ...], caller_chain=...)` (Round 4 item #7 — refactored signature).
3. Master spec **§12.1** — every exception class is in the hierarchy; use the specific class for each failure (e.g., `WorkflowBindCollision` for §7.1's bind-key collision; `WorkflowBranchOnNonBranchable` for §7.3; `WorkflowJinjaRefUnknown` for §7.2 cross-graph ref failures; `WorkflowUnreachableNode` for §7.1 unreachable-node failures; `WorkflowMapperWorkerNotAddressable` for §7.2 worker-external-ref failures; `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` for §7.7 `from:` field failures; etc.). NOTE: `WorkflowMultiInputForbidden` is REMOVED (Round 4 item #11) — DO NOT raise it.
4. Master spec **§12.3** — error message contract: every YAML-origin error MUST include file + line + identifying names + fix-path block. Validator uses these tokens.

**What to implement:**

1. **`src/workflows/validator.py`**:
   - **R3 #6 — DUAL public surface**:
     - `validate_workflow(workflow: Workflow, catalog: AssetCatalog) -> ValidationResult` — structured API. Returns dataclass `ValidationResult{errors: tuple[WorkflowValidationError, ...], warnings: tuple[WorkflowValidationWarning, ...]}` with `is_valid: bool` property (`len(errors) == 0`). NEVER raises on validation failures. Authors of CLI / programmatic callers who want to surface warnings use this.
     - `validate_workflow_or_raise(workflow: Workflow, catalog: AssetCatalog) -> None` — thin wrapper. Calls `validate_workflow`; if `not result.is_valid`, raises (warnings silent in this path). On SINGLE error, raises the specific `WorkflowValidationError` subclass. On MULTIPLE sibling-independent errors, raises `ExceptionGroup[WorkflowValidationError]` (stdlib — no custom wrapper class). Tests access sub-exceptions via `exc.exceptions`; `except* WorkflowBindMustBeJinja` filters by sub-type.
   - **`ValidationResult` dataclass** (also exported from `src/workflows/__init__.py`): `@dataclass(frozen=True, slots=True)` with `errors: tuple[WorkflowValidationError, ...] = ()`, `warnings: tuple[WorkflowValidationWarning, ...] = ()`, and `@property is_valid -> bool` returning `not self.errors`.
   - Sub-functions per master spec §7 sub-section, called in order:
     - `_resolve_extends(workflow, catalog)` — §7.5 — returns the unioned Workflow; raises `WorkflowExtendsCycle` on cycle, **`WorkflowExtendsDepthExceeded`** on depth-16 overflow (DISTINCT exception — split from cycle), dupes, unresolved refs. **Round 6 S5 — extends-policy-forbidden check**: as part of (or just after) extends resolution, walk each extended workflow's `policy:` block. If ANY extended workflow (transitive — every ref in the resolved chain) has non-empty `policy:` rules, raise `WorkflowExtendsPolicyForbidden` (hard error) with identifying-names including the extending workflow's ref + the offending extended workflow's ref + the count of policy rules found. Rationale (S5 lock): `policy:` blocks are NOT mergeable across `extends:` — `extends:` imports nodes + edges ONLY; an extended workflow's `policy:` applies only when invoked DIRECTLY but is NOT inherited by extenders. Shared policy patterns belong in `bundle.yaml` or `platform.yaml` (already supported via the layer walk). The Coder picks the implementation site (either inside `_resolve_extends` or in a sibling `_check_extends_policy(resolved_chain)` called immediately after); document the choice in the diff.
     - `_check_structural(workflow)` — §7.1 — DAG, start, reachability, single-start. **Multi-input edges are LEGAL** (Round 4 item #11 — single-input rule DROPPED; do NOT check for `WorkflowMultiInputForbidden`). **NEW per Round 4 item #11 — bind-key collision check**: for every node, gather all inbound edges; if two or more edges into the same target node bind the SAME key, raise `WorkflowBindCollision` (parse-time error; identifying names include target node, the colliding bind key, and the source nodes of the conflicting edges). **PRE-PASS**: walk every YAML node-name reference (`edge.from`, `edge.to`, `copy_of:` source, `map.apply:` target — the `edge.ref` entry source is GONE per Round 4 item #11); any unresolved name raises `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`). After the pre-pass: `WorkflowUnreachableNode` for any node defined but with no incoming edge AND not a mapper `apply:` target. `copy_of:` source reachability — the named source MUST be graph-reachable (no template-only nodes).
     - `_check_branching(workflow)` — §7.3 — branch edge invariants; edge-level `when:` Jinja expressions are extracted and fed to `_check_jinja_refs` (below).
     - `_check_schema(workflow)` — §7.2 — bind sources + type-compat. **Cross-graph `nodes.<X>.<field>` refs ARE pre-validated** via `_check_jinja_refs` (see below).
     - `_check_jinja_refs(workflow)` — NEW SUB-CHECK — for every Jinja-evaluatable config field (binds RHS values, config strings, `when:`, `condition:` on `branch`/`filter`, `compute.value`, `map.over`, `set_state.keys` values):
       - **Round 6 J5/β — Only walk values that LOOK like Jinja expressions**: a value is considered Jinja iff it is a `str` AND contains `"{{"`. Pure YAML literals (any non-string, OR a string with no `"{{"` substring) skip Jinja parsing entirely — they pass through as-is. Predecessor closure / `nodes.<X>.<field>` validation logic is UNCHANGED in spirit, but only applies to Jinja-marked values. `binds: {x: "{{ nodes.foo.bar }}"}` is checked; `binds: {x: "foo.bar"}` is NOT checked (it's a literal string).
       - Walk the Jinja AST via `JINJA_ENV.parse(...)` from the singleton sandboxed env (Round 6 S1, `src/workflows/jinja_env.py`). NOT a bare `jinja2.Environment()` — all parsing routes through the sandboxed singleton.
       - Extract `Getattr` chains rooted at `Name("nodes")` → each yields `(X, Y)` pairs (where X is the source node name, Y is a field).
       - **Precompute graph-reachability closure** (Round 4 item #11): for each node in `workflow.nodes`, compute the set of transitive ancestors by walking back via inbound edges. This closure replaces the prior edge-`ref:`-list lookup.
       - For each `(X, Y)`, run the LOCKED check order:
         1. Does X exist in `workflow.nodes`? (Should be caught by `_check_structural`'s pre-pass; defensive check here.) If not → `WorkflowNodeNotDefined`.
         2. Is X in the consuming node's graph-reachability ancestor closure (Round 4 item #11)? → if not, `WorkflowJinjaRefUnknown(reason="not_predecessor")`. **SPECIAL CASE for `binds:` RHS on a given edge**: `nodes.<edge.from>.<field>` is IMPLICITLY ALLOWED (binds: is literally for parent → target wiring).
         3. Is X's output schema known? → if not, `WorkflowJinjaRefUnknown(reason="not_declared")`.
         4. Is Y in X's output schema? → if not, `WorkflowJinjaRefUnknown(reason="unknown_field")`.
       - **`reason` is typed as `str`** (Round 4 item #9 — NOT `Literal`; documented value table at master spec §7.2).
       - If X is a worker node inside a mapper and the reference comes from OUTSIDE the mapper → `WorkflowMapperWorkerNotAddressable`. Inside-mapper references are fine.
       - Collect per-node findings into a list; if list len > 1, raise `ExceptionGroup` at the end of the layer; if exactly 1, raise it directly.
     - `_check_type_specific(workflow, catalog)` — §7.4 — mapper, **`compute`** (replaces the 8 aggregations — verify Jinja expression in `value:` parses, no further runtime checks), workflow node, branch node, filter.
     - `_check_catalog_node_defs(workflow, catalog, secrets_provider)` — §7.6 — Round 4 item #3 renamed from `_check_catalog_node_refs`. For each `CatalogNodeDef` (`type: <bundle>/nodes/<name>`) in `workflow.nodes`: resolve the catalog entry via `catalog.lookup(ref, expected_type="node")`; import the entry's `.py` file via `importlib.import_module`; introspect the decorated `run` via `inspect.get_type_hints`; check signature, return type against the output-shape table in master spec §4, secrets exist. (The OLD `_check_python_modules` that imported a `body:` file from the YAML is REMOVED — there is no `body:` field anymore. This new function handles the authored-Python-node case via the catalog.)
     - `_check_authz(workflow, catalog, authz_provider)` — §7.7 — internal function name stays `_check_authz` at Phase 3.0 per Round 5 H1's code-side defer (queued for rename to `_check_policy` in `process/qol-policy-rename.md`); user-facing YAML field is the new `policy:` block (Round 5 H1 — renamed from `permissions:`). Rules parse; refs resolve via `parse_three_segment` + `catalog.lookup`. **`from:` STRICT validation (Round 4 item #13 — HARD ERRORS appended to `result.errors`)**: not a 3-seg ref → `WorkflowFromShapeError`; asset_type segment not `agents`/`workflows` → `WorkflowFromAssetTypeError`; ref's target doesn't exist in the catalog → `WorkflowFromNotFound`. The prior R3 #6 `WorkflowFromShapeWarning` (which appended to `result.warnings`) is REMOVED entirely. Workflow's own invocation passes the layer walk via `allow(action, target, *, layers=(PermissionLayer(name="workflow", rules=...), ...), caller_chain=...)` (Round 4 item #7 refactored signature; `PermissionLayer` is the internal class name unchanged at Phase 3.0 per H1).
     - **R3 #2 — `_check_timeouts(workflow)` NEW sub-check**: validate `Workflow.timeout_seconds` (positive int; non-positive → `WorkflowInvalidTimeout` error); validate every node's `timeout_seconds` (positive int; non-positive → `WorkflowInvalidTimeout`); if both workflow-level + per-node set AND per-node > workflow-level, emit `WorkflowTimeoutShadowsNode` WARNING. Per-node timeout fails the individual node only at runtime (`NodeTimeoutExceeded`); workflow-level fires `WorkflowTimeoutExceeded` at next-node boundary. Sub-workflows have independent budgets (no parent-budget propagation). `waiting_for_input` PAUSES workflow timer (runtime contract, documented).
     - **NO `_check_state_collisions` function (per polish-pass Decision P5).** Multiple `set_state` writes to the same key are the canonical pattern for evolving state — last-write-wins in topological order. The validator does NOT look at state keys for collision.
   - **Accumulation semantics**: within each `_check_*` call, gather all findings into a per-call list. At the end of the call: if list len == 0, no-op; if len == 1, raise the single specific exception; if len > 1, raise `ExceptionGroup("<layer-name>", [...])` (stdlib). Across calls (one `_check_*` to the next), FAIL FAST — but before raising, run the next layer's checks in best-effort mode (catch all exceptions including `ExceptionGroup`) and append a "Related issues" tail to the message. ExceptionGroup formatting is stdlib (renders as tree); no custom `__str__` on the group.
   - For depth-16 enforcement: track depth in `_resolve_extends`; on exceed, raise `WorkflowExtendsDepthExceeded` (NOT `WorkflowExtendsCycle` — these are distinct exceptions now).
   - For Python node introspection: use `importlib.import_module` to import; track imports in a per-call list so they can be cleaned up after validation (avoid polluting `sys.modules`). For Step 5, you may import and leave imported; full cleanup discipline lands in Phase 3.1.
   - For type-compat in `_check_schema`: use Pydantic's type matching where possible. Since `binds:` RHS is now ALWAYS Jinja (Step 2 enforces), use `_check_jinja_refs` to verify the `nodes.<X>.<field>` source field exists; precise type-compat is best-effort (unknown for non-trivial Jinja expressions).

2. **`tests/component/test_workflow_validator.py`**:
   - Organize as test classes per sub-section: `TestStructural`, `TestSchema`, `TestJinjaRefs` (new), `TestBranching`, `TestTypeSpecific`, `TestExtends`, `TestCatalogNodeDefIntrospection` (Round 4 item #3 renamed from `TestCatalogNodeRefIntrospection`), `TestAuthz`, `TestTimeouts` (R3 #2 NEW), `TestValidationResultAPI` (R3 #6 NEW — exercises the structured `validate_workflow` returning `ValidationResult`), `TestAccumulation` (new), `TestNodeNotDefined` (new — exercises the trigger cases for `WorkflowNodeNotDefined`), `TestBindCollision` (Round 4 item #11 NEW), `TestFromStrictValidation` (Round 4 item #13 NEW — exercises `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`). NO `TestStateCollisions` (per polish-pass Decision P5 — state-collision detection is removed).
   - For EVERY exception class in master spec §12.1's `WorkflowValidationError` tree — INCLUDING the new + R3-renamed ones — at least one test triggers it (use `validate_workflow_or_raise` to assert raising).
   - Construct Workflow Pydantic models in-memory (no YAML files) to isolate validator logic.
   - Positive cases: a clean workflow returns `ValidationResult(errors=(), warnings=())` AND `validate_workflow_or_raise` returns None.
   - **`WorkflowNodeNotDefined` (new)**: trigger cases each get a test — `edge.from` names a non-existent node; `edge.to` names a non-existent node; `copy_of: X` where X is not defined; `map.apply: X` (R3 #9) where X is not defined. (NOTE: the prior `edge.ref: [X]` trigger case is GONE — edges no longer carry `ref:` per Round 4 item #11.)
   - Structural: `WorkflowUnreachableNode` triggers for a defined-but-unreachable node; does NOT trigger for a `map.apply:` target.
   - Structural: `copy_of:` pointing at a graph-unreachable source raises (treat as either unreachable or unknown ref — Coder picks the most specific class).
   - **Bind-key collision (Round 4 item #11 NEW)**: two edges to the same target node binding the SAME key → `WorkflowBindCollision`. Identifying names include target node, the colliding bind key, and the source nodes of the conflicting edges.
   - **Multi-input edges legal (Round 4 item #11)**: two edges with the same `to` value and DIFFERENT bind keys validate cleanly. AND-strict trigger semantics are documented (no validator check needed at this layer; runtime concern).
   - Branching: both `WorkflowBranchOnNonBranchable` and `WorkflowBranchMissingArm` (R3 #9 renamed) trigger correctly.
   - **Jinja refs (new — predecessor closure via graph reachability per Round 4 item #11)**: `nodes.foo.bar` where `foo` is NOT a graph-reachable transitive ancestor of the consuming node → `WorkflowJinjaRefUnknown(reason="not_predecessor")` (R3 #9 renamed). `nodes.foo.unknown_field` where `foo` is a valid graph ancestor but `unknown_field` isn't in `foo`'s output schema → `WorkflowJinjaRefUnknown(reason="unknown_field")`. `nodes.foo.bar` where `foo` is an authored Python node whose module failed to declare an output schema → `WorkflowJinjaRefUnknown(reason="not_declared")`. Reference from outside a `map` to one of its workers → `WorkflowMapperWorkerNotAddressable` (R3 #9 KEPT). Adding a direct edge from `foo` toward the consuming node makes the ref valid (transitive ancestors also count). **`reason` is typed as `str`** (Round 4 item #9 — NOT `Literal`).
   - **Jinja refs — `binds:` parent special case**: in `binds:` RHS on a given edge, `nodes.<edge.from>.<field>` is allowed even if `edge.from` is not yet a multi-hop ancestor (binds: is literally for parent → target wiring).
   - **Jinja refs (positive)**: `nodes.foo.bar` resolves when `foo` is a graph-reachable transitive ancestor of the consuming node AND `bar` is in `foo`'s output schema.
   - Extends: depth 16 OK, 17 raises `WorkflowExtendsDepthExceeded` (DISTINCT from cycle); cycle raises `WorkflowExtendsCycle`; dupe raises `WorkflowDuplicateNode` / `WorkflowDuplicateEdge`; diamond is idempotent.
   - Catalog node ref introspection: missing `run` → `WorkflowNodeMissingRun`; not decorated → `WorkflowNodeNotDecorated`; bad signature → `WorkflowNodeSignatureInvalid`; bad return type → `WorkflowNodeReturnTypeInvalid`; bogus secret → `WorkflowSecretUnresolved`. (Triggered via a workflow whose YAML has `type: acme/nodes/<broken>` and a catalog with the broken `.py` registered.)
   - **R3 #2 timeouts (new — `TestTimeouts`)**: workflow `timeout_seconds: 60` validates cleanly; `timeout_seconds: -1` → `WorkflowInvalidTimeout` in `result.errors`; `timeout_seconds: 0` → `WorkflowInvalidTimeout`. Same for per-node `timeout_seconds`. Workflow `timeout_seconds: 30` + per-node `timeout_seconds: 60` → `WorkflowTimeoutShadowsNode` in `result.warnings`. Workflow + per-node both unset → no error, no warning.
   - **R3 #6 `ValidationResult` API (new — `TestValidationResultAPI`)**: `validate_workflow` returns `ValidationResult` with `errors` + `warnings` tuples; `is_valid` property is `True` iff `errors == ()`; `from:` shape warning appears in `result.warnings` as `WorkflowFromShapeWarning`, NOT raised. `validate_workflow_or_raise` raises on errors, silent on warnings.
   - **Accumulation (new)**: a workflow with 3 sibling-independent failures in the SAME L3 layer (e.g., 3 nodes each with an unknown bind field) populates `result.errors` with 3 entries; `validate_workflow_or_raise` raises stdlib `ExceptionGroup` with `len(exc.exceptions) == 3`. A workflow with a structural error AND a schema error fails fast on structural in `_or_raise`, but the error message includes a "Related issues" tail mentioning the schema finding.
   - **State mutations as evolving values (NEW positive test, per polish-pass Decision P5)**: a workflow with 3 `set_state` nodes (R3 #9 renamed) writing the SAME key (e.g., `state.attempts` incremented across a graph) validates CLEANLY — no warning, no error. This locks the "evolving state is canonical" semantic.
   - **Authz `from:` STRICT (Round 4 item #13 NEW — `TestFromStrictValidation`)**: rule with `from: random` (not 3-seg) → `WorkflowFromShapeError` in `result.errors` (HARD ERROR, NOT warning). Rule with `from: kolsch/nodes/foo` (3-seg but wrong asset_type) → `WorkflowFromAssetTypeError` in `result.errors`. Rule with `from: kolsch/agents/nonexistent` (3-seg + agents but target missing from catalog) → `WorkflowFromNotFound` in `result.errors`. The prior R3 #6 `WorkflowFromShapeWarning` behavior is REMOVED — the test asserts `result.warnings` does NOT contain `WorkflowFromShapeWarning` (the class no longer exists).

**Step 5 Acceptance Criteria:**

5.1. Every check in master spec §7.1 (structural) implemented and raises the correct exception. INCLUDES: `WorkflowNodeNotDefined` pre-pass (for `edge.from`/`edge.to`/`copy_of:` source/`map.apply:` target — R3 #9 rename `mapper` → `map`; note that `edge.ref` is no longer a trigger source per Round 4 item #11); `WorkflowUnreachableNode` for defined-but-unreachable nodes (excluding `map.apply:` targets); `copy_of:` source-reachability check; **Round 4 item #11 — `WorkflowBindCollision`** (two edges to the same target binding the same key). Multi-input edges are LEGAL (no `WorkflowMultiInputForbidden` — REMOVED).
5.2. Every check in master spec §7.2 (schema) implemented. **`nodes.<X>.<field>` references ARE pre-validated** via Jinja-AST walk + **graph-reachability predecessor closure** (Round 4 item #11 — NOT edge-`ref:`-based) + output-schema field check (the prior "NOT pre-validated" carve-out is GONE). **Round 6 J5/β — only Jinja-marked values are walked**: a value is considered Jinja iff it is a `str` containing `"{{"`; pure YAML literals (non-string, or string without `"{{"`) skip Jinja parsing entirely. Predecessor closure / `nodes.<X>.<field>` validation is unchanged in spirit but only applies to Jinja-marked values. Failures raise **`WorkflowJinjaRefUnknown`** (R3 #9 renamed from `WorkflowNodeRefUnknown`) with the correct `reason: str` value (Round 4 item #9 — typed as `str`; documented values per the locked order: existence → predecessor via graph reachability → schema known → field in schema). `binds:` RHS allows `nodes.<edge.from>.<field>` implicitly. External references to map workers raise `WorkflowMapperWorkerNotAddressable`. **Round 6 S1 — All Jinja parsing routes through `src/workflows/jinja_env.py`'s singleton sandboxed env**; the validator does NOT instantiate bare `jinja2.Environment()`.
5.3. Every check in master spec §7.3 (branching) implemented. Edge `when:` is Jinja and goes through the same ref pre-validation against the same graph-reachability closure (Round 4 item #11).
5.4. Every check in master spec §7.4 (type-specific) implemented for `map` (R3 #9 renamed), `compute` (the new aggregation replacement), workflow node, branch node, filter. The 8 dropped aggregations are NOT checked (they shouldn't reach this point — parser rejects). The `python` discriminator is NOT checked (it doesn't exist — authored Python nodes are `CatalogNodeDef` per Round 4 item #3, handled in §7.6).
5.5. Every check in master spec §7.5 (extends + composition) implemented; depth-16 cap raises `WorkflowExtendsDepthExceeded` (DISTINCT from `WorkflowExtendsCycle`). **Round 6 S5 — `WorkflowExtendsPolicyForbidden` fires** when ANY extended workflow (transitive in the resolved chain) has non-empty `policy:` rules; hard error appended to `result.errors`. `extends:` imports nodes + edges ONLY — never policy.
5.6. Every check in master spec §7.6 (authored Python node introspection) implemented via `_check_catalog_node_defs` (Round 4 item #3 renamed from `_check_catalog_node_refs`). For each `CatalogNodeDef` in `workflow.nodes`, the catalog entry is resolved via `catalog.lookup(ref, expected_type="node")`; the `.py` is imported; introspection uses `inspect.get_type_hints`; return type checked against the output-shape table in master spec §4. NO `body:` field is consulted (it doesn't exist).
5.7. Every check in master spec §7.7 (authz) implemented; uses Step 4's `layers: tuple[PermissionLayer, ...]` API (Round 4 item #7). **Round 4 item #13 — `from:` STRICT validation (HARD ERRORS)**: `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` appended to `result.errors`; raised by `_or_raise`. The prior R3 #6 `WorkflowFromShapeWarning` behavior is REMOVED — the class no longer exists.
5.8. **R3 #6 — `validate_workflow` returns `ValidationResult`** (errors + warnings tuples; `is_valid` property); `validate_workflow_or_raise` is the thin wrapper that raises on errors (warnings silent). Validator raises the SPECIFIC exception class per master spec §12.1 for each failure type — NO catch-all `WorkflowValidationError` for specific failures.
5.9. Every exception class in master spec §12.1's `WorkflowValidationError` tree — INCLUDING **`WorkflowJinjaRefUnknown`** (R3 #9 renamed; all 3 documented reason values), `WorkflowNodeNotDefined` (with all surviving trigger cases — `edge.from`/`edge.to`/`copy_of:`/`map.apply:`; the `edge.ref` trigger is gone per Round 4 item #11), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, **`WorkflowBranchMissingArm`** (R3 #9 renamed from `WorkflowBranchDeadSide`), `WorkflowExtendsDepthExceeded`, **`WorkflowInvalidTimeout`** (R3 #2), **`WorkflowInvalidRef`** (R3 #5; raised by parser), **`WorkflowBindCollision`** (Round 4 item #11 NEW), **`WorkflowFromShapeError`** / **`WorkflowFromAssetTypeError`** / **`WorkflowFromNotFound`** (Round 4 item #13 NEW), **`WorkflowJinjaSecurityError`** (Round 6 S1 NEW — fired at Jinja evaluation when the sandbox blocks an unsafe attribute/callable; raised by `src/workflows/jinja_env.py` wrapping `jinja2.exceptions.SecurityError`), **`WorkflowExtendsPolicyForbidden`** (Round 6 S5 NEW — fired by `_resolve_extends`/`_check_extends_policy` when an extended workflow has non-empty `policy:` rules) — is triggered by at least one unit test. `WorkflowMultiInputForbidden` is NOT triggered (REMOVED per Round 4 item #11). `WorkflowFromShapeWarning` is NOT triggered (REMOVED per Round 4 item #13). **`WorkflowBindMustBeJinja` is NOT triggered** (REMOVED per Round 6 J5/β — binds: now accepts YAML literals OR Jinja-marked strings; parser no longer raises this class).
5.10. **Multi-write state semantics (per polish-pass Decision P5)**: a workflow with multiple `set_state` nodes (R3 #9 renamed) writing the SAME key validates CLEANLY — no warning, no error. There is no `WorkflowStateKeyCollision` check; the validator does NOT look at state keys for collision. Test asserts a 3-write fixture passes the gate without raising and without emitting any warning.
5.11. **Accumulation within layer**: a workflow with N (N > 1) sibling-independent failures in the same L3 layer populates `result.errors` with N entries; `validate_workflow_or_raise` raises stdlib `ExceptionGroup[WorkflowValidationError]`; tests assert `len(exc.exceptions) == N` and that `except* WorkflowBindMustBeJinja` filters by sub-type. N == 1 raises the specific exception directly (NOT wrapped in ExceptionGroup).
5.12. **Fail-fast across layers**: when a structural failure populates errors, `_or_raise` runs the next layer's checks in best-effort mode and appends a "Related issues" tail to the exception message.
5.13. Every error message originating from YAML is STRICT single-line; contains `<file>:<line>` + identifying names + the literal `"Fix: "` token (capital F, colon, space). Assertion: `"\n" not in str(exc) and "Fix: " in str(exc)` for any YAML-origin error.
5.14. **R3 #2 — timeout validation**: `_check_timeouts` enforces positive int for both workflow-level and per-node `timeout_seconds` (non-positive → `WorkflowInvalidTimeout`); emits `WorkflowTimeoutShadowsNode` WARNING when per-node > workflow-level.
5.15. **Round 4 item #13 — `from:` STRICT validation as HARD ERRORS**: `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` surface in `result.errors` (raised by `_or_raise`). `WorkflowFromShapeWarning` (R3 #6) is REMOVED — the class no longer exists; validator never appends it to `result.warnings`.
5.16. **R3 #9 — all renamed exception classes used throughout**: validator code references `WorkflowBindMustBeJinja`, `WorkflowJinjaRefUnknown`, `WorkflowBranchMissingArm` exclusively (NO survivors of `WorkflowBindNotJinja`/`WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide` — grep returns ZERO matches in `src/workflows/validator.py`).

**Step 5 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_validator.py -v`
- **Expect:** all tests pass (~60-90 tests across the 11 sub-section classes, including the new `TestJinjaRefs`, `TestAccumulation`, `TestNodeNotDefined`, `TestTimeouts`, `TestValidationResultAPI`).
- **Fail means:** a validator rule is missing, wrong, or raises the wrong exception class.

**Self-check before handoff:**

- [ ] All 16 Step 5 acceptance criteria met
- [ ] Validator test file exists with classes per sub-section (including the new ones: TestJinjaRefs, TestAccumulation, TestNodeNotDefined, TestTimeouts, TestValidationResultAPI, **TestBindCollision** (Round 4 item #11), **TestFromStrictValidation** (Round 4 item #13), **TestCatalogNodeDefIntrospection** (Round 4 item #3 renamed)). NO `TestStateCollisions` class (polish-pass Decision P5)
- [ ] Every exception class in `WorkflowValidationError` tree triggered by a test — INCLUDING R3 #9 renamed `WorkflowJinjaRefUnknown` (all 3 documented reasons), `WorkflowBranchMissingArm`, `WorkflowNodeNotDefined` (surviving 4 trigger cases — `edge.ref` trigger gone), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, **`WorkflowInvalidTimeout`** (R3 #2), **`WorkflowInvalidRef`** (R3 #5; parser-raised), **`WorkflowBindCollision`** (Round 4 item #11 NEW), **`WorkflowFromShapeError`** / **`WorkflowFromAssetTypeError`** / **`WorkflowFromNotFound`** (Round 4 item #13 NEW), **`WorkflowJinjaSecurityError`** (Round 6 S1 NEW), **`WorkflowExtendsPolicyForbidden`** (Round 6 S5 NEW). `WorkflowMultiInputForbidden` is NEVER triggered (REMOVED). `WorkflowBindMustBeJinja` is NEVER triggered (Round 6 J5/β REMOVED).
- [ ] **R3 #6 — `validate_workflow` returns `ValidationResult`** (errors + warnings); `validate_workflow_or_raise` thin wrapper raises on errors (warnings silent)
- [ ] **Round 4 item #13 — `from:` STRICT HARD ERRORS** in `result.errors` (not warnings); `WorkflowFromShapeWarning` REMOVED — grep for the class name across `src/workflows/` returns ZERO matches
- [ ] **R3 #2 — `_check_timeouts` sub-check** validates positive int for workflow + per-node `timeout_seconds`; emits `WorkflowTimeoutShadowsNode` warning when per-node > workflow-level
- [ ] **R3 #9 — all renamed exception classes used in validator code**: grep for old names (`WorkflowBindNotJinja`/`WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide`) returns ZERO matches
- [ ] Cross-graph `nodes.<X>.<field>` refs ARE pre-validated (prior carve-out is GONE)
- [ ] **Round 6 J5/β — Jinja-AST walk only runs on values that look like Jinja**: a value is walked iff it is a `str` containing `"{{"`. Pure YAML literals skip Jinja parsing. Predecessor closure / `nodes.<X>.<field>` validation unchanged in spirit but only applies to Jinja-marked values
- [ ] **Round 6 S1 — Jinja parsing routes through `src/workflows/jinja_env.py`'s singleton**: validator does NOT instantiate bare `jinja2.Environment()`; grep for `Environment()` / `NativeEnvironment()` in `src/workflows/validator.py` returns ZERO matches (all parsing goes through `JINJA_ENV`)
- [ ] **Round 6 S5 — `_resolve_extends` (or sibling `_check_extends_policy`) raises `WorkflowExtendsPolicyForbidden`** when any extended workflow has non-empty `policy:` rules; test fixture: workflow A extends workflow B; workflow B has policy rules; validating workflow A raises `WorkflowExtendsPolicyForbidden`
- [ ] Jinja-AST walk implemented via `JINJA_ENV.parse()` (NOT regex)
- [ ] **Round 4 item #11 — Predecessor closure computed from graph reachability** (transitive ancestors via inbound edges), NOT from any edge `ref:` declaration (which no longer exists)
- [ ] `binds:` RHS special case: `nodes.<edge.from>.<field>` implicitly allowed
- [ ] `copy_of:` source must be graph-reachable
- [ ] Validator check order locked: existence → predecessor via graph reachability → schema known → field in schema
- [ ] **Round 4 item #11 — Bind-key collision check** (`WorkflowBindCollision`): two edges to the same target binding the same key → raises
- [ ] **Round 4 item #11 — Multi-input edges legal**: no `WorkflowMultiInputForbidden` raised; the class is REMOVED entirely (grep `src/workflows/` returns ZERO matches)
- [ ] Accumulation within layer uses stdlib `ExceptionGroup` (NO custom `WorkflowValidationErrorGroup`)
- [ ] N == 1 raises the specific exception directly (NOT wrapped)
- [ ] Fail-fast across layers appends "Related issues" tail
- [ ] `WorkflowExtendsCycle` and `WorkflowExtendsDepthExceeded` are DISTINCT
- [ ] **NO `_check_state_collisions` function** (per polish-pass Decision P5); validator does NOT look at state keys for collision. Positive test verifies a multi-write-same-key fixture passes the gate cleanly
- [ ] **Round 4 item #3 — `_check_catalog_node_defs`** (renamed from `_check_catalog_node_refs`) replaces the old `_check_python_modules`: no `body:` field is read; introspection keyed off catalog lookup
- [ ] Python node introspection uses `inspect.get_type_hints`, NOT raw `__annotations__`
- [ ] Return type checked against the output-shape table in master spec §4
- [ ] All YAML-origin errors are single-line `__str__()` with the `"Fix: "` token
- [ ] Existing tests still green
- [ ] No execution machinery sneaking in (validator does NOT invoke nodes)

**Handoff trigger:** All boxes checked → "Step 5 Complete — [summary]".

## Step 5 — Reviewer Prompt

> You are reviewing Step 5. Reply "Step 5 Approved" or "Changes requested — [list]".

**What was built in Step 5:** the run-start validation gate. Dual public surface (`validate_workflow -> ValidationResult` + `validate_workflow_or_raise`) covering master spec §7.1–§7.7 PLUS the new Jinja-AST `nodes.<X>.<field>` pre-validation (prior carve-out gone) via **graph-reachability predecessor closure** (Round 4 item #11 — NOT edge-`ref:` lookup; the field no longer exists), `copy_of:` reachability, unreachable-node detection, mapper-worker addressability, `WorkflowNodeNotDefined` pre-pass for unresolved YAML node-name refs, **`WorkflowBindCollision`** check for two edges to the same target binding the same key (Round 4 item #11 NEW), **STRICT `from:` validation** with three hard errors (Round 4 item #13: `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`), accumulation within layer via stdlib `ExceptionGroup` + fail-fast across layers with "Related issues" tail. Per-rule unit tests covering every error class — including the R3 + Round 4 new ones. NO state-collision check (per polish-pass Decision P5 — state mutations are by design). NO `WorkflowMultiInputForbidden` (Round 4 item #11 REMOVED). NO `WorkflowFromShapeWarning` (Round 4 item #13 REMOVED).

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 5.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §7 (all sub-sections), §12.1 (exception hierarchy).

**Acceptance criteria to verify (Step 5):** Verify each of 5.1–5.16. PAY EXTRA ATTENTION to:
- 5.2 (cross-graph ref pre-validation via graph-reachability — Round 4 item #11): use the validator on a hand-crafted workflow with `{{ nodes.foo.unknown }}` where `foo` is a graph-reachable ancestor; assert `WorkflowJinjaRefUnknown(reason="unknown_field")` raises (R3 #9 renamed). Verify the locked check order (existence → predecessor via graph reachability → schema known → field). Verify the `binds:` parent special case. Verify `reason` is `str` (not `Literal`).
- 5.8 (R3 #6 — `ValidationResult` API + specific exception classes — not catch-all): for each failure type, verify the raised exception is the specific subclass via `validate_workflow_or_raise`; verify `validate_workflow` returns a `ValidationResult` with `errors` populated.
- 5.9 (every exception class covered): enumerate master spec §12.1's `WorkflowValidationError` tree (including R3 #9 renamed `WorkflowBindMustBeJinja`/`WorkflowJinjaRefUnknown`/`WorkflowBranchMissingArm`, `WorkflowNodeNotDefined`, R3-new `WorkflowInvalidTimeout`/`WorkflowInvalidRef`/`WorkflowBuiltinShadowed`, and Round 4 new `WorkflowBindCollision` (item #11) + `WorkflowFromShapeError`/`WorkflowFromAssetTypeError`/`WorkflowFromNotFound` (item #13)) and confirm each class has a triggering test. Verify `WorkflowMultiInputForbidden` is NEVER triggered (REMOVED). List any missing classes in your feedback.
- 5.11 (accumulation): construct a 3-failure workflow; assert the raised exception (`_or_raise`) is `ExceptionGroup` and `len(exc.exceptions) == 3`. Confirm a SINGLE-finding case raises the specific exception directly (NOT wrapped). Grep validator source: NO `WorkflowValidationErrorGroup` class.
- 5.12 (related issues tail): construct a structural-then-schema-failure workflow; assert the structural error's message contains a "Related issues" tail referencing the schema finding.
- 5.13 (message contract): pick 3 raised errors and assert each `str()` is strict single-line (`"\n" not in str(exc)`), contains the `<file>:<line>` token, an identifying-name token, AND the literal `"Fix: "` token (capital F, colon, space).
- 5.14 (R3 #2 timeouts): hand-craft a workflow with `timeout_seconds: -5` → assert `WorkflowInvalidTimeout` in `result.errors`; hand-craft one with workflow-level `30` + per-node `60` → assert `WorkflowTimeoutShadowsNode` in `result.warnings` (NOT errors).
- 5.15 (Round 4 item #13 from STRICT): hand-craft a rule with `from: random` → assert `WorkflowFromShapeError` in `result.errors` AND raised by `_or_raise`; hand-craft with `from: kolsch/nodes/foo` → `WorkflowFromAssetTypeError`; hand-craft with `from: kolsch/agents/nonexistent` → `WorkflowFromNotFound`. Verify `WorkflowFromShapeWarning` is NOT in `result.warnings` (the class no longer exists).

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/component/test_workflow_validator.py -v` — record output.
- `podman-compose exec dev uv run pytest tests/component/ -q` — full regression.

**Review checklist:**
- [ ] All 16 Step 5 acceptance criteria demonstrably met
- [ ] Validator structure mirrors master spec §7's sub-section order (helps future reviewers)
- [ ] Every exception class in `WorkflowValidationError` tree has a triggering test (enumerate to verify) — INCLUDING R3 renamed/new + Round 4 new: `WorkflowNodeNotDefined`, `WorkflowJinjaRefUnknown` (3 documented reasons), `WorkflowBindMustBeJinja`, `WorkflowBranchMissingArm`, `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, `WorkflowInvalidTimeout`, `WorkflowInvalidRef`, `WorkflowBuiltinShadowed`, **`WorkflowBindCollision`** (Round 4 item #11), **`WorkflowFromShapeError`** / **`WorkflowFromAssetTypeError`** / **`WorkflowFromNotFound`** (Round 4 item #13). `WorkflowMultiInputForbidden` is NEVER triggered (REMOVED).
- [ ] **R3 #6 — `validate_workflow -> ValidationResult` + `validate_workflow_or_raise` thin wrapper** both implemented; structured API returns errors + warnings tuples; raise wrapper preserves single-vs-ExceptionGroup behavior
- [ ] **R3 #2 — `_check_timeouts` sub-check** present; `WorkflowInvalidTimeout` + `WorkflowTimeoutShadowsNode` triggered by tests
- [ ] **R3 #9 — renamed exception classes** used throughout: grep for `WorkflowBindNotJinja`/`WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide` in `src/workflows/validator.py` returns ZERO matches
- [ ] Cross-graph `nodes.<X>.<field>` refs ARE pre-validated via Jinja AST walk (search the validator code for `jinja2.Environment().parse` or equivalent to confirm)
- [ ] `WorkflowExtendsCycle` and `WorkflowExtendsDepthExceeded` are DISTINCT classes raised by distinct code paths
- [ ] **Round 4 item #11 — Predecessor closure computed from graph reachability** (verify by reading `_check_jinja_refs`); the prior edge-`ref:`-based logic is GONE. Edge-level `ref:` is no longer a parser field. Node-level `collect:` survivors: grep validator source for `collect` returns ZERO matches
- [ ] **Round 4 item #11 — Bind-key collision check** present (`WorkflowBindCollision`); triggered by test with 2 edges binding same key into same target
- [ ] **Round 4 item #11 — `WorkflowMultiInputForbidden` REMOVED**: grep `src/workflows/` returns ZERO matches
- [ ] `binds:` RHS parent special case verified: `nodes.<edge.from>.<field>` works without further declarations
- [ ] `copy_of:` source-reachability check fires when source is unreachable
- [ ] `WorkflowMapperWorkerNotAddressable` fires for external refs to mapper workers
- [ ] **NO state-collision check** (per polish-pass Decision P5): grep validator source for `_check_state_collisions` and `WorkflowStateKeyCollision` — both must return ZERO matches. A positive fixture with 3 `state_set` writes to the same key passes the gate cleanly
- [ ] Accumulation within layer uses stdlib `ExceptionGroup` (NO custom `WorkflowValidationErrorGroup` — grep source)
- [ ] Fail-fast across layers with "Related issues" tail
- [ ] Every YAML-origin error message is strict single-line, contains `<file>:<line>` + identifying names + the literal `"Fix: "` token
- [ ] Validator does NOT invoke any node (no execution machinery)
- [ ] **Round 4 item #3 — `_check_catalog_node_defs`** (renamed) is the introspection path for authored Python nodes (no `body:` field, no `_check_python_modules` survivor); grep `_check_catalog_node_refs` returns ZERO matches
- [ ] **Round 4 item #13 — `from:` STRICT validation**: `WorkflowFromShapeError`, `WorkflowFromAssetTypeError`, `WorkflowFromNotFound` triggered; `WorkflowFromShapeWarning` REMOVED (grep ZERO matches)
- [ ] `inspect.get_type_hints` used for Python node introspection (handles forward refs, generic types correctly)
- [ ] Python node return type compared against output-shape table in master spec §4
- [ ] No regressions

**Handoff trigger:** All boxes → "Step 5 Approved". Fails → "Changes requested — [itemized list]". Each item must cite (a) the master spec §7 sub-section the validator violates, OR (b) the missing exception class from §12.1, OR (c) the Step 5 acceptance criterion (5.1–5.13).

---

# STEP 6 — CLI commands

**Goal**: implement `kolsch workflows list/show/validate/cancel` AND `kolsch nodes list/show` (Round 7 L7-12 — symmetric asset-discovery CLI). Stub `kolsch workflows run` with the Phase 3.1 message. Mount at the root CLI. **R3 #1 — `cancel` command** writes a sidecar `cancel.flag` file into the run directory (Phase 3.0 ships CLI + write contract; Phase 3.1 wires executor polling + orphan sweep). **R3 #6 — `validate` uses `validate_workflow_or_raise`**; `--format json` envelope includes BOTH `errors` AND `warnings` arrays. **R3 #8/#11 — `src/cli/_format.py:render_exception(exc)`** is the single generic JSON formatter (dataclass introspection + ClassVar lookup; renders `caller_message` per R3 #8). **Round 7 L7-12 — symmetric asset-discovery CLI**: master spec §14 documents the cross-asset pattern `kolsch <asset-type> list/show` for `workflows`, `nodes`, `agents`, `tools` (skills + prompts deferred to a follow-up QoL — `process/qol-asset-cli-symmetric.md`); Phase 3.0 adds `nodes list`/`nodes show` symmetric with `workflows list`/`show`.

**Files created/modified:**
- `src/cli/workflows.py` (new) — Click subcommand group for `workflows list/show/validate/cancel/run-stub`
- `src/cli/nodes.py` (new — Round 7 L7-12) — Click subcommand group for `nodes list/show`; implementation symmetric with `src/cli/workflows.py` (shares the `src/cli/_format.py:render()` helper)
- `src/cli/__init__.py` (or wherever subcommands mount — modified) — mount BOTH the `workflows` and `nodes` groups
- `tests/component/test_workflow_cli.py` (new) — CLI tests via `CliRunner` for the `workflows` group
- `tests/component/test_nodes_cli.py` (new — Round 7 L7-12) — CLI tests for `kolsch nodes list/show`

**Effort:** ~1 day.

## Step 6 — Coder Prompt

> You are implementing Step 6 of a 7-step phase. Steps 1-5 are approved.
> Reply "Step 6 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Implement the `kolsch workflows ...` CLI subcommand group. **Five commands**: `list`, `show`, `validate`, `cancel` (R3 #1 NEW), `run` (stub). Each wired to the catalog, parser, validator from prior steps. **AND** implement the symmetric `kolsch nodes ...` CLI subcommand group (Round 7 L7-12) with TWO commands: `list` and `show` — symmetric implementation with `workflows list`/`show`, sharing `src/cli/_format.py:render()` for text/json branching. Master spec §14.1's commands list is expanded to include the `nodes` group; §14.3 per-command data schemas now include `nodes list → {nodes: [{ref, kind, label, secrets_needed}, ...]}` and `nodes show <ref> → {node: {...full structure...}}`.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 6.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §14 (CLI surface).
3. **`src/cli/`** — existing CLI structure. Find how `kolsch agents`, `kolsch tools`, etc. are mounted; mirror.

**What to implement:**

1. **`src/cli/_format.py`** (NEW shared helper) — define `render(*, command: str, data: Any = None, errors: list = None, warnings: list = None, fmt: str) -> str`. Two formats: `text` (current pretty behavior) and `json` (the envelope per master spec §14.3 + §14.4). **R3 #6**: the envelope schema now includes BOTH `errors` AND `warnings` arrays. Click context carries `fmt`; commands pull it from context and thread to `render()`. This is the SINGLE place where text vs json branching happens — no other module may print payload directly.
   - **R3 #11 — `render_exception(exc: WorkflowError) -> dict`** is the single generic JSON formatter. Uses dataclass introspection (via `dataclasses.fields(exc)` or `__dict__`) + ClassVar lookup (`type(exc).identifying_names_keys`) to auto-extract those keys into `identifying_names: {...}` and place all remaining instance fields into `attrs: {...}`. Output dict shape: `{class, summary, caller_message, identifying_names: {...}, attrs: {...}, file, line, docs_anchor}` per master spec §14.4. Works generically across all exception classes — NO per-class formatter functions.
   - **R3 #8 — `caller_message` rendering**: `render_exception` includes the `caller_message: ClassVar[str]` from `type(exc).caller_message` in the output dict. Chat capsule surfaces (per §15.x) render `caller_message` instead of `str(exc)`.

2. **`src/cli/workflows.py`** — Click subcommand group `workflows`:
   - All 5 commands (`list`, `show`, `validate`, `cancel`, `run`-stub) accept `--format text|json`.
   - Default `fmt = 'text'`.
   - `--format` may be specified at the group level (`kolsch --format json workflows ...`) or at the subcommand (`kolsch workflows --format json ...`) — Click handles both naturally.
   - Every command's terminal output goes through `render(...)` — no direct `print` / `click.echo` of the payload.
   - On error, build the per-error JSON dict from `str(exc)` parsing tokens + structured attrs; pass to `render` with `status='error'`.

   Per-command behavior:
   - `kolsch workflows list`:
     - Iterates the catalog for `asset_type="workflow"`.
     - Text: tabular output with columns `ref`, `kind` (workflow|node_library), `description` (truncated to ~50 chars).
     - JSON: envelope with `data = { workflows: [{ref, kind, description}, ...] }`.
     - Exit 0.
   - `kolsch workflows show <ref>`:
     - Resolves ref via catalog. Unknown ref → error envelope/stderr message; exit 1.
     - Parses the workflow (Step 2).
     - Text: prints description, resolved extends chain, permissions block, nodes table (name + type + key config — abbreviate type-specific fields to ~3 columns), edges table, derived input schema (from start node), derived output schema (dict keyed by terminal node names).
     - JSON: envelope with `data = { workflow: {...full structure...}, derived_input_schema, derived_output_schema }`.
     - Exit 0.
   - `kolsch workflows validate <ref>`:
     - Resolves ref via catalog. Unknown ref → error envelope/stderr message; exit 1.
     - Parses, then runs **`validate_workflow_or_raise(...)`** (Step 5, R3 #6 — preserves raise-on-failure pattern).
     - Success — text: prints `valid` followed by derived input + output schemas. JSON: envelope with `data = { workflow_ref, input_schema, output_schema }`.
     - Failure path: traverse `ExceptionGroup.exceptions`; build one error dict per sub-exception via `render_exception(exc)` (R3 #11 generic formatter — uses dataclass introspection + ClassVar lookup; emits `caller_message` per R3 #8). Text: print `str(exc)` (strict single-line per Decision 9) for each on its own line; optionally print structured attributes below. JSON: envelope with `status='error'` and **BOTH `errors` AND `warnings` arrays** (R3 #6); each error/warning object includes `caller_message`.
     - Exit 0 on success; 1 on failure.
   - **`kolsch workflows cancel <run_id>`** (R3 #1 NEW):
     - Resolves `<run_id>` to the run directory (under the workspace's runs root).
     - Writes a sidecar `cancel.flag` file (empty file is fine; timestamp content optional). Atomic write (`tmp + rename`).
     - Text: prints `cancel requested for <run_id>; executor will halt at the next node boundary`. JSON: envelope with `data = { run_id, cancel_flag_path, message }`.
     - Exit 0 on flag-write success; exit 1 if `<run_id>` does not resolve OR the run is already in a terminal state.
     - NOTE: Phase 3.0 ships ONLY the CLI + flag-write contract. Phase 3.1 ships executor cooperation (poll flag between node boundaries; emit `workflow.aborted` event on detection; exit cleanly) AND the orphan sweep (engine startup marks any `status=running` run without a live PID as `failed` with reason `orphaned`; appends a synthetic `workflow.failed` event). Document this 3.0/3.1 split in the command's docstring + the cancellation-protocol section below.
   - `kolsch workflows run <ref>` (stub):
     - Text: prints to stderr `"Workflow execution is not implemented in Phase 3.0. Ships in Phase 3.1."`.
     - JSON: envelope `{ command: "workflows run", status: "error", errors: [{class: "NotImplemented", summary: "Workflow execution is not implemented in Phase 3.0. Ships in Phase 3.1."}] }`.
     - Exit 1.
     - The command is registered (visible in `--help`) so users discover it.

2a. **`src/cli/nodes.py`** — Click subcommand group `nodes` (Round 7 L7-12 — symmetric with `workflows`). Two commands: `list` and `show`. Both accept `--format text|json` via the same group-level OR subcommand-level pattern. Both route terminal output through `src/cli/_format.py:render(...)`. The implementation is symmetric in shape with `src/cli/workflows.py` — DO NOT factor a shared base abstraction at v1; the per-command behaviors are short and clear enough that copy-symmetry beats premature abstraction.

   Per-command behavior:
   - `kolsch nodes list [--bundle <name>] [--format text|json]`:
     - Iterates the catalog for `asset_type="node"`.
     - Optional `--bundle <name>` filter restricts to nodes whose ref's bundle segment matches.
     - Text: tabular output with columns `ref`, `kind` (always `node` at v1; placeholder for future per-node-kind disambiguation), `label` (via `display_label(node)` — Round 7 L7-8), `secrets_needed` (comma-joined; `-` if empty).
     - JSON: envelope with `data = { nodes: [{ref, kind, label, secrets_needed}, ...] }` per master spec §14.3.
     - Exit 0.
   - `kolsch nodes show <ref> [--format text|json]`:
     - Resolves `<ref>` via `catalog.lookup(ref, expected_type="node")`. Unknown ref → error envelope/stderr message; exit 1.
     - Imports + introspects the node file (signature, input model, output model, secrets_needed) — same introspection path as Step 5's validator.
     - Text: prints `ref`, `label` (via `display_label`), `bundle`, `file path`, `signature`, `inputs schema`, `outputs schema`, `secrets_needed`, `description`.
     - JSON: envelope with `data = { node: {ref, bundle, path, label, signature, inputs_schema, outputs_schema, secrets_needed, description} }` per master spec §14.3.
     - Exit 0 on success; exit 1 on unknown ref or introspection failure (the introspection failure path reuses the Step 5 validator's `WorkflowNodeImportFailed` / `WorkflowNodeNotDecorated` / `WorkflowNodeSignatureInvalid` / `WorkflowNodeReturnTypeInvalid` error classes — formatted via `render_exception(exc)`).
   - `kolsch nodes run <ref>` is NOT shipped at Phase 3.0 (would require executor; deferred to Phase 3.1+ together with the broader `kolsch <asset> run` symmetric pattern).
   - Implementation budget: ~80-120 LoC, symmetric with `kolsch workflows list/show`.

3. **`src/cli/__init__.py`** (or wherever subcommands mount) — register the `workflows` group AND the `nodes` group (Round 7 L7-12).

4. **Cancellation and orphan recovery protocol** (R3 #1 NEW SUBSECTION — locked contract; Phase 3.0 ships CLI write, Phase 3.1 ships executor cooperation + orphan sweep):
   - **Sidecar location**: `<workspace>/runs/<run_id>/cancel.flag` (alongside the run's JSONL event log).
   - **Write contract (Phase 3.0)**: `kolsch workflows cancel <run_id>` atomically writes the file (`tmp + rename`); empty file is sufficient (timestamp content optional). CLI does NOT check whether the executor is alive; CLI does NOT signal any process.
   - **Polling discipline (Phase 3.1)**: executor polls `cancel.flag` ONLY between node boundaries (NOT mid-node). On detection: emit `workflow.aborted` event, set run status to `aborted`, exit cleanly. NEVER attempt to rescue hung nodes — operator must `kill -9` for those, then `cancel` reconciles the JSONL status via the orphan sweep.
   - **Orphan sweep (Phase 3.1, engine startup)**: on engine startup, scan `<workspace>/runs/`; for each run with `status=running` in its JSONL tail, check whether the run's PID (recorded in the run directory) is alive. If not alive → mark status `failed` with reason `orphaned`; append a synthetic `workflow.failed` event with `{reason: "orphaned"}`. If a `cancel.flag` exists for an orphan, reason is `aborted` instead.

5. **`tests/component/test_workflow_cli.py`** — Click `CliRunner` tests:
   - `list` returns the expected catalog rows (text default).
   - `show` with valid ref prints expected output (text); with invalid ref exits 1.
   - `validate` with valid workflow exits 0; with invalid workflow exits 1 and prints the exception class. R3 #6: warnings (e.g., `WorkflowFromShapeWarning`) appear in `--format json`'s `warnings` array but exit 0 (warnings don't fail validation).
   - **`cancel` writes sidecar `cancel.flag`** (R3 #1): test creates a fake run directory; `kolsch workflows cancel <run_id>` writes `<run_dir>/cancel.flag`; assert file exists; assert exit 0. Test with non-existent `<run_id>` → exit 1.
   - `run` exits 1 with the Phase 3.1 message.
   - `--help` lists the **five** subcommands.
   - **`--format json` test per command (5 minimum)**: each command produces a well-formed JSON envelope per master spec §14.3.
   - **R3 #6 — `validate --format json` envelope includes both `errors` AND `warnings` arrays**: test on a workflow with a `from:` shape warning + a structural error → assert envelope has `errors: [...]` AND `warnings: [...]`.
   - **R3 #8 — `caller_message` per error**: each error JSON object includes `caller_message` populated from `type(exc).caller_message` ClassVar.
   - **R3 #11 — generic `render_exception` formatter**: test that 3 different exception classes (e.g., `WorkflowBindMustBeJinja`, `WorkflowJinjaRefUnknown`, `WorkflowInvalidTimeout`) all serialize via `render_exception` with the expected `identifying_names: {...}` + `attrs: {...}` shape — keys in `identifying_names` match each class's `identifying_names_keys` ClassVar exactly.
   - **ExceptionGroup → errors array of length N**: `validate` on a multi-error fixture produces an `errors` array with N entries (one per sub-exception).
   - **Text default unchanged**: tests without `--format` verify current human output is preserved.

**Step 6 Acceptance Criteria:**

6.1. `kolsch workflows list` prints catalog entries in tabular form (text) or JSON envelope (json); exit 0.
6.2. `kolsch workflows show <ref>` prints full structure + derived I/O (text) or JSON envelope (json); unknown refs exit 1.
6.3. `kolsch workflows validate <ref>` runs full gate via `validate_workflow_or_raise` (R3 #6); success exits 0 with derived schemas (text or json); failure exits 1 with error class info (text: single-line `str(exc)` per Decision 9; json: BOTH `errors` AND `warnings` arrays per master spec §14.4 + R3 #6; each error/warning includes `caller_message` per R3 #8).
6.4. `kolsch workflows run <ref>` prints Phase 3.1 stub message to stderr; exit 1. Honors `--format` (json stub: `{ command: 'workflows run', status: 'error', errors: [{class: 'NotImplemented', summary: 'Workflow execution is not implemented in Phase 3.0...'}] }`).
6.5. **R3 #1 — `kolsch workflows cancel <run_id>`** writes sidecar `<workspace>/runs/<run_id>/cancel.flag` atomically; exit 0 on success; exit 1 if `<run_id>` does not resolve. Text + `--format json` both supported. The 3.0 contract is CLI + flag-write only; executor cooperation + orphan sweep land Phase 3.1 (documented in command docstring + the §"Cancellation and orphan recovery protocol" subsection).
6.6. The **five** commands (`list`, `show`, `validate`, `cancel`, `run`) appear in `kolsch workflows --help`.
6.7. `--format text|json` is accepted at the group level OR subcommand level.
6.8. `--format json` output is well-formed JSON for both success and failure cases on all 5 commands.
6.9. `src/cli/_format.py` exports `render()` AND `render_exception(exc)` (R3 #11 generic formatter). `render` is the SINGLE place where text vs json branching happens. `render_exception` uses dataclass introspection + `type(exc).identifying_names_keys` ClassVar lookup to auto-build `{class, summary, caller_message, identifying_names: {...}, attrs: {...}, file, line, docs_anchor}` per master spec §14.4 — works generically across ALL exception classes.

6.10. **Round 7 L7-12 — `kolsch nodes list [--bundle <name>] [--format text|json]`** ships. Iterates `asset_type="node"` catalog entries; optional `--bundle <name>` filter restricts by bundle segment of the ref; text default = tabular `ref / kind / label / secrets_needed`; `--format json` envelope per master spec §14.3 with `data = {nodes: [{ref, kind, label, secrets_needed}, ...]}`. Exit 0. Implementation symmetric with `kolsch workflows list`. Label rendered via `display_label(node)` (Round 7 L7-8).

6.11. **Round 7 L7-12 — `kolsch nodes show <ref> [--format text|json]`** ships. Resolves ref via `catalog.lookup(ref, expected_type="node")`; unknown ref → exit 1. Imports + introspects the node file (signature, inputs schema, outputs schema, secrets_needed). Text default = labeled key-value layout; `--format json` envelope per master spec §14.3 with `data = {node: {ref, bundle, path, label, signature, inputs_schema, outputs_schema, secrets_needed, description}}`. Exit 0 on success; exit 1 on unknown ref or introspection failure (reuses Step 5 validator's `WorkflowNodeImportFailed`/`WorkflowNodeNotDecorated`/`WorkflowNodeSignatureInvalid`/`WorkflowNodeReturnTypeInvalid` classes via `render_exception(exc)`). Implementation symmetric with `kolsch workflows show`.

**Step 6 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/component/test_workflow_cli.py -v`
- **Expect:** all CLI tests pass (including 4+ `--format json` tests and the ExceptionGroup-to-errors-array test).

**Manual smoke test:**
```bash
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows --help
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows list
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows list --format json
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows run kolsch/workflows/hello
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows --format json run kolsch/workflows/hello
# R3 #1 cancel command:
mkdir -p /tmp/kolsch-wf-test/runs/wrk_test123
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows cancel wrk_test123
ls /tmp/kolsch-wf-test/runs/wrk_test123/cancel.flag
# Round 7 L7-12 — symmetric nodes CLI:
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes --help
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes list
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes list --bundle kolsch
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes list --format json
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes show kolsch/nodes/echo
podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test nodes show kolsch/nodes/echo --format json
```

**Self-check before handoff:**
- [ ] All 11 Step 6 acceptance criteria met (9 original + 6.10 Round 7 L7-12 `nodes list` + 6.11 Round 7 L7-12 `nodes show`)
- [ ] CLI tests pass (including `--format json` tests on all 5 `workflows` commands + cancel command tests + `nodes list/show` tests)
- [ ] Manual smoke test passes (text default + `--format json` + cancel sidecar write + `kolsch nodes list` + `kolsch nodes show kolsch/nodes/echo`)
- [ ] **Round 7 L7-12 — `kolsch nodes list/show` ships**: `src/cli/nodes.py` registered; symmetric with `src/cli/workflows.py`; both share `src/cli/_format.py:render()`; `tests/component/test_nodes_cli.py` covers `list` (with + without `--bundle` filter), `show` (text + json + unknown ref), and both formats per command
- [ ] `src/cli/_format.py` exists; `render()` is the SINGLE branching point for text vs json; `render_exception(exc)` is the SINGLE generic JSON formatter (R3 #11 — dataclass introspection + ClassVar lookup)
- [ ] `--format` accepted at group OR subcommand level
- [ ] ExceptionGroup → errors array of length N verified by test
- [ ] **R3 #6 — `validate` uses `validate_workflow_or_raise`** AND envelope includes BOTH `errors` AND `warnings` arrays
- [ ] **R3 #8 — every error JSON object includes `caller_message`** from `type(exc).caller_message` ClassVar
- [ ] **R3 #1 — `cancel` command writes sidecar `cancel.flag`** atomically; 3.0/3.1 split documented in command docstring + spec section
- [ ] Text default output unchanged from prior behavior
- [ ] `run` stub does NOT execute anything — pure stderr message + exit 1 (or JSON error envelope)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 6 Complete — [summary]".

## Step 6 — Reviewer Prompt

> You are reviewing Step 6. Reply "Step 6 Approved" or "Changes requested — [list]".

**What was built in Step 6:** `kolsch workflows list/show/validate/cancel/run-stub` commands wired to catalog + parser + validator. All 5 commands accept `--format text|json` per the cross-cutting CLI convention (master spec §14.3 + §14.4). `src/cli/_format.py:render()` is the single shared helper where text vs json branching happens; `render_exception(exc)` is the single generic JSON formatter (R3 #11). `validate` uses `validate_workflow_or_raise` (R3 #6); JSON envelope includes both `errors` AND `warnings` arrays + per-error `caller_message` (R3 #8). `cancel` (R3 #1) writes a sidecar `cancel.flag`; Phase 3.1 wires executor cooperation + orphan sweep.

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 6.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §14 (including §14.3 envelope + §14.4 exception JSON shape).

**Acceptance criteria to verify (Step 6):** Verify each of 6.1–6.11. PAY EXTRA ATTENTION to 6.4 — the `run` stub MUST NOT execute anything; if it does, scope creep into Phase 3.1. Also 6.8 — confirm `--format json` produces a well-formed envelope for both success and failure cases on all 5 `workflows` commands. Also 6.5 — confirm `cancel` writes ONLY the sidecar flag and does NOT signal any process or attempt to cooperate with an executor (Phase 3.1 territory). Also 6.10/6.11 (Round 7 L7-12) — confirm `kolsch nodes list/show` are wired symmetrically with `kolsch workflows list/show`, share the `_format.py:render()` helper, and `nodes show <ref>` re-uses Step 5's validator introspection on failure paths (NOT a re-implementation).

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/component/test_workflow_cli.py -v`
- Manual smoke: `podman-compose exec dev uv run kolsch --root /tmp/kolsch-wf-test workflows list` AND `... workflows list --format json`
- `podman-compose exec dev uv run pytest tests/component/ -q` (regression)

**Review checklist:**
- [ ] All 11 Step 6 acceptance criteria demonstrably met (9 original + 6.10 Round 7 L7-12 `nodes list` + 6.11 Round 7 L7-12 `nodes show`)
- [ ] **Round 7 L7-12 — `src/cli/nodes.py` ships** with `list` + `show` symmetric with `src/cli/workflows.py`; shares `_format.py:render()` for text/json; `--bundle <name>` filter on `list`; `show <ref>` introspection failure path reuses Step 5 validator's error classes via `render_exception(exc)`; `tests/component/test_nodes_cli.py` exists and passes
- [ ] `run` stub does NOTHING beyond printing the Phase 3.1 message + exit 1 (verify by reading the source)
- [ ] **R3 #1 — `cancel` writes ONLY the sidecar flag**: source DOES NOT signal/poll/read any process; protocol section is documented
- [ ] **R3 #6 — `validate` uses `validate_workflow_or_raise`** and JSON envelope includes both `errors` AND `warnings` arrays
- [ ] **R3 #8 — every error JSON object includes `caller_message`** populated from ClassVar
- [ ] **R3 #11 — `render_exception` is the SINGLE generic JSON formatter**; grep `src/cli/workflows.py` for per-class formatter functions returns ZERO; reviewer manually traces an error from raise to JSON via `render_exception`
- [ ] `src/cli/_format.py` exists; `render()` is the SINGLE branching point — no other module prints payload directly (grep `click.echo` / `print` in `src/cli/workflows.py` returns ZERO direct payload prints)
- [ ] `--format text|json` accepted at group level OR subcommand level (Click)
- [ ] `--format json` envelope shape matches master spec §14.3 (`{command, status, data, errors, warnings}`)
- [ ] Failure-path `errors` array shape matches master spec §14.4 (one object per sub-exception of any wrapping `ExceptionGroup`)
- [ ] Output formats are readable (text default unchanged; json well-formed)
- [ ] Exit codes correct (0 success, non-zero failure regardless of `--format`)
- [ ] No regressions

**Handoff trigger:** All boxes → "Step 6 Approved". Fails → "Changes requested — [list]".

---

# STEP 7 — Fixture suite + starter bundle

**Goal**: write ~25-35 workflow YAML fixtures covering every validator rule and error class. Add the starter bundle workflow + node. Wire golden tests that discover and run every fixture. **Round 4 item #1 — Fixture annotation conventions LOCKED**: top-of-file YAML comment block annotates the expected outcome; file naming follows `valid-*.yaml` / `invalid-*.yaml` / `warning-*.yaml`; a README documents the convention; a `conftest.py` ships a `make_workflow(**overrides)` helper for Pydantic-model-construction tests.

**Files created/modified:**
- `tests/golden/workflows/*.yaml` (~25-35 new files)
- `tests/golden/workflows/_README.md` (new — Round 4 item #1) — documents fixture annotation conventions (top-of-file comment block; naming `valid-*.yaml` / `invalid-*.yaml` / `warning-*.yaml`)
- `tests/golden/workflows_test.py` (new) — discovers + runs fixtures; parses the top-of-file annotation block
- `tests/workflows/conftest.py` (new — Round 4 item #1) — ships `make_workflow(**overrides) -> Workflow` helper for in-memory Pydantic model construction
- `bundles/kolsch/workflows/hello.yaml` (new) — starter example
- `bundles/kolsch/nodes/echo.py` (new) — starter authored node

**Round 4 item #1 — Fixture annotation format LOCKED**:
- Top-of-file YAML comment block (2 lines):
  ```yaml
  # expected_error: WorkflowJinjaRefUnknown
  # expected_error_field: nodes.fetch.binds.url
  ```
- Valid fixtures use `# expected: valid`.
- Warning fixtures use `# expected_warning: <ClassName>`.
- Multi-error fixtures use `# expected_errors: <Class1>, <Class2>, <Class3>`.
- File naming: `valid-<scenario>.yaml` / `invalid-<scenario>.yaml` / `warning-<scenario>.yaml`.
- `tests/golden/workflows/_README.md` documents the convention so future authors don't need to reverse-engineer it.
- Exception-class shape goldens (separate from YAML fixtures — verifying the JSON shape of a raised exception) KEEP their sidecar JSON pattern from master spec §14.4 (unchanged).

**Effort:** ~2 days.

## Step 7 — Coder Prompt

> You are implementing Step 7 of a 7-step phase. Steps 1-6 are approved. THIS IS THE FINAL STEP.
> Reply "Step 7 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Write ~25-35 workflow YAML fixtures at `tests/golden/workflows/` covering every validator rule and every error class — including R3-renamed classes (`WorkflowJinjaRefUnknown` ×3 reasons, `WorkflowBranchMissingArm`), `WorkflowNodeNotDefined` (surviving trigger cases — `edge.from`/`edge.to`/`copy_of:`/`map.apply:`; the `edge.ref` trigger is GONE per Round 4 item #11), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsDepthExceeded`, R3-new (`WorkflowInvalidTimeout` (R3 #2), `WorkflowInvalidRef` (R3 #5), `WorkflowBuiltinShadowed` (R3 #5)), `WorkflowTimeoutShadowsNode` (warning), **Round 4 NEW**: `WorkflowBindCollision` (item #11 — two edges to the same target binding the same key), `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13 — the `from:` STRICT triple, all HARD ERRORS), and **Round 6 NEW**: `WorkflowJinjaSecurityError` (S1 — fixture exercising a bash command that attempts the canonical Jinja escape pattern → expect `WorkflowJinjaSecurityError`), `WorkflowExtendsPolicyForbidden` (S5 — fixture: child workflow extending a library workflow with non-empty `policy:` rules → expect `WorkflowExtendsPolicyForbidden`). NO `WorkflowStateKeyCollision` fixture (the class is deleted per polish-pass Decision P5 — state mutations across `set_state` nodes are by design). NO `WorkflowMultiInputForbidden` fixture (the class is REMOVED per Round 4 item #11 — multi-input edges are now LEGAL). NO `WorkflowFromShapeWarning` fixture (the class is REMOVED per Round 4 item #13 — `from:` shape problems are HARD ERRORS now). **NO `WorkflowBindMustBeJinja` fixture** (the class is REMOVED per Round 6 J5/β — `binds:` now accepts YAML literals OR Jinja-marked strings; any prior `invalid-bind-must-be-jinja.yaml` fixture that relied on a bare RHS being rejected MUST be REMOVED or REPURPOSED — it no longer triggers an error). **Update `set_state` fixtures to natural literal forms** (per Round 6 J5/β): `set_state: {keys: {status: pending, attempts: 0}}` parses as `pending` string + `0` int; no Jinja markers required for literal values. ADD `valid-single-node-runnable.yaml` (one node + `graph: { edges: [] }`) asserting `kind="workflow"` + validator passes (per polish-pass Decision P3). **R3 #5 — ALL workflow YAMLs use full 3-seg `type:` refs** (e.g., `type: kolsch/nodes/bash`); no 1-seg shorthand survives. **R3 #9 — node types use renamed names**: `map`/`set_state`/`delete_state` everywhere. **R3 #7 — `description:` replaces `summary:`** in every fixture. **Round 4 item #1 — Fixture annotation conventions LOCKED**: top-of-file YAML comment block (see Goal section above); `valid-*.yaml` / `invalid-*.yaml` / `warning-*.yaml` naming; ship `tests/golden/workflows/_README.md` + `tests/workflows/conftest.py:make_workflow(**overrides)` helper. Add a starter bundle workflow + Python node that validate cleanly. Wire golden tests that discover all fixtures and assert their declared outcomes.

**Authoritative references — read BEFORE coding:**

1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 7. Note the fixture list in the Technical Context's "File layout to create".
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §4 (catalog — for fixture-by-node-type coverage of the FINAL 14 BUILT-IN types; the 8 dropped aggregations get an "invalid" fixture proving they're rejected with a helpful fix-path; the dropped `python` discriminator also gets an "invalid" fixture proving the catalog-ref path is required), §7 (validator rules — for fixture-by-rule coverage), §12.1 (error classes — for fixture-by-error coverage including the new ones), §12.3 (error message contract — fixtures assert the `<file>:<line>` and literal `"Fix: "` tokens), §13 (permissions — fixtures cover `from:` field).

**What to implement:**

1. **`tests/golden/workflows/`** — ~22-30 YAML fixture files. Per master spec coverage requirements, EVERY error class in `WorkflowValidationError`'s tree must have at least one `invalid-*.yaml` fixture; EVERY BUILT-IN node type's config shape (all 14) must have at least one `valid-*.yaml` fixture exercising it, PLUS at least one `valid-*.yaml` exercising the CatalogNodeRef path (`type: <bundle>/nodes/<name>` for authored Python nodes); one fixture proves multi-error accumulation via stdlib `ExceptionGroup`; one fixture proves refs unification (`<bundle>/workflows/<name>` ref in a **`policy:`** block resolves — Round 5 H1 — renamed from `permissions:`); one fixture proves `from:` field shape; one fixture proves the parser rejects the 8 dropped aggregation node types; one fixture proves rejection of the dropped `python` discriminator; one fixture proves RESERVED bundle names are rejected. **Round 5 H1 — every fixture using the old `permissions:` field MUST be renamed to `policy:`**; reviewer greps the fixture directory and asserts ZERO `permissions:` occurrences at the top level. **Round 5 H4 — every fixture using `input.<key>` Jinja references MUST be renamed to `inputs.<key>`**; reviewer greps for the bare singular `input.` namespace and asserts ZERO occurrences (the plural `inputs.` is the only allowed form). **Round 5 H1 — any golden JSON/event-kind fixtures use the renamed wire kinds** per H1's table: `workflow.run.completed`, `workflow.node.started`, `workflow.node.completed`, `workflow.node.failed`, `workflow.node.timed_out`, `workflow.node.logged`, `workflow.node.emitted` (author-emitted: `node.author.<bundle>.<subkind>` per R1), `policy.decision.made`, `model.request.sent`, `model.response.received`, `workflow.tool.called`. Reviewer greps the fixture/golden tree and asserts ZERO occurrences of the legacy event-kind names (`workflow.completed`, `workflow.node_started`, `node.log`, `node.emit`, `authz.decision`, `model.request`, `model.response`, `tool.called`).
   - File naming: `valid-<scenario>.yaml` or `invalid-<scenario>.yaml` (or `warning-<scenario>.yaml` for lint warnings) — locked per Round 4 item #1.
   - Each fixture starts with a top-of-file YAML comment block (Round 4 item #1):
     - Valid: `# expected: valid`
     - Invalid: `# expected_error: <ExceptionClassName>` plus optional `# expected_error_field: <YAML path>` (e.g., `# expected_error_field: nodes.fetch.binds.url`)
     - Multi-error: `# expected_errors: <Class1>, <Class2>, <Class3>` (asserts all surface in accumulation)
     - Warning: `# expected_warning: <WarningClassName>`
   - Minimum coverage set (~25-35 fixtures):
     - **Valid (~15)**: minimal workflow (uses `type: kolsch/nodes/bash`); linear pipeline; branching workflow with edge `when:`; extends-union; `copy_of:` (the rename, replacing `instance_of:`); node-library (no `graph:` key entirely); **single-node-runnable** (`valid-single-node-runnable.yaml` — one node + `graph: { edges: [] }` — asserts `kind="workflow"` and validator passes, per polish-pass Decision P3); workflow with permissions block including a `from: kolsch/agents/triage` rule (valid 3-seg + valid asset_type + catalog hit); **`type: kolsch/nodes/map`** (R3 #9 renamed) + `kolsch/nodes/compute` (replacing aggregations); sub-workflow with `<bundle>/workflows/<name>` ref; **`valid-multi-input-fan-in.yaml`** (Round 4 item #11 NEW — two edges to the same target with DIFFERENT bind keys → multi-input is now the canonical fan-in mechanism); `compute` node demoing a Jinja idiom that replaces an old aggregation (e.g., `{{ items | sum(attribute='score') }}`); **authored Python node invoked via `type: acme/nodes/echo`** (`CatalogNodeDef` path per Round 4 item #3); **state-evolves-across-writes** (3 `set_state` nodes — R3 #9 renamed — writing the same key, `state.attempts`, validates cleanly per polish-pass Decision P5); **`valid-with-timeout.yaml`** (R3 #2 — workflow-level `timeout_seconds: 60` + per-node `timeout_seconds: 30` validate cleanly); **`valid-with-description.yaml`** (R3 #7 — uses common `description:` on multiple node types); **`valid-binds-literals-and-jinja.yaml`** (Round 6 J5/β NEW — demonstrates mixed forms: one bind with bare YAML literal `count: 5`, one with bare string `status: pending`, one with Jinja-marked `url: "{{ nodes.fetch.url }}"` → all parse cleanly; only the Jinja-marked value triggers Jinja evaluation in the validator's AST walk); the `state-evolves-across-writes` fixture's `set_state` nodes use **natural literal forms** per J5/β (`keys: {status: pending, attempts: 0}` — bare string + bare int, NOT `keys: {status: "{{ 'pending' }}", attempts: "{{ 0 }}"}`).
     - **Invalid (~20+)**: ambiguous-start; no-start; cycle (DAG); **node-not-defined-from-edge** (edge.from names non-existent node → `WorkflowNodeNotDefined`); **node-not-defined-from-edge-to** (edge.to names non-existent node → `WorkflowNodeNotDefined`); **node-not-defined-from-copy-of** (copy_of source not defined → `WorkflowNodeNotDefined`); **node-not-defined-from-map-apply** (map.apply target not defined — R3 #9 renamed — → `WorkflowNodeNotDefined`); **REMOVED `bind-must-be-jinja` fixture** (Round 6 J5/β — `WorkflowBindMustBeJinja` class is REMOVED; bare `foo.bar` RHS is now a legal string literal, not an error. Any prior fixture using this trigger is DELETED or repurposed to demonstrate the new literal-or-Jinja semantics); **`invalid-bind-collision.yaml`** (Round 4 item #11 NEW — two edges to same target binding the SAME key → `WorkflowBindCollision`); **`invalid-bind-type-mismatch.yaml`** (Round 4 item #11 NEW — rendered bind value fails receiver Pydantic strict validation, e.g., 'int' rejects `"5"` string → `WorkflowBindTypeMismatch`. Note: this fires at NODE-FIRE time, not parse/validate; the fixture documents the runtime contract — Phase 3.1+ test exercises it); **jinja-ref-not-predecessor** (`WorkflowJinjaRefUnknown` reason=not_predecessor — Jinja reference to a node NOT in the graph-reachability ancestor closure per Round 4 item #11); **jinja-ref-unknown-field** (reason=unknown_field); **jinja-ref-not-declared** (reason=not_declared, e.g., authored Python node failing introspection); **unreachable-node** (`WorkflowUnreachableNode`); **map-worker-external-ref** (`WorkflowMapperWorkerNotAddressable`); branch-on-non-branchable; **branch-missing-arm** (R3 #9 renamed from `branch-dead-side` → `WorkflowBranchMissingArm`); **extends-cycle** (`WorkflowExtendsCycle`); **extends-depth-exceeded** (`WorkflowExtendsDepthExceeded` — 17-deep chain); extends-unresolved; duplicate-node; duplicate-edge; map-worker-missing-field; workflow-call-cycle; node-import-failed; node-missing-run; node-not-decorated; node-signature-invalid; node-return-type-invalid; **`invalid-1seg-rejected.yaml`** (R3 #5 NEW — `type: bash` bare 1-seg → `WorkflowInvalidRef`); **dropped-aggregation-type** (`type: sum` → `WorkflowInvalidRef` — 1-seg; fix-path mentions `compute` + Jinja idiom); **dropped-python-type** (`type: python` → `WorkflowInvalidRef` — 1-seg; fix-path mentions `type: <bundle>/nodes/<name>`); **`invalid-builtin-shadowed.yaml`** (R3 #5 NEW — user-authored `bundles/kolsch/nodes/bash.py` → `WorkflowBuiltinShadowed` at catalog scan); **`invalid-timeout-negative.yaml`** (R3 #2 NEW — `timeout_seconds: -5` → `WorkflowInvalidTimeout`); **`invalid-timeout-zero.yaml`** (R3 #2 — `timeout_seconds: 0`); **legacy-instance-of** (`instance_of: foo` → rejected with fix-path suggesting `copy_of:`); **legacy-edge-condition** (edge with `condition:` → rejected with fix-path suggesting `when:`); **legacy-node-collect** (node-level `collect: [...]` → rejected with fix-path suggesting additional edges per Round 4 item #11); **`invalid-edge-ref-rejected.yaml`** (Round 4 item #11 NEW — YAML edge with `ref: [...]` is REJECTED via `extra="forbid"`; fix-path suggests adding additional edges); **`invalid-from-shape.yaml`** (Round 4 item #13 NEW — `from: random` (not 3-seg) → `WorkflowFromShapeError` HARD ERROR); **`invalid-from-asset-type.yaml`** (Round 4 item #13 NEW — `from: kolsch/nodes/foo` (3-seg but asset_type is `nodes`, not `agents`/`workflows`) → `WorkflowFromAssetTypeError`); **`invalid-from-not-found.yaml`** (Round 4 item #13 NEW — `from: kolsch/agents/nonexistent` → `WorkflowFromNotFound`); **`invalid-jinja-sandbox-escape.yaml`** (Round 6 S1 NEW — a `kolsch/nodes/bash` node whose `command:` field includes the canonical Jinja escape pattern `{{ ''.__class__.__mro__[1].__subclasses__() }}` — or another representative payload from the S1 ~10-payload set — triggers `WorkflowJinjaSecurityError` at Jinja evaluation. NOTE: this fires when the value is rendered at parse-time `_check_jinja_refs` walk OR at runtime; the fixture documents the contract. The companion `tests/security/test_jinja_sandbox_escape.py` exercises the full ~10-payload set at the `JINJA_ENV` singleton level — this fixture is the YAML-shaped end-to-end smoke test); **`invalid-extends-policy-forbidden.yaml`** (Round 6 S5 NEW — child workflow `acme/workflows/child` has `extends: [acme/workflows/library]`; `acme/workflows/library` declares non-empty `policy:` rules; validating the child raises `WorkflowExtendsPolicyForbidden`. The fixture's `_README.md` cross-reference explains: `policy:` blocks are NOT mergeable across `extends:` — extended workflow's `policy:` applies only when invoked directly. Shared policy patterns belong in `bundle.yaml` / `platform.yaml`); **multi-error-accumulation** (3 sibling-independent failures surface as a single `ExceptionGroup` report — fixture test asserts `len(exc.exceptions) == 3`).
     - **Warnings (~2)**: **`warning-timeout-shadows-node.yaml`** (R3 #2 — workflow `timeout_seconds: 30` + per-node `60` → `WorkflowTimeoutShadowsNode` in `result.warnings`). NO `from:` shape warning fixture (per Round 4 item #13 — `WorkflowFromShapeWarning` is REMOVED; `from:` shape problems are HARD ERRORS now). NO state-key-collision fixture (per polish-pass Decision P5 — `WorkflowStateKeyCollision` is deleted; state mutations are by design). NO multi-input fixture (per Round 4 item #11 — `WorkflowMultiInputForbidden` is REMOVED; multi-input edges are LEGAL).
     - **REMOVED fixtures (per Round 4 item #11)**: any prior `invalid-multi-input.yaml` (or similar) — the rule is GONE. Any prior `valid-edge-ref-fan-in.yaml` (or similar) — the edge `ref:` field is GONE; the fan-in pattern is now `valid-multi-input-fan-in.yaml` instead.

2. **`tests/golden/workflows_test.py`**:
   - Pytest test that discovers `tests/golden/workflows/*.yaml`.
   - For each file, parses the **top-of-file YAML comment block** (Round 4 item #1 LOCKED format) to determine expected outcome: `# expected: valid` / `# expected_error: <ClassName>` / `# expected_error_field: <YAML path>` / `# expected_errors: <C1>, <C2>, ...` / `# expected_warning: <ClassName>`. The annotation parser is shared with `tests/workflows/conftest.py` so the convention has a single source of truth.
   - For valid: parses + validates via `validate_workflow_or_raise`; expects no exception.
   - For invalid: parses + validates; expects the named exception class to be raised. If `expected_error_field` is present, also asserts the raised exception's identifying-names field matches the YAML path. For `# expected_errors:` (multi-error accumulation), asserts the raised exception is `ExceptionGroup` and that EACH named class appears in `exc.exceptions` (use `except*` filtering for clarity).
   - **R3 #6 — For warning**: parses + calls `validate_workflow` (structured API; NOT `_or_raise`); asserts the named warning class appears in `result.warnings`. Asserts `result.is_valid is True` (warnings don't fail validation).
   - For YAML-origin error fixtures: assert the raised exception's `str()` is strict single-line (`"\n" not in str(exc)`), contains the `<file>:<line>` token, AND contains the literal `"Fix: "` token (capital F, colon, space — per Decision 9). ALSO assert STRICT-no-code in the Fix portion (per master spec §12.3 + polish-pass Decision P4): `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`. NO fixture's assertion may rely on Jinja delimiters or backticks appearing in the Fix portion of the error message.
   - **R3 #8 — `caller_message` assertion**: for at least 5 distinct exception classes triggered by fixtures, assert `type(exc).caller_message` is non-empty, contains NO class name, NO `{{`/`{%`, NO `<file>:<line>` token (style rules per R3 #8).
   - **R3 #12 — `docs_anchor` assertion**: for every YAML-origin error message that includes a `docs/workflows#anchor` reference, parse the anchor name and assert the anchor exists in `docs/workflows.md`. (Reviewer manually verifies the docs file deliverable.)
   - Parameterize via `pytest.mark.parametrize` so each fixture is a separate test case with a readable name.

2a. **`tests/golden/workflows/_README.md`** (Round 4 item #1 NEW): documents the fixture annotation conventions (top-of-file comment format, naming conventions, `expected_error_field` syntax). One page; written FOR future fixture authors.

2b. **`tests/workflows/conftest.py`** (Round 4 item #1 NEW): ships `make_workflow(**overrides) -> Workflow` helper that constructs a minimal valid `Workflow` Pydantic model and applies the keyword overrides (deep-merging into the canonical-minimum). Used by validator/parser unit tests that need to construct workflows in-memory without YAML round-tripping. Example: `wf = make_workflow(nodes={"foo": {"type": "kolsch/nodes/bash", "command": "echo"}})`.

3. **`bundles/kolsch/workflows/hello.yaml`** — starter example workflow:
   - Minimal runnable workflow with 1-2 nodes.
   - Uses only node types whose config is sufficient at 3.0 (no execution needed — the workflow doesn't run, it just validates).
   - **R3 #5 — uses full 3-seg `type:` refs** throughout: a `type: kolsch/nodes/bash` node + an authored Python node referenced via `type: kolsch/nodes/echo` (NOT `type: python` — that discriminator is gone; NOT bare `type: bash` — 1-seg shorthand is removed), connected with a Jinja `binds:` showing `{{ nodes.bash.stdout }}`.
   - **R3 #7 — uses `description:`** on each node (renamed from `summary:`).
   - Optionally: include a `type: kolsch/nodes/compute` node with a Jinja-idiom example as a tiny demo of the new aggregation replacement.

4. **`bundles/kolsch/nodes/echo.py`** — starter authored node:
   - `@node` decorated `run` function.
   - Pydantic input model + output model matching the output-shape table (per master spec §4 — for a python node, return either a `BaseModel`, a `bool`, or a `tuple[bool, BaseModel]`).
   - Trivial logic (echo input to output) — never actually invoked in Phase 3.0 but must pass validator introspection.

**Step 7 Acceptance Criteria:**

7.1. ~25-35 fixtures exist at `tests/golden/workflows/`.
7.2. EVERY exception class in master spec §12.1's `WorkflowValidationError` tree has at least one `invalid-*.yaml` fixture — INCLUDING R3 #9 renamed: **`WorkflowJinjaRefUnknown`** (all 3 documented reasons), **`WorkflowBranchMissingArm`**, AND `WorkflowNodeNotDefined` (surviving 4 trigger cases — `edge.from`/`edge.to`/`copy_of:`/`map.apply:`; the `edge.ref` trigger is GONE per Round 4 item #11), `WorkflowUnreachableNode`, `WorkflowMapperWorkerNotAddressable`, `WorkflowExtendsCycle` (distinct from depth-exceeded), `WorkflowExtendsDepthExceeded` (distinct from cycle), R3-new `WorkflowInvalidRef` (R3 #5), `WorkflowBuiltinShadowed` (R3 #5), `WorkflowInvalidTimeout` (R3 #2), **Round 4 NEW**: `WorkflowBindCollision` (item #11), `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13), **Round 6 NEW**: `WorkflowJinjaSecurityError` (S1 — `invalid-jinja-sandbox-escape.yaml`), `WorkflowExtendsPolicyForbidden` (S5 — `invalid-extends-policy-forbidden.yaml`). Warning `WorkflowTimeoutShadowsNode` (R3 #2) has `warning-*.yaml` fixture. NO `WorkflowMultiInputForbidden` fixture (Round 4 item #11 REMOVED). NO `WorkflowFromShapeWarning` fixture (Round 4 item #13 REMOVED — the class no longer exists). **NO `WorkflowBindMustBeJinja` fixture** (Round 6 J5/β REMOVED — `binds:` now accepts YAML literals OR Jinja-marked strings; any prior fixture using this trigger is DELETED or repurposed).
7.3. EVERY built-in node type in master spec §4 (14 types — R3 #9 renames: bash, agent, workflow, noop, read_file, write_file, list_files, **set_state**, **delete_state**, filter, **map**, branch, compute, user_input) has at least one `valid-*.yaml` fixture exercising its config shape **using the full 3-seg ref `type: kolsch/nodes/<name>`** (R3 #5 — no 1-seg shorthand). Authored Python nodes (`CatalogNodeDef` path per Round 4 item #3: `type: <bundle>/nodes/<name>`) have at least one valid fixture. The 8 dropped aggregations have ONE shared `invalid-dropped-aggregation-type.yaml` (`type: sum` → `WorkflowInvalidRef` because 1-seg). The dropped `python` discriminator has ONE `invalid-dropped-python-type.yaml` (`type: python` → `WorkflowInvalidRef`).
7.4. Each fixture has a top-of-file YAML comment block annotation (Round 4 item #1 LOCKED format): `# expected:` / `# expected_error:` (+ optional `# expected_error_field:`) / `# expected_errors:` / `# expected_warning:`. `tests/golden/workflows/_README.md` documents the convention. `tests/workflows/conftest.py` ships the `make_workflow(**overrides)` helper.
7.5. `tests/golden/workflows_test.py` discovers all fixtures and asserts the expected outcome. For YAML-origin errors, asserts the message is strict single-line and contains the `<file>:<line>` form + the literal `"Fix: "` token. For `# expected_errors:` (multi-error accumulation) fixtures, asserts the raised exception is stdlib `ExceptionGroup` and each named class appears in `exc.exceptions`.
7.6. `bundles/kolsch/workflows/hello.yaml` validates cleanly. Uses Jinja `binds:` syntax. Optionally demos `compute`.
7.7. `bundles/kolsch/nodes/echo.py` is `@node`-decorated and passes validator introspection (signature + return type matching output-shape table).
7.8. `kolsch workflows list` in a fresh workspace surfaces `kolsch/workflows/hello`.
7.9. **Renames covered**: at least one `valid-` fixture uses `copy_of:`; at least one uses edge `when:`. At least one `invalid-` fixture each catches the legacy `instance_of:`, edge-level `condition:`, and node-level `collect:` with fix-path errors. **Round 4 item #11**: at least one `invalid-` fixture catches edge-level `ref:` (the field is GONE — `invalid-edge-ref-rejected.yaml`).
7.10. **Round 4 item #11 — Multi-input edges covered**: at least one `valid-` fixture (`valid-multi-input-fan-in.yaml`) uses two edges to the same target with different bind keys → canonical fan-in pattern (multi-input edges are now LEGAL). At least one `invalid-` fixture has a Jinja `nodes.X.Y` ref where X is NOT a graph-reachable ancestor → `WorkflowJinjaRefUnknown(reason="not_predecessor")`. At least one `invalid-` fixture (`invalid-bind-collision.yaml`) has two edges to the same target binding the SAME key → `WorkflowBindCollision`. At least one `invalid-` fixture (`invalid-bind-type-mismatch.yaml`) documents the runtime contract for `WorkflowBindTypeMismatch`.
7.11. **Refs unification covered**: at least one `valid-` fixture exercises a `<bundle>/workflows/<name>` ref inside a **`policy:`** block (Round 5 H1 — renamed from `permissions:`); resolves via `parse_three_segment` + `catalog.lookup`. At least one `valid-` fixture uses `type: <bundle>/nodes/<name>` to invoke an authored Python node (`CatalogNodeDef` path per Round 4 item #3).
7.12. **`from:` field STRICT validation covered (Round 4 item #13)**: at least one `valid-` fixture has a rule with `from: kolsch/agents/<name>` (3-seg + agents + catalog hit). Three `invalid-` fixtures: `invalid-from-shape.yaml` (not 3-seg → `WorkflowFromShapeError`), `invalid-from-asset-type.yaml` (3-seg but wrong asset_type → `WorkflowFromAssetTypeError`), `invalid-from-not-found.yaml` (3-seg + valid asset_type but target missing → `WorkflowFromNotFound`). NO `warning-from-shape.yaml` fixture — the warning class is REMOVED per Round 4 item #13.
7.13. **Multi-error accumulation covered**: at least one `invalid-` fixture (with `# expected_errors:`) surfaces 3+ sibling-independent findings; the golden test asserts the raised exception is stdlib `ExceptionGroup` and `len(exc.exceptions) >= 3`.
7.14. **Single-node runnable covered (per polish-pass Decision P3)**: `valid-single-node-runnable.yaml` exists (one node + `graph: { edges: [] }`); golden test asserts `kind="workflow"` (NOT `node_library`) AND validator passes cleanly. NO `WorkflowStateKeyCollision` fixture exists (per polish-pass Decision P5 — the class is deleted; state mutations are by design). One `valid-state-evolves-across-writes.yaml` fixture (3 `state_set` writes to the same key) validates cleanly with no warning, no error.
7.15. **Strict-no-code in Fix portion (per master spec §12.3 + polish-pass Decision P4)**: every YAML-origin error fixture's golden assertion checks `"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`. No fixture authors rely on Jinja delimiters or backticks appearing in the Fix portion of the error message.
7.16. **R3 #5 — All fixtures use full 3-seg `type:` refs** (no 1-seg shorthand survives). `invalid-1seg-rejected.yaml` exists (`type: bash` bare → `WorkflowInvalidRef`). `invalid-builtin-shadowed.yaml` exists (user authoring at `bundles/kolsch/nodes/bash.py` → `WorkflowBuiltinShadowed` at catalog scan).
7.17. **R3 #2 — Timeout fixtures**: `valid-with-timeout.yaml` (workflow + per-node positive ints validate cleanly); `invalid-timeout-negative.yaml` (`timeout_seconds: -5` → `WorkflowInvalidTimeout`); `invalid-timeout-zero.yaml` (`timeout_seconds: 0`); `warning-timeout-shadows-node.yaml` (per-node > workflow → `WorkflowTimeoutShadowsNode` in `result.warnings`).
7.18. **R3 #7 — `description:` in fixtures**: at least one `valid-` fixture uses common `description:` on multiple node types (renamed from `summary:`). No fixture uses `summary:` (grep returns ZERO matches).
7.19. **R3 #1 — Cancel command CLI tests** (in `tests/component/test_workflow_cli.py`, exercised through the fixture flow): verify `kolsch workflows cancel <run_id>` writes sidecar `cancel.flag`.
7.20. **R3 #12 — `docs/workflows.md` anchor coverage**: every `docs_anchor` referenced in YAML-origin error messages (e.g., `see docs/workflows#binds-rhs`, `#ref`, `#timeout`, `#cancel`, `#node-types`) resolves to an actual anchor in `docs/workflows.md`. The docs file itself is created as a parallel Phase 3.0 deliverable (~16 anchors: `#binds-rhs`, `#ref`, `#extends`, `#fan-in`, `#single-input`, `#node-types`, `#copy-of`, `#map`, `#branch`, `#authz-from`, `#timeout`, `#cancel`, `#trace-id`, `#error-format`, `#secrets`, `#chat-capsules`); reviewer verifies each is present.

7.21. **Round 4 item #1 — Fixture annotation conventions LOCKED**: `tests/golden/workflows/_README.md` exists and documents the top-of-file YAML comment annotation block (`# expected:` / `# expected_error:` + optional `# expected_error_field:` / `# expected_errors:` / `# expected_warning:`) plus the file-naming convention (`valid-` / `invalid-` / `warning-` prefixes). `tests/workflows/conftest.py` exists and exports `make_workflow(**overrides) -> Workflow` helper used by Pydantic-model-construction tests. Both files are referenced by `tests/golden/workflows_test.py` so the annotation parser and the in-memory helper have a single source of truth.

7.22. **Round 5 H1 — Fixture YAMLs use `policy:` (not `permissions:`)** and renamed wire event kinds: `grep -rn 'permissions:' tests/golden/workflows/` returns ZERO matches (every fixture uses `policy:` at the top level — the H1 rename to the unified user-facing term that matches OPA/Cerbos/AWS IAM Policy). Any event-kind goldens (sidecar JSON fixtures verifying JSON shape per master spec §14.4) use the renamed wire kinds per H1's table: `workflow.run.completed`, `workflow.node.{started,completed,failed,timed_out,logged,emitted}`, `policy.decision.made`, `model.request.sent`, `model.response.received`, `workflow.tool.called`. `grep -rn 'authz\.decision\|workflow\.completed\|workflow\.node_started\|workflow\.node_completed\|workflow\.node_failed\|workflow\.node_timed_out\|node\.log\|node\.emit\|model\.request[^_]\|model\.response[^_]\|tool\.called' tests/golden/` returns ZERO matches. (R1 — author-emitted event goldens use the wire form `node.author.<bundle>.<subkind>`.)

7.23. **Round 5 H4 — Fixture YAMLs use `inputs.*` (plural) not `input.*` (singular)**: `grep -rn '{{ *input\.' tests/golden/workflows/` returns ZERO matches (every fixture using the bind-namespace in Jinja config strings uses `inputs.<key>` — plural — per the H4 rename to match the Python `inputs: ModelType` param convention). Reviewer verifies any prior fixture that used `{{ input.user }}` / `{{ input.orders }}` (e.g., the C1 Pattern A example) has been renamed to `{{ inputs.user }}` / `{{ inputs.orders }}`.

7.24. **Round 6 J5/β — Binds-and-literals fixture present + legacy fixture removed**: `valid-binds-literals-and-jinja.yaml` exists demonstrating mixed forms (bare YAML literals like `count: 5` / `status: pending` AND Jinja-marked strings like `url: "{{ nodes.fetch.url }}"`) — all parse cleanly; only Jinja-marked values trigger Jinja-AST walk in the validator. Any prior `invalid-bind-must-be-jinja.yaml` (or equivalent) fixture that relied on `WorkflowBindMustBeJinja` is REMOVED (the class is REMOVED — the fixture no longer triggers an error). `set_state` fixtures use natural literal forms: `set_state: {keys: {status: pending, attempts: 0}}` — bare string + bare int, not Jinja-wrapped. Reviewer greps `tests/golden/workflows/` for `WorkflowBindMustBeJinja` in `expected_error:` annotations — must return ZERO matches.

7.25. **Round 6 S1 — `WorkflowJinjaSecurityError` fixture present**: `invalid-jinja-sandbox-escape.yaml` exists with a `kolsch/nodes/bash` node whose `command:` (or another Jinja-evaluated field) contains a canonical Jinja-to-Python escape pattern from the S1 ~10-payload set (e.g., `{{ ''.__class__.__mro__[1].__subclasses__() }}`, `{{ lipsum.__globals__ }}`). Validating raises `WorkflowJinjaSecurityError`. The companion `tests/security/test_jinja_sandbox_escape.py` (Step 1) exercises the full ~10-payload set at the `JINJA_ENV` singleton level — this YAML fixture is the end-to-end smoke test.

7.26. **Round 6 S5 — `WorkflowExtendsPolicyForbidden` fixture present**: `invalid-extends-policy-forbidden.yaml` exists where a child workflow extends a library workflow (or any other workflow) that declares non-empty `policy:` rules. Validating the child raises `WorkflowExtendsPolicyForbidden`. The fixture's `_README.md` cross-reference (or inline comment) explains the rationale: `policy:` blocks are NOT mergeable across `extends:` — the extended workflow's `policy:` applies only when invoked DIRECTLY but is NOT inherited by extenders; shared policy patterns belong in `bundle.yaml` / `platform.yaml`.

7.27. **Round 6 R6-R1 — Author-emitted event-kind goldens use `node.<bundle>.<subkind>` wire form**: any golden JSON/event-kind fixtures asserting author-emitted event kinds use the `node.<bundle>.<subkind>` form (e.g., `node.acme.ticket_classified`) — NOT the R5 R1 `node.author.<bundle>.<subkind>` form (the "author" segment was DROPPED per R6-R1). Reviewer greps `tests/golden/` for `node.author.` and asserts ZERO matches.

7.28. **Round 7 CV1 — `valid-from-json-filter.yaml` fixture demonstrating the `from_json` Jinja filter**: a workflow with a `kolsch/nodes/bash` node that emits a JSON document to stdout (e.g., `command: 'echo ''{"first_name":"Ada"}'''`) followed by a downstream node that consumes the parsed value via `{{ nodes.fetch.stdout | from_json | attr('first_name') }}` in a `binds:` RHS or other Jinja-evaluated field. Validates cleanly. The fixture is the v1 end-to-end smoke test for the core `from_json` filter (the filter itself is registered in `src/workflows/jinja_env.py` per Round 7 CV1 + Step 1's 1.22; the contribution chokepoint for future core filters lives at `docs/explanation/jinja-extensions.md`). NO bundle-supplied filter fixture (bundles cannot register filters).

7.29. **Round 7 A7-1 — Test patterns use `NodeContext.for_test()`**: any new tests written in this step (or migrations of prior tests) that need a `NodeContext` construct via `NodeContext.for_test(**overrides)` instead of hand-rolling all 7 fields. Reviewer greps `tests/` for any new `NodeContext(workflow_run_id=..., invocation_id=..., bundle=..., ...)` direct constructions in the Step 7 diff — the `for_test()` helper is now the documented test-pattern entry point. Existing tests outside the Step 7 diff are NOT required to migrate in this step (a cross-cutting migration sweep is queued as a separate QoL); but the test-pattern documentation in `tests/workflows/conftest.py` (or its README) cross-references `NodeContext.for_test()` as the recommended pattern.

7.30. **Round 7 A7-3 — Test patterns documentation rewords "byte-equality on replay"**: the §19 / Testing Strategy reword lands in the master spec (§19.3). Within THIS Phase 3.0 spec, any inline reference to the prior wording (e.g., in test-pattern docs, fixture annotation tooling, or future executor-replay hooks) reads "semantic equality modulo volatile fields (timestamps, all *Id prefix-typed fields)". Phase 3.1's designer chooses the normalization mechanism when shipping the executor. NO canonicalization tool is committed in Phase 3.0. Reviewer verifies: any test-pattern doc that references replay-comparison semantics uses the new wording; no fixture asserts byte-equality of event JSON.

**Step 7 Tests:**

- **Run:** `podman-compose exec dev uv run pytest tests/golden/workflows_test.py -v`
- **Expect:** all fixtures pass per their declared outcomes.

- **Run (smoke):** in a fresh workspace, `podman-compose exec dev uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive && podman-compose exec dev uv run kolsch --root /tmp/kolsch-fresh workflows list`
- **Expect:** output includes `kolsch/workflows/hello`. `kolsch workflows validate kolsch/workflows/hello` exits 0 with `valid`.

**Self-check before handoff:**
- [ ] All 30 Step 7 acceptance criteria met (23 original + 7.24 J5/β + 7.25 S1 + 7.26 S5 + 7.27 R6-R1 + 7.28 Round 7 CV1 from_json fixture + 7.29 Round 7 A7-1 for_test test pattern + 7.30 Round 7 A7-3 semantic-equality reword)
- [ ] Every error class in master spec §12.1 has a fixture (enumerate to confirm) — including R3 renamed (`WorkflowJinjaRefUnknown`/`WorkflowBranchMissingArm`), R3-new (`WorkflowInvalidRef`/`WorkflowBuiltinShadowed`/`WorkflowInvalidTimeout`), `WorkflowNodeNotDefined` (surviving 4 cases) and all the other new ones; R3 warning (`WorkflowTimeoutShadowsNode`); **Round 4 NEW**: `WorkflowBindCollision` (item #11), `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13); **Round 6 NEW**: `WorkflowJinjaSecurityError` (S1), `WorkflowExtendsPolicyForbidden` (S5). `WorkflowBindMustBeJinja` has NO fixture (Round 6 J5/β REMOVED)
- [ ] **Round 4 item #11 — NO `WorkflowMultiInputForbidden` fixture** (class is REMOVED). NO `valid-edge-ref-fan-in.yaml` (edge `ref:` field is GONE)
- [ ] **Round 4 item #13 — NO `WorkflowFromShapeWarning` fixture** (class is REMOVED — `from:` shape problems are HARD ERRORS now)
- [ ] Every BUILT-IN node type in master spec §4 has a valid fixture (enumerate the 14 to confirm — R3 #9 renames: `map`/`set_state`/`delete_state`); the 8 dropped aggregations have a shared invalid fixture; the dropped `python` discriminator has an invalid fixture
- [ ] `CatalogNodeDef` path (authored Python node via `type: <bundle>/nodes/<name>`) has a valid fixture (Round 4 item #3 renamed)
- [ ] Renames covered: `copy_of:` valid, `instance_of:` invalid; edge `when:` valid, edge `condition:` invalid; **edge-level `ref:` INVALID (Round 4 item #11 — `invalid-edge-ref-rejected.yaml`)**; node-level `collect:` invalid
- [ ] **Round 4 item #11 — Multi-input fan-in fixture present** (`valid-multi-input-fan-in.yaml` — two edges to same target with different bind keys)
- [ ] **Round 4 item #11 — Bind-collision fixture present** (`invalid-bind-collision.yaml` — two edges binding the same key)
- [ ] **Round 4 item #11 — Bind-type-mismatch fixture present** (`invalid-bind-type-mismatch.yaml` — runtime contract documentation)
- [ ] Refs unification fixture present (`<bundle>/workflows/<name>` in permissions; `<bundle>/nodes/<name>` as a node type)
- [ ] **Round 4 item #13 — `from:` STRICT fixtures present**: valid (`from: kolsch/agents/...`); 3 invalid fixtures (`invalid-from-shape.yaml`, `invalid-from-asset-type.yaml`, `invalid-from-not-found.yaml`) for the 3 hard-error cases
- [ ] **Round 4 item #1 — Annotation conventions LOCKED**: top-of-file YAML comment block per locked format; `_README.md` documents the convention; `tests/workflows/conftest.py:make_workflow(**overrides)` helper ships
- [ ] Multi-error accumulation fixture present; golden test asserts `ExceptionGroup` + `len(exc.exceptions) >= 3`
- [ ] **R3 #5 — `invalid-1seg-rejected.yaml` present** (`type: bash` bare → `WorkflowInvalidRef`)
- [ ] **R3 #5 — `invalid-builtin-shadowed.yaml` present** (user authoring at `bundles/kolsch/nodes/bash.py` → `WorkflowBuiltinShadowed`)
- [ ] **R3 #5 — Bundle-name reservation REMOVED**: NO fixture asserts a bundle named `bash`/`agent`/etc. is rejected (the old reservation is gone; a bundle named `bash` is FINE)
- [ ] **`valid-single-node-runnable.yaml` present** (one node + `graph: { edges: [] }` → `kind="workflow"` + validator passes; polish-pass Decision P3)
- [ ] **NO `WorkflowStateKeyCollision` fixture present** (polish-pass Decision P5 — the class is deleted); `valid-state-evolves-across-writes.yaml` (3 `set_state` writes — R3 #9 renamed — to the same key) validates cleanly
- [ ] **R3 #2 — Timeout fixtures present**: `valid-with-timeout.yaml`, `invalid-timeout-negative.yaml`, `invalid-timeout-zero.yaml`, `warning-timeout-shadows-node.yaml`
- [ ] **R3 #7 — `description:` in fixtures**: at least one valid fixture uses common `description:`; grep `summary:` in `tests/golden/workflows/` returns ZERO matches
- [ ] **R3 #1 — Cancel command test exists** (CLI tests exercise `kolsch workflows cancel <run_id>` writing sidecar)
- [ ] **R3 #12 — `docs/workflows.md` anchor coverage verified**: every `docs_anchor` referenced in error messages resolves; reviewer manually checks the docs file
- [ ] Starter bundle visible end-to-end; uses `type: kolsch/nodes/echo` (NOT `type: python`, NOT bare `type: echo`)
- [ ] **R3 #5 — All fixtures use full 3-seg `type:` refs**: grep for bare `type: bash`, `type: agent`, etc. in `tests/golden/workflows/` returns ZERO matches (except in `invalid-1seg-rejected.yaml`)
- [ ] Golden tests pass; YAML-origin error assertions check for the literal `"Fix: "` token AND strict-no-code (no `{{`/`{%`/backticks in Fix portion)
- [ ] **Round 5 H1 — `grep -rn 'permissions:' tests/golden/workflows/` returns ZERO matches**; all fixtures use `policy:`. Event-kind goldens use renamed wire kinds (`workflow.run.completed`, `workflow.node.*`, `policy.decision.made`, `model.request.sent`, `model.response.received`, `workflow.tool.called`); grep for legacy kinds (`authz.decision`, `workflow.completed`, `workflow.node_started`, `node.log`, `node.emit`, `model.request`, `model.response`, `tool.called`) returns ZERO matches in `tests/golden/`
- [ ] **Round 5 H4 — `grep -rn '{{ *input\.' tests/golden/workflows/` returns ZERO matches** (every fixture uses the plural `inputs.*` namespace, not the singular `input.*`). The C1 Pattern A example fixture (if present) uses `{{ inputs.user }}` / `{{ inputs.orders }}`
- [ ] **Round 6 J5/β — `valid-binds-literals-and-jinja.yaml` present**; any prior `invalid-bind-must-be-jinja.yaml` (or `expected_error: WorkflowBindMustBeJinja`) annotation is REMOVED; grep `tests/golden/workflows/` for `WorkflowBindMustBeJinja` returns ZERO matches; `set_state` fixtures use natural literal forms (bare YAML values, no Jinja markers for literals)
- [ ] **Round 6 S1 — `invalid-jinja-sandbox-escape.yaml` present** triggering `WorkflowJinjaSecurityError`; companion `tests/security/test_jinja_sandbox_escape.py` (Step 1) covers ~10 canonical escape payloads at the singleton level
- [ ] **Round 6 S5 — `invalid-extends-policy-forbidden.yaml` present** triggering `WorkflowExtendsPolicyForbidden` (child workflow extends a workflow with non-empty `policy:` rules)
- [ ] **Round 6 R6-R1 — Author-emitted event-kind goldens use `node.<bundle>.<subkind>` wire form**; grep `tests/golden/` for `node.author.` returns ZERO matches
- [ ] **Round 7 CV1 — `valid-from-json-filter.yaml` fixture present** demonstrating the `from_json` Jinja filter end-to-end (e.g., bash node emits JSON to stdout; downstream Jinja consumes via `{{ ... | from_json | attr('first_name') }}`); validates cleanly
- [ ] **Round 7 A7-1 — New tests use `NodeContext.for_test(**overrides)`** instead of hand-rolling all 7 fields; grep the Step 7 diff for any new direct `NodeContext(workflow_run_id=...)` constructions and migrate them; `tests/workflows/conftest.py` (or its README) cross-references `for_test()` as the recommended pattern
- [ ] **Round 7 A7-3 — Any inline reference to test/replay equality** uses the new wording "semantic equality modulo volatile fields (timestamps, all *Id prefix-typed fields)" (NOT "byte-equality on replay"); no fixture asserts byte-equality of event JSON; Phase 3.1's executor designer chooses normalization mechanism
- [ ] Existing tests still green

**Handoff trigger:** All boxes checked → "Step 7 Complete — [summary]". When Reviewer approves Step 7, Coordinator runs Phase-level Integration Tests; on green, the phase ships.

## Step 7 — Reviewer Prompt

> You are reviewing Step 7. THIS IS THE FINAL STEP.
> Reply "Step 7 Approved" or "Changes requested — [list]". After approval, Coordinator runs
> Phase-level Integration Tests.

**What was built in Step 7:** ~25-35 fixture YAMLs covering every error class (including the R3 renamed: `WorkflowBindMustBeJinja`/`WorkflowJinjaRefUnknown`/`WorkflowBranchMissingArm`; R3 new: `WorkflowInvalidRef`/`WorkflowBuiltinShadowed`/`WorkflowInvalidTimeout`; `WorkflowNodeNotDefined` with surviving 4 trigger cases; **Round 4 NEW**: `WorkflowBindCollision` (item #11), `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13)). Every BUILT-IN node type (the final 14 with R3 #9 renames: `map`/`set_state`/`delete_state`) plus the `CatalogNodeDef` path for authored Python nodes (Round 4 item #3 renamed). The renames (`copy_of:`, edge `when:`). **Round 4 item #11 fixtures**: `valid-multi-input-fan-in.yaml`, `invalid-bind-collision.yaml`, `invalid-bind-type-mismatch.yaml`, `invalid-edge-ref-rejected.yaml`. **Round 4 item #13 fixtures**: 3 invalid `from:` fixtures (`shape` / `asset-type` / `not-found`). Refs unification, multi-error accumulation via stdlib `ExceptionGroup`, R3 #5 fixtures (`invalid-1seg-rejected.yaml`, `invalid-builtin-shadowed.yaml`), R3 #2 timeout fixtures (valid + invalid + shadowing warning), the single-node-runnable fixture (`valid-single-node-runnable.yaml` per polish-pass Decision P3), a multi-write state-evolves fixture, R3 #7 `description:` fixture. **Round 4 item #1**: top-of-file YAML annotation block locked; `tests/golden/workflows/_README.md` documents the convention; `tests/workflows/conftest.py:make_workflow(**overrides)` helper ships. Golden test runner; starter bundle (uses `type: kolsch/nodes/echo`). R3 #1 cancel CLI tests. Strict-no-code Fix-portion assertion enforced on every YAML-origin error fixture (per master spec §12.3 + polish-pass Decision P4). R3 #12 — every `docs_anchor` referenced in error messages resolves to an actual anchor in `docs/workflows.md`. NO `WorkflowMultiInputForbidden` fixture (Round 4 item #11 REMOVED). NO `WorkflowFromShapeWarning` fixture (Round 4 item #13 REMOVED).

**Authoritative references — read BEFORE reviewing:**
1. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-build.md`** — Step 7. Note the minimum coverage set in the Coder Prompt.
2. **`/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md`** — §4 (14 built-in node types + CatalogNodeRef path), §7 (validator rules), §12.1 (error classes including the new ones), §12.3 (error message contract), §13 (permissions + `from:`).

**Acceptance criteria to verify (Step 7):** Verify each of 7.1–7.30. The CRITICAL coverage checks:
- 7.2: enumerate every `WorkflowValidationError` subclass in master spec §12.1 (including R3 renamed `WorkflowJinjaRefUnknown`/`WorkflowBranchMissingArm`, R3-new `WorkflowInvalidRef`/`WorkflowBuiltinShadowed`/`WorkflowInvalidTimeout`, `WorkflowNodeNotDefined` x surviving 4 cases, **Round 4 NEW `WorkflowBindCollision` (item #11)**, **Round 4 NEW `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13)**, **Round 6 NEW `WorkflowJinjaSecurityError` (S1)**, **Round 6 NEW `WorkflowExtendsPolicyForbidden` (S5)**, etc.). For each, find the fixture that triggers it. Report any missing. **Confirm `WorkflowStateKeyCollision` is NOT in the hierarchy (polish-pass Decision P5) — no fixture must reference it.** **Confirm `WorkflowMultiInputForbidden` is REMOVED (Round 4 item #11)** — no fixture references it. **Confirm `WorkflowFromShapeWarning` is REMOVED (Round 4 item #13)** — no fixture references it. **Confirm `WorkflowBindMustBeJinja` is REMOVED (Round 6 J5/β)** — no fixture has `expected_error: WorkflowBindMustBeJinja`; any prior fixture relying on this trigger is DELETED. Confirm OLD names (`WorkflowBindNotJinja`/`WorkflowNodeRefUnknown`/`WorkflowBranchDeadSide`) appear NOWHERE in fixtures.
- 7.3: enumerate every BUILT-IN node type (master spec §4 — 14 types with R3 #9 renames). For each, find the fixture that exercises it via `type: kolsch/nodes/<name>` (R3 #5 — full 3-seg). Report any missing. Confirm the 8 dropped aggregations have a shared invalid fixture; confirm the dropped `python` discriminator has an invalid fixture; confirm at least one valid fixture uses the `CatalogNodeDef` path (Round 4 item #3 renamed; `type: <bundle>/nodes/<name>` for a non-kolsch bundle).
- 7.9 / 7.10 / 7.11 / 7.12 / 7.13: confirm fixtures for the renames; **Round 4 item #11**: multi-input fan-in valid + bind-collision invalid + bind-type-mismatch invalid + edge-`ref:`-rejected invalid; refs unification; **Round 4 item #13**: `from:` STRICT — valid (agents/workflows + catalog hit) + 3 invalid hard-error fixtures (shape / asset-type / not-found); multi-error accumulation (via `ExceptionGroup`) each exist.
- 7.14: confirm `valid-single-node-runnable.yaml` exists with one node + `graph: { edges: [] }`, resolves to `kind="workflow"` (NOT `node_library`), and validates cleanly (polish-pass Decision P3). Confirm `valid-state-evolves-across-writes.yaml` exists (3 `set_state` writes — R3 #9 renamed — to the same key) and validates cleanly with no warning.
- 7.15: confirm every YAML-origin error fixture's golden assertion checks STRICT-no-code in the Fix portion (`"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`) per master spec §12.3.
- 7.16: **R3 #5 fixtures**: confirm `invalid-1seg-rejected.yaml` exists (`type: bash` → `WorkflowInvalidRef`); confirm `invalid-builtin-shadowed.yaml` exists. Confirm NO fixture asserts a bundle named `bash` is rejected (the old reservation is gone).
- 7.17: **R3 #2 timeout fixtures** (4): valid + invalid-negative + invalid-zero + warning-shadows-node.
- 7.18: **R3 #7 `description:`**: at least one valid fixture exercises common `description:`; grep `summary:` in `tests/golden/workflows/` returns ZERO matches.
- 7.20: **R3 #12 `docs/workflows.md` anchor verification**: every `docs_anchor` referenced in error messages from fixtures resolves to an actual anchor; the docs file exists with the ~16 anchors.

**Tests to run:**
- `podman-compose exec dev uv run pytest tests/golden/workflows_test.py -v` — record output
- Manual smoke: fresh workspace + `kolsch workflows list`, `show`, `validate` for `kolsch/workflows/hello`
- `podman-compose exec dev uv run pytest tests/ -q` — full regression

**Review checklist:**
- [ ] All 30 Step 7 acceptance criteria demonstrably met (23 original + 7.24 J5/β + 7.25 S1 + 7.26 S5 + 7.27 R6-R1 + 7.28 Round 7 CV1 from_json fixture + 7.29 Round 7 A7-1 for_test test pattern + 7.30 Round 7 A7-3 semantic-equality reword)
- [ ] Every error class in master spec §12.1's validation tree has a fixture — INCLUDING R3 renamed (`WorkflowJinjaRefUnknown`/`WorkflowBranchMissingArm`), R3-new (`WorkflowInvalidRef`/`WorkflowBuiltinShadowed`/`WorkflowInvalidTimeout`), `WorkflowNodeNotDefined` (surviving 4 cases — `edge.ref` trigger gone) and the other new ones; **Round 4 NEW**: `WorkflowBindCollision` (item #11), `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound` (item #13); **Round 6 NEW**: `WorkflowJinjaSecurityError` (S1), `WorkflowExtendsPolicyForbidden` (S5). `WorkflowStateKeyCollision` is NOT in the hierarchy (polish-pass Decision P5) — verify no fixture references it. `WorkflowMultiInputForbidden` REMOVED (Round 4 item #11) — verify no fixture references it. `WorkflowFromShapeWarning` REMOVED (Round 4 item #13) — verify no fixture references it. **`WorkflowBindMustBeJinja` REMOVED (Round 6 J5/β) — verify no fixture references it; the `valid-binds-literals-and-jinja.yaml` fixture demonstrates the new semantics**
- [ ] Every BUILT-IN node type in master spec §4 (14 types with R3 #9 renames `map`/`set_state`/`delete_state`) has a valid fixture **using `type: kolsch/nodes/<name>` (R3 #5 — full 3-seg)**; the 8 dropped aggregations have a shared invalid fixture; the dropped `python` discriminator has an invalid fixture; `CatalogNodeDef` path has at least one valid fixture (Round 4 item #3 renamed)
- [ ] Renames covered: `copy_of:` valid, `instance_of:` invalid (with fix-path); edge `when:` valid, edge `condition:` invalid (with fix-path); **edge-level `ref:` INVALID (Round 4 item #11 `invalid-edge-ref-rejected.yaml`)**, node-level `collect:` invalid (with fix-path)
- [ ] **Round 4 item #11 — Multi-input fan-in fixture present** (`valid-multi-input-fan-in.yaml`); `invalid-bind-collision.yaml` + `invalid-bind-type-mismatch.yaml` present
- [ ] Refs unification covered (workflow ref in permissions block resolves; node ref as `type:` value)
- [ ] **Round 4 item #13 — `from:` STRICT fixtures**: `valid-` case with `from: kolsch/agents/...`; THREE invalid fixtures (`invalid-from-shape.yaml`, `invalid-from-asset-type.yaml`, `invalid-from-not-found.yaml`) — all HARD ERRORS, NOT warnings
- [ ] **Round 4 item #1 — Annotation conventions LOCKED**: top-of-file YAML comment block; `tests/golden/workflows/_README.md` present; `tests/workflows/conftest.py:make_workflow` helper present
- [ ] Multi-error accumulation fixture present (`# expected_errors:` annotation); test asserts `ExceptionGroup` wrapping
- [ ] **R3 #5 — `invalid-1seg-rejected.yaml` + `invalid-builtin-shadowed.yaml` present**; NO fixture asserts bundle-name reservation (REMOVED)
- [ ] **`valid-single-node-runnable.yaml` present** (polish-pass Decision P3 — one node + `graph: { edges: [] }` resolves to `kind="workflow"` + validator passes)
- [ ] **`valid-state-evolves-across-writes.yaml` present** (polish-pass Decision P5 — 3 `set_state` writes — R3 #9 renamed — to same key validate cleanly); NO state-key-collision warning fixture
- [ ] **R3 #2 — Timeout fixtures present** (valid + 2 invalid + 1 warning)
- [ ] **R3 #7 — Common `description:` fixture present**; grep `summary:` in fixtures returns ZERO
- [ ] **R3 #1 — Cancel command exercised** in `tests/component/test_workflow_cli.py`
- [ ] **R3 #12 — `docs/workflows.md` exists** with all ~16 anchors; every `docs_anchor` in fixture error messages resolves
- [ ] YAML-origin error fixtures' assertions check the message is strict single-line AND contains `<file>:<line>` AND contains the literal `"Fix: "` token AND **strict-no-code in Fix portion** (`"{{" not in str(exc) and "{%" not in str(exc) and "`" not in str(exc)`) per master spec §12.3 + polish-pass Decision P4
- [ ] **R3 #8 — `caller_message` non-empty + style-clean** for every fixture-triggered exception class (no class name; no `{{`/`{%`; no `<file>:<line>`)
- [ ] Each fixture has the correct top-line annotation
- [ ] Starter bundle visible end-to-end (uses `type: kolsch/nodes/echo` — full 3-seg per R3 #5)
- [ ] **Round 5 H1 — Fixture YAMLs use `policy:` (not `permissions:`)**; legacy event-kind names absent from goldens; reviewer greps `tests/golden/` for both
- [ ] **Round 5 H4 — Fixture YAMLs use plural `inputs.*` (not singular `input.*`)** in Jinja config strings; reviewer greps for `{{ input.`
- [ ] Golden tests + full regression pass

**Handoff trigger:**
- All boxes → "Step 7 Approved". Signal Coordinator to run Phase-level Integration Tests.
- Fails → "Changes requested — [itemized list with citations]". Each missing fixture must cite the master-spec section it leaves uncovered.

---

## Phase completion ritual (Coordinator)

After Step 7 is Approved:

1. Run all 4 Phase-level Integration Tests (above).
2. If all pass: mark this spec `Status: Done`. Append a "Shipped: <commit SHA> on <date>" line under the metadata block.
3. Update master spec `/Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase3.0-workflow-spec.md` §18.2's Phase 3.0 line with shipping commit SHA and date.
4. Notify stakeholder. The next phase (3.1 — Minimum executor) can begin.
