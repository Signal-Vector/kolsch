# Phase 1 Schemas — Draft 0.4

**Pi-style context assembly: events store inputs; context is derived.**

> Conceptual schemas — structure, fields, rules, rationale. Not yet literal
> Pydantic classes. Supersedes draft 0.3.

---

## Part 0 — Decisions Locked Through Draft 0.4

- Asset references: **Option B** — namespaced paths.
- Resolver merge: **Option B** — JSON Merge Patch (RFC 7386). `null` deletes.
- `model` is a **base field** of AgentConfig, not a subsystem.
- Subsystem wrapper is `{variant, config}` — absence = disabled.
- **State vs. event split:** entity records hold mutable state (Postgres);
  the event log holds immutable execution history (JSONL files).
- **Agent Engine is the sole emitter** of the session event log.
- **Event log schema is part of the Agent Engine contract** — stable across
  engine swaps.
- 100% fidelity: log is self-contained, summarizes nothing.
- **The event log is a tree.** `parent_event_id` is the single structural
  ordering mechanism. No `seq` field.
- **The current leaf of a thread is DERIVED from the log**, not stored.
- **The model's context is built on the fly** from the tree, every turn.
  Pi's model: events store the inputs (asymmetrically — only what cannot be
  re-derived); the assembled context is never stored. (NEW in 0.4)
- **The Materialization Rule:** anything contributing to a model call's
  context must be captured as an immutable event with actual content at the
  time of use. (NEW in 0.4 — load-bearing.)
- **The Assembly Procedure is normative and versioned.** Its version is part
  of the resolved configuration (recorded on `config.set`). (NEW in 0.4)
- Phase 1 has no explicit user branching, no fork operation, no UI. Tree
  machinery is exercised through retries.

---

# Part A — The Config Schema

## A.1 What it is

Defines the configuration of one agent. Every configuration artifact is a
*partial* of this schema; the effective config for an agent run is the
JSON-Merge-Patch merge of an ordered list of partials.

## A.2 Shape

```
AgentConfig
├── schema_version       string        — schema version this conforms to
├── id                   string        — "ag_..."  immutable system identity
├── name                 string        — human handle; renameable
├── description          string        — what this agent is for
├── model                ModelSpec     — the LLM and its call parameters
├── fallback_models      [ModelSpec input]  — optional, default []; ordered
│                                            list of backup models the engine
│                                            walks on Recoverable model.*
│                                            errors. See A.4.2 (Phase 1.3,
│                                            FB-R1).
├── io                   IOSpec        — typed declared inputs/outputs + mode
├── prompt               PromptSpec     — layered system-prompt composition;
│                                        rendered as a Jinja2 template at
│                                        engine pre-flight (Phase 2.2; see
│                                        §A.11). Backward-compatible.
├── extends              string?       — Phase 2.2; optional prefixed path to
│                                        a parent agent file (§A.11.3).
├── permissions          [Rule]        — Phase 2.2; optional per-agent authz
│                                        rules composed agent → extends →
│                                        defaults → platform → default-deny
│                                        (§A.11.4).
├── context_files        [ContextFileSpec]
│                                       — Phase 2.2; optional list of
│                                         documentation files inlined or
│                                         referenced in the rendered prompt
│                                         (§A.11.5).
├── assembly_version     string        — assembly procedure version in effect
│                                        (see Part B.5; recorded on config.set)
└── subsystems           map<string, SubsystemConfig>
                                       — memory, tools, skills; each optional;
                                         absence = disabled
```

Base layer = *what the agent is*. `subsystems` map = *how each pluggable
subsystem is configured*. `assembly_version` selects which version of the
context-assembly procedure (Part B.5) is in force for this agent. The
Phase 2.2 envelope fields (`extends`, `permissions`, `context_files`) are
covered in §A.11.

## A.3 Identity — `id` and `name`

Every entity has both:
- `id` — prefixed UUID, immutable, system identity.
  Prefixes: `ag_` agent, `ses_` session, `th_` thread, `ev_` event, `cl_` call.
  Phase 2 adds `wf_`, `run_`, `node_`.
- `name` — human handle; renameable; what appears in YAML and UI.

Authoring references resolve by `name`; execution binds and records `id`.

## A.4 ModelSpec

```
ModelSpec
├── provider     string            — e.g. "anthropic"
├── model        string            — e.g. "claude-opus-4-7"
├── parameters   map<string,any>   — temperature, max_tokens, thinking level
│                                    / reasoning budget, etc.
└── fallback     [ModelSpec]        — optional fallback chain; may be empty
                                      in Phase 1 (shape reserved)
```

Reasoning/"thinking" models add `parameters` here and a reasoning trace to
the event log — they do NOT touch IOSpec.

### A.4.1 Input shapes (Phase 1.1 amendment, LLM-R1)

`ModelSpec.model` accepts three input shapes for ergonomics — the kernel
resolver normalises all three to the canonical object form above during
Layer 1 validation. Downstream code sees only `{provider, model}`.

1. **Canonical object form** —
   `{provider: "opencode", model: "gpt-oss-120b"}`.
2. **Compact `provider/model` string** — `"opencode/gpt-oss-120b"` —
   split on the first `/`; left = `provider`, right = `model`.
3. **Alias string** — `"smart"` — looked up in `platform.yaml`'s
   `model_aliases` map (see Asset Management §3.2) and expanded to the
   canonical form. Aliases do not chain; an unknown alias raises
   `config.unknown_alias` (Fatal) at Layer 1 validation.

This permissiveness lives entirely in the resolver. The ModelSpec
schema above is what every other module sees.

### A.4.2 `fallback_models` — backup chain (Phase 1.3, FB-R1)

`AgentConfig.fallback_models` is an optional ordered list. Default
`[]`. Each entry accepts the same three input shapes as
`ModelSpec.model` (§A.4.1):

1. **Canonical object form** — `{provider, model}`.
2. **Compact `provider/model` string** — split on the first `/`.
3. **Alias string** — looked up in `platform.yaml`'s `model_aliases`.

All entries are normalised to the canonical object form during
Layer 1 validation. Downstream code (engine, replay, etc.) sees
only `list[{provider, model}]`.

When the agent omits the field, the platform-level
`defaults.fallback_models` (Asset Management §3.2) applies; when the
agent specifies it (even as `[]`), the agent value wins — no
merging. Same precedence rule as every other agent/platform default.

The engine walks this list on Recoverable `model.*` errors only —
the trigger table lives in the Phase 1.3 patch §2.2. Fatal errors
abort without consulting the chain.

Duplicates within the chain (or with the primary) are allowed and
emit a `config.fallback_duplicate` warning at agent load (Error
Model §4.15) — they're not a mistake; a user may have intentionally
listed the same model twice to retry it.

> NOTE — Phase 1 reserved `ModelSpec.fallback` (the inner list on
> `ModelSpec` itself) as a future shape. FB-R1 makes
> `AgentConfig.fallback_models` the canonical agent-level chain;
> the relationship between the two fields is a structural
> follow-up flagged for review.

## A.5 IOSpec

The typed task contract.

```
IOSpec
├── mode      enum                    — single_shot | conversational
├── inputs    map<string, TypeSpec>   — single_shot: invocation inputs
│                                       conversational: shape of ONE
│                                       user message
└── outputs   map<string, TypeSpec>   — single_shot: final outputs
                                        conversational: shape of ONE
                                        agent message
```

(TypeSpec details unchanged from draft 0.3.)

## A.6 PromptSpec

Two shapes:

**List of refs** — a list of asset refs that get loaded from disk and
concatenated in order:

```yaml
prompt:
  - "acme-research/prompts/role.md"
  - "my-bundle/prompts/project-context.md"
```

**Single string** — inline content used directly:

```yaml
prompt: "You are a research assistant. Your purpose is to..."
```

The platform infers which shape from the YAML structure (list → list of
refs; scalar → inline content). At Asset Management's pre-resolution
step (per Asset Management contract §6.2), refs in a list are replaced
with file content; the inline string passes through unchanged. The
resolved config carries actual content, not refs.

The Agent Engine, when assembling the system prompt at execution time,
concatenates the list items (in the list-shape case) or uses the string
directly. It then appends the available-skills listing (per Skills
subsystem §5.1) — the skills listing is engine-appended, not declared
in PromptSpec.

There is no `id`, no per-item wrapper, no `source` discriminator.
Composition (sharing content across multiple agents) is the user's
authoring concern; they handle it via file references and standard
markdown patterns.

## A.7 SubsystemConfig — the common wrapper

```
SubsystemConfig
├── variant   string   — which provider implementation
└── config    object   — variant-specific body (schema owned by the variant)
```

Absence in resolved config = subsystem disabled. Two-stage validation:
kernel validates wrapper; named subsystem validates body. Phase 1 subsystems:
`memory`, `tools`, `skills`.

## A.8 Asset References (Option B)

A namespaced path: `<package>/<asset-type>/<name>`. Reserved package names:
`local`, `core`. Collision-safe across packages. Not rename-stable —
authoring refs may dangle on rename; past runs are unaffected because the
log captures actual content materialized at use time (Materialization Rule,
Part B.6).

## A.9 The Resolver (JSON Merge Patch, RFC 7386)

```
resolve(layers: [PartialAgentConfig]) -> ResolvedAgentConfig
```

Pure function. Ordered list of partials, merged per RFC 7386:
- objects merge recursively, key by key;
- a key set to `null` deletes that key (how a higher layer disables an
  inherited subsystem);
- non-object values replace wholesale;
- omission = inherit.

A resolved config is fully validated before the Agent Engine executes it.

## A.10 Schema and procedure versioning

- `schema_version` is on every artifact; older snapshots remain
  interpretable.
- `assembly_version` is part of every resolved config; an older event log
  replays under the assembly procedure version recorded in its `config.set`
  events, preserving fidelity across procedure evolution.
- External references (model name, asset path) are NOT versioned — they
  resolve to what they name now.
- Fidelity comes from the event log capturing fully-dereferenced actual
  values at execution time and from the assembly procedure being
  deterministic at its recorded version.

## A.11 Phase 2.2 — Agent Envelope (`extends`, `permissions`, `context_files`)

Phase 2.2 (process/phase2.2-agent-envelope-specs.md) adds three optional
fields to `AgentConfig` plus reinterprets `prompt:` as a Jinja2 template.

### A.11.1 `prompt:` as a Jinja2 template

The `prompt:` field carries a Jinja2 template; the engine pre-flight
renders it once per exchange with a standardized variable context. The
render uses `StrictUndefined` — typo'd variable names raise
`config.template_render_failed` (Fatal) at agent-load. A prompt with no
Jinja markers renders byte-identical to itself; every Phase 1.x / 2.0 /
2.1 agent file continues to work unchanged.

### A.11.2 Composition order (binding)

For every agent-envelope field:

```
schema default  ←  defaults.<field>  ←  extends chain  ←  agent
        (lowest precedence)                      (highest precedence)
```

Per-field wholesale override (no list / dict merging). Silence (field
absent) is transparent — the next-lower layer is consulted. An explicit
`[]` or `null` is a present-but-empty value that wholesale-replaces
the next-lower layer.

### A.11.3 `extends: str | None`

Optional prefixed-path reference to a parent agent YAML:

- `kolsch:<rel>` — resolves to a resource inside the installed kolsch
  package via `importlib.resources.files("kolsch") / <rel>`.
- `workspace:<rel>` — resolves to `<workspace_root> / <rel>`.

Plain paths are rejected at agent-load with
`config.context_file_unprefixed`. Cycles raise
`config.agent_inheritance_cycle`; missing parents raise
`config.parent_agent_not_found`.

### A.11.4 `permissions: list[Rule]`

Optional list of per-agent authz rules. Same shape as
`platform.yaml:authorization.config.rules` entries — `who` matches the
principal; `what` matches the action OR the resolved target. Composed
agent → platform → default-deny.

### A.11.5 `context_files: list[ContextFileSpec]`

Optional list of documentation files. Each entry:

| Field | Required | Type | Default |
|---|---|---|---|
| `path` | yes | `str` | Prefixed: `kolsch:<rel>` or `workspace:<rel>` |
| `summary` | no | `str | None` | first non-empty line of file |
| `primary` | no | `bool` | `false` (secondary = listed by path + summary only) |

Each file is capped at 64 KiB; oversize raises `context.file_too_large`.

---

# Part B — The Event Log

## B.1 What it is

The append-only, **tree-structured** record of everything that happened
during agent execution in a session. Ground truth; all execution state is
reconstructed from it. File-resident, append-only, one JSONL file per
session.

The log stores **inputs and outputs of execution, not assembled artifacts.**
Specifically, the log captures *anything that contributes to a model call's
context, materialized at the time of use* (the Materialization Rule, §B.6).
What the model actually saw is *derived* from the log by the Assembly
Procedure (§B.5) — it is never stored.

The log records EXECUTION HISTORY only — not entity state (Part C). Emitted
solely by the Agent Engine. Schema is part of the Agent Engine contract.

## B.2 The Event envelope

```
Event
├── id                  string    — "ev_..."   the event's own id
├── ts                  string     — ISO-8601 UTC
├── type                string     — the event type
├── thread              string?    — "th_..."  the thread, when applicable
├── parent_event_id     string?    — "ev_..."  parent in the tree
│                                    (for the first event of a thread,
│                                     points at the thread's id)
└── data                object     — type-specific contents
```

Notes (unchanged from 0.3):
- No `seq` field. Tree via `parent_event_id` is the order.
- `session_id` not in the envelope — the file IS the session.
- `agent` and `run` not in the envelope — derivable / Phase 2.
- Every event carries genuine `data`; UI state is on entity records (Part C).

## B.3 The Tree Model

The events of a session form a tree, with the file as a linearized view:

- Every event has `id` and `parent_event_id`.
- The first event of a thread points at the thread's `id` (`th_...`).
- The current leaf is **derived from the log**, not stored:
  - Phase 1: the most recently appended event in the thread.
  - Later phases (branching): the most recently appended *childless* event.

## B.4 The Append Protocol

The Agent Engine, when appending a new event to a thread:

1. Find the current leaf by reading the log.
2. Set the new event's `parent_event_id` to the leaf's id.
3. Append the event.

No stored leaf update — the new event's existence makes it the new leaf for
future appends. Runtime may cache the tree/leaf in memory as an optimization,
discarded and rebuilt on resume.

## B.5 The Assembly Procedure (Normative)

**This procedure is part of the Agent Engine contract.** It is versioned via
`AgentConfig.assembly_version` and recorded on `config.set` events. Any
replay of the log at a past point uses the version in effect at that point,
preserving fidelity across procedure evolution.

The procedure constructs, on demand, the input to a model call:

1. **Walk the tree.** Starting at the thread's current leaf, walk
   `parent_event_id` pointers back to the thread root, collecting all events
   on the path. Reverse the list to chronological order.

2. **Determine effective config.** The most recent `config.set` on the path
   (or, for the very first call, the `config.set` emitted at thread start)
   gives the resolved AgentConfig in effect — model spec, prompt layers,
   subsystem configurations, assembly procedure version.

3. **Compose the system prompt** by concatenating the configured prompt
   layers in declared order.

4. **Project events to conversational messages.** Walk the path, projecting
   each event to a model-facing message (or omitting it) per the table
   below. The projection rule for each event type is part of the assembly
   procedure version:

   | Event type | Projection |
   |---|---|
   | `user.message` | one user message, content verbatim |
   | `agent.message` | one assistant message, content verbatim |
   | `tool.called` | the tool-call block of an assistant message |
   | `tool.result` | one tool-result message, content verbatim |
   | `tool.failed` | one tool-result message (error variant) |
   | `memory.retrieved` | content from the event injected as instructed by the variant's projection rule (see §B.6) |
   | `context.compacted` | the summary replaces the events listed in `compacted_event_ids`; those events are skipped in the projection |
   | all other types | not projected (config/progress/observability only) |

5. **Apply tool definitions.** From the effective config's tool subsystem,
   provide the tool list to the model in its expected format.

6. **Hand to the model.** The system prompt, message list, and tool list
   are the model's input.

The output of this procedure is **not stored** anywhere. It is computed
fresh on every call. What *is* stored is the inputs (the events on the path)
plus the call's metadata (`model.request`) and the model's output
(`model.response`).

**Determinism requirement:** for a given tree path and assembly version, the
procedure MUST produce a byte-identical output. No clock reads, no
random ordering, no implementation-specific variation.

## B.6 The Materialization Rule (Load-Bearing)

**Anything contributing to a model call's context must be captured as an
immutable event with the actual content at the time of use.** The Assembly
Procedure replays events; if an event references something external whose
value could change, fidelity is silently broken on replay.

Concretely:

- **Configuration in effect** — captured on `config.set` events, fully
  dereferenced. No live references to mutable assets; actual model spec,
  actual prompt layer contents, actual tool definitions.
- **Memory retrievals** — `memory.retrieved` events carry the actual content
  returned by the retrieval, verbatim. The Assembly Procedure uses the
  captured content; it does NOT re-run the retrieval at replay time.
- **Skill bodies** — if a skill body is loaded into context for a turn, the
  body content is captured (either in a dedicated `skill.loaded` event or
  inline in the message that triggered the load). A skill referenced by name
  in the system prompt is captured via the prompt layer's content in
  `config.set`.
- **File contents read into context** — captured in the `tool.result` of the
  reading tool. Files read via `@`-references in user messages are
  materialized into the `user.message` event itself.
- **User input and agent output** — captured verbatim on `user.message` and
  `agent.message`.

If anything contributes to context and is NOT captured as an immutable
event, that is a spec violation. The Materialization Rule is the bargain
that makes on-the-fly context assembly honest: replay reconstructs what
the model saw because every input is in the log.

**Amendment (draft 0.6, 2026-05-28) — `model.request.data.messages` is
required and authoritative.** Earlier drafts (and §B.8 above) said
"the context is derivable from the tree via the Assembly Procedure";
that derivation is sufficient under a non-wrapping framework but
diverges under any framework that mutates messages between the
engine's hand-off and the LLM call (CrewAI's prompt-template
injection, multimodal reshaping, role normalization). To close the
gap, **`model.request.data` MUST carry a `messages` field — the
verbatim list as observed at the LLM seam, immediately before the
LLM is invoked.**

Concretely:

- `messages` is the list of `{role, content, tool_call_id?,
  tool_calls?, name?}` dicts the underlying LLM received.
- Captured post-framework-wrap, pre-LLM-invoke. The wrap is part of
  the framework's deterministic contribution; the engine does not
  attempt to "unwrap" — the wrapped view is the materialized fact.
- **Replay reads `messages` directly from this event** for "what
  did the model see at this call." The Assembly Procedure (§B.5)
  still applies for projecting context for *future* calls (calls
  that haven't happened yet and therefore haven't been captured).
- The amendment is additive — schemas honoring the old "no messages
  on model.request" shape are forward-compatible by treating
  missing `messages` as the empty list.

This is the load-bearing fix for the Materialization Rule under
wrapping frameworks. Without it, replay against any non-trivial
framework adapter (Step 12b's CrewAI adapter, future engine swaps)
would silently lose fidelity.

## B.7 Phase 1 Event Types

Grouped. All emitted by the Agent Engine.

### Session identity (the file's first event)
- `session.opened`

### Thread progress
- `thread.exchange_started`
- `thread.exchange_completed`
- `thread.exchange_failed`

### Conversation content (projected to context)
- `user.message`
- `agent.message`
- `tool.called`
- `tool.result`
- `tool.failed`

### Model interaction (record of the call, not the call's input)
- `model.request` — what call was made; the input is derivable from the tree
- `model.response` — what the model produced (not derivable; must be stored)

### Config
- `config.set` — full resolved config in effect, including assembly_version

### Memory (Materialization Rule)
- `memory.written`
- `memory.retrieved` — carries the actual content retrieved, verbatim

### Compaction
- `context.compacted` — summary plus full pre-compaction content

Phase 2 adds `run.*`, `node.*`, `edge.*` and adds `run` to the envelope.

## B.8 Per-type `data` Specifications

**`session.opened`** — thread: none
```
data:
  session_id      string    — "ses_..."
  initial_agent   object    — { id, name } of the agent the session started with
  forked_from     object?   — provenance pointer (forks deferred)
```

**`thread.exchange_started`** — thread: the thread
```
data:
  trigger_event_id   string   — ev_ id of the user.message (or other trigger)
```

**`thread.exchange_completed`** — thread: the thread
```
data:
  exchange_started_id   string
  agent_message_id      string
```

**`thread.exchange_failed`** — thread: the thread
```
data:
  exchange_started_id   string
  error                 object   — {type, message, detail, where}
```

**`user.message`** — thread: the thread
```
data:
  content   object     — typed per the agent's IOSpec.inputs (conversational mode)
  images    [object]?  — attached images (content, or content + hash)
```

**`agent.message`** — thread: the thread
```
data:
  content       object   — typed per IOSpec.outputs
  exchange_id   string   — ev_ id of the matching exchange_started
```

**`model.request`** — thread: the thread
```
data:
  call_id           string   — "cl_..." correlates with model.response
  model             object   — concrete model spec used (provider, model,
                              parameters) — actual values, not references
  config_event_id   string   — ev_ id of the config.set in effect
```
**No `assembled_context` field.** The context for this call is derivable
from the tree via the Assembly Procedure at the assembly version recorded
in the effective `config.set`.

**`model.response`** — thread: the thread
```
data:
  call_id           string   — matches model.request
  content           object   — the model's raw response, verbatim
  reasoning_trace   object?  — for reasoning models: full trace, verbatim
  usage             object   — input_tokens, output_tokens, reasoning_tokens?
  cost              number
  latency_ms        integer
  stop_reason       string
```

**`tool.called`** — thread: the thread
```
data:
  tool_name      string
  tool_call_id   string   — correlates with tool.result / tool.failed
  arguments      object   — actual arguments passed, verbatim
```

**`tool.result`** — thread: the thread
```
data:
  tool_call_id   string
  tool_name      string
  result         object   — actual result, verbatim (large: content + hash)
  is_error       boolean
```

**`tool.failed`** — thread: the thread
```
data:
  tool_call_id   string
  tool_name      string
  error          object
```

**`config.set`** — thread: the thread
```
data:
  resolved_config   object   — FULL, fully-dereferenced resolved AgentConfig
                              including assembly_version. Self-contained.
  config_hash       string   — hash of resolved_config
```

**`memory.written`** — thread: the thread
```
data:
  scope      string
  content    object    — actual content written, verbatim
  variant    string    — memory provider variant
```

**`memory.retrieved`** — thread: the thread
```
data:
  scope      string
  query      object     — the retrieval query
  results    [object]   — actual items returned, verbatim
                          (Materialization Rule — the Assembly Procedure
                          uses THIS content, never re-runs the query)
  variant    string
```

**`context.compacted`** — thread: the thread
```
data:
  compacted_event_ids   [string]   — ev_ ids whose events the projection
                                     should skip in favor of the summary
  pre_compaction        object     — FULL content compacted, verbatim
                                     (the log keeps everything; only the
                                      model's projected context is shorter)
  summary               object     — the summary that replaces those events
                                     in the projection
  tokens_before         integer
  tokens_after          integer
```

## B.9 Retries — Natural Branching In Phase 1

When the Agent Engine retries a failed call, the failure remains in the
tree (`model.response` with error / `tool.failed`) and the retry attaches as
a sibling — both children of the same parent. The parent walk from the
successful leaf naturally skips the failed sibling (it's not on the path).
Fidelity preserved; no special retry logic; tree machinery validated.

## B.10 Reconstruction Guarantee

Given a thread's events at any point and the assembly procedure version
recorded in the effective `config.set` at that point, replaying through the
Assembly Procedure (§B.5) reproduces **byte-identically** what the model saw
at that point. The guarantee rests on:

- Events being immutable.
- The Materialization Rule ensuring every input is captured.
- The Assembly Procedure being deterministic at its recorded version.

A session is resumed by opening the JSONL file — no other lookup needed for
session content.

---

# Part C — Entity Records (State, not Events)

```
Session  (entity record)
├── id           string    — "ses_..."
├── name         string    — mutable; human-readable session name
├── starred      boolean    — mutable; UI-organizational
├── archived     boolean    — mutable; UI-organizational
├── created_at   string
└── (the session's event log is the JSONL file identified by this id)

Thread  (entity record)
├── id           string    — "th_..."
├── session_id   string
├── agent_id     string
├── created_at   string
└── (events are those in the session log with this thread id;
    current leaf is DERIVED from the log)
```

Phase 1: one session has one thread. Index rows: small projected summaries
per session for cross-cutting queries.

---

# Part D — How It All Relates

- `AgentConfig` (Part A) is resolved by the kernel and handed to the Agent
  Engine.
- The Agent Engine executes it, narrating execution into the session's
  JSONL log (Part B) via the tree append protocol.
- The model's context is computed fresh every turn by the Assembly Procedure
  walking from the thread's leaf back to the root.
- Inputs to model calls are captured as events (per the Materialization
  Rule); outputs are captured on `model.response`. The assembled context
  itself is never stored.
- Entity records (Part C) hold mutable state — name, starred, archived,
  identity bindings — not content history.
- The event log + the Assembly Procedure version together are the contract;
  same tree + same procedure version = same context, byte-identically.

Two stores:
- **Postgres** — entity records, index rows.
- **JSONL files** — the event log.

Memory's own storage is internal to the Memory Provider Module.

---

# Part E — Open Items

1. **`error` object shape** — `{type, message, detail, where}` sketched.
2. **Large-content handling** — content + hash, content store mechanism.
3. **`file_ref` resolution** — local path Phase 1; future object-storage
   abstraction.
4. **Assembly Procedure precise specification** — §B.5 sketches it; a
   later pass writes the procedure precisely enough to be conformance-tested.
   Determinism must be enforceable.
5. **Cross-thread event ordering** — Phase 2 concern.
6. **Tree-walk efficiency at scale** — Phase 1 is constant-time-at-end-of-
   file; runtime cache amortizes for active sessions.
7. **Materialization Rule conformance** — every subsystem that contributes
   to model context (memory, tools, skills) needs a documented capture
   mechanism. Per-subsystem detail in a later pass.
