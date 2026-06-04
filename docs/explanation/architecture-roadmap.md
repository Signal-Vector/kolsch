# Phase Architecture Roadmap

**Draft 0.1**

> High-level architecture per phase, with migration paths between
> phases. Phase 1 and 2 in detail; Phase 3 sketched. The goal is
> pragmatic modularity — choose real implementations, but design the
> seams so Phase 2 plugs in without rewriting Phase 1.

---

## Part 1 — Phase 1 Architecture (Single-Agent Foundation)

### 1.1 What Phase 1 Is

A local-first platform for running one configured agent. The user
writes an agent definition (YAML), runs it via CLI or HTTP API,
watches the agent work through tool use, gets a result. The platform
captures everything in an event log so the session is replayable,
inspectable, resumable.

**No workflows, no IDE, no multi-agent composition.** Just: define an
agent, run it, see what it did.

### 1.2 The Module Landscape

```
                  ┌────────────────────┐
                  │  CLI    /    API   │   ← entry surfaces
                  └─────────┬──────────┘
                            ▼
                  ┌────────────────────┐
                  │   Agent Engine     │   ← orchestrates execution
                  │  (wraps CrewAI)    │
                  └─────────┬──────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  ┌──────────┐       ┌──────────┐        ┌──────────┐
  │  Tools   │       │  Skills  │        │  Memory  │
  │subsystem │       │subsystem │        │ provider │
  └────┬─────┘       └────┬─────┘        └────┬─────┘
       │                  │                   │
       └──────────────────┼───────────────────┘
                          ▼
                  ┌────────────────┐
                  │ Provider layer │
                  │  Secrets, Sandbox,
                  │  Authorization │
                  └────────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  ┌──────────┐      ┌──────────┐       ┌──────────┐
  │   Asset  │      │ Session/ │       │  Event   │
  │   Mgmt   │      │ Run Store│       │   Bus    │
  └────┬─────┘      └────┬─────┘       └──────────┘
       │                 │
       ▼                 ▼
  ┌──────────┐      ┌──────────┐
  │filesystem│      │ Postgres │
  │ bundles, │      │  +  JSONL│
  │  data,   │      │  events  │
  │ content  │      │          │
  └──────────┘      └──────────┘
                          │
                          ▼
                    ┌──────────┐
                    │  Kernel  │
                    │ Resolver │   (pure lib, no I/O)
                    └──────────┘
```

Eleven modules, organized by category:

**Entry surfaces (2):** CLI, FastAPI.

**Engine module (1):** Agent Engine, wraps CrewAI.

**Subsystems (5):** Asset Management, Session/Run Store, Event Bus,
Tools, Skills.

**Provider modules (4):** Memory, Secrets, Sandbox, Authorization.

**Kernel (1):** Resolver (pure config validation, no I/O).

Plus the Error Model and File Handling specs as cross-cutting
concerns.

### 1.3 Data Flow for a Single `execute` Call

The flow when a user runs `kolsch run my-agent --input "do this"`:

1. **CLI parses** the command and constructs an `ExecutionContext`.
2. **CLI calls** the Agent Engine's `execute` method.
3. **Engine pre-flight:**
   - Asks Asset Management for the resolved agent config (which
     internally builds layers, calls Kernel resolver).
   - Authorization checks `agent.execute.<agent_id>` against the
     principal (v1: `user.local`).
   - Opens the session's JSONL file via Session/Run Store, computes
     the leaf.
   - Binds memory context (Memory replays `memory.written`/`memory.deleted`
     events from the log to rebuild its cache).
   - Emits `thread.exchange_started`, materializes user input as
     `user.message`.
4. **Engine setup:**
   - Runs Assembly Procedure on the event path to build model context.
   - Appends skills listing to system prompt.
   - Registers tools with CrewAI through an adapter (tool dispatch
     routes through Authorization → Secrets → Sandbox → tool function).
   - Resolves secrets for tool registration.
5. **Hand to CrewAI:** the engine invokes CrewAI's agent loop. CrewAI
   calls the model, dispatches tool calls, retries failures.
6. **Engine observes** via lifecycle hooks: each model.called →
   `model.request`/`model.response` events; each tool.dispatched →
   `tool.called`/`tool.result`/`tool.failed` events. Each emission:
   - Goes through Session/Run Store's `append_event` (lock-protected,
     atomic).
   - Then `bus.publish(event)` to the ephemeral Event Bus (best-
     effort, doesn't block).
   - And, in streaming mode, yields from `execute_stream`.
7. **Finalize:** when CrewAI returns the final answer, emit
   `agent.message` then `thread.exchange_completed`. Sync `execute`
   returns the agent.message; streaming `execute_stream` ends.

### 1.4 The Filesystem at Runtime

```
<platform-root>/                        ← user's chosen CWD
├── platform.yaml                       ← loaded by Asset Mgmt
├── .env                                ← read by Secrets
├── bundles/                            ← scanned by Asset Mgmt at startup
│   ├── my-bundle/
│   └── another-bundle/
├── sessions/                           ← Session/Run Store writes here
│   ├── ses_abc.jsonl
│   └── ses_abc.jsonl.lock
├── content/                            ← engine writes oversized payloads
│   └── <hash>.bin
├── data/                               ← user's working files
├── logs/                               ← platform diagnostic logs
└── .platform/                          ← caches, indexes
```

Plus a separate Redis instance (for Event Bus) and Postgres instance
(for entity records — the filesystem JSONL is ground truth; Postgres
is cache + UI state).

### 1.5 Deployment Shape

**Single Python process.** The CLI imports the platform code and runs
in-process. The FastAPI surface, when started, is the same process
exposing HTTP endpoints — same code, different entry. No daemon, no
runner subprocess.

**The default tier requires no external services** — SQLite for entity
records and an in-process event bus. The only hard dependency is an LLM
provider. Optional scale-up backends (adopted only when needed):
- Redis (Event Bus backend, for cross-process fan-out — Tier 2+).
- Postgres (entity backend, for volume/concurrency at scale — Tier 3).
- An LLM provider (Anthropic, OpenAI, etc.) — always required.

The user starts the platform with `kolsch server` (FastAPI) or
just runs one-off `kolsch run my-agent`. Both work.

### 1.6 What's Pragmatic vs. What's Disciplined

**Pragmatic choices** (real implementations, not abstractions):
- CrewAI as the agent loop. Not a "framework-agnostic" abstraction layer;
  the engine specifically wraps CrewAI and uses its lifecycle hooks.
- SQLite + JSONL files by default. Not an over-abstracted persistence
  layer — but the entity store IS backend-pluggable (SQLite default,
  Postgres at scale) because that modularity costs nothing (the contract
  was already backend-agnostic) and buys a zero-dependency default
  install. JSONL on the filesystem is ground truth regardless.
- In-process event bus by default; Redis as the cross-process scale-up
  backend. Not "abstract over every broker someday" — just two concrete
  backends behind one contract.
- Single-process. Not "microservices ready"; one Python process.

**Disciplined choices** (modularity preserved for Phase 2):
- The Agent Engine is behind a contract (`execute_stream`, `execute`).
  Swapping CrewAI for something else means a new engine implementation
  conforming to the same contract.
- The Memory, Secrets, Sandbox, Authorization providers have variant
  selection in config. The v1 variants are concrete (`recent_buffer`,
  `dotenv`, `native`, `rule_based`), but the contracts are written for
  N variants.
- The Session/Run Store has a contract that doesn't reference Postgres
  or JSONL directly; the implementation does, but consumers see the
  contract.
- Tools and Skills are subsystems with explicit contracts. New tools
  arrive via the `@tool` decorator; new skills arrive via SKILL.md
  files in bundles. No code changes to the platform.

The pragmatic/disciplined boundary matters because Phase 2 will swap
or extend each pragmatic choice. Designing the disciplined contracts
in Phase 1 means Phase 2 doesn't rewrite the platform.

### 1.7 What's Genuinely Risky in Phase 1

Three risks worth flagging:

**CrewAI feasibility.** The Materialization Rule (every piece of model
context must be captured as a verbatim event) is honest only if
CrewAI's hooks expose the actual model input. If hooks are insufficient,
the rule becomes aspirational. This is the gating test (Test 1 in the
test plan).

**Memory cache reconstruction.** Rebuilding the memory cache from log
replay on every session resume is fine for small sessions; under heavy
memory write traffic it could become slow. v1 accepts the simplicity;
performance is a watch item.

**Concurrent callbacks.** CrewAI's async lifecycle hooks may fire
concurrently. The engine's adapter must serialize event construction
per thread (per the audit revision R8). This is engine-internal
correctness work that's easy to get wrong and hard to test
exhaustively.

---

## Part 2 — Phase 1 → Phase 2 Seams

What Phase 1 already prepared for, so Phase 2 plugs in without rewrites.

### 2.1 Seams That Exist by Design

**Seam 1 — The Engine Contract.** The Agent Engine's `execute_stream`
and `execute` methods are the abstraction. CrewAI lives behind them.
Phase 2 may add a Workflow Engine that *also* implements this contract
(or a related one), or wraps the Agent Engine to orchestrate multi-
agent compositions. The Agent Engine doesn't change.

**Seam 2 — Subsystem variants.** Memory, Secrets, Sandbox,
Authorization each support a registry of variants. v1 ships one variant
per slot. Phase 2 adds variants: `semantic_memory`, `docker_sandbox`,
`policy_based_authz`. No core changes — just register a new variant
class with the existing factory mechanism.

**Seam 3 — Tools and Skills are content-driven.** New tools arrive as
`@tool`-decorated Python files in bundles; new skills as SKILL.md
directories. The platform's catalog discovers them automatically. Phase
2 just means more bundles get installed.

**Seam 4 — The IOSpec contract on agent inputs/outputs.** Phase 1 has
typed `IOSpec` on every agent. Phase 2's workflow engine composes
agents by matching the output type of one agent to the input type of
the next. The mechanism (typed declaration) already exists; the
composer is new.

**Seam 5 — The Event Bus is wired but has no internal subscribers.**
v1 publishes events; nobody subscribes internally. Phase 2's IDE
subscribes to the bus for real-time updates. The publish side is
unchanged.

**Seam 6 — The Asset Catalog supports new asset types.** v1 has tools,
skills, agents, prompts. A `workflows` directory is straightforward to
add — Phase 2 just defines the asset type in Asset Management and the
catalog discovers `bundles/<bundle>/workflows/<name>.yaml` files.

**Seam 7 — The event vocabulary is open-ended.** New event types
(`workflow.started`, `workflow.completed`, `node.dispatched`) plug into
the existing schema. The materialization rule and event tree mechanics
don't change.

### 2.2 What Changes Shape Between Phases

These items don't fit cleanly into existing seams; Phase 2 will adapt
them:

**Multi-thread sessions.** Phase 1 has one thread per session. Phase 2
needs multi-thread to support workflow execution (a workflow may
dispatch parallel agent calls). The Thread entity exists in v1; the
event tree mechanics support multi-thread; but the engine's pre-flight
assumes one thread per session. Real multi-thread support is small but
real engine work.

**Cross-session memory.** Phase 1 memory is session-scoped. Phase 2
likely wants workflow-scoped or user-scoped memory (a workflow's first
agent writes a memo; the third agent retrieves it). Either a new
memory variant (`shared_memory`) or extending the existing one.

**The MemoryContext lifetime.** Phase 1 binds it per session. Phase 2's
workflow engine may bind a context that spans multiple agent
executions within one workflow.

**Single-process assumption.** Phase 1 is one process. Phase 3 splits
server/runner. Phase 2 may already start exposing the split — the IDE
talks to a server that may spawn runners. The Phase 1 → 3 seam (Part 4
below) covers this; Phase 2 may begin the transition.

### 2.3 Migration Cost Estimate

A rough estimate of how much Phase 1 code survives unchanged:

- **Kernel/Resolver, Schemas, Error Model:** unchanged. These are
  foundation; the rules don't change.
- **Asset Management, Session/Run Store, Event Bus:** unchanged
  in API; possibly internal updates for new asset types or new event
  types.
- **Memory, Secrets, Sandbox, Authorization providers:** unchanged
  contracts; possibly new variants added.
- **Tools, Skills subsystems:** unchanged. New tools and skills arrive
  as bundle content.
- **Agent Engine:** mostly unchanged; possible adjustments for multi-
  thread support and workflow-engine integration.
- **CLI and API:** extended with new commands/endpoints for workflows
  and IDE-server communication.

The estimate is that **Phase 2 is mostly additive** — new modules slot
into existing seams, existing modules grow new variants. The
engine and CLI need real but bounded changes. This is what "modular"
should mean.

---

## Part 3 — Phase 2 Architecture (Multi-Agent + IDE)

### 3.1 What Phase 2 Adds

Three big new things:

1. **Workflow Engine** — agents composed into multi-step flows. Outputs
   of one agent become inputs of the next.
2. **IDE** — a visual interface for building agents, designing
   workflows, watching sessions live, inspecting event logs.
3. **Additional Provider Module variants** — at minimum, real sandbox
   (Docker-based) and richer memory (semantic retrieval).

Plus secondary things:
- Multi-thread sessions for parallel workflow nodes.
- Cross-session/workflow memory scoping.
- Workflow asset type in bundles.
- HITL (human-in-the-loop) Provider Module for approval gates.

### 3.2 The Module Landscape

```
                ┌─────────────────────────┐
                │          IDE            │   ← visual interface
                │   (browser-based UI)    │
                └────────────┬────────────┘
                             │ HTTP + SSE/WS
                             ▼
                ┌─────────────────────────┐
                │   CLI    /    API       │
                └────────────┬────────────┘
                             │
                ┌────────────┴────────────┐
                ▼                         ▼
       ┌──────────────────┐      ┌──────────────────┐
       │   Agent Engine   │      │ Workflow Engine  │   NEW
       │  (wraps CrewAI)  │      │  (orchestrator)  │
       └────────┬─────────┘      └────────┬─────────┘
                │                         │
                │  ┌──────────────────────┘
                ▼  ▼
       ┌─────────────────────────────────────────────┐
       │  Subsystems (unchanged)                     │
       │  Tools, Skills, Asset Mgmt, Session/Run     │
       │  Store, Event Bus                           │
       └─────────────────────────────────────────────┘
                             ▼
       ┌─────────────────────────────────────────────┐
       │  Provider Modules (expanded variants)       │
       │  Memory: recent_buffer + semantic           │
       │  Sandbox: native + docker                   │
       │  Authorization: rule_based + policy_based   │
       │  Secrets: dotenv + os_env + vault           │
       │  HITL: (new)                                │
       └─────────────────────────────────────────────┘
                             ▼
                ┌──────────────────────┐
                │       Kernel         │   (unchanged)
                └──────────────────────┘
```

New modules in red bold: Workflow Engine, IDE, HITL provider.

New variants: semantic memory, docker sandbox, policy authorization,
vault secrets.

Everything else: same as Phase 1.

### 3.3 The Workflow Engine

**What it is.** A new top-level module that orchestrates multi-step
agent flows. A workflow is a declared YAML asset (in `bundles/<bundle>/
workflows/<name>.yaml`) specifying:

- Nodes (each invokes an agent or another workflow).
- Edges (data flow — output of node A as input of node B).
- Optional branching, retries, parallel execution.

**Where it sits.** Above the Agent Engine. The Workflow Engine
dispatches Agent Engine `execute` calls for each node. It does NOT
replace the Agent Engine; it composes with it.

**Contract sketch.**

```python
class WorkflowEngine:
    async def execute(
        self,
        workflow_config: ResolvedWorkflowConfig,
        context: WorkflowContext,
    ) -> WorkflowResult: ...
    
    async def execute_stream(
        self,
        workflow_config: ResolvedWorkflowConfig,
        context: WorkflowContext,
    ) -> AsyncIterator[Event]: ...
```

Same shape as Agent Engine. Same "engine as recorder" discipline. New
event types: `workflow.started`, `node.dispatched`, `node.completed`,
`workflow.completed`.

**Multi-thread semantics.** A workflow with parallel nodes spawns
multiple threads under the session. The session is the workflow run;
each thread is one node's execution history. The Phase 1 thread
mechanics support this; the Phase 2 engine actually uses it.

**Asset management.** A new asset type `workflow` joins
`tools/agents/prompts`. Discovered the same way.

**Composition mechanism.** Workflow nodes declare which agent (or sub-
workflow) they invoke. The IOSpec of each node's agent must align with
the edges — output type of node A matches input type of node B. The
Workflow Engine validates this at workflow-config resolution time.

### 3.4 The IDE

**What it is.** A browser-based UI for the platform. Three primary
views:

1. **Agent designer.** YAML editor with schema-aware autocomplete,
   live validation, prompt preview. Could also offer a non-YAML graph
   editor for users who prefer click-and-drag.
2. **Workflow designer.** Visual graph editor for workflow YAML. Nodes
   are agents; edges are data flow. Inputs/outputs typed from IOSpec.
3. **Session inspector.** Live view of running sessions. Event log as
   timeline. Tool calls expandable. Replay any event tree position.

**Where it sits.** Above the API surface. The IDE is a separate web
application that talks to the platform's HTTP/SSE/WebSocket endpoints.

**Why this works without restructuring Phase 1.** The IDE consumes
existing surfaces:
- Asset catalog via API (for listing agents, skills, etc.).
- Session list and events via API (for the inspector).
- Event Bus via SSE/WebSocket bridge (for live updates).

No Phase 1 contract changes. The API surface grows new endpoints; the
modules behind them don't change.

**The Event Bus finally has a subscriber.** Phase 1 publishes events
to Redis with no internal consumers. Phase 2's IDE subscribes through
a server-side bridge that translates Redis pub/sub to SSE/WebSocket
for the browser.

### 3.5 Additional Provider Variants

**Memory: `semantic`** — vector-store-backed memory with similarity
retrieval. New variant slots into the Memory Provider's registry. New
fields in the variant's config (embedding model, vector store backend).
Existing `recent_buffer` variant unchanged.

**Sandbox: `docker`** — tool execution in Docker containers per call.
Real filesystem isolation (path policy enforced; `bash` exception
disappears). Network isolation possible. Variant config specifies
resource limits, allowed mounts.

**Authorization: `policy_based`** — declarative policies beyond simple
regex rules. Possibly OPA/Cedar-based. The existing `rule_based`
variant remains for simple cases.

**Secrets: `vault`, `os_env`** — pull from Vault, AWS Secrets Manager,
1Password, or OS environment. Each is a variant.

**HITL** — new Provider Module entirely. A workflow node can declare
"requires human approval"; HITL provider's variant determines how to
ask (CLI prompt, IDE notification, email, Slack).

### 3.6 Phase 2 Deployment Shape

Likely still single-process for personal use; the IDE is a separate
process (a browser tab), but it talks HTTP to the platform server.

For team use: the platform server runs on a shared machine; multiple
users connect through their own IDE instances. Authorization becomes
non-trivial (multiple principals; real `user.<id>` identities).

Phase 2 doesn't require the server/runner separation — single-process
with concurrent workflow execution is fine for v2 scale. The separation
is a Phase 3 concern, but the seams should already exist.

### 3.7 Phase 2 Module Catalog

| Module | Phase 1 | Phase 2 | Change |
|--------|---------|---------|--------|
| Kernel/Resolver | ✓ | ✓ | None |
| Schemas | ✓ | ✓ | New event types (workflow.*) |
| Asset Management | ✓ | ✓ | New asset type (workflow) |
| Session/Run Store | ✓ | ✓ | None |
| Event Bus | ✓ | ✓ | Browser bridge added |
| Memory Provider | recent_buffer | + semantic | New variant |
| Secrets Provider | dotenv | + vault, os_env | New variants |
| Sandbox Provider | native | + docker | New variant |
| Authorization | rule_based | + policy_based | New variant |
| HITL Provider | — | new | New provider module |
| Tools Subsystem | ✓ | ✓ | None |
| Skills Subsystem | ✓ | ✓ | None |
| Agent Engine | ✓ | ✓ | Multi-thread support |
| Workflow Engine | — | new | New top-level module |
| IDE | — | new | New surface |
| Error Model | ✓ | ✓ | New error types |
| File Handling | ✓ | ✓ | Updated for docker sandbox |

Most rows have "None" or "New variant" — the architecture is
additive. The risk rows are Workflow Engine and IDE (genuinely new),
and the Agent Engine's multi-thread support (revision, not addition).

---

## Part 4 — Phase 2 → Phase 3 Seams

Phase 3 is hosted/production deployment. The seams that matter:

**Seam 8 — Server/runner separation.** Phase 1-2 is one process. Phase
3 splits into:
- **Server** — long-running, holds state, exposes API/SSE/WebSocket.
- **Runner** — short-lived, executes one agent call, exits.

The CLI either runs standalone (no server) or as a server client. The
IDE always talks to a server.

What needs to exist for this seam to work cleanly:
- The Agent Engine's contract is process-agnostic (it doesn't care if
  the caller is in-process or via API). ✓ already true.
- The Session/Run Store works with a runner having a file lock on a
  session log while the server reads from the same log. ✓ already
  true (Session/Run Store §4 cross-process flock).
- The Event Bus is the runner → server notification channel. The
  runner publishes events; the server bridges to IDE subscribers. ✓
  already true.
- Asset Management catalog is shared. The runner asks the server for
  resolved configs and asset paths rather than re-scanning. **NOT yet
  true** — Phase 1 has each process re-scan. Phase 3 needs a "fetch
  catalog from server" API on Asset Management.

**Seam 9 — Multi-tenancy.** Phase 3 supports multiple users on shared
hosting. Real `user.<id>` principals; per-user data isolation;
authorization rules per user/group.

What needs to exist:
- Authorization is variant-pluggable. ✓ — Phase 2 adds `policy_based`.
- Principal derivation is explicit at every check site. ✓ — per
  Engine §1.8.
- Filesystem layout supports multi-tenant separation. **Partially**:
  Phase 1's "platform root = CWD" is per-installation; Phase 3 needs
  per-user roots within a single installation. Asset Management's
  path-handling needs revision.

**Seam 10 — Hosted execution.** Agents run on managed infrastructure;
users don't install the platform locally.

What needs to exist:
- All Provider Modules have non-local variants. Phase 2 starts this
  (vault secrets, docker sandbox). Phase 3 completes it.
- Sessions are not on the user's filesystem. Either the server's
  filesystem (per-user-namespaced) or an object store. The Session/Run
  Store contract is shape-compatible; the implementation backend
  changes.
- Bundle distribution becomes managed. Phase 3 may include a bundle
  marketplace (open item from v1 scope).

**Seam 11 — Observability and ops.** Phase 3 needs metrics, traces,
alerting at production scale. Phase 1-2 has logs; Phase 3 adds
prometheus metrics, OpenTelemetry tracing, etc.

What needs to exist:
- Event types are stable; metrics can be derived from event stream
  patterns. ✓.
- The Event Bus can fan out to multiple consumers including metrics
  collectors. ✓ already in design.

### 4.1 Phase 2 Design Decisions That Make Phase 3 Possible

Three things Phase 2 should be careful about, even if not implementing
fully:

1. **API stability.** Phase 2's API surface (HTTP endpoints, event
   types) should be designed knowing Phase 3 uses it. Don't add
   convenience endpoints that mix concerns; keep the surface minimal
   and composable.

2. **The IDE is API-only.** No back-channels, no shared memory with
   the platform server. The IDE communicates exclusively via the
   public API. This is what makes "remote IDE / server elsewhere"
   trivial in Phase 3.

3. **Workflow Engine state lives in the event log.** No in-memory
   workflow state that would be lost on server restart. The log is
   the source of truth; the engine reconstructs running workflows from
   it. (Same discipline as Phase 1's Agent Engine.)

---

## Part 5 — Phase 3+ Architecture (Sketched)

This is the speculative section. Less commitment, more "make sure the
seams hold."

### 5.1 The Module Landscape (Sketch)

```
                  ┌──────────────────────────────┐
                  │       IDE (browser)          │
                  │  per-user, runs anywhere     │
                  └──────────────┬───────────────┘
                                 │ HTTPS
                                 ▼
              ┌────────────────────────────────────┐
              │       Platform Server              │
              │ Long-running. Hosts API, holds     │
              │ catalog, mediates Event Bus.       │
              └─┬────────────┬─────────────┬───────┘
                │            │             │
       spawns   │            │             │ reads/writes
                ▼            ▼             ▼
       ┌──────────────┐ ┌─────────┐ ┌──────────────────┐
       │   Runner     │ │ Runner  │ │   Storage tier   │
       │ (one per     │ │ ... N   │ │  Postgres,       │
       │  execute)    │ │         │ │  session log     │
       │              │ │         │ │  store, content  │
       │              │ │         │ │  store           │
       └──────────────┘ └─────────┘ └──────────────────┘
                                            │
                                            ▼
                                    ┌──────────────────┐
                                    │  Asset registry  │
                                    │ shared bundles,  │
                                    │ per-user bundles │
                                    └──────────────────┘
```

**Server** — stateless service. Reads sessions, serves API, mediates
bus. Horizontally scalable behind a load balancer.

**Runners** — short-lived processes spawned to execute one agent or
workflow run. Stateless except for the execution they're handling.

**Storage tier** — Postgres for entities, blob store (S3-like) for
session JSONL and content. The "platform root = CWD" of Phase 1
becomes "platform root = a tenant's namespace in the blob store" in
Phase 3.

**Asset registry** — shared catalog of installed bundles. Per-user
bundles for private content; shared bundles for team-wide assets.

### 5.2 What Stays the Same

Almost everything from Phase 2's contracts:

- The Agent Engine, Workflow Engine contracts unchanged.
- All Subsystem and Provider Module contracts unchanged.
- The Kernel, Error Model, Schemas unchanged.
- The IDE talks to the same API.

What's different is **where** things run and **how** state is stored,
not **what** the contracts say.

### 5.3 What's Genuinely New in Phase 3

**Multi-tenancy controls.** Per-user filesystem namespacing, per-user
quota enforcement, per-user audit trails. New cross-cutting concerns
not present in Phase 1-2.

**Operational tooling.** Backup, restore, migration, version upgrades
that don't break running sessions. Production-ops machinery.

**Marketplace.** Bundle distribution becomes a curated registry with
discovery, ratings, security review, version management. Out of scope
for current spec set.

**Cost accounting.** Per-user model usage tracking, billing
integration, per-agent cost limits. The data exists in events (model
usage is in `model.response.usage`); the accounting layer is new.

### 5.4 Phase 3 Risk Items

**The server/runner boundary is hard to get right.** Process spawn
overhead per `execute` call vs. keeping warm runners vs. routing to
existing runners. The right answer depends on workload patterns we
can't predict from Phase 1.

**Storage scaling.** Session JSONL files are append-only and one per
session. At scale (millions of sessions, hundreds of gigabytes), the
filesystem-like access pattern needs object-store-friendly redesign.
The Session/Run Store contract is shape-compatible but the impl is
nontrivial.

**Asset catalog distribution.** Phase 1's "rebuild catalog at startup
by scanning" doesn't scale to a shared multi-tenant registry. Needs a
real registry service with caching, versioning, access control.

---

## Part 6 — How to Use This Roadmap

For Phase 1 (now): read Part 1. Build against it.

For Phase 2 planning (when Phase 1 is shipped): read Parts 2 and 3.
The seams in Part 2 tell you what's already prepared; the architecture
in Part 3 tells you what to add.

For Phase 3 planning (later): read Part 4 and Part 5 to validate that
Phase 2's design decisions support the eventual transition. If Phase 2
makes a decision that closes a Phase 3 seam, revisit.

The roadmap is intentionally honest about what's speculative:
- Phase 1 is fully specified (the spec suite).
- Phase 2 is architected at a high level; module-level specs come
  when Phase 2 starts.
- Phase 3 is sketched to validate Phase 2's design decisions, not to
  drive implementation.

This is the right altitude. Too much Phase 3 detail now would be
speculation. Too little, and we might paint Phase 1 or 2 into a
corner.

---

## Part 7 — Architectural Principles (Cross-Phase)

Five principles that hold across all phases:

1. **Filesystem is ground truth.** Events on disk are the durable
   record. Postgres, caches, in-memory state are all rebuildable from
   the log. No exceptions.

2. **The engine is a recorder; the framework drives.** This applies to
   Phase 1's Agent Engine wrapping CrewAI, and to Phase 2's Workflow
   Engine wrapping Agent Engine calls. The pattern scales.

3. **Modules behind contracts; variants behind registries.** New
   capabilities arrive as new variants of existing Provider Modules,
   or new Subsystem implementations. Never by patching existing code.

4. **State lives in the event log, not in the modules.** A module that
   restarts must reconstruct from events. Memory cache, workflow
   state, session listings — all derivable. This is what makes the
   server/runner split possible.

5. **Pragmatic implementations, modular contracts.** Don't abstract for
   the sake of abstraction. Pick concrete implementations and ship
   them. But put them behind contracts so Phase N+1 can swap or
   extend without rewriting Phase N.
