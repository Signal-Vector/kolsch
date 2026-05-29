# Agent Workflow Platform — Specification

**Draft 0.2 — Consolidated Architectural Skeleton**

> Status: architectural level only. Vision, principles, scope boundaries, module
> map, and inter-module contracts. Per-module detailed requirements are a later
> pass. Open questions are stated explicitly rather than papered over.

---

## Part 1 — Vision and Goals

### 1.1 One-sentence vision

A local-first platform for building, running, observing, and sharing
**multi-agent workflows** — supporting the full spectrum from conversational
agent use to fully autonomous, definition-orchestrated processes, where agent
contexts are isolated by default and every layer of configuration is
overridable.

### 1.2 The problem being solved

Developers building multi-agent LLM systems face a fragmented landscape. Each
existing tool solves one slice of the problem and leaves the developer to be
the integrator:

- Code-first agent frameworks (CrewAI, LangGraph) give execution power but no
  observability, testing, asset management, or sharing story.
- Orchestrators (Conductor, Kestra) sequence steps but bring no asset layer,
  no session model, no authoring environment.
- Single-agent tools (pi, OpenClaw) give an excellent one agent but no
  multi-agent orchestration.
- LLM-orchestrated systems (AutoGen-style) are unpredictable and expensive;
  2025-2026 research shows shared-context multi-agent systems degrade
  measurably as agent count and history grow.

No tool spans the whole job. The user assembles assets, secrets, observability,
testing, authoring, and sharing themselves.

### 1.3 Primary goals

1. **One-stop shop.** Assets, agents, workflows, sessions, observability,
   authoring, and sharing as a single integrated, local-first system. The
   platform is the integrator, not the user.
2. **Orchestration maturity spectrum.** Support the full path: talk to an
   agent -> notice a repeatable job -> have an agent author a workflow file
   that captures it -> run that workflow autonomously. The platform assists
   the migration along this spectrum.
3. **Context isolation by default.** Each agent gets only what its task
   requires. Coordination happens through explicit, typed channels, never
   shared transcripts.
4. **Observability as a property, not a feature.** Every run is a navigable
   tree projected from an append-only event log. Nothing is added later to
   "make it observable."
5. **Local-first.** Runs in Docker/Podman on the user's machine as the
   primary deployment. A hosted service is a later concern the architecture
   must not preclude.
6. **Layered overridability.** Every setting, at every level of abstraction,
   can be overridden or templatized through one uniform resolution mechanism.
7. **Swappable execution.** The agent execution engine and the workflow
   execution engine are replaceable behind formal contracts ("bring your own
   agent module").
8. **Shareable.** Workflows, agents, and assets bundle into git-hostable
   packages that are additive to a user's setup.

### 1.4 Explicit non-goals (v1)

The exclusion list matters more than the inclusion list.

- Not a hosted multi-tenant service. Single user, single workspace.
- Not a general data-pipeline orchestrator (not competing with Airflow/Kestra
  on ETL).
- Not a visual drag-and-drop builder in v1. The "no-code" path in v1 is
  *the built-in agent authoring YAML for you*, not a canvas.
- No marketplace/registry in v1. Sharing is git URLs. (Registry is a named
  later phase; the bundle format must not preclude it.)
- No automatic dependency installation. The platform *describes* requirements;
  the user satisfies them.
- No unbounded loops. Every loop declares a hard iteration cap.

### 1.5 The target user (v1)

**OPEN QUESTION — must be resolved before per-module requirements.**
Working hypothesis, to be validated by the desirability test:

> A developer who currently chains LLM/agent calls in scripts and is
> frustrated by three things: they cannot see what happened inside a run,
> they cannot test it against fixed scenarios, and they cannot share it as a
> reusable unit.

Everything in the spec is designed for this person until the test says
otherwise.

### 1.6 Differentiation thesis

> Existing tools each solve one slice — execution, or orchestration, or a
> single agent — and leave the user to assemble assets, secrets,
> observability, testing, authoring, and sharing themselves. This platform is
> the one-stop shop: an integrated, local-first system with a smooth path
> from "talk to an agent" to "ship an autonomous workflow." The user is not
> the integrator; the platform is.

This answers "why not Conductor": Conductor is one slice (orchestration);
this is the whole kitchen, plus the conversational on-ramp Conductor lacks.

---

## Part 2 — Core Concepts and Vocabulary

The spec uses these terms precisely.

| Term | Definition |
|---|---|
| **Agent call** | The atomic unit. One execution of one configured agent against inputs. Everything is a way to configure, compose, or sequence agent calls. |
| **Asset** | A configurable raw material: a memory binding, knowledge, a tool, a skill, a prompt fragment, a model config, a secret binding. Tagged hierarchically. An *input to* agent calls. |
| **Agent** | A named, saved *template*: a selection of assets plus configuration. Not a running thing. |
| **Session** | A live or historical *instance* of agent activity. The user-facing conversation container. Owns isolated threads. Permanently a session — never becomes a workflow. |
| **Thread** | One agent's isolated context line within a session. Threads never share token context. |
| **Channel** | An organizational bucket of sessions around a topic. An entry point, not an execution concept. |
| **Workflow** | A YAML-defined DAG: a *template* sequencing agent calls over a longer horizon. |
| **Workflow run** | A live or historical *instance* of a workflow. |
| **Node** | One step in a workflow. Internally a code function, an agent, or a sub-workflow. Externally a typed `inputs -> outputs` contract. |
| **Bundle** | A git-hostable package of workflows, agents, and assets. Additive to a setup. |
| **Resolved config** | The effective configuration of an agent call after merging the override chain. |
| **Event log** | The append-only historical record of what happened inside runs/sessions. Ground truth. File-resident. |
| **Lifecycle hooks** | Intra-module, synchronous extension points within one execution (e.g. `tool_call`). |
| **Event Bus** | Inter-module, asynchronous, reactive delivery so modules react to each other's changes. Distinct from the event log and from lifecycle hooks. |

Two template/instance pairs are the system's spine: **Agent -> Session** and
**Workflow -> Workflow-run**. An entity never changes type; a workflow may
carry an optional "authored from session X" backreference and nothing stronger.

---

## Part 3 — Architectural Principles

Invariants. Every later decision must conform.

1. **One config schema.** A single central schema — the agent-call config.
   Every configuration artifact is a *partial instance* of it.
2. **Resolution by layered merge.** Effective config is computed by
   deep-merging an ordered override stack: system defaults -> agent definition
   -> workflow definition -> workflow instantiation -> call context. Inner
   overrides outer. Override and templatization are the *same* mechanism (a
   template is an override with a required-parameter hole).
3. **Isolation by default, coordination by explicit channel.** Threads share
   nothing. The only inter-node channels are *typed edge data* and *bounded,
   declared session_state*.
4. **Typed edges are validation gates.** A node output that does not conform
   to its declared schema fails the run *at that edge*. Error boundaries are
   active, not descriptive.
5. **Append-only event log is ground truth.** All run/session state is a
   projection over an immutable event log. Observability is views over it.
6. **Dependencies point down.** The module dependency graph is an acyclic
   DAG. No module imports a peer's internals or anything above it.
7. **Orchestration matures along a spectrum.** Conversational/LLM-driven
   orchestration is the on-ramp; explicit definition-orchestration is the
   mature destination. The transition is achieved by an *authoring act* (an
   agent writes a workflow file), never by an entity changing type.
8. **Mechanism core, policy pluggable.** Engines provide mechanisms; policies
   (which engine, which provider, which algorithm) are swappable.
9. **Configuration is multi-file, additive, and layered.** A setup is the
   additive union of the user's own config files and all installed packages
   (availability). A package contributes; it never silently overwrites.
   Separately, any single agent call's effective config is computed by
   layered merge (resolution). Availability-union and resolution-merge are
   distinct operations.
10. **Unsettled domains become Provider Modules.** Wherever the field has no
    settled answer (memory, HITL channels, secret backends, sandboxing), the
    core depends only on a contract; implementations live behind it and are
    swappable, possibly several at once.

---

## Part 4 — The Module Architecture

### 4.1 Module categories

- **Kernel** — pure libraries, no I/O, imported by everything.
- **Engine Modules** — exactly one active, swapped wholesale, formal contract.
- **Provider Modules** — a contract plus a registry of one-or-many
  implementations, selected by policy (v1: no policy, see 4.5).
- **Subsystems** — bounded API, single implementation.
- **Data** — configurations expressed in the above, not code.

### 4.2 Kernel

- **Config Schema** — the central agent-call config definition; formal,
  versioned, machine-readable (JSON Schema export).
- **Config Resolver** — the pure layered-merge function. Override +
  templatization. No state, no I/O.

### 4.3 Engine Modules

- **Agent Engine** — executes one resolved agent call. v1 implementation
  wraps CrewAI, fully encapsulated. Contract:
  `execute(resolved_config, inputs, context) -> (result, event_stream)`
  plus `capabilities() -> CapabilitySet`.
- **Workflow Engine** — sequences agent calls into a DAG run. Owns
  topological execution, fan-out/fan-in, conditionals, the one bounded-loop
  primitive, checkpointing, durable execution, resume. Calls the Agent
  Engine through its contract. v1 implementation is a custom durable runner
  on Postgres; Temporal is a noted later swap behind the same contract.

### 4.4 Provider Modules

Each is a contract + a registry. v1 ships exactly one provider per module.

- **Memory** — write/retrieve over scoped memory. v1 provider: a crude
  recent-buffer with truncation (the compaction floor). Later providers:
  semantic, hybrid, extraction-pipeline. All storage choices (incl. any
  vector store) are internal to this module and invisible to the core.
- **Human-in-the-Loop** — `request_decision(context) -> decision`. v1
  provider: CLI/prompt. Later: web approval, Slack, webhook.
- **Secrets** — `resolve(secret_name) -> value`. v1 provider: `.env`. Later:
  OS env, vault, cloud secret managers; resolved as an ordered chain.
- **Sandbox** — `execute_in(policy, work) -> result`. v1 provider: native
  execution. Later: Docker, strict container, deny.
- **Authorization** — permission checks. v1 provider: allow-all (single
  admin user). Real implementation deferred to multi-tenant.

### 4.5 Provider selection policy

v1 has **no selection policy**. Degenerate-case default rule:

- Exactly one provider registered -> use it.
- Zero registered -> the feature is unavailable.
- More than one -> error until a policy mechanism exists.

A selection-policy mechanism is a later, non-breaking addition (it inserts a
resolution step before the registry lookup; "no policy" is the degenerate
case of that step).

### 4.6 Subsystems

- **Asset Management** — storage, hierarchical tagging (prefix-matching
  selectors + explicit allow/deny override), versioning, secret-binding
  references, retrieval. Depends only on the kernel. Knows nothing of agents
  or workflows.
- **Session/Run Store** — sessions, threads, channels, the append-only event
  log, run state, the session tree, retention. The instance-side hub.
- **Event Bus** — inter-module asynchronous reactive delivery. Two-tiered
  (see Part 6). Sits low, beside the kernel.
- **Platform Tools Adapter** — exposes the other modules' APIs as
  agent-callable, schema-typed tools, with a read/act permission split. This
  is what makes the built-in agent an ordinary agent.
- **IDE / Authoring** — the React app and authoring experience. Consumes
  module APIs only; contains no business logic. Nothing depends on it.

### 4.7 Data, not modules

- **The built-in / navigator agent** — an ordinary agent definition whose
  tools are the Platform Tools Adapter. Carries the signature
  `author_workflow` capability (see 4.9). Its existence is the architecture's
  dogfooding test.
- **Node presets** (`task_node`, `conversational_node`, `coordinated_node`,
  `assistant_node`), **workflow templates**, **standard tool/best-practice
  defaults** — configurations in the schema.

### 4.8 Not in the module map

- **Queue/worker infrastructure** (Arq + Redis) — deployment substrate the
  Workflow Engine and Event Bus dispatcher run on.
- **Observability** — a property of the event log, surfaced as IDE views.
- **Prompt-injection defense / trust posture** — a non-modular discipline
  that the Agent Engine and Platform Tools Adapter must uphold; it cannot be
  swapped, so it is not a module.

### 4.9 The `author_workflow` capability

A tool available to agents that takes the agent's current context (its
thread, the session's events, the user's stated intent) and produces a
workflow YAML file as a new, separate artifact. The session that hosted the
authoring is unaffected. This single capability is *both* the
conversation->autonomy on-ramp and the v1 no-code authoring path. It is not
platform-privileged: any agent can be given it; the built-in navigator agent
has it by default. The produced file is validated by the same Kernel
validation pipeline as any hand-written workflow.

### 4.10 The dependency graph

```
              Kernel  +  Event Bus
                   |  (depended on by all; depend only on infra)
        +----------+-----------+
        |          |           |
   Asset Mgmt   Session/Run Store
        |          |
        +----+-----+
             |
       Agent Engine        (+ Provider Modules: Memory, HITL,
             |               Secrets, Sandbox, Authorization)
       Workflow Engine
             |
   Platform Tools Adapter
             |
            IDE
```

Arrows are "depends on." Provider Modules are depended on by the engines and
the Agent Engine through their contracts. The enforced rule: dependencies
point down only; cross-module calls go through public APIs; modules
communicate upward/sideways only via the Event Bus.

### 4.11 Build order

The dependency graph is the build order:
**Kernel + Event Bus -> (Asset Mgmt, Session/Run Store) -> Provider Modules
(stub implementations) -> Agent Engine -> Workflow Engine -> Platform Tools
Adapter -> IDE.** Each layer has a stable, tested foundation before the next.

---

## Part 5 — Storage Architecture

Storage is chosen per data type by two axes: **concurrency profile** (who
writes, how often) and **query pattern** (how it is read).

| Data | Profile | Store |
|---|---|---|
| Live run state, queue state, session_state, scheduler state | concurrently mutated | **Postgres** |
| Index rows (run status, cost, duration, timestamps) | structured, filter/group/aggregate | **Postgres** |
| Durable inter-module events (outbox) | append-only, transactional | **Postgres** |
| Event log, traces, agent messages, compaction summaries | append-only long-form | **Files (JSONL)** |
| Run snapshots, archived runs | immutable | **Files** |

Two stores only: **Postgres** and **files**. No SQLite in the core. Memory's
storage (including any vector store) is internal to the Memory Provider
Module and not a core concern.

Rationale: Postgres for anything concurrently mutated (the "always
durable/concurrent" decision requires real concurrency control) and for
structured index rows (filter/group/aggregate is the relational query
pattern). Files for append-only, immutable, sequentially-read data
(precedented by pi and OpenClaw session logs; snapshot-friendly; avoids
unbounded table growth). The event log is ground truth; Postgres index rows
are a projection for fast cross-cutting queries.

Consequence: a full installation's data lives in two forms (Postgres + a file
tree). "Backup, restore, and relocate the installation" is therefore an
explicit cross-cutting requirement (Part 6).

---

## Part 6 — The Event Bus

### 6.1 Purpose

The strict downward-dependency rule forbids a module from calling upward or
sideways. The Event Bus is the mechanism that lets a module announce a change
so other modules can *react*, without the publisher depending on the
subscribers. It is what makes the layering survivable.

"Event" now means three distinct things, kept strictly separate:

- **Event log** — historical record, file-resident, read later.
- **Lifecycle hooks** — intra-module synchronous extension points.
- **Event Bus** — inter-module asynchronous reactive delivery.

A single occurrence (e.g. a run fails) produces both a log entry (the record)
and a bus message (the notification).

### 6.2 Two tiers

- **Durable tier** — for events where loss is a correctness bug (a
  self-scheduled trigger fired, a cost cap was crossed, a workflow completed
  and a downstream one should start). Implemented as a **transactional
  outbox**: the event is written to a Postgres event table *in the same
  transaction* as the change that produced it (atomic — both commit or
  neither). A dispatcher reads unprocessed rows, delivers to subscribers,
  marks delivered. Survives shutdown (rows persist; replayed on restart).
  Retries with backoff; after N failures an event goes to a dead-letter
  table.
- **Ephemeral tier** — for events where loss is harmless (UI progress hints).
  Redis pub/sub, fire-and-forget.

The publisher declares the tier.

### 6.3 Contract

Deliberately small:

- `publish(event_type, payload, tier)` — durable publish must be callable
  within an existing DB transaction.
- `subscribe(event_type_pattern, handler)` — register a reactive handler.

Replay, retry, backoff, dead-lettering are internal to the dispatcher, not
API surface.

### 6.4 Required subscriber disciplines

- **Idempotency.** The bus retries and replays; a subscriber may receive the
  same event more than once. Every handler must be idempotent — processing
  twice equals processing once. Events carry unique IDs to enable dedup.
- **Failure isolation.** One subscriber's failing handler must not break
  delivery to others and must not lose the event. Failure is tracked
  per-(event, subscriber).

### 6.5 Guardrail — the bus is for reactions, not orchestration

Orchestration — the deliberate sequencing of steps — is the Workflow
Engine's job and is explicit and definition-driven. The bus must never become
a hidden orchestrator where process logic hides in a chain of event handlers.
If something is a *process*, it is a workflow. If something is a *reaction*
(refresh a cache, send a notification, update a view), it is a bus
subscriber. Durable events are inspectable (they are Postgres rows), so bus
activity itself is debuggable.

---

## Part 7 — Cross-Cutting Concerns

Named here; specified in a later pass. Listed so they are not lost.

1. **Cost** — estimation before a run; caps (run/session/channel);
   attribution in the event log; behavior on mid-run cap breach.
2. **Non-determinism** — what "a passing test" means; debugging
   unreproducible runs; reviewer-grading inconsistency.
3. **Error and partial failure** — resume from checkpoint; partial-node
   state; the recovery UX.
4. **Human-in-the-loop** — handled as a Provider Module (4.4); the
   suspend/approve/resume mechanism, notification, timeout still need
   specification.
5. **Security** — downloaded bundles run arbitrary code; the trust posture;
   the system-agent prompt-injection boundary. Sandboxing and Authorization
   are Provider Modules; injection defense is a non-modular discipline.
6. **Secrets** — handled as a Provider Module; v1 is `.env`; the binding from
   a node's needed secret to a value needs specification.
7. **Schema migration** — the central config schema will change; suspended
   runs hold snapshots of older schema versions; a runner must execute an
   older-schema snapshot.
8. **Data lifecycle** — sessions and event logs grow unbounded; retention and
   archival.
9. **Platform versioning** — bundles target a platform version; compatibility
   as the platform evolves.
10. **Backup / restore / relocate** — installation data spans Postgres + a
    file tree; moving or backing up an installation needs a deliberate
    (small) tool.
11. **Cross-package name collisions** — the additive union of packages can
    produce duplicate names; namespacing (`package-name/agent-name`) is the
    expected resolution; needs specification.

---

## Part 8 — Open Questions Blocking the Next Pass

1. **The target user** (1.5) — a hypothesis; needs the desirability test or a
   committed decision.
2. **The v1 scope cut** — the design spans 6-8 products. An explicit v1
   inclusion list AND exclusion list is required. The built-in agent and its
   `author_workflow` capability are now load-bearing for the core value
   proposition, which constrains how aggressively the agent layer can be cut.
3. **The central config schema's actual shape** — everything merges onto it;
   it must be drafted formally; this is itself a multi-pass effort.
4. **Event Bus dispatcher topology** — shares the Workflow Engine's Arq
   worker pool, or a separate process. Leaning shared for local-first v1.
5. **Sub-workflow session inheritance** — confirmed earlier as `inherit` by
   default, `isolated` opt-in; needs to be carried into the schema.

---

## Part 9 — Iteration Plan

- **Pass 1** — architectural skeleton. *Done (draft 0.1).*
- **Pass 2 (this)** — consolidation of findings. *This document, draft 0.2.*
- **Pass 3** — resolve Part 8 open questions; draft the central config schema
  formally. Highest-leverage pass.
- **Pass 4** — per-module detailed requirements, in build order.
- **Pass 5** — cross-cutting concerns, item by item.
- **Pass 6** — the MVP/test plan as a spec section (the four-test sequence).
- **Pass 7+** — refinement; resolve contradictions surfaced by lower passes.

Lower passes will send us back up. That is expected.
