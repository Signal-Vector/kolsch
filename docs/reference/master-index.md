# Phase 1 — Master Index

**Draft 0.1**

> A guide to reading the Phase 1 spec suite. What each document is, in
> what order to read them, what depends on what, and where to find
> specific concepts.

---

## The Spec Suite

Fifteen documents:

**Foundation (read first):**
1. `specification-draft-0.2.md` — consolidated architectural skeleton
2. `phase1-v1-scope-draft-0.1.md` — what v1 ships and what it doesn't
3. `phase1-schemas-draft-0.4.md` — config schema, event log, entity records

**Subsystem contracts:**
4. `phase1-kernel-resolver-contract-draft-0.1.md` — config validation and merge
5. `phase1-session-run-store-contract-draft-0.1.md` — session/thread store, JSONL log
6. `phase1-event-bus-contract-draft-0.1.md` — Redis pub/sub for ephemeral events
7. `phase1-memory-provider-contract-draft-0.1.md` — memory variant contract, recent_buffer
8. `phase1-secrets-sandbox-authz-contract-draft-0.1.md` — three Provider Modules
9. `phase1-tools-subsystem-draft-0.1.md` — @tool decorator, dispatch, MCP
10. `phase1-skills-subsystem-draft-0.1.md` — Agent Skills, progressive disclosure
11. `phase1-asset-management-contract-draft-0.1.md` — bundles, filesystem layout, catalog
12. `phase1-file-handling-draft-0.1.md` — `file_ref`, path-access policy
13. `phase1-agent-engine-contract-draft-0.1.md` — execution engine, CrewAI wrapping

**Cross-cutting:**
14. `phase1-error-model-draft-0.1.md` — error envelope, three levels, type catalog

**Verification:**
15. `phase1-contract-audit-draft-0.1.md` — inter-module contract audit
16. `phase1-test-plan-draft-0.1.md` — per-module + overall validation tests
17. `phase1-audit-revisions-draft-0.1.md` — corrections from the audit

---

## Recommended Reading Order

The documents have real dependencies. Reading in this order minimizes
back-references.

**Pass 1 — get the architectural picture (1 hour):**

1. `specification-draft-0.2.md` — the consolidated skeleton. Read this
   first; it frames everything.
2. `phase1-v1-scope-draft-0.1.md` — what v1 actually ships. Sets
   expectations: this isn't the whole vision, it's the slice that
   becomes real first.

**Pass 2 — the data model (45 min):**

3. `phase1-schemas-draft-0.4.md` — config, events, entities. The
   spec's foundation. Many later documents reference §A, §B, §C.

**Pass 3 — the runtime mechanisms (2-3 hours):**

Read in this order; each depends on what came before:

4. `phase1-kernel-resolver-contract-draft-0.1.md` — config resolution.
   Depended on by everything that has config.
5. `phase1-asset-management-contract-draft-0.1.md` — bundle discovery,
   filesystem layout, platform bootstrap. Depended on by every
   subsystem that reads configuration.
6. `phase1-session-run-store-contract-draft-0.1.md` — sessions,
   threads, JSONL persistence. Depended on by the engine and memory.
7. `phase1-event-bus-contract-draft-0.1.md` — Redis ephemeral
   notification. Independent but conceptually adjacent to the store.
8. `phase1-memory-provider-contract-draft-0.1.md` — memory contract +
   v1 recent_buffer.
9. `phase1-secrets-sandbox-authz-contract-draft-0.1.md` — three Provider
   Modules. Used by Tools and the Engine.
10. `phase1-tools-subsystem-draft-0.1.md` — @tool decorator, ToolContext,
    builtin tools, MCP. Depends on Secrets, Sandbox, Authorization.
11. `phase1-skills-subsystem-draft-0.1.md` — skills overlay on tools.
    Depends on Tools and Asset Management.
12. `phase1-file-handling-draft-0.1.md` — `file_ref` and path policy.
    Builds on Asset Management, Authorization, Tools.
13. `phase1-agent-engine-contract-draft-0.1.md` — pulls everything
    together. Read last in this pass.

**Pass 4 — cross-cutting and verification (1 hour):**

14. `phase1-error-model-draft-0.1.md` — references error types defined
    by every other spec; easier to read after seeing them in context.
15. `phase1-contract-audit-draft-0.1.md` — verifies the spec set is
    internally consistent. Identifies gaps; introduces revisions.
16. `phase1-audit-revisions-draft-0.1.md` — applied corrections. (As
    of draft 0.1, these are already incorporated into the spec files
    above.)
17. `phase1-test-plan-draft-0.1.md` — what implementations must
    demonstrate.

Total reading time: roughly 5-7 hours for someone new to the spec.
Re-readers can skip Passes 1-2 and focus on the area they need.

---

## Dependency Map

Which documents depend on which, for understanding (not implementation).

```
specification-draft-0.2 ─── foundation; informs everything
v1-scope ─── independent; sets v1 boundaries
schemas ─── foundation for events, config, entities

kernel-resolver
├── consumed by: asset-management, all subsystems for validation
└── depends on: schemas (config shape)

asset-management
├── consumed by: tools, skills, engine
└── depends on: schemas, kernel-resolver

session-run-store
├── consumed by: engine, memory
└── depends on: schemas (events)

event-bus
├── consumed by: engine (publish), external (subscribe)
└── depends on: schemas (events)

memory-provider
├── consumed by: engine
└── depends on: schemas, session-run-store, kernel-resolver

secrets-sandbox-authz
├── consumed by: tools, engine
└── depends on: schemas, kernel-resolver

tools-subsystem
├── consumed by: engine, skills
└── depends on: schemas, kernel-resolver, secrets, sandbox, authz, assets

skills-subsystem
├── consumed by: engine
└── depends on: schemas, kernel-resolver, tools, assets

file-handling
├── consumed by: tools, engine
└── depends on: tools (builtin), authz (parallel mechanism), assets (bootstrap)

agent-engine
├── consumed by: CLI, API
└── depends on: ALL of the above

error-model
├── consumed by: all subsystems (every error type registered here)
└── depends on: schemas (events carry errors)

contract-audit
├── verifies internal consistency
└── reads: all contracts

audit-revisions
├── corrections to apply
└── modifies: 8 specs

test-plan
├── acceptance criteria for v1
└── reads: all contracts
```

---

## Where to Find Specific Concepts

A topical index for jumping to the right spec.

### Architecture
- **Module categories** — Kernel, Engine, Provider, Subsystem definitions →
  `specification-draft-0.2` Part 5
- **What's in v1 vs. deferred** → `v1-scope` Parts 2-3
- **Forward-looking architecture** (server / runner / IDE) →
  `v1-scope` Part 6

### Data Model
- **AgentConfig schema** → `schemas` §A
- **Event log schema, tree structure** → `schemas` §B
- **Entity records (Postgres)** → `schemas` §C
- **Assembly Procedure** (event tree → model context) → `schemas` §B.5
- **Materialization Rule** → `schemas` §B.6
- **TypeSpec, IOSpec** → `schemas` §A.4

### Configuration
- **Layer 1/2/3 validation** → `kernel-resolver` Part 4
- **JSON Merge Patch (RFC 7386)** → `kernel-resolver` Part 3
- **PromptSpec** → `schemas` §A.6
- **platform.yaml** → `asset-management` §3.2, §3.10
- **SubsystemConfig wrapper** → `schemas` §A.7

### Sessions & Logs
- **Session/Thread/Branch model** → `schemas` §C, `session-run-store` Part 1
- **JSONL append, file locking** → `session-run-store` Part 4
- **Filesystem layout** → `asset-management` Part 3
- **Reconstruction from filesystem** → `session-run-store` §2.5
- **Content store** (`content/<hash>.bin`) → `session-run-store` §2.3,
  `asset-management` §3.6

### Tools
- **@tool decorator** → `tools-subsystem` Part 2
- **ToolContext** → `tools-subsystem` §2.4
- **Builtin tools (read/write/edit/bash)** → `tools-subsystem` Part 4
- **MCP server integration** → `tools-subsystem` Part 5
- **Tool dispatch order** (validate → authz → secrets → sandbox → execute) →
  `tools-subsystem` §6.2

### Skills
- **Agent Skills standard adoption** → `skills-subsystem` Part 1
- **Progressive disclosure** → `skills-subsystem` Part 2
- **Force-invoke** → `skills-subsystem` §6.2
- **Skill discovery** → `skills-subsystem` Part 3,
  `asset-management` §4.3

### Memory
- **Recent buffer variant** → `memory-provider` Part 5
- **Tag system** (asymmetric write_tag/read_tags) → `memory-provider` §5.2
- **MemoryContext** → `memory-provider` §3
- **Cache reconstruction from log** → `memory-provider` §3.3

### Security & Permissions
- **Authorization rules** → `secrets-sandbox-authz` §3
- **Path-access policy** → `file-handling` Part 4
- **Principal derivation** → `agent-engine` §1.8
- **Secrets via dotenv** → `secrets-sandbox-authz` §1
- **Sandbox (native)** → `secrets-sandbox-authz` §2

### Errors
- **PlatformError class** → `error-model` Part 3
- **Canonical Error envelope** (type, message, where, detail, cause) →
  `error-model` Part 2
- **Three error levels** (Diagnostic, Recoverable, Fatal) →
  `error-model` Part 1
- **Error type catalog** → `error-model` Part 4

### Bundle & Asset Management
- **Bundle layout** → `asset-management` Part 2
- **Asset catalog** → `asset-management` Part 4
- **Reference resolution** (shorthand, full) → `asset-management` Part 5
- **Pre-resolution of prompts** → `asset-management` §6.2

### Files
- **`file_ref` as string** → `file-handling` Part 1
- **Path policy** (allowed_paths, denied_paths) → `file-handling` §4.1
- **`bash` exception** → `file-handling` §4.7
- **Tool output destination** (`data/`, never `content/`) →
  `file-handling` Part 3

### Execution
- **Engine pre-flight** → `agent-engine` §2.1
- **Framework setup** → `agent-engine` §2.2
- **execute / execute_stream** → `agent-engine` §1.1
- **Capability declaration** → `agent-engine` §1.7

### Event Bus
- **Publish/subscribe** → `event-bus` Part 2
- **v1 ephemeral-only** → `event-bus` §5.1
- **Strict subscriber pattern** → `event-bus` §5.1

### Testing
- **Per-module test categories** → `test-plan` Parts 1-11
- **Four overall validation tests** → `test-plan` Part 12

---

## Key Vocabulary

Concise definitions of the platform's core terms. For **which word to
use when writing docstrings** (rather than what each term means), see
[`vocabulary.md`](vocabulary.md) — it sits on top of this section and
assigns each near-synonym to a lane.

**Agent call** — an atomic unit of work: one execution of one
configured agent. The smallest unit the platform schedules.

**Asset** — a configurable raw material: a tool, skill, agent, or
prompt. Identified by namespaced ref `<bundle>/<type>/<name>`.

**Asset Catalog** — the in-memory registry of all assets across all
installed bundles. Built at platform startup.

**Agent** — a template for an agent (configuration + behavior).
Distinguished from "session" (an instance).

**Assembled context** — the ephemeral payload sent to the model for
one model call. Built fresh per call from the event tree; never
stored as a single object.

**Assembly Procedure** — the deterministic algorithm that projects a
tree path into model-facing messages. Versioned (`assembly_version`)
because future changes need replay compatibility.

**Branch / Path** — a thread is a tree (Phase 2 has real branching);
the path is the linear sequence from root to current leaf.

**Bundle** — a directory with manifest containing assets (tools,
skills, agents, prompts). Distributed via git or any other means.

**Diagnostic / Recoverable / Fatal** — the three error levels.
Diagnostic = informational event the model sees; Recoverable =
retried internally; Fatal = ends the exchange.

**Engine** — the Agent Engine; the module that orchestrates an
execute call by wrapping CrewAI.

**Event** — an immutable record in the session log. Carries
`event_id`, `parent_event_id`, `type`, `data`, `actor`.

**Leaf** — the most recent event on a thread (Phase 1) or branch
(Phase 2). The point where new events are appended.

**Materialization Rule** — anything contributing to the model's
context must be captured as an immutable event with verbatim
content at the time of use. (Per `schemas` §B.6.)

**MemoryContext** — a session-lifetime handle the engine uses for
memory operations. Bound by `memory.bind_session`.

**Pi-style tree event log** — the design where `parent_event_id` is
the primary ordering mechanism; no monotonic `seq`; the leaf is
derived from the log, not stored.

**Platform root** — the current working directory at platform
startup. The platform's filesystem layout is relative to this.

**Principal** — the actor for an authorization check. `user.local`
for top-level execute; `agent.<id>` for tool calls within; `system`
for synthetic platform operations.

**Provider Module** — a swappable module category with a contract
plus registry plus future policy. Memory, Secrets, Sandbox,
Authorization in v1.

**Resolved config** — an AgentConfig produced by merging partials
through the kernel resolver. Carries pre-resolved prompt content
(refs replaced with text) but preserves tool/skill refs.

**Session** — an instance of an agent's conversation. One JSONL
event log file per session. Distinct from Agent (the template).

**Subsystem** — a module category with a bounded API and a single
v1 implementation. Asset Management, Session/Run Store, Event Bus,
Tools, Skills.

**Thread** — a persistent isolated history line within a session.
Phase 1 has one thread per session; multi-thread arrives later.

---

## What's Owed

After this index, the spec set is substantively complete and
internally consistent. The remaining work is implementation, not
specification.

**Per-spec open items** — each spec has its own Open Items section
listing future-phase work. Survey them as a group:

- v1 Scope §6: forward-looking architecture (server/runner/IDE).
- Schemas §B.7: branching model details.
- Kernel/Resolver: variant validator registration mechanism.
- Session/Run Store: configuration of paths (open item already
  closed by the platform-root = CWD decision).
- Memory: real-memory variants (semantic, vector).
- Tools: tool versioning, deprecation.
- Skills: skill versioning.
- Error Model: localization, traceback policy.
- Asset Management: bundle distribution UX, hot reload.
- File Handling: structured `file_ref` future variant.
- Agent Engine: workflow integration.
- Test Plan: CI infrastructure.

None of these is a v1 blocker. All are noted in their respective
specs and can be addressed as the platform evolves.

---

## Document Status

| Document | Status | Length |
|----------|--------|--------|
| specification-draft-0.2 | stable foundation | ~20 pages |
| phase1-v1-scope-draft-0.1 | revised, includes R17 | ~7 pages |
| phase1-schemas-draft-0.4 | revised, includes R18 | ~12 pages |
| phase1-kernel-resolver-contract-draft-0.1 | stable | ~8 pages |
| phase1-session-run-store-contract-draft-0.1 | revised, includes R1-R3, I11 | ~12 pages |
| phase1-event-bus-contract-draft-0.1 | stable | ~7 pages |
| phase1-memory-provider-contract-draft-0.1 | revised, includes R12, R13 | ~12 pages |
| phase1-secrets-sandbox-authz-contract-draft-0.1 | stable | ~10 pages |
| phase1-tools-subsystem-draft-0.1 | revised, includes R13 | ~13 pages |
| phase1-skills-subsystem-draft-0.1 | revised, includes R13 | ~11 pages |
| phase1-error-model-draft-0.1 | revised, includes R14, R15 | ~14 pages |
| phase1-asset-management-contract-draft-0.1 | revised, includes R9-R11 | ~13 pages |
| phase1-file-handling-draft-0.1 | revised, includes R16 | ~8 pages |
| phase1-agent-engine-contract-draft-0.1 | revised, includes R4-R8 | ~10 pages |
| phase1-contract-audit-draft-0.1 | reference, no revisions needed | ~11 pages |
| phase1-audit-revisions-draft-0.1 | reference document | ~10 pages |
| phase1-test-plan-draft-0.1 | stable | ~13 pages |
| phase1-master-index (this doc) | navigation | ~6 pages |

**Total: 18 documents, ~190 pages.**

After all 18 audit revisions land, the spec set is internally
consistent and ready to drive implementation.
