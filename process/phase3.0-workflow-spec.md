# Phase 3.0 — Workflows

**Status: DRAFT — complete specification of locked design decisions across 21 sections, refreshed for the 14-node-type catalog and the Round 4 + Round 5 + Round 6 + Round 7 design locks. Round 6 layered in: Jinja sandbox contract (§2.7.2, S1), bash shell-injection deferral note (§4.1.1, S2), authored Python `@node` trust model (§1.5, S3), `ctx.bundle` field with engine-set spoof resistance (§5.5, S4), `extends:` does not import `policy:` + `WorkflowExtendsPolicyForbidden` hard error (§2.1, §7.5, §13.2, S5), JSONL integrity deferral + single-user resume note (§10.4, §11, S6/S7), `retry:` simplicity note (§2.6, L1), minimal chat UX scope (§15.2, L2), cross-JSONL correlation by `trace_id` + fork-point legality (§9.5, §11.5, L3), agent-identity overrides out of scope at all phases with `outputs:` carve-out (§4.1.2, L4 + L8 closed), context management + multi-provider fallback as agent-layer concerns (§4.1.2, L6 + L9), Jinja-evaluated fields accept YAML literals OR `{{ }}` (§2.7, §4.3.1, §6.4, J5/β) + removal of `WorkflowBindMustBeJinja` + addition of `WorkflowJinjaSecurityError` / `WorkflowExtendsPolicyForbidden` (§12.1), wire prefix `node.<bundle>.<subkind>` dropping the `author` segment (§5.5, §11.1, R6-R1), engine does not aggregate token usage at v1 (§11, R6-R3). Round 7 layered in: `from_json` Jinja filter ships core-only (§2.7, §2.7.3, CV1), `NodeContext` field rename `_bundle` → `bundle` / `_emit_fn` → `emit_fn` (§5.5, L7-3), `state: Mapping[str, Any]` read-only on `NodeContext` (§5.5, A7-6), `NodeContext.for_test()` classmethod (§5.5, A7-1), `display_label()` public helper as single source of truth for capsule labels (§15.4, L7-8), decision log decoder (§0.5, L7-10), symmetric `kolsch <asset-type> list/show` CLI pattern + `nodes list/show` (§14, L7-12), AND-strict completion semantics for skip/failure/retry (§8.1, A7-7), §19.3 reworded for semantic-equality-modulo-volatile-fields (A7-3). Sections referenced above moved to companion files; see Companion Documents below. Companion: `phase3.0-prior-art-memo.md` (research synthesis).**

---

## Companion documents

This spec was trimmed to its focused Phase 3.0 scope (parser + validator + asset surface). Material that grew beyond Phase 3.0 scope was extracted to the following companion files:

- `docs/explanation/workflow-glossary.md` — the precise term definitions (node, node type, captured output, invocation, node NAME) + the reference map of related kolsch contracts.
- `docs/explanation/workflow-foundation.md` — the Locked Foundation Decisions (cross-cutting locks, authoring shape, graph topology, execution model, entity model, branching, modifiers, Jinja universal rule + sandbox + filter set, Python node API surface, policy posture, errors posture).
- `docs/explanation/workflow-event-log.md` — the event kinds catalog, common fields, ID naming conventions, capture rules, and cross-JSONL correlation by `trace_id`.
- `docs/explanation/workflow-policy-model.md` — the policy layer walk, proximate-caller rule for layer selection, `from:` field caller-chain scoping, caller-chain construction, refs unification, and configurable audit emission.
- `process/phase3.1-executor-spec.md` — sequential executor algorithm, AND-strict completion semantics, module loading + per-run isolation, iterative graph traversal, state management, cancellation + orphan recovery.
- `process/phase3.4-user-input-spec.md` — `user_input` node pause/resume mechanics (CLI + chat), nested pauses, resume authentication.
- `process/phase3.8-fork-spec.md` — fork-from-event operation, side-effect discipline, sub-workflow forking, fork-point legality (workflow-run-level events only).
- `process/phase3.9-chat-surface-spec.md` — chat slash commands (`/workflow`, `/fork`), capsule rendering, event ID surface, capsule label fallback, error rendering via `caller_message`.
- `process/phase3.x-roadmap.md` — the eleven-phase sub-phasing plan (3.0 → 3.11), dependency graph, cumulative user value at each gate.

---

## 0.5 Decision log decoder

This spec carries inline references to design-round and item codes (e.g., "per S5", "L7-3 amended", "R6-R1"). They're not error codes or implementation IDs — they're a TRACEABILITY KEY back to the iteration that locked the decision. This subsection decodes them; the codes themselves stay inline in the contract sections where they apply.

**Round codes** — the design rounds whose decisions are still encoded in the spec:

- **R3** — pre-spec lock-in of asset shape, node-type catalog, and `extends:`/`copy_of:` semantics.
- **R4** — multi-input fan-in, AND-strict trigger semantics, three-segment refs canonical, run-start gate.
- **R5** — Jinja universal rule with NativeEnvironment, wire prefix `node.author.<bundle>.<subkind>`, two-channel error rendering, `caller_chain` persistence.
- **R6** — Jinja sandbox contract (S1), `extends:` does not import `policy:` (S5), authored Python trust model (S3), `ctx.bundle` field (S4; originally locked as `ctx._bundle`, renamed under R7 L7-3), wire prefix shortened to `node.<bundle>.<subkind>` (R6-R1), JSONL integrity deferral (S6/S7).
- **R7** — `from_json` filter ships (CV1), `NodeContext` field rename + `ctx.state` + `for_test()` (L7-3, A7-1, A7-6), `display_label()` public helper (L7-8), decision log decoder (L7-10), symmetric asset-discovery CLI (L7-12), AND-strict completion semantics for failure/skip/retry (A7-7), §19.3 reword (A7-3).

**Item-code groups** — the prefixes used on individual decisions within a round:

- **L** — Lighter-wave + LLM-specific items (often QoL adjacent; e.g., L7-3, L4, L8).
- **S** — Security items (sandbox, trust model, extends-policy isolation; e.g., S1, S5, S6, S7).
- **J** — Junior-onboarding items (clarity of the surface a first-time author hits; e.g., J3, J5).
- **H** — High-leverage items (architectural moves with broad downstream payoff; e.g., H1).
- **M** — Medium-leverage items (focused improvements, not architectural).
- **C** — Critical items (must-fix-before-ship blockers).
- **CV** — Convergent reopens (decisions re-examined in a later round and re-locked; e.g., CV1, CV2, CV4).
- **R6-R#** — Round 6 reopen items (decisions amending or superseding an earlier round; e.g., R6-R1 amended the R5 wire prefix).

Codes stay inline in the contract sections because the traceability matters: "the wire prefix changed under R6-R1" is shorter and more searchable than re-paraphrasing the prior shape every time. This subsection is the key — when in doubt, decode here.

---

## 1. Goal & Scope

### 1.1 Goal

Add a workflow asset type to kolsch: a YAML-authored DAG of nodes that compose agents, tools, and authored Python into reproducible, forkable executions. Workflows are first-class assets (alongside agents/tools/skills) with their own runtime entity (`workflow_run`), CLI surface, chat surface, and JSONL-backed replay/fork mechanism.

### 1.2 Scope

In scope at v1:
- Workflow asset type + parser + run-start validation gate.
- Sequential executor (no parallelism).
- 14 built-in node types (catalog in §4).
- Authored Python nodes referenced via three-segment catalog refs (`<bundle>/nodes/<name>`) as the `type:` value; backed by the `@node` decorator with signature-driven schemas.
- Fork-from-event mechanism producing immutable original + new run.
- CLI commands: `list`, `show`, `validate`, `run`, `fork`, `events`.
- Chat slashes: `/workflow`, `/fork`.
- Sub-workflow nesting via the `workflow` node type.
- Policy integration through the existing rule-based provider with a new `workflow_policy` layer.

Out of scope (deferred):
- Concurrent execution (parallel fan-out) — Phase 3.10.
- Scheduled/event triggers, webhook callbacks, HTTP entry points — **`kolsch-server` concerns, not in the Phase 3.x roadmap** (see boundary below).
- `on_error: continue|route_to` — future Phase 3.x.
- Workflow versioning — future.
- `author_workflow` meta-agent — Phase 3.11.

**Architectural boundary: `kolsch-engine` vs `kolsch-engine.LocalController` vs `kolsch-server`.** Phase 3.0–3.10 ship `kolsch-engine` — the pure execution concerns: parser, validator, executor, node types, agent integration, fork mechanism, chat surface. Inside the engine at v1 lives `kolsch-engine.LocalController` — the per-host coordination concerns: PID tracking, `cancel.flag` IPC, JSONL append, orphan sweep, sub-process resume. These are conceptually one component at v1 (no module extraction yet — Phase 3.1's executor designer picks the LocalController layout). The SEPARATE concern is `kolsch-server` — triggers (cron / webhook / scheduled), HTTP entry points, scheduler daemons, callback receivers, multi-tenant routing — targeted for Phase 4+ or as a companion project. The pattern matches Argo Workflows + Argo Events: an execution engine and a trigger/events surface are distinct components with distinct deployment shapes. No webhook/scheduler primitives appear in the Phase 3.x roadmap; the engine's only invocation entry points at v1 are CLI (`kolsch workflows run`) and chat (`/workflow`).

### 1.3 Non-goals

Workflows do NOT replace agents/chat at v1. The eventual unification ("chats are workflows of a single node") is post-Phase-3 work. Workflows run alongside the existing chat/agent surfaces.

### 1.4 Mental model (verbatim, for author-facing docs)

These three paragraphs are the canonical phrasing — author-facing docs (`docs/explanation/workflows-contract.md`, `docs/how-to/authoring-a-workflow.md`) re-use them verbatim.

**Output access**: Every node produces a flat per-type dict, accessed exclusively as `{{ nodes.<name>.<field> }}` from anywhere in the workflow — and `nodes.<name>` is `Undefined` until `<name>` completes successfully.

**Fan-in**: A node may have any number of incoming edges. Fan-in is structural — just add another edge. The engine waits for ALL incoming edges' source nodes to complete before firing the target (AND-strict trigger semantics). For OR-style routing, hoist the choice into an upstream `branch` node. Edge-bound data is delivered as `inputs.<key>` at the receiver; cross-graph reads via `{{ nodes.<X>.<field> }}` are allowed for any transitive ancestor.

**Policy**: Workflow policy applies only when the workflow's own nodes are firing actions. Embedded agents fall back to their own `agent_policy` naturally. For explicit caller scoping, add a `from:` glob matching agent or workflow refs (e.g., `from: acme/workflows/triage`).

### 1.5 Authored Python `@node` trust model

Authored Python `@node` files are FULLY-TRUSTED CODE running at engine privilege. Bundles are a code-review surface, not a config surface. `ctx.secrets` is a convenience for clean access — NOT a security boundary. Node code can read `os.environ` directly. Operators who load bundles from external sources should treat that as equivalent to running arbitrary code on the engine host. Treat bundles like Ansible playbooks or GitHub Actions — code, not config.

No technical mitigation at v1: no allowlist of importable modules, no subprocess isolation, no sandboxing of authored Python. The trust model is documentary and operational — operators control which bundles load on their host, and that decision IS the security boundary.

---

## 3. Architecture Overview

### 3.1 Module layout

Proposed (to be confirmed during 3.0 implementation):

```
src/workflows/
├── __init__.py           # public API (`node` decorator, `NodeContext`, exception classes)
├── decorator.py          # @node decorator + introspection
├── context.py            # NodeContext
├── errors.py             # exception hierarchy
├── parser.py             # YAML → in-memory Workflow object
├── catalog.py            # asset-catalog integration
├── validator.py          # run-start gate
├── engine.py             # sequential executor
├── fork.py               # fork mechanism
├── builtin/              # built-in node type implementations (14 types)
│   ├── bash.py
│   ├── agent.py
│   ├── user_input.py
│   ├── noop.py
│   ├── read_file.py
│   ├── write_file.py
│   ├── list_files.py
│   ├── set_state.py
│   ├── delete_state.py
│   ├── filter.py         # filter (predicate-based list reduction)
│   ├── compute.py        # general-purpose Jinja-expression node (subsumes the dropped aggregations)
│   ├── workflow.py
│   ├── map.py
│   └── branch.py
├── catalog_node.py       # generic dispatcher for `type: <bundle>/nodes/<name>` refs (authored Python)
└── events.py             # event schema definitions
```

### 3.2 Asset-catalog registration

Workflows join `agents`, `tools`, `skills` as a registered asset type. Two new directories per bundle:

- `bundles/<bundle>/workflows/*.yaml` — workflow definitions (runnable workflows + node libraries).
- `bundles/<bundle>/nodes/*.py` — authored Python nodes (each file = one `@node`-decorated `run`).

**Auto-discovery is the default mechanism**, matching how tools at `bundles/<bundle>/tools/*.py` are discovered today. The loader scans both directories on bundle load and registers each file as a catalog entry. No `bundle.yaml` manifest entry required for the common case.

Optional opt-out via `bundle.yaml`'s `exclude:` block (existing mechanism): list specific files to skip. Used for in-progress drafts, deprecated nodes, or files reserved for tests.

### 3.3 Engine boundary

The workflow engine sits parallel to the existing CrewAI engine, not under it. Agent-typed nodes dispatch to the existing engine via the asset catalog's agent invocation path. Sub-workflows recurse into the workflow engine. Other node types are leaf executions.

---

## 4. Node Type Catalog

14 built-in **node types** organized by category (the catalog citizens addressable via 3-segment refs; distinct from per-workflow nodes which are invocation sites):
- 4 leaf execution (bash, agent, user_input, noop)
- 3 leaf file-IO (read_file, write_file, list_files)
- 2 leaf state-mutator (set_state, delete_state)
- 1 leaf list reducer (filter)
- 1 general-purpose Jinja expression (compute)
- 3 composite (workflow, map, branch)

There is NO `python` built-in node type. Authored Python node types are referenced directly as `type: <bundle>/nodes/<name>` — a three-segment catalog ref (see §4.0.1).

Each entry includes: configuration fields, input schema, output schema, branching support, failure modes. Authz target is the same shape across all node types (per `docs/explanation/workflow-policy-model.md`).

### 4.0.1 Catalog node-type references — built-ins and authored Python

**The `type:` field is ALWAYS a three-segment catalog ref** to a node TYPE — `<bundle>/nodes/<name>`. There is no 1-segment shorthand. Both built-in node types and authored Python node types use the same shape; the parser does not branch on a bare literal vs a 3-seg ref.

- **Built-in node types** live at the canonical catalog refs `kolsch/nodes/{bash, agent, workflow, noop, read_file, write_file, list_files, set_state, delete_state, filter, map, branch, compute, user_input}`. These 14 refs are RESERVED catalog slots — a user authoring a node-type file at `bundles/kolsch/nodes/bash.py` collides with the built-in `kolsch/nodes/bash` slot and the catalog raises `WorkflowBuiltinShadowed` at scan time.
- **Authored Python node types** live at any other `<bundle>/nodes/<name>` (file at `bundles/<bundle>/nodes/<name>.py` with a `@node`-decorated `run`). Parser dispatches to a generic `CatalogNodeDef` Pydantic model carrying the common fields (`when:`, `retry:`, `timeout_seconds:`, `description:`, `secrets:`); type-specific config is validated against the node type's declared config model after catalog lookup at the run-start gate.

```yaml
nodes:
  classify:
    type: kolsch/nodes/parse_classification   # three-segment catalog ref
    when: "{{ state.enabled }}"
    retry: 2
    # additional fields are interpreted against the node's declared config model
```

**Bundle naming.** The strings `bash`, `agent`, `workflow`, etc. are NOT reserved bundle names — a user is free to author a bundle named `bash`. Only the specific 14 BUILT-IN CATALOG SLOTS (`kolsch/nodes/<name>` for the 14 names above) are reserved. Assets inside a user-authored bundle named `bash` are referenced via full 3-seg (e.g., `bash/nodes/parse`). The `core` segment is NO LONGER reserved (was speculative for an earlier `core/principals/*` design that was dropped).

A non-3-seg `type:` value (e.g., `type: bash`, `type: kolsch/bash`, or malformed) raises `WorkflowInvalidRef` at parse. There is no `body:` field — the `type:` value IS the catalog ref.

### 4.0.2 Per-built-in `Inputs(BaseModel)` models (strict-mode validation)

Every built-in node type declares an `Inputs(BaseModel)` Pydantic model that the validator and runtime use to type-check the rendered `binds:` values arriving at the receiver. The model carries the type's `model_config = ConfigDict(extra=...)` policy and any strict-typed fields the type's primary config consumes directly.

| Type | `Inputs` class | `extra=` | Strict-typed fields |
|---|---|---|---|
| `kolsch/nodes/bash` | `BashInputs` | `"allow"` | none (`binds:` keys are author-named) |
| `kolsch/nodes/agent` | `AgentInputs` | `"allow"` | none |
| `kolsch/nodes/workflow` | `WorkflowSubInputs` (name chosen to avoid collision with the `workflow` asset model) | `"allow"` | none |
| `kolsch/nodes/noop` | `NoopInputs` | `"forbid"` | none (no inputs accepted; binding to noop is a parse error) |
| `kolsch/nodes/user_input` | `UserInputInputs` | `"allow"` | none |
| `kolsch/nodes/read_file` | `ReadFileInputs` | `"forbid"` | `path:` is on `NodeDef.config` (`str`), not Inputs |
| `kolsch/nodes/write_file` | `WriteFileInputs` | `"forbid"` | `path: str`, `content: str \| bytes` on `NodeDef.config` |
| `kolsch/nodes/list_files` | `ListFilesInputs` | `"forbid"` | `path: str`, `pattern: str \| None` on `NodeDef.config` |
| `kolsch/nodes/set_state` | `SetStateInputs` | `"allow"` | none |
| `kolsch/nodes/delete_state` | `DeleteStateInputs` | `"forbid"` | none |
| `kolsch/nodes/filter` | `FilterInputs` | `"allow"` | `items: list[Any]` |
| `kolsch/nodes/map` | `MapInputs` | `"allow"` | `over: list[Any]` |
| `kolsch/nodes/branch` | `BranchInputs` | `"allow"` | none |
| `kolsch/nodes/compute` | `ComputeInputs` | `"allow"` | none |

**Strict-mode behavior.** Pydantic strict-mode validation applies UNIFORMLY at the receiver: at node fire time, the engine renders each `binds:` Jinja expression and validates the resulting dict against the receiver's `Inputs` model under Pydantic's `strict=True` mode. Mismatches raise `WorkflowBindTypeMismatch`. In practice, MOST built-ins are `extra="allow"` with `Any`-typed accept-all fields (because bind keys are author-named — the engine can't know in advance what authors will name them), so the strict-mode catches mostly fire on authored Python nodes (whose Inputs models are author-declared with concrete field types) and on the `extra="forbid"` built-ins above (`noop`, `read_file`, `write_file`, `list_files`, `delete_state` — these reject any `binds:` keys whose names aren't on the model).

Per-built-in tightening — converting more `extra="allow"` types to `extra="forbid"`, adding strict-typed fields — is a Phase 3.x QoL as real workflow patterns emerge and we learn which fields authors consistently name.

### 4.0 Built-in node types — labels and output shapes

| Node type | `label:` | Output shape |
|---|---|---|
| `kolsch/nodes/bash` | Shell | `{stdout: str, stderr: str, exit_code: int}` |
| `<bundle>/nodes/<name>` (authored) | per `@node(label=...)` or cleaned function name | model fields (for a `BaseModel` return); `{value: bool}` (for a `bool` return); `{value: bool, **model.model_dump()}` (for a `tuple[bool, BaseModel]` return) |
| `kolsch/nodes/agent` | Agent | the agent's declared output schema (or the per-node `outputs:` override) |
| `kolsch/nodes/user_input` | Wait for input | `{response: str}` |
| `kolsch/nodes/noop` | Pass through | `{}` |
| `kolsch/nodes/read_file` | Read file | `{content: str}` |
| `kolsch/nodes/write_file` | Write file | `{bytes_written: int}` |
| `kolsch/nodes/list_files` | List files | `{entries: list[str]}` |
| `kolsch/nodes/set_state` | Set state | `{}` |
| `kolsch/nodes/delete_state` | Delete state | `{}` |
| `kolsch/nodes/filter` | Filter | `{result: list[T]}` |
| `kolsch/nodes/workflow` | Sub-workflow | the called workflow's inferred terminal dict (per §6.6) |
| `kolsch/nodes/map` | Map | `{results: list[<applied_node_output_type>]}` |
| `kolsch/nodes/branch` | Branch | `{value: bool}` |
| `kolsch/nodes/compute` | Compute | `{value: <Jinja-evaluated>}` |

The `label:` column shows the type-level label per `docs/explanation/workflow-foundation.md` §2.6.1 — the second-tier fallback for chat capsules and `show` CLI when a per-node `description:` is absent. Authored Python node types declare their label via `@node(label="...")` (§5.2); without it, the renderer cleans the function name.

The 8 aggregations that appeared in earlier drafts (sum, count, min, max, sort, flatten, any, all) are GONE. Use `compute` with stock Jinja filters instead. Recipes:

| Old node | `compute.value:` Jinja |
|---|---|
| `sum` | `{{ items | sum(attribute='score') }}` |
| `count` | `{{ items | length }}` |
| `min` | `{{ items | min(attribute='score') }}` |
| `max` | `{{ items | max(attribute='score') }}` |
| `sort` | `{{ items | sort(attribute='score', reverse=true) }}` |
| `flatten` | `{{ items | sum(start=[]) }}` (one level) |
| `any` | `{{ items | selectattr('urgent') | list | length > 0 }}` |
| `all` | `{{ items | rejectattr('urgent') | list | length == 0 }}` |

### 4.1 Leaf execution types

#### 4.1.1 `bash`
- Config: `command: str`, `cwd: str?`, `env: dict?`
- Inputs: any Jinja-referenced bind names in config strings
- Outputs: `{stdout: str, stderr: str, exit_code: int}`
- Branching: yes (`exit_code == 0`)
- Routes through existing `kolsch/bash` tool (sandbox + authz).
- **Shell-safe interpolation is the author's responsibility.** The `bash` node is a basic shell-string wrapper at v1: Jinja-interpolated values land in the `command:` string verbatim, with no automatic quoting or escaping. Authors interpolating user-supplied or untrusted data MUST quote defensively (e.g., wrap in single quotes, use `shlex.quote`-equivalent transforms via a Python node upstream). Future improvements (argv-form `command_argv:`, automatic shell-escape filter, validator warnings on unquoted interpolation) are deferred to Phase 3.x QoL.

#### 4.1.2 `agent`
- Config: `agent: str` (asset ref), optional `outputs:` for per-invocation output shape (overrides agent's declared outputs via CrewAI structured output mechanism)
- The `outputs:` override grammar reuses the existing `IOSpec` schema (`src/schemas/config.py:58`) — same dict-of-name-to-TypeSpec shape agents already use in their YAML. No new grammar.
- Inputs: agent's declared input schema
- Outputs: agent's declared output schema (or workflow-node-overridden)
- Branching: no (route via `branch` node for branching off agent results)
- Creates a child session for the agent's reasoning thread.

**Per-invocation overrides of agent identity are OUT OF SCOPE at all phases.** The `model`, `prompt`, `system_prompt`, `temperature`, and iteration-limit fields belong to the agent asset's definition, NOT to the workflow node's invocation. Agents are first-class assets; for prompt or model A/B testing, define separate agent assets and reference them by ref (the agent system already supports inheritance for shared base config). The workflow node CANNOT override these — there is no `model:`, `prompt:`, `system_prompt:`, `temperature:`, or `max_iterations:` field on the `agent` node config.

**The `outputs:` override is EXEMPT from this rule.** `outputs:` shapes the workflow's CONSUMPTION of the agent's response (a workflow-side concern), not the agent's identity (model + prompt + behavior). The override goes through CrewAI's structured-output mechanism using the existing `IOSpec` grammar — same machinery agents already use to declare their own output shape. This carve-out is what removes the need for a separate `kolsch/nodes/llm` primitive: `agent` + `outputs:` already covers structured-output workflows.

**Context management lives at the AGENT level, not the workflow level.** Pre-flight token budgeting, overflow strategies, and context-window selection are agent-asset concerns — agents own their context discipline as part of their definition (per-model windows, summarization strategies, sliding-window policies). Workflow `agent:` nodes that fail because the agent layer raised a context-overflow error surface as `WorkflowNodeFailed` with the agent layer's error in `caller_message`. No workflow-level `context_strategy:` or `max_tokens:` knobs at any Phase 3.x phase.

**Multi-provider fallback lives at the AGENT level.** Strategies like "try Anthropic, fall back to OpenAI on rate-limit/5xx" are agent-asset concerns. Agent assets define provider selection and fallback chains; workflow `agent:` nodes inherit whatever the agent declares. Workflow-level provider fallback expression (a hypothetical `providers: [anthropic, openai]` field on the node) is out of scope at all Phase 3.x phases.

#### 4.1.3 `user_input`
- Config: `prompt: str` (Jinja)
- Inputs: anything referenced in `prompt:`
- Outputs: `{response: str}`
- Branching: no
- Pause mechanism: the engine emits `workflow.node.started` for this node but no completion event; workflow_run status flips to `waiting_for_input`. The open invocation IS the marker that input is awaited (no separate pause event). Resume via `kolsch workflows resume` or chat reply completes the invocation with `{response: <text>}` and execution continues. See `process/phase3.4-user-input-spec.md` for details.

#### 4.1.4 `noop`
- Config: none
- Inputs: any (pass-through)
- Outputs: empty `{}`
- Branching: no
- Placeholder for fan-out structural needs or explicit single-entry pattern.

### 4.2 Leaf file-IO types (route through existing `file_access` subsystem)

#### 4.2.1 `read_file`
- Config: `path: str` (Jinja)
- Inputs: anything referenced in `path:`
- Outputs: `{content: str}`
- Matches existing `read_file` tool surface 1:1.

#### 4.2.2 `write_file`
- Config: `path: str`, `content: str` (both Jinja)
- Inputs: anything referenced
- Outputs: `{bytes_written: int}`

#### 4.2.3 `list_files`
- Config: `path: str` (Jinja)
- Inputs: anything referenced
- Outputs: `{entries: list[str]}` — immediate children, non-recursive.
- 1:1 with the existing `list_files` tool surface.

### 4.3 Leaf state-mutator types

#### 4.3.1 `set_state`
- Config: `keys: dict[str, Any]` — each value is either a bare YAML literal (string, int, float, list, bool, null) or a `"{{ ... }}"` Jinja template evaluated at fire time (per the universal Jinja rule in `docs/explanation/workflow-foundation.md` §2.7)
- Inputs: anything referenced
- Outputs: `{}`
- Replaces the named state keys. Composition over existing state (e.g., merging dicts, appending to lists, incrementing counters) is expressed using Jinja's native operators and built-in filters when the value needs to be computed — the author wraps the expression in `{{ }}`. No special merge filter at v1.

**Pattern: state as evolving variable.** Multiple `set_state` nodes writing the same key is the canonical pattern for values that change as the workflow runs. Example — a retry-with-attempt-counter shape:

```yaml
nodes:
  init:
    type: kolsch/nodes/set_state
    keys: { attempts: 0, status: pending }       # YAML literals — natural form
  attempt:
    type: kolsch/nodes/agent
    agent: kolsch/agents/worker
  inc_attempts:
    type: kolsch/nodes/set_state
    keys: { attempts: "{{ state.attempts + 1 }}" }  # Jinja form — wrapped in {{ }}
  set_done:
    type: kolsch/nodes/set_state
    when: "{{ nodes.attempt.success }}"
    keys: { status: done }                       # YAML literal again
graph:
  edges:
    - { from: init,         to: attempt,      binds: {} }
    - { from: attempt,      to: inc_attempts, binds: {} }
    - { from: inc_attempts, to: set_done,     binds: {} }
```

Three writes to `state.attempts` (init sets 0; inc_attempts increments; potentially again on a retry loop) and two writes to `state.status` (init pending; set_done done) — the canonical pattern.

#### 4.3.2 `delete_state`
- Config: `keys: list[str]`
- Inputs: none
- Outputs: `{}` (explicit empty dict)
- Branching: no
- Removes the named keys from state.

### 4.4 Leaf list reducer

#### 4.4.1 `filter`
- Config: `over: <Jinja list expr>` (required), `condition: <Jinja predicate>` (required, evaluated per element), `item_as: <field_name>` (optional, default `"item"`).
- Inputs: anything referenced in `over:` or `condition:`.
- Outputs: `{result: list[T]}` — elements where the predicate evaluates truthy.
- Branching: no.
- Fails when: `over:` resolves to non-list; per-item predicate evaluation raises.
- Note: `condition:` here is the node's PRIMARY config (the predicate), not the gate modifier — same word disambiguated by node type. The gate modifier is `when:`.

### 4.5 General-purpose Jinja expression

#### 4.5.1 `compute`
- Config: `value: str` (required — Jinja expression evaluated under NativeEnvironment per `docs/explanation/workflow-foundation.md` §2.7.1; can return any Python type). `description:` is the common node-level modifier (optional) — use it to label what this compute does for chat capsules and `show` output.
- Inputs: anything referenced in `value:` — `inputs.<key>` for keys bound by incoming edges, or `nodes.<X>.<field>` for any transitive ancestor in the edge graph.
- Outputs: `{value: <evaluated_result>}` — the literal result of the Jinja expression preserved as a Python value.
- Branching: no (wrap with a `branch` node if you need to gate downstream on a boolean).
- Fails when: the Jinja expression raises; `value:` template references undefined `nodes.<X>` or missing `state.*` keys without `default()`.
- Replaces the 8 dropped aggregation node types (sum, count, min, max, sort, flatten, any, all) via stock Jinja filters. See §4.0 recipe table.

### 4.6 Composite types

#### 4.6.1 `workflow`
- Config: `ref: str` (asset ref to another workflow, must be runnable — have a graph)
- Inputs: called workflow's inferred input shape (its start node's input schema)
- Outputs: called workflow's inferred output shape (dict keyed by called workflow's terminal node names; see §6.6)
- Creates a child workflow_run with `parent:` pointer.
- Detected cycle in workflow-call chain → `WorkflowCallCycle`.
- Maximum nesting depth of 16 enforced at run-start gate (mirrors the `extends:` cap).

#### 4.6.2 `map`
- Config: `apply: <node_name>`, `over: <Jinja list expression>`, `item_as: <field_name>` (required), `with: dict?` (constants supplied to every iteration)
- Inputs: `over:` and `with:` references
- Outputs: `{results: list[<applied_node_output_type>]}`
- Sequential at v1; parallel deferred to Phase 3.10.
- Applied-node validation: must have an input named per `item_as:` whose type matches the list element.
- Worker nodes (the targets of `apply:`) are NOT addressable as `nodes.<worker>` from outside the map node — only the map node itself surfaces results via `nodes.<map>.results`. References from outside the map raise `WorkflowMapperWorkerNotAddressable`.
- Worker-spawned invocation events (`workflow.node.started`, `workflow.node.completed`, `workflow.node.failed`) carry `parent_invocation_id: <map_node_invocation_id>` AND `iteration_index: int` (0-based) on the payload, so consumers can correlate worker invocations to their parent map node and ordinal.

#### 4.6.3 `branch`
- Config: `condition: <Jinja boolean expression>`
- Inputs: anything referenced in `condition:`
- Outputs: `{value: bool}` — the evaluated condition result, addressable as `nodes.<branch_node>.value`
- Always branching; QoL sugar around "evaluate this Jinja expression and branch."
- Distinct from the node-level `when:` gate modifier. On a `branch` node, `condition:` IS the node's primary work; the gate modifier elsewhere is spelled `when:`. The `filter` node also uses `condition:` as primary config (the per-element predicate).

---

## 5. Python Node API (Author-Facing)

### 5.1 File layout

Authored Python nodes live at `bundles/<bundle>/nodes/<name>.py`. One `@node`-decorated `run` per file. Filename and bundle determine the node's ref (`<bundle>/nodes/<name>` after stripping `.py`).

### 5.2 Decorator surface

The decorator signature:

```python
def node(
    *,
    label: str | None = None,
    secrets_needed: list[str] | None = None,
) -> Callable: ...
```

Usage:

```python
@node(label="Enrich lead with CRM data", secrets_needed=["api_key"])
def run(inputs: ParseIn, ctx: NodeContext) -> ParseOut: ...
```

Two keyword arguments:

- `label: str | None` — the node TYPE's human-readable label per `docs/explanation/workflow-foundation.md` §2.6.1, used by chat capsules and `show` CLI as the fallback when the per-instance `description:` is absent. When omitted, the renderer cleans the function name (snake_case → title-case).
- `secrets_needed: list[str] | None` — mirrors `@tool(secrets_needed=[...])`. Names resolved at node fire time and injected into `ctx.secrets`.

The decorator surface intentionally stays small — type info comes from signature annotations (§5.3, §5.4).

### 5.3 Signature rules

- Param 1: required. Must annotate with a `pydantic.BaseModel` subclass. Receives the bound-and-validated input instance.
- Param 2: required. Must annotate as `NodeContext` exactly.
- Param 3 (optional): if present, must annotate with a `pydantic.BaseModel` subclass. Receives the Jinja-interpolated + validated config instance.
- No other positional or keyword parameters allowed.

### 5.4 Return type rules

- `BaseModel` subclass → non-branching; outputs are the model's fields.
- `bool` → branching only; no other outputs.
- `tuple[bool, BaseModel subclass]` → branching with structured output.
- Any other return type → `WorkflowNodeReturnTypeInvalid` at run-start gate.

### 5.5 `NodeContext` surface

```python
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

@dataclass(frozen=True, slots=True)
class NodeContext:
    workflow_run_id: str
    invocation_id: str
    workspace_root: Path
    log: StructLogger                                                   # ctx.log.info/warn/error(msg, **fields)
    secrets: Mapping[str, str]                                          # pre-resolved per secrets_needed manifest; read-only (MappingProxyType at runtime)
    state: Mapping[str, Any]                                            # workflow state at fire-time; read-only (MappingProxyType at runtime)
    bundle: str                                                         # PUBLIC test seam; engine-set from catalog-resolved invocation file's bundle
    emit_fn: Callable[[str, Mapping[str, Any]], None]                   # PUBLIC test seam; engine-bound at construction

    def emit(self, subkind: str, payload: Mapping[str, Any]) -> None:
        """Emit a custom event into the workflow run's JSONL.

        `subkind` is the FREEFORM author-supplied suffix (no regex enforcement).
        The engine auto-prepends `node.<bundle>.` based on `self.bundle` (set
        by the engine at construction from the catalog-resolved invocation
        file's bundle), so the wire `kind` on disk becomes
        `node.<bundle>.<subkind>`.
        """
        self.emit_fn(self.bundle, subkind, payload)

    @classmethod
    def for_test(
        cls,
        *,
        workflow_run_id: str = "wrk_test",
        invocation_id: str = "inv_test",
        bundle: str = "test",
        workspace_root: Path | None = None,        # defaults to tempfile.mkdtemp()
        log: "StructLogger | None" = None,         # defaults to a no-op StructLogger
        emit_fn: Callable | None = None,           # defaults to lambda *_: None
        secrets: Mapping[str, str] | None = None,  # defaults to {}
        state: Mapping[str, Any] | None = None,    # defaults to {}
    ) -> "NodeContext":
        """One-line NodeContext construction for unit tests.

        All seven fields default; tests override only what they're exercising:

            ctx = NodeContext.for_test(state={"attempts": 3})
            ctx = NodeContext.for_test(secrets={"api_key": "tk"})

        Tests of authored Python nodes import this classmethod and call it
        directly — no engine spinup, no JSONL machinery, no fixtures.
        """
        ...
```

- `frozen=True` — prevents top-level field reassignment by node code (no `ctx.workflow_run_id = ...`).
- `slots=True` — memory-efficient, matches the established kolsch dataclass pattern (`PermissionsBinding` at `src/tools/permissions.py:70-71`).
- `secrets: Mapping[str, str]` — typed as the read-only protocol, NOT `dict`. The runtime (Phase 3.1+ executor) wraps the resolved secrets dict in `MappingProxyType` before constructing each `NodeContext`, so node code cannot mutate the workflow's secret view. Tests can pass a plain `dict` (which IS a `Mapping`); type-checkers accept it; production wraps for true read-only enforcement.
- `state: Mapping[str, Any]` — the workflow's state dict at fire-time, wrapped in `MappingProxyType` at runtime (per A7-6). Authored `@node` code READS via `ctx.state["foo"]`; mutating state from inside an authored `@node` is FORBIDDEN — only `set_state` and `delete_state` nodes mutate state. The read-only contract is structural (MappingProxyType raises on mutation attempts) and documentary (this paragraph). The `Mapping[str, Any]` typing accepts a plain `dict` from tests; production wraps for true read-only enforcement. The previous draft passed state implicitly via Jinja `{{ state.* }}` only; A7-6 adds the read-only handle for authored Python so node code that wants to make decisions on state doesn't need a Jinja-evaluated bind.
- `bundle: str` — PUBLIC field per L7-3 (renamed from `_bundle`; the underscore-prefixed form is gone). The bundle name owning the currently-invoked `@node`. **Engine-set at `NodeContext` construction** from the catalog-resolved invocation file's bundle (the bundle path on disk that produced the `@node`-decorated module), NOT from `inspect`-ing the calling `@node`'s `__module__`. The engine tracks the resolved bundle through catalog lookup; node code never supplies it. **Spoof-resistance**: re-exporting an authored `@node` from a different bundle does NOT change `bundle` — the wire prefix follows the catalog resolution, not the call site or the `__module__` of the function object. A node re-exported into bundle `evil` still emits with the prefix of the bundle whose `bundles/<owner>/nodes/<name>.py` file the catalog resolved against. Structurally, the engine tracks the resolution; nothing in author Python code can override it. Frozen-dataclass discipline prevents reassignment.
- `emit_fn: Callable[[str, str, Mapping[str, Any]], None]` — PUBLIC test seam per L7-3 (renamed from `_emit_fn`; the underscore-prefixed form is gone). Engine-bound at construction. Documented as a test seam: unit tests of authored Python nodes pass a `Mock` or a list-appender to capture emitted events without an executor running. `ctx.emit()` is the user-facing entry point; `emit_fn` is what gets called underneath. Authors who use `ctx.emit("custom_metric", {...})` never touch `emit_fn` directly.
- `emit(subkind, payload) -> None` is a real METHOD with a typed signature — IDEs surface it, authors call `ctx.emit("custom_metric", {"k": v})`. The parameter is named `subkind` (not `kind`) deliberately: the author supplies the SUFFIX, not the full wire kind. `subkind` is FREEFORM — no regex enforcement, no enumeration — so authors choose whatever suffix makes sense for their domain. The engine auto-prepends `node.<bundle>.` based on `ctx.bundle`; the wire `kind` written to the JSONL is `node.<bundle>.<subkind>` (e.g., a node in bundle `acme` calling `ctx.emit("ticket_classified", {...})` lands on disk as `kind: node.acme.ticket_classified`). The earlier bare `emit: Callable` field (no signature) is GONE — the method form makes the contract IDE-visible. The earlier `node.author.<bundle>.<subkind>` wire prefix (per R5 R1) is AMENDED to `node.<bundle>.<subkind>` per R6-R1 — the engine's own kinds all start with `workflow.`, `policy.`, `model.`, or `tool.` (never `node.`), so the `node.*` namespace is unambiguously authored, and the dropped `author` segment removes a redundant marker.
- `NodeContext.for_test(**overrides) -> NodeContext` — classmethod per A7-1, ~30 LoC, listed in `__all__`. All seven fields have defaults (`workspace_root` defaults to `tempfile.mkdtemp()`; `log` to a no-op StructLogger; `emit_fn` to `lambda *_: None`; `secrets` and `state` to empty `{}`). Tests override only what they're exercising. Amends R6 J3 D (which had no helper at all); the helper makes the test pattern one line instead of seven kwargs of boilerplate per test.

**Test pattern.** Unit tests of authored Python nodes look like:

```python
from kolsch.workflows import NodeContext

def test_classify_routes_urgent():
    ctx = NodeContext.for_test(state={"threshold": 0.8})
    result = run(ClassifyIn(text="..."), ctx)
    assert result.label == "urgent"
```

No executor spinup, no JSONL machinery, no fixture rigging. Tests that want to capture emitted events pass `emit_fn=lambda bundle, subkind, payload: events.append((subkind, payload))`.

Deliberately tight. No DB access, no peer-node output reads (use `binds:` / `inputs.<key>`), no child-workflow spawning from inside nodes.

### 5.6 Failure semantics

Any exception raised inside `run` → `WorkflowNodeFailed` with the original as `__cause__`. Pydantic validation failures on input, output, or config map the same way.

---

## 6. Workflow YAML Reference

### 6.0 Minimal runnable workflow

The smallest legal workflow is a single bash node and no edges. Save as `bundles/myteam/workflows/hello.yaml`:

```yaml
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo Hello, {{ name }}!"
graph:
  edges: []
```

Run it:

```
kolsch workflows run myteam/workflows/hello --input name=Maya
```

Expected output:

```json
{ "greet": { "stdout": "Hello, Maya!\n", "stderr": "", "exit_code": 0 } }
```

Four things to notice:

- `nodes:` declares each invocation site by name. `greet` is both the node's name AND the key under which its output is captured (`nodes.greet.stdout`).
- `graph.edges: []` is legal — a single-node workflow has no edges. (Empty `edges:` is fine — single-node workflows are runnable. Missing the `graph:` key entirely makes the workflow a node library; see §6.2.)
- `{{ name }}` is a Jinja input reference. The workflow's inputs are inferred from the start node's input schema (§6.5); the CLI's `--input name=Maya` supplies `name`.
- The workflow's output is the terminal-keyed dict described in §6.6. With one terminal (`greet`), the output is `{"greet": <bash-output-dict>}`.

### 6.0.1 Common patterns

Four canonical shapes worth knowing.

**Linear pipeline** — fetch → transform → write:

```yaml
nodes:
  fetch:     { type: kolsch/nodes/bash,        command: "curl -s https://api.example.com/data" }
  transform: { type: kolsch/nodes/compute,     value: "{{ data | upper }}", description: "uppercase the body" }
  write:     { type: kolsch/nodes/write_file,  path: "out.txt", content: "{{ result }}" }
graph:
  edges:
    - { from: fetch,     to: transform, binds: { data:   "{{ nodes.fetch.stdout }}" } }
    - { from: transform, to: write,     binds: { result: "{{ nodes.transform.value }}" } }
```

Data flows edge-to-edge via `binds:`; each node sees its parent's captured output explicitly named.

**Branching** — classify, then route to two handlers:

```yaml
nodes:
  classify:        { type: kolsch/nodes/agent,  agent: kolsch/agents/classifier }
  is_urgent:       { type: kolsch/nodes/branch, condition: "{{ label == 'urgent' }}" }
  urgent_handler:  { type: kolsch/nodes/agent,  agent: kolsch/agents/urgent_responder }
  normal_handler:  { type: kolsch/nodes/agent,  agent: kolsch/agents/normal_responder }
graph:
  edges:
    - { from: classify,  to: is_urgent,      binds: { label: "{{ nodes.classify.label }}" } }
    - { from: is_urgent, to: urgent_handler, branch: true }
    - { from: is_urgent, to: normal_handler, branch: false }
```

The `branch` node evaluates its `condition:` and the engine only fires outgoing edges whose `branch:` value matches.

**Fan-in via multi-input edges** — multiple upstream nodes feeding one consumer:

```yaml
nodes:
  fetch_a: { type: kolsch/nodes/bash, command: "curl {{ url_a }}" }
  fetch_b: { type: kolsch/nodes/bash, command: "curl {{ url_b }}" }
  merge:
    type: kolsch/nodes/compute
    value:       "{{ inputs.body_a }}\n{{ inputs.body_b }}"
    description: "concatenate the two responses"
graph:
  edges:
    - { from: fetch_a, to: merge, binds: { body_a: "{{ nodes.fetch_a.stdout }}" } }
    - { from: fetch_b, to: merge, binds: { body_b: "{{ nodes.fetch_b.stdout }}" } }
```

Fan-in is structural — `merge` has two incoming edges and the engine fires it once both `fetch_a` and `fetch_b` have completed (AND-strict). Each edge's `binds:` keys flow into the receiver's `inputs.*` namespace; `merge` reads `inputs.body_a` and `inputs.body_b`. Direct cross-graph reads (`{{ nodes.fetch_a.stdout }}`) also work and may be mixed in freely. If two edges into the same target bind the same key name, the parser raises `WorkflowBindCollision`.

**Sub-workflow invocation** — call another workflow as a node:

```yaml
nodes:
  prepare:    { type: kolsch/nodes/bash,     command: "echo {{ topic }}" }
  delegate:   { type: kolsch/nodes/workflow, ref: acme/workflows/triage }
graph:
  edges:
    - { from: prepare, to: delegate, binds: { input_text: "{{ nodes.prepare.stdout }}" } }
```

The `workflow` node creates a child `workflow_run` with `parent:` pointer; `nodes.delegate.<terminal_name>` exposes the called workflow's terminals (§6.6).

### 6.1 Top-level structure

```yaml
description: str                   # optional, defaults to "" — free text for catalog display
extends: list[str]                 # optional, defaults to [] — workflow refs to union into this
policy: list[Rule]                 # optional, defaults to [] — workflow-scope policy rules (was: permissions:)
timeout_seconds: int               # optional, default unset — workflow-level cooperative deadline
nodes: dict[str, NodeDef]          # required (may be empty if extends provides all)
graph:
  edges: list[Edge]                # required (absence makes this a node library)
```

All optional fields have defaults (empty string / empty list) when omitted. Author never needs to write them out for the simple case.

Note: there is no top-level `inputs:` or `outputs:` block. Inputs are inferred from the start node's input schema; outputs are inferred from the terminal nodes' output schemas (see §6.5 and §6.6).

### 6.2 Workflow kind inference

A workflow is RUNNABLE if it has a `graph:` key (any value of `edges:`, including empty). A workflow is a NODE LIBRARY if the `graph:` key is absent entirely. Libraries can be `extends:`'d but not `run:`'d; attempting `kolsch workflows run` on a library raises `WorkflowNotRunnable`. A single-node workflow with `graph: { edges: [] }` IS runnable: the validator's structural checks (unambiguous start + unambiguous terminal) catch genuinely-ambiguous shapes.

### 6.3 NodeDef shape (varies by type)

Common fields across types:
- `type: str` (required) — a three-segment catalog ref `<bundle>/nodes/<name>`. Built-ins live at `kolsch/nodes/<name>` for the 14 reserved names (see §4 and §4.0.1); authored Python lives at any other `<bundle>/nodes/<name>`. 1-segment shorthand is NOT supported (see `docs/explanation/workflow-policy-model.md`, *Node-type refs*).
- `copy_of: <other_node_name>` (alternative to `type:`) — creates a fresh copy of another node defined in the same workflow; inherits the parent's type and config. Any additional fields in the child override the parent's same-named fields. NOT to be confused with workflow-level `extends:` (which composes workflow assets); `copy_of:` works strictly within one workflow. The source node MUST itself be graph-reachable — no "template-only" status. For pure config sharing without graph residency, use YAML anchors.
- `description: str` (optional) — free-text label used by chat capsules and the `show` CLI to describe what this node does. When absent, surfaces fall back to the type-level `label:` (see `docs/explanation/workflow-foundation.md` §2.6.1) and then to the cleaned class/function name, per the 3-step rule in `process/phase3.9-chat-surface-spec.md` §15.4. Must be non-empty when set (empty string rejected at parse with `min_length=1`). `description:` is a USER AID for display; it is NEVER an identifier — the node NAME (the `nodes:` mapping key) is the only identifier.
- `when: str` (optional, node-level) — Jinja boolean expression that gates execution; node fires only when the expression evaluates truthy. NOT applicable to `branch` and `filter` nodes (which use `condition:` as their primary config field). The word `condition:` is reserved for that primary-config role; the gate modifier is spelled `when:`.
- `retry: int` (optional) — number of retry attempts on failure. Defaults to 0 (no retry). Engine uses a retry library (proposed: `tenacity`) under the hood; the simple integer at v1 is a thin facade exposing only the attempt count. Future versions can extend the surface (backoff, exception filters, jitter) by accepting a dict shape alongside the integer.
- `timeout_seconds: int` (optional, node-level) — per-node cooperative deadline; positive seconds the node may run before the engine fails it with `NodeTimeoutExceeded`. Retry policy applies. Independent from the workflow-level `timeout_seconds:` (§6.1).

There is NO `collect:` field on `NodeDef` and NO `ref:` field on either `NodeDef` or `Edge`. Fan-in is structural — multiple incoming edges per receiver (§6.4). The non-parent-predecessor declaration concept from earlier drafts is GONE; cross-graph `nodes.<X>.<field>` references are validated by graph-reachability (any transitive ancestor; §7.2).

Type-specific fields per §4.

Example of node copying with override:

```yaml
fetch_a: { type: kolsch/nodes/bash, command: "curl {{ url_a }}" }
fetch_b: { copy_of: fetch_a, command: "curl {{ url_b }}" }   # same type, different command
```

Example of fan-in via multi-input edges (see §6.4 for the edge shape):

```yaml
nodes:
  fetch_a: { type: kolsch/nodes/bash, command: "curl {{ url_a }}" }
  fetch_b: { type: kolsch/nodes/bash, command: "curl {{ url_b }}" }
  merge:
    type: kolsch/nodes/compute
    value:       "{{ inputs.body_a }}\n{{ inputs.body_b }}"
    description: "join the two curls"
graph:
  edges:
    - { from: fetch_a, to: merge, binds: { body_a: "{{ nodes.fetch_a.stdout }}" } }
    - { from: fetch_b, to: merge, binds: { body_b: "{{ nodes.fetch_b.stdout }}" } }
```

### 6.4 Edge shape

```yaml
- from: <node_name>
  to: <node_name>
  binds: dict[str, Any]            # target_input: <YAML literal> OR "{{ Jinja expression }}"
  branch: bool                     # optional, when source is branchable
```

The edge dict has exactly four fields: `from`, `to`, `binds`, `branch`. There is NO `ref:` field — fan-in is structural via multi-input edges, not declared on a single edge.

**`binds:` semantics.** `binds:` RHS follows the universal Jinja-field rule (see `docs/explanation/workflow-foundation.md` §2.7): values may be either YAML literals (string, int, float, list, bool, null) accepted as-is, OR Jinja expressions wrapped in `"{{ }}"` and evaluated under the sandboxed NativeEnvironment. Typical Jinja form: `"{{ nodes.<source>.<field> }}"` for a single-source bind, or any composite expression (`"{{ nodes.a.stdout | upper }}"`, `"{{ nodes.a.x + nodes.b.x }}"`). Typical YAML-literal form: `binds: { max_retries: 3, mode: aggressive }`. The legacy bare `source.field` shorthand is NOT supported. The earlier strict rule "every value must be a Jinja template" (R4 #11) is reversed under J5/β; the `WorkflowBindMustBeJinja` error class is removed (§12.1).

**`inputs.*` namespace.** Every key in `binds:` flows into the receiving node's `inputs.*` Jinja scope. For authored Python nodes, the same keys populate the Pydantic input model (same mechanism as before). For built-in nodes (bash, compute, branch, etc.), `inputs.<key>` is addressable from any of the node's own Jinja config fields. Authors may STILL use `nodes.<X>.<field>` directly for cross-graph reads; both forms coexist.

Note on naming consistency: the YAML Jinja namespace `inputs.*` (plural) matches the Python `@node` parameter convention `inputs: ModelType` (plural) — same word, two surfaces.

**Multi-input edges and AND-strict trigger semantics.** A receiver may have multiple incoming edges. The engine waits for ALL incoming edges' source nodes to complete before firing the target (AND-strict). For OR semantics, hoist the choice into an upstream `branch` node.

**Bind key collision.** If two or more edges into the same target bind the same `inputs.<key>` name, the parser raises `WorkflowBindCollision` with the offending edges and key name. Authors must rename one side.

**Runtime type validation via per-built-in `Inputs` models.** Strict-mode Pydantic validation applies UNIFORMLY: at node fire time, the engine renders each `binds:` Jinja expression and validates the result against the receiver's per-built-in `Inputs(BaseModel)` model (catalogued in §4.0.2) under `strict=True`. Mismatches raise `WorkflowBindTypeMismatch` (`'int'` rejects `'"5"'`). In practice, MOST built-ins use `Any`-typed accept-all fields with `extra="allow"` (because bind keys are author-named — the engine can't predict the names), so strict-mode catches MOSTLY fire on authored Python nodes and the `extra="forbid"` built-ins (`noop`, `read_file`, `write_file`, `list_files`, `delete_state`). Per-built-in tightening is a Phase 3.x QoL. `state.*` values are typed as `Any` at v1 (per-key typing deferred).

**Ordering-only edges** (no data binding) are legal: `{ from: A, to: B, binds: {} }` simply sequences `A` before `B`. Use this when you need topological ordering without dataflow.

### 6.5 Workflow input — inferred from start node

Workflow inputs are NOT declared at the top level. They're inferred from the start node's input schema. CLI's `kolsch workflows run --input k=v ...` assembles the key-value pairs into a dict and validates against this derived schema.

### 6.6 Workflow output — inferred from terminals

There is no `outputs:` declaration at the workflow level. The workflow's output is inferred from the graph topology:

- **Terminal node**: any node with no outgoing edges.
- **Workflow output schema** = dict keyed by terminal node name, with each value being that terminal's output schema.
- **At runtime**: the workflow output dict contains entries for terminals that actually fired; terminals on untaken branches yield `null`.

Example:

```yaml
nodes:
  classify:       { type: kolsch/nodes/agent,  agent: kolsch/agents/classifier }
  is_urgent:      { type: kolsch/nodes/branch, condition: "{{ label == 'urgent' }}" }
  urgent_handler: { type: kolsch/nodes/agent,  agent: kolsch/agents/urgent_responder }
  normal_handler: { type: kolsch/nodes/agent,  agent: kolsch/agents/normal_responder }

graph:
  edges:
    - { from: classify,  to: is_urgent }
    - { from: is_urgent, to: urgent_handler, branch: true }
    - { from: is_urgent, to: normal_handler, branch: false }
```

Both `urgent_handler` and `normal_handler` are terminals. The workflow's output schema is:

```
{
  urgent_handler: { response: str } | null,
  normal_handler: { response: str } | null
}
```

At runtime, exactly one of those two keys carries a populated value depending on which branch fired.

**For callers (workflow-call node, CLI)**: the workflow's output dict is the runtime result. Callers handle the polymorphism by checking which keys are present (idiomatically with `default()` filter or `if` modifier downstream).

**Single-terminal workflows** (no branching, single terminal) produce a one-key dict; trivial to consume. Callers expecting a stable shape should design workflows with a single terminal — the topology IS the contract.

### 6.7 Node identity & reuse

- Every entry in `nodes:` is a single invocation site, producing at most one captured output per workflow run, addressable as `nodes.<name>`.
- To reuse the same node logic, EITHER re-declare via `copy_of:` (gives the copy its own name and its own captured output), OR invoke as a callable target (map `apply:`, sub-workflow `ref:`).
- `copy_of:` source MUST be graph-reachable. No "template-only" status: if it's defined in `nodes:`, it must be reached by an edge from the start node (or itself be a map.apply: target). For pure config sharing without graph residency, use standard YAML anchors.
- Map workers are NOT addressable from outside the map node. Cross-graph references like `nodes.<worker>.<field>` raise `WorkflowMapperWorkerNotAddressable`. Only `nodes.<map_name>.results` is addressable externally.
- Sub-workflow internals are opaque. From outside, only `nodes.<workflow_node>.<terminal>` is visible (the called workflow's terminals per §6.6).
- Shared upstream consumed by two siblings is canonical and supported: `A → B` and `A → C` both reading `nodes.A`; node `A` runs exactly once and both siblings see the same captured output.
- State values evolve over the course of a workflow run. Multiple `set_state` nodes may write the same key — this is the canonical pattern for values that change as nodes execute (e.g., a running counter, a classification that gets refined, a flag that flips on a condition). The semantic is last-write-wins in topological order. The platform makes no determinism guarantee for the workflow as a whole — agent nodes are inherently non-deterministic — so state mutations are no different. Authors who need deterministic ordering structure their graph to make the desired writer last-in-topology.

---

## 7. Run-Start Validation Gate

The gate runs once at the start of every workflow_run. Failures here abort BEFORE any node executes (no side effects).

### 7.1 Structural checks

- Every YAML field that names a node must name a node DEFINED in this workflow's `nodes:` dict. Concretely: `edge.from`, `edge.to`, `copy_of:` source, and `map.apply:` target. An unresolved name raises `WorkflowNodeNotDefined` (carries the offending name and the field it appeared in). This is the structural "does the named thing exist?" check; it is DISTINCT from `WorkflowJinjaRefUnknown`, which covers Jinja `nodes.<X>.<field>` references and the predecessor relationship (see §7.2).
- Graph is a DAG (no cycles).
- Single unambiguous start node. Single-start rule preserved (exactly one node has no incoming edges).
- All nodes reachable from start OR named as a `map.apply:` target — anything else raises `WorkflowUnreachableNode`. Map workers don't appear in the edge graph (the map node invokes them per-iteration), so reachability is extended to include `apply:` targets explicitly.
- Multi-input nodes are LEGAL (a node may have any number of incoming edges; fan-in is structural). Per §6.4, the engine enforces AND-strict trigger semantics at runtime. The earlier `WorkflowMultiInputForbidden` check is GONE.
- Across all edges into the same target, no two edges may bind the same `inputs.<key>` name → `WorkflowBindCollision`.

### 7.2 Schema checks

- Every `binds:` RHS is either a YAML literal accepted as-is OR a valid Jinja template wrapped in `{{ }}` (per the universal Jinja rule in `docs/explanation/workflow-foundation.md` §2.7 and §6.4 binds semantics). There is NO "must-be-Jinja" check at v1; the `WorkflowBindMustBeJinja` class is removed under J5/β.
- Across all edges into the same target, no two edges may bind the same `inputs.<key>` name → `WorkflowBindCollision`.
- Static-type compatibility between bound expression and receiver input field is checked where statically determinable; at node fire time the engine validates rendered values against the Pydantic input model in STRICT mode → `WorkflowBindTypeMismatch`.
- Every `nodes.<X>.<field>` reference appearing in any Jinja-evaluated string field (binds RHS, every node-type's config strings, `when:`, `branch.condition:`, `filter.condition:`, `compute.value:`, `map.over:`, etc.) is pre-validated at the gate.

**Predecessor definition: graph reachability.** For a node `N`, the legitimate predecessor set is the set of TRANSITIVE ANCESTORS of `N` in the edge graph (closed under `edge.from → edge.to`). Any `X` in that closure is a valid target of `nodes.<X>.<field>` from inside `N`'s Jinja-evaluated fields. The relationship IS transitive — if `A → B → N`, then `N` may reference `nodes.A.<field>` directly, because `A` is an ancestor of `N`.

**AST walk.** For each Jinja template field:
1. Walk the template's AST via `jinja2.Environment().parse()`.
2. Extract every `Getattr` chain rooted at `Name("nodes")` to recover the `(X, field)` pair.
3. For each `(X, field)` pair, run the three checks below in order.
4. Map-worker references from outside their map node → `WorkflowMapperWorkerNotAddressable`.

**Check order for `nodes.<X>.<field>` references** (each check short-circuits the rest):

1. **Does `X` exist in `workflow.nodes`?** → if not, raise `WorkflowJinjaRefUnknown(reason="not_declared")`. The Jinja reference names a node that simply isn't defined in this workflow.
2. **Is `X` a transitive ancestor of the current node in the edge graph?** → if not, raise `WorkflowJinjaRefUnknown(reason="not_predecessor")`. The node exists, but is not reachable as an ancestor of the current node.
3. **Is `field` a declared output field of `X`'s output schema?** → if not, raise `WorkflowJinjaRefUnknown(reason="unknown_field")`.

**`WorkflowJinjaRefUnknown.reason` is typed `str`** (not `Literal`), with the documented value table:

| `reason` | Meaning |
|---|---|
| `not_predecessor` | The referenced node IS in the workflow but is not an ancestor (per graph reachability) of the current node. |
| `unknown_field` | The referenced node IS a predecessor but the field doesn't exist on its output schema. |
| `not_declared` | The referenced node doesn't exist in the workflow's `nodes:` map. |

Typing as `str` (not `Literal`) keeps the field forward-compatible — adding a new reason value is an additive change. The documented table is the contract; tests assert on these exact string values.

The earlier draft's carve-out — "nodes.* references are NOT pre-validated" — is DROPPED. Every `nodes.<X>.<field>` reference is checked statically at the gate via graph reachability. Runtime evaluation still surfaces Undefined errors for `nodes.<X>` that exist in the schema but were skipped/failed at runtime — the gate validates "this reference is well-formed"; runtime decides "did `X` actually run on this branch."

**Error accumulation: stdlib `ExceptionGroup`.** Structural independents accumulate within a single validator layer. The gate stops between layers (fail-fast across layers, accumulate within). When N > 1 errors accumulate in a layer, the validator raises a stdlib `ExceptionGroup("Workflow validation failed", [...])` whose `.exceptions` is the list of `WorkflowValidationError` subclasses; tests use `exc.exceptions` to iterate. When N == 1, the single exception is raised directly (no wrapping). Callers filter by sub-type using Python 3.11+'s native `except* WorkflowBindTypeMismatch:` syntax. There is NO custom `WorkflowValidationErrorGroup` class — the stdlib `ExceptionGroup` is the only wrapper.

### 7.3 Branching checks

- Edges with `branch:` originate from branchable nodes (bash, authored Python nodes whose `run` returns `bool` or `tuple[bool, BaseModel]`, branch).
- Branchable nodes with branching usage have both `branch: true` AND `branch: false` outgoing edges.
- Non-branching nodes don't have `branch:` on outgoing edges.

### 7.4 Type-specific checks

- `map`: `apply:` references a defined node; `over:` resolves to `list[T]`; applied node's input named per `item_as:` has type `T`. Worker nodes are NOT addressable as `nodes.<worker>` from outside the map node → `WorkflowMapperWorkerNotAddressable`.
- `filter`: `over:` resolves to a list; `condition:` is a valid Jinja predicate. Element-type compatibility checked at runtime (validator can't always determine element type statically from `over:`).
- `compute`: `value:` is a valid Jinja expression. Output is `{value: <evaluated>}`; downstream consumers reading `nodes.<compute>.value` are validated against the inferred return type when statically determinable.
- Workflow node: target ref resolves; not a node library; no transitive self-call.
- `branch`: `condition:` expression is valid Jinja that evaluates to bool.

### 7.4.1 Timeout checks

- Workflow-level `timeout_seconds:` (top-level) must be a positive int → else `WorkflowInvalidTimeout` (error). Same for any node-level `timeout_seconds:`.
- When both workflow-level AND per-node `timeout_seconds:` are set, a per-node value greater than the workflow value raises `WorkflowTimeoutShadowsNode` (warning) — the node's timeout will never fire because the workflow deadline will trip first.

### 7.5 Extends + composition checks

- `extends:` chain has no cycles → `WorkflowExtendsCycle`.
- `extends:` chain depth ≤ 16 → `WorkflowExtendsDepthExceeded`.
- Strict union: no duplicate node names or edge `(from, to)` pairs across the chain → `WorkflowDuplicateNode` / `WorkflowDuplicateEdge`.
- All resolved refs exist.
- **`extends:` does NOT import `policy:` (S5).** When any workflow in the `extends:` chain declares non-empty `policy:` rules, the run-start gate raises `WorkflowExtendsPolicyForbidden` (hard error) naming the offending extended workflow and the rules it declares. Shared policy patterns belong in `bundle.yaml` or `platform.yaml` (already walked by the layer order in `docs/explanation/workflow-policy-model.md` under "Layer order"). Cross-reference: `docs/explanation/workflow-foundation.md` §2.1.

### 7.6 Python node module checks

- Every `type: <bundle>/nodes/<name>` reference resolves to a discoverable file (`bundles/<bundle>/nodes/<name>.py`); the module imports successfully.
- Each module has a `run` function decorated with `@node`.
- Each `run` has a valid signature per §5.3 + valid return type per §5.4.
- Secret names in `@node(secrets_needed=[...])` exist in the secrets subsystem.

### 7.7 Policy checks

- All workflow `policy:` rules parse; all referenced tool/node-type refs resolve.
- Every Rule's `from:` field (when set) passes the strict 3-class grammar check per `docs/explanation/workflow-policy-model.md`, *`from:` field on Rule (caller-chain scoping)*: `WorkflowFromShapeError` (not 3-seg), `WorkflowFromAssetTypeError` (asset_type not `agents` or `workflows`), `WorkflowFromNotFound` (target ref doesn't exist in the catalog; for glob patterns, fires only when the glob has zero catalog matches).
- The workflow's invocation itself passes the policy layer walk.

### 7.8 `kolsch workflows validate` command

Runs the full gate without executing. Same checks; just stops after the validation pass.

### 7.9 Validator return shape: `ValidationResult` + thin raise wrapper

The validator's public API splits structured and raise-on-failure use cases:

- **`validate_workflow(workflow: Workflow, catalog: AssetCatalog) -> ValidationResult`** — the structured API. Returns a `ValidationResult` dataclass with `errors: tuple[WorkflowValidationError, ...]`, `warnings: tuple[WorkflowValidationWarning, ...]`, and an `is_valid: bool` property (true when `errors` is empty). Callers that want to surface warnings (CLI `--format json`, IDE tooling, the docs reviewer) use this API.
- **`validate_workflow_or_raise(workflow: Workflow, catalog: AssetCatalog) -> None`** — thin wrapper preserving the raise-on-failure pattern. Invokes the structured API; if `errors` is empty, returns. Otherwise raises (single `WorkflowValidationError` when N==1; stdlib `ExceptionGroup` wrapping the list when N>1, per §7.2). Warnings are SILENT in this path — callers that want warnings use the structured API. The CLI `validate` command default text mode uses this wrapper; `--format json` uses the structured API and includes both `errors` and `warnings` arrays in the envelope.

`ValidationResult` shape:

```python
@dataclass(frozen=True, slots=True)
class ValidationResult:
    errors: tuple[WorkflowValidationError, ...]
    warnings: tuple[WorkflowValidationWarning, ...]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0
```

---

## 12. Errors

### 12.1 Hierarchy

```
WorkflowError                         (base)
├── WorkflowValidationError
│   ├── WorkflowDuplicateNode
│   ├── WorkflowDuplicateEdge
│   ├── WorkflowExtendsCycle
│   ├── WorkflowExtendsDepthExceeded
│   ├── WorkflowExtendsUnresolved
│   ├── WorkflowExtendsPolicyForbidden    # NEW (R6 S5): extended workflow has policy: rules
│   ├── WorkflowNodeNotDefined            # was WorkflowUnknownNodeRef; any YAML name → no such node in nodes:
│   ├── WorkflowAmbiguousStart
│   ├── WorkflowNoStartNode
│   ├── WorkflowUnreachableNode
│   ├── WorkflowBindCollision             # two edges into same target bind the same inputs.<key>
│   ├── WorkflowBindTypeMismatch          # runtime: rendered bind value fails receiver's Pydantic strict-mode validation
│   ├── WorkflowJinjaRefUnknown           # was WorkflowNodeRefUnknown; reason: str (typed; see §7.2 table)
│   ├── WorkflowMapperWorkerNotAddressable
│   ├── WorkflowBranchOnNonBranchable
│   ├── WorkflowBranchMissingArm          # was WorkflowBranchDeadSide (clearer)
│   ├── WorkflowMapperWorkerMissingField
│   ├── WorkflowCallCycle
│   ├── WorkflowNodeImportFailed
│   ├── WorkflowNodeMissingRun
│   ├── WorkflowNodeNotDecorated
│   ├── WorkflowNodeSignatureInvalid
│   ├── WorkflowNodeReturnTypeInvalid
│   ├── WorkflowInvalidTimeout            # workflow- or node-level timeout_seconds is non-positive
│   ├── WorkflowInvalidRef                # type: value is not a valid 3-seg ref (e.g., 1-seg shorthand)
│   ├── WorkflowBuiltinShadowed           # user authoring at a reserved kolsch/nodes/<builtin> catalog slot
│   ├── WorkflowFromShapeError            # from: value is not a 3-seg ref
│   ├── WorkflowFromAssetTypeError        # from: asset_type segment is not "agents" or "workflows"
│   └── WorkflowFromNotFound              # from: ref's target doesn't exist in the catalog
├── WorkflowRuntimeError
│   ├── WorkflowNotRunnable
│   ├── WorkflowInputInvalid
│   ├── WorkflowNodeFailed
│   ├── WorkflowSecretUnresolved
│   ├── WorkflowFileAccessDenied
│   ├── WorkflowRunNotFound               # fork-from-event target run's JSONL has been deleted
│   ├── WorkflowJinjaSecurityError        # NEW (R6 S1): sandbox blocked unsafe attribute/callable access
│   ├── WorkflowTimeoutExceeded           # workflow-level timeout hit at a node boundary
│   └── NodeTimeoutExceeded               # per-node timeout exceeded; retry applies
└── WorkflowValidationWarning             # base class for non-fatal validator findings
    └── WorkflowTimeoutShadowsNode        # per-node timeout > workflow-level timeout (will never fire)
```

**Classes REMOVED in Round 4:**

- `WorkflowMultiInputForbidden` — the single-input rule is gone (see `docs/explanation/workflow-foundation.md` §2.2, §6.4, §7.1). Multi-input edges ARE the fan-in mechanism; no error is raised for them.
- `WorkflowFromShapeWarning` — REPLACED by the three hard errors `WorkflowFromShapeError` / `WorkflowFromAssetTypeError` / `WorkflowFromNotFound`. The `from:` grammar is fully strict at parse/validate time per `docs/explanation/workflow-policy-model.md`, *`from:` field on Rule (caller-chain scoping)*; what was a "looks like a typo" warning is now a hard error class triplet.

**Classes ADDED in Round 4:**

- `WorkflowBindCollision` — parse-time error when two edges into the same target node bind the same `inputs.<key>` name (§6.4, §7.1).
- `WorkflowBindTypeMismatch` — runtime error at node-fire time when a rendered `binds:` value fails the receiver's Pydantic strict-mode input validation (§6.4 type-validation pass).
- `WorkflowFromShapeError`, `WorkflowFromAssetTypeError`, `WorkflowFromNotFound` — the three hard-error classes for the strict `from:` grammar (see `docs/explanation/workflow-policy-model.md`, *`from:` field on Rule (caller-chain scoping)*).
- `WorkflowRunNotFound` — raised on fork-from-event when the parent run's JSONL has been deleted (see `process/phase3.8-fork-spec.md`).

**Classes REMOVED in Round 6:**

- `WorkflowBindMustBeJinja` — removed under J5/β (see `docs/explanation/workflow-foundation.md` §2.7 and §6.4). `binds:` RHS values may now be YAML literals OR Jinja expressions; there is no "must-be-Jinja" check.

**Classes ADDED in Round 6:**

- `WorkflowJinjaSecurityError` — raised by the sandboxed Jinja environment (see `docs/explanation/workflow-foundation.md` §2.7.2) when an unsafe attribute or callable access is blocked (e.g., `__class__`, `__mro__`, `__globals__`). Subclass of `WorkflowRuntimeError`.
- `WorkflowExtendsPolicyForbidden` — raised at run-start when any workflow in the `extends:` chain declares `policy:` rules (§7.5, see also `docs/explanation/workflow-foundation.md` §2.1 and `docs/explanation/workflow-policy-model.md` under "Layer order"). Subclass of `WorkflowValidationError`.

Notes on the classes:
- `WorkflowExtendsCycle` and `WorkflowExtendsDepthExceeded` are distinct.
- `WorkflowNodeNotDefined` (renamed from `WorkflowUnknownNodeRef`) is the STRUCTURAL "this YAML name refers to a node that doesn't exist in `nodes:`" error. Fires for `edge.from`, `edge.to`, `copy_of:` source, and `map.apply:` target. The corresponding Jinja-reference miss (`nodes.<X>.<field>` with `X` not defined) is raised as `WorkflowJinjaRefUnknown(reason="not_declared")` per §7.2 — the structural class and the Jinja class are kept distinct so error messages can be precise about origin.
- `WorkflowJinjaRefUnknown` is the Jinja `nodes.<X>.<field>` error. The `reason` field is typed `str` (not `Literal`) with the value table documented in §7.2: `"not_predecessor"`, `"unknown_field"`, `"not_declared"`. Semantics shifted in Round 4 — predecessor checking is now graph-reachability-based, not parent+`ref:`-list-based.
- `WorkflowUnreachableNode` fires for nodes defined in `nodes:` but unreachable from the start node AND not named as a `map.apply:` target.
- `WorkflowMapperWorkerNotAddressable` fires when an outside-map Jinja reference points at a map node's worker. (Class name retained — verbose but accurate; alternatives are vaguer.)
- `WorkflowInvalidRef` fires for any non-3-seg `type:` value (e.g., `type: bash` 1-seg shorthand, malformed `kolsch/bash` 2-seg, etc.). The 1-segment shorthand is GONE everywhere (see `docs/explanation/workflow-policy-model.md`, *Node-type refs*).
- `WorkflowBuiltinShadowed` fires when a user authors a node-type file at a reserved built-in catalog slot (e.g., `bundles/kolsch/nodes/bash.py` collides with the `kolsch/nodes/bash` built-in). Detected at catalog scan time.

**Per-class `caller_message` and `identifying_names_keys` ClassVars.** Every `WorkflowError` subclass (validation, runtime, and warning) declares two class-level attributes alongside the dataclass fields:

```python
class WorkflowBindTypeMismatch(WorkflowValidationError):
    caller_message: ClassVar[str] = "A workflow step received the wrong type of value. Share the run ID with your engineer."
    identifying_names_keys: ClassVar[tuple[str, ...]] = ("node", "edge", "bind")
    # … dataclass fields naming the structured identifiers
```

- `caller_message` is the end-user-targeted plain-English channel (see §12.3 two-channel rendering).
- `identifying_names_keys` declares which dataclass fields the `--format json` renderer should hoist into the `identifying_names` dict (see §14.4 per-class shape).

The base `WorkflowError` carries a generic `caller_message` default; subclasses override.

**Multi-error wrapper: stdlib `ExceptionGroup`.** The validator collects per-layer findings into a list. When N > 1 errors accumulate, the validator raises `ExceptionGroup("Workflow validation failed", [<list of WorkflowValidationError>])` — the stdlib type from Python 3.11+ (kolsch runs on 3.13). Tests access sub-exceptions via `exc.exceptions` (ExceptionGroup convention). Callers filter by sub-type using the native `except* <ErrorClass>:` syntax. There is NO custom `WorkflowValidationErrorGroup` class. When N == 1 the single exception is raised directly without the group wrapper. ExceptionGroup formatting is stdlib (renders as a tree); the workflow module does NOT override `__str__` on the group.

### 12.2 Conventions

Each exception class:
- Carries structured attributes (the relevant identifiers — node names, refs, types).
- `__str__()` produces a single human-readable line.
- Wrapped original exceptions go in `__cause__`.
- No error codes. Caller introspects the class hierarchy.

### 12.3 Error message contract

Every `WorkflowValidationError` subclass that originates from author-supplied YAML produces a STRICT SINGLE-LINE `__str__()` rendering. No `\n`. No multi-line snippets. No embedded YAML blocks.

**Format:**

```
<ClassName>: <file>:<line> <identifying-names>: <problem summary>. Fix: <one-line action>.
```

Three required components on every line:

1. **Source location** — the YAML file path AND line number of the offending construct, formatted `<file>:<line>`. (Parser carries line numbers through to validator-time errors via ruamel.yaml's line-info-preserving load.)
2. **Identifying names** — the affected node name(s), edge endpoints, ref(s), and any other identifier the author needs to find the construct, immediately after the file:line token.
3. **Fix-path sentence** — the fix-path is a single-line action prefixed by the literal token `Fix: ` (capital F, colon, space). The fix-path is PROSE describing the action. The following are FORBIDDEN in the Fix portion:
   - Inline Jinja code: `{{`, `}}`, `{%`, `%}`
   - YAML literal fragments showing syntax (e.g., `ref: [foo]`, `condition: bool`)
   - Fenced or backticked code spans
   - Multi-line content of any kind (already precluded by the single-line rule)

   The following ARE allowed:
   - Single-quoted identifiers naming workflow elements (`'fetch_a'`, `'merge'`)
   - Technical vocabulary as prose (`binds-edge`, `extends chain`, `Jinja delimiters`)
   - Cross-references to docs (see `docs/workflows#anchor-name`)

   The fix-path tells the author WHAT to do; for exact syntax, the docs link carries it.

Example:

```
WorkflowJinjaRefUnknown: workflows/triage.yaml:42 node 'summarize', reference 'nodes.parse.label': node 'parse' is not a transitive ancestor of 'summarize'. Fix: add an edge that makes 'parse' reachable from 'summarize' (see docs/workflows#fan-in).
```

**Reviewer asserts on every error class:**
- No `\n` in `str(exc)`.
- Message contains `:` between file and line (the file:line token).
- Message contains the literal `"Fix: "` token (capital F, colon, space).
- Message contains NO `{{`, `{%`, or fenced/backticked code.

This contract is binding on the implementation: an error message that omits any of the three or breaks the single-line rule is a defect.

**Multi-error accumulation.** Per §7.2, when N > 1 errors accumulate the validator raises a stdlib `ExceptionGroup` whose `.exceptions` is the list of single-line `WorkflowValidationError` instances. ExceptionGroup formatting is stdlib (renders sub-exceptions as a tree); the workflow module does not customize it.

### 12.4 Two-channel rendering: `caller_message`

Every `WorkflowError` subclass declares a class-level `caller_message: ClassVar[str]` attribute — a one-sentence plain-English statement for non-engineer end users. The strict single-line `str(exc)` (per §12.3) is engineer-targeted; `caller_message` is the parallel end-user-targeted channel.

Surfaces that render `caller_message`:
- Chat capsules (per `process/phase3.9-chat-surface-spec.md`) — when an error surfaces in chat, the capsule shows `caller_message`, NOT the engineer `str(exc)`.
- CLI `--format json` envelope — the per-error JSON object includes both fields (per §14.4).

Style rules — `caller_message` strings MUST:
- Be 1-3 sentences. Plain English.
- Include NO class names (`WorkflowJinjaRefUnknown` etc.).
- Include NO `file:line` tokens.
- Include NO Jinja delimiters (`{{`, `}}`, `{%`, `%}`).
- Include NO technical jargon (no "predecessor", "RHS", "AST", etc.).
- Recommend an action — typically "try again", "share the run ID with your engineer", "contact your engineer".

Examples:

```
WorkflowBindTypeMismatch.caller_message =
    "A workflow step received the wrong type of value. Share the run ID with your engineer."

WorkflowJinjaSecurityError.caller_message =
    "A workflow step tried to do something that isn't allowed. Share the run ID with your engineer."

WorkflowTimeoutExceeded.caller_message =
    "This workflow took longer than expected and was stopped. You can try again."

WorkflowSecretUnresolved.caller_message =
    "A required secret isn't available. Contact your engineer to configure it."
```

The base `WorkflowError` carries a generic default ("Something went wrong running the workflow. Share the run ID with your engineer."); subclasses override with specific text.

The reviewer asserts on every subclass: non-empty `caller_message` ClassVar; passes the style rules above (no class names, no `{{`, no `file:`-style tokens, no fenced code).

### 12.5 Author-facing docs (`docs/workflows.md`)

Phase 3.0 ships `docs/workflows.md` as a deliverable. Every `docs_anchor` referenced in an error message (e.g., `docs/workflows#binds-rhs`) MUST resolve to an actual anchor in that file. Initial coverage (~16 anchors): `#binds-rhs`, `#inputs-namespace`, `#extends`, `#fan-in`, `#node-types`, `#copy-of`, `#map`, `#branch`, `#policy-from`, `#layer-selection`, `#timeout`, `#cancel`, `#trace-id`, `#error-format`, `#secrets`, `#chat-capsules`. Each anchor is 200-500 words: concept, syntax, examples, common mistakes. The reviewer verifies anchor resolution as part of the parser-validator gate.

---

## 13. Policy (validation rules)

The conceptual policy model — layer walk, proximate-caller rule for layer selection, `from:` field semantics, caller-chain construction, refs unification, and configurable audit emission — lives in `docs/explanation/workflow-policy-model.md`. This section retains ONLY the Phase 3.0 validator-side rules: the parse-time and run-start-gate errors that the parser/validator must emit when policy declarations are malformed.

### 13.7 Validator-side policy errors

The run-start gate (§7.7) validates every workflow `policy:` block. The errors raised are:

- **`WorkflowFromShapeError`** — a Rule's `from:` value is not a 3-segment ref (e.g., `from: triage`, `from: acme/triage`). Subclass of `WorkflowValidationError`.
- **`WorkflowFromAssetTypeError`** — a Rule's `from:` middle segment is not `agents` or `workflows` (e.g., `from: acme/tools/bash`). Subclass of `WorkflowValidationError`.
- **`WorkflowFromNotFound`** — a Rule's `from:` ref shape is valid and its asset_type is in scope, but the target (after glob expansion if non-glob) doesn't exist in the catalog. For glob patterns, this fires only when the glob has ZERO catalog matches at validate time — partial matches pass. Subclass of `WorkflowValidationError`.
- **`WorkflowExtendsPolicyForbidden`** — any workflow in the `extends:` chain declares non-empty `policy:` rules. Subclass of `WorkflowValidationError`. The reasoning behind this hard error (preventing accidental policy inheritance) is documented in `docs/explanation/workflow-policy-model.md` under "Layer order".

The earlier soft `WorkflowFromShapeWarning` class is REMOVED — the `from:` grammar is now fully tight (three hard-error classes, no warnings, no soft acceptance).

All four fire at parse/validate time before any node executes. The conceptual policy model these errors enforce is documented in `docs/explanation/workflow-policy-model.md`.

---

## 14. CLI Surface (parser/validator commands)

The Phase 3.0 CLI ships parser- and validator-side commands ONLY. The execution-side commands (`run`, `resume`, `cancel`, `fork`, `events`) ship with the phases that introduce their machinery (`process/phase3.1-executor-spec.md`, `process/phase3.4-user-input-spec.md`, `process/phase3.8-fork-spec.md`).

### 14.0 Symmetric asset-discovery pattern (L7-12)

Across asset types, kolsch presents a UNIFORM discovery surface: `kolsch <asset-type> list` enumerates the bundles' contributions of that asset type, and `kolsch <asset-type> show <ref>` prints the structured definition of one. The pattern applies to `workflows`, `nodes`, `agents`, and `tools` — every asset type that has a catalog presence. The symmetric pair gives authors a single muscle-memory verb across the whole catalog: "to see what's available, type `kolsch <thing> list`; to inspect one, type `kolsch <thing> show <ref>`."

**Phase 3.0 ships the workflow side of the symmetry (already extant) plus the NODE side new in this phase:**

- `kolsch [--format text|json] nodes list [--bundle <name>]` — list every node TYPE in the catalog (built-ins + authored Python from every loaded bundle). Optional `--bundle <name>` filter limits results to one bundle.
- `kolsch [--format text|json] nodes show <ref>` — print the structured definition of one node type: its inputs schema, outputs schema, secrets_needed, label, and source path.

**Cross-asset symmetry — current state and follow-up.** `kolsch agents list/show` and `kolsch tools list/show` already exist (they pre-date Phase 3.0). Phase 3.0 adds `kolsch nodes list/show`. The `skills` and `prompts` asset types are NOT yet rationalized into the symmetric pattern at Phase 3.0; their migration is tracked in `process/qol-asset-cli-symmetric.md` as a follow-up QoL. The rationalization of the existing `agents`/`tools` commands to share a common Click implementation pattern is also captured there. Cross-reference: §14.3 per-command data schemas.

### 14.1 Commands

```
kolsch [--format text|json] workflows list
kolsch [--format text|json] workflows show <ref>
kolsch [--format text|json] workflows validate <ref>

kolsch [--format text|json] nodes list [--bundle <name>]
kolsch [--format text|json] nodes show <ref>
```

`nodes list` and `nodes show` are NEW in Phase 3.0 per L7-12 — they complete the symmetric `kolsch <asset-type> list/show` surface (per §14.0) for the node-type asset family that this phase introduces. The commands operate on the SAME catalog the validator and parser consult; built-ins and authored Python both appear.

(`--format` can be specified before or after the subcommand path; Click handles both naturally.)

### 14.3 CLI output format convention

The `kolsch` CLI supports a global `--format text|json` flag that applies to every command. Default is `text` (current human-friendly behavior unchanged); `json` produces a structured envelope for machine consumption.

**Envelope shape** (all commands, `--format json`):

```json
{
  "command": "<subcommand path>",
  "status": "ok" | "error",
  "data": { ... },
  "errors": [ ... ]
}
```

- `command` echoes the invoked subcommand path (e.g., `workflows validate`).
- `status` is `ok` on success or `error` on failure.
- `data` carries the command-specific payload (per-command schema; see below).
- `errors` is present when `status='error'`; an array of per-error objects (see §14.4).

Exit codes are unchanged: 0 on success, non-zero on failure, regardless of `--format`.

**Per-command data schemas:**

- `workflows list` → `{ workflows: [{ref, kind, description}, ...] }`
- `workflows show <ref>` → `{ workflow: {...full structure...}, derived_input_schema, derived_output_schema }`
- `workflows validate <ref>` → on success: `{ workflow_ref, input_schema, output_schema }`; on failure: `status='error'` + errors array
- `nodes list` → `{ nodes: [{ref, kind, label, secrets_needed}, ...] }` — `ref` is the 3-segment catalog ref; `kind` is `"builtin"` or `"authored"`; `label` is the type-level label per `docs/explanation/workflow-foundation.md` §2.6.1; `secrets_needed` is the list from `@node(secrets_needed=[...])` (empty for built-ins).
- `nodes show <ref>` → `{ node: {ref, kind, label, secrets_needed, inputs_schema, outputs_schema, config_schema, source_path} }` — the FULL structure of one node type: inputs/outputs/config schemas as JSON-Schema-style dicts derived from the Pydantic models, `source_path` is the on-disk file (`bundles/<bundle>/nodes/<name>.py` for authored, `src/workflows/builtin/<name>.py` for built-ins).

**Migration of existing CLI commands.** Existing `kolsch agents`, `kolsch tools`, `kolsch permissions`, etc. do not yet honor `--format`. Migration is tracked in `process/qol-cli-format-output-rollout.md` and ships as a follow-up QoL. The contract above is normative for any new command authored in Phase 3.0+.

### 14.4 Exception JSON shape

When `--format json` is in effect, each error in the envelope's `errors` array is rendered as:

```json
{
  "class": "<exception class name>",
  "file": "<relative path>",
  "line": <int>,
  "identifying_names": { "<role>": "<name>", ... },
  "summary": "<one-line problem summary>",
  "fix": "<one-line action sentence>",
  "caller_message": "<end-user-targeted plain-English sentence>",
  "docs_anchor": "docs/workflows#<anchor>",
  "attrs": { ... }
}
```

- `class` is the Python class name (e.g., `WorkflowJinjaRefUnknown`).
- `file` and `line` are the YAML source location for YAML-origin errors; absent or null for non-YAML errors.
- `identifying_names` is a dict of role → name (e.g., `{"node": "merge", "edge": "merge→summarize"}`). **Auto-derived** from the exception class's `identifying_names_keys: ClassVar[tuple[str, ...]]` declaration plus dataclass introspection: the renderer extracts the listed keys from instance fields. No per-class formatter is required.
- `summary` is the one-line problem summary (same text as the `str(exc)` summary token).
- `fix` is the one-line action sentence (the text after `Fix: ` in `str(exc)`).
- `caller_message` is the end-user-targeted plain-English sentence from the class's `caller_message: ClassVar[str]` attribute (§12.4).
- `docs_anchor` is the docs URL fragment referenced in the message (if any; null otherwise).
- `attrs` is everything left over — every dataclass field NOT in `identifying_names_keys` lands here automatically.

**Single formatter, generic across classes.** `src/cli/_format.py:render_exception(exc)` is the lone JSON formatter. It uses dataclass introspection plus the `ClassVar` lookups (`caller_message`, `identifying_names_keys`) to produce the shape above. Adding a new exception class requires NO new formatter code — declare the ClassVars and the field set, and the renderer Just Works.

The reviewer asserts on every exception class:
- `identifying_names_keys` ClassVar is non-empty (when the error has any identifying context).
- Every key listed in `identifying_names_keys` IS a real dataclass field on the class.
- Golden JSON fixtures per exception class verify the rendered shape.

For `ExceptionGroup`-wrapped multi-error reports, the `errors` array contains one object per sub-exception. The wrapping `ExceptionGroup` is implicit in the envelope's `status: error`.

---

## 16. Asset Catalog Integration

### 16.1 Discovery

`bundles/<bundle>/workflows/*.yaml` auto-discovered. Workflow node libraries (no graph) and runnable workflows coexist in the same directory.

### 16.2 Catalog entry

```python
CatalogEntry(
    asset_type="workflow",
    ref="<bundle>/workflows/<name>",
    bundle=<bundle>,
    path=<absolute path>,
    metadata={"kind": "workflow"|"node_library", "extends": [...], ...},
)
```

### 16.3 Validation at load

Asset loader validates the YAML schema (not the full run-start gate — that happens per-run). Catches malformed YAML, unknown node types, structural shape issues.

---

## 17. Migration

### 17.1 No-op for existing workspaces

Workflows are new. Existing workspaces gain the capability the moment Phase 3.0 ships; no platform.yaml changes needed.

### 17.2 Starter bundle

Add one example workflow to `bundles/kolsch/workflows/hello.yaml` demonstrating the basic shape, plus one authored Python node at `bundles/kolsch/nodes/echo.py`. Discoverable via `kolsch workflows list` immediately after `kolsch setup`.

---

## 19. Testing Strategy

### 19.1 Unit tests

Per-node-type tests: each built-in type tested in isolation with mock context. Python nodes tested by calling `run()` directly.

### 19.2 Component tests

Workflow harness: load a YAML, run through the executor, assert event log + final outputs. Coverage matrix: every node type, branching paths, fan-out, fork mechanism, sub-workflow nesting, pause/resume.

### 19.3 Golden tests

Add `tests/golden/tc_wf_*.jsonl` for canonical workflow runs. Capture full event streams; assert semantic equality modulo volatile fields (timestamps, all `*Id` prefix-typed fields). The Phase 3.1 executor designer chooses the normalization mechanism (regex-substitute volatile tokens before diff, structured-diff with a per-field ignore list, or AST-walk equality on parsed JSON) when shipping the executor. No canonicalization tool is committed at Phase 3.0; the principle — "events compare equal when their non-volatile content is equal" — is the contract; the mechanism is an implementation choice.

### 19.4 Integration with existing engine

Verify that `agent`-typed nodes drive the existing CrewAI engine path with correct framing (workflow_run_id surfaces in agent events; agent's session links back).

---

## 20. Open Questions Deferred to Implementation

### 20.1 Implementation discovery items

Items intentionally left for implementation discovery rather than spec lock-in:

- Exact wire format for `workflow_run_id` (prefix? length? — match existing session ID conventions).
- Per-event-kind payload schemas in JSON (the `docs/explanation/workflow-event-log.md` list is the kinds; payloads enumerated during implementation).
- Exact CLI output format (table vs JSON; flag spelling).
- Chat capsule visual design (ASCII shape; collapse behavior).
- Where workflow YAML schema lives in `docs/reference/schemas.md`.

### 20.2 Spec items proposed but not locked in conversation

The conversation locked the design but not these specific shapes. Implementation may refine:

- Library choice for retry semantics. `tenacity` is the canonical Python retry library and the proposal; alternatives include `backoff` and `retry`. Selection during 3.3 implementation; the workflow surface (just an integer count at v1) stays library-agnostic.

### 20.3 Explicitly out-of-scope for Phase 3.0

- **Cost telemetry / dollar-cost reporting.** Cost calculation is AGENT-LAYER scope, not workflow-layer scope. Phase 3.0 does NOT introduce cost telemetry primitives. The `model.response.received.data.usage` payload already carries token counts (captured from the LLM response); the `.cost` field is RESERVED (Phase 3.0 populates it as `0.0`). When the agent layer ships pricing tables and computes dollar cost per call, workflow events inherit that data automatically — no workflow-engine changes required. Cross-reference: `docs/explanation/workflow-event-log.md` emit-rules note on `model.response.received`.
- **Triggers, schedulers, webhooks, HTTP entry points.** These are `kolsch-server` concerns, not `kolsch-engine`. See the §1.2 architectural boundary and `process/phase3.x-roadmap.md`. Targeted for Phase 4+ or as a companion project.
- **Multi-tenant retention quotas.** "Forever retention" is operator-controlled at v1 (see `docs/explanation/workflow-foundation.md` §2.3): JSONLs are plain files on disk and operators manage them with standard filesystem tools. Per-tenant quotas and automated retention policies are deferred to Phase 4+ multi-tenancy work.

### 20.4 Known gaps deferred to Phase 3.x (Round 5 + Round 7)

- **Hermetic `validate --offline` for customer CI (CV4).** Customer CI integration with hermetic `kolsch workflows validate --offline` (Terraform-style validate-without-creds — parser/validator runs without contacting any external service or requiring resolved secrets) is a known gap deferred to Phase 3.x. At v1 the validator may incidentally require secret resolution for some checks; tightening the boundary so the validator is provably offline-clean is a follow-up. Round 7 CV4 acknowledged this as a CI gap and deferred resolution alongside the broader hermeticity story.
- **Layered config CLI (CV2).** A `kolsch config validate` (cross-file consistency check across platform.yaml / bundle.yaml / workspace overrides) and `kolsch config show --effective` (print the fully resolved config as it would be seen by the engine, with layer-of-origin annotations) are deferred to Phase 3.x. Operators currently inspect the layered config by reading the source files directly. Spec versioning (an explicit `spec_version:` field on workflow YAML, an `extends_version:` field on `extends:` refs, version-negotiation rules across bundle.yaml / platform.yaml) is deferred TOGETHER with the layered config work under CV2 — both touch the same "what's the canonical resolved config?" question and are best designed as one piece.
- **Signals primitive (T7-3).** A `signal` node type — a way for one workflow run to send a named signal to another and for receivers to wait on a signal — is OUT of scope at all Phase 3.x phases. The use cases that motivate it (cron-triggered workflows, webhook-receiver workflows, scheduled wake-ups) are covered by the `kolsch-server` split documented in R4 #15 and §1.2 — triggers, schedulers, and webhook callbacks live in a separate component, not in `kolsch-engine`. T7-3 is closed by the R4 #15 architectural boundary; no engine-side signal primitive is planned.
- **`from_json` Jinja filter (CV1) — CLOSED.** Stock Jinja2 has no JSON-parse filter, and the earlier draft left this as an open question. **CV1 LOCKS the decision: `from_json` ships at v1 as a core-only filter (per `docs/explanation/workflow-foundation.md` §2.7.3); no longer an open question.** The idiom is `{{ nodes.fetch.stdout | from_json }}`; the filter is implemented in `src/workflows/jinja_env.py` and documented in `docs/explanation/jinja-extensions.md` (kolsch-maintainer contribution pattern for adding new core filters; explicitly NOT a bundle-author surface).
