# Phase 1 — v1 Scope: Inclusion and Exclusion

**Draft 0.1**

> The thing that gets built and shipped as v1. Two lists, deliberate.
> The exclusion list is more important than the inclusion list.
>
> If a feature is not on the inclusion list, it is not in v1, even if it
> seems obvious. If a feature is on the exclusion list, it is not in v1
> even if it seems small. "Add later, no contract foreclosed" is the
> standard for everything excluded.

---

## Part 1 — What v1 IS

v1 is a **local-first, single-agent platform** that lets a developer
configure agents from layered YAML/Markdown files, run conversations
with them, and observe and replay every detail of what happened.

The deliverable, stated as plainly as possible:

> *Configure an agent. Talk to it. The agent uses tools and skills it
> was configured with. Every detail is logged with 100% fidelity. The
> session can be resumed at any time. The session can be replayed
> byte-identically.*

That is the entire user-facing surface of v1. It is intentionally small.

---

## Part 2 — v1 INCLUDES

### 2.1 Architecture in v1

- **Config Kernel** — the schema (Phase 1 schemas §A) and the JSON Merge
  Patch resolver. Pure, no I/O.
- **Agent Engine** — the contract (engine-contract-0.1). v1 implementation
  wraps CrewAI, fully encapsulated.
- **Session/Run Store** — sessions and threads as entity records;
  per-session JSONL event log files; leaf computation; per-thread locking.
- **Event Bus** — two-tier (durable + ephemeral) inter-module bus.
- **Asset Management** — layered config file composition; additive package
  union; namespaced-path asset references.
- **Provider Modules** — one provider per module, contracts in place:
  - Memory: `recent_buffer` variant.
  - Secrets: `.env` variant.
  - Sandbox: `native` variant (no actual sandboxing).
  - Authorization: `allow_all` variant (single admin user).
- **Built-in tools** — minimal standard library, see §2.6.
- **Skills** — Agent Skills standard format; discovery; lazy load; force
  invoke.

### 2.2 Agent capabilities in v1

- Conversational mode (`io.mode: conversational`) — talk to an agent.
- Single-shot mode (`io.mode: single_shot`) — invoke an agent once and get
  a result.
- Typed I/O per agent's IOSpec — `string`, `number`, `integer`, `boolean`,
  `object`, `array`, `file_ref`.
- Tool use via the underlying framework (CrewAI's tool mechanism).
- MCP server tools — lazy and eager lifecycle.
- Reasoning models — `parameters` for thinking level / reasoning budget;
  reasoning trace captured in events.
- Layered system prompt — AGENTS.md / SOUL.md / TOOLS.md convention as the
  default preset; layer order is data.
- Layered configuration — system defaults → agent definition → call context,
  via the resolver.
- Multi-file, additive package union — install a package; it adds assets
  and agents to the available pool; never silently overwrites.
- **Model fallback chains** *(Phase 1.3, FB-R9)* — declare
  `fallback_models: [haiku, gpt4o]` on the agent (or
  `defaults.fallback_models` on the platform) and the engine
  walks the chain on Recoverable `model.*` errors (rate limit,
  transient 5xx, timeout, context overflow). Fallback is
  pre-stream only in v1.3: once the first content event has
  fired, the exchange ends with the truncation rather than
  swapping models mid-stream.
- **Jinja2-templated prompts** *(Phase 2.2, ENV-R4)* — the
  `prompt:` field is rendered as a Jinja2 template with
  `StrictUndefined`. Plain-text prompts (no `{{`, `{%`) render
  byte-identical to themselves. Standardized variables: `tools`,
  `docs`, `workspace`, `agent`, `platform`.
- **Per-agent envelope fields** *(Phase 2.2)* — agents declare
  `permissions:` (per-agent authz rules composed over platform
  rules), `context_files:` (kolsch- or workspace-prefixed
  documentation files inlined or referenced in the prompt), and
  `extends:` (single-parent inheritance with wholesale-per-field
  override).

**Agent YAML fields in v1** (post-Phase 2.2): `schema_version`,
`assembly_version`, `agent_id`, `name`, `description`, `model`,
`fallback_models`, `io`, `prompt`, `subsystems`, and the Phase
2.2 envelope additions `extends`, `permissions`, `context_files`.
Composition order: `schema default ← defaults.<field> ← extends
chain ← agent` (wholesale-per-field override; see Schemas
§A.11.2).

### 2.3 Session and log capabilities in v1

- One session per JSONL file. One thread per session in v1.
- Append-only tree-structured event log per session.
- 100% fidelity logging per the Materialization Rule.
- Session resume — open the JSONL file, derive the leaf, continue.
- Session naming, starring, archiving — as mutable entity-record state.
- Compaction — engine triggers when projected context exceeds threshold;
  Memory provider produces summary; full pre-compaction content kept in
  the log.
- Retries appear as tree siblings (natural branching from retries).

### 2.4 Interfaces in v1

- **CLI** — invoke an agent, run a conversation, list sessions, resume a
  session. Minimal but real.
- **API** (FastAPI) — execute / execute_stream endpoints; session and
  thread CRUD-lite; log retrieval. Local-only by default.
- **First-run setup CLI** *(Phase 1.2, UX-R3)* — `kolsch setup`
  scaffolds the workspace, walks the first-provider connect, ships a
  starter agent, and verifies the install with a real run. Idempotent
  — re-running picks up where it left off without overwriting. Accepts
  `--non-interactive` for CI / scripted installs (driven by the
  `KOLSCH_PROVIDER` / `KOLSCH_PROVIDER_KEY` env vars).
- **LLM provider management CLI** *(Phase 1.1, LLM-R9)* — five
  commands for connecting and managing LLM providers:
  - `kolsch connect <provider>` — interactive auth flow + model
    discovery; writes to `platform.yaml` + `.env`.
  - `kolsch disconnect <provider>` — removes the provider entry
    and secret.
  - `kolsch providers` — list configured providers, model counts,
    last-refresh time.
  - `kolsch refresh [<provider>]` — re-run model discovery; one
    provider if named, all of them otherwise.
  - `kolsch models [<provider>] [--filter <substring>] [--verbose]
    [--json]` — list discovered models per connected provider so
    users know what to put in agent YAML's `model:` field.
  v1.1 implements these for `opencode` and `openrouter`; the four
  other named providers (anthropic, openai, bedrock, llama) are
  planned for later patches per the established connector pattern.
- **Permissions management CLI** *(Phase 2.0, TOOL-R6)* — four
  commands for live-editing the workspace's authorization rule
  list out of chat:
  - `kolsch permissions list` — print the rule list with stable
    rule ids.
  - `kolsch permissions add --what <ref> --pattern <glob>
    [--from <caller-ref>] --effect {allow,prompt,deny}
    [--before <id> | --after <id> | --first]` — insert a new rule
    at the chosen position; writes `platform.yaml` atomically and
    reloads the in-memory rule list if a runtime is active in the
    same process. `--from` (optional) scopes the rule to
    invocations whose caller-chain contains a matching
    `<bundle>/<agents|workflows>/<name>` frame.
  - `kolsch permissions remove <id>` — remove the named rule.
    Refuses to remove the protected default-deny tail
    (`rl_default_deny`).
  - `kolsch permissions show <id>` — print one rule, including
    its `effect` (now `allow` | `deny` | `prompt`) and the
    rule's place in evaluation order.
  These commands plus the in-chat four-choice modal (Phase 2.1)
  are the two surfaces for editing the rule list; direct
  `platform.yaml` edits also work but the CLI is the path the
  docs point at. See `docs/how-to/permissions.md` for recipes.
- **Interactive chat REPL** *(Phase 2.1, CHAT-R3)* —
  `kolsch chat [<agent_ref>] [--session <SES_ID>]
  [--model <MODEL_REF>]` opens a streaming conversation with
  the named agent (defaults to the configured starter agent
  when `<agent_ref>` is omitted). `--session` resumes an
  existing session; `--model` overrides the agent's declared
  model for this session only (recorded as a session-scoped
  attribute — the YAML is untouched). The chat is the canonical
  consumer of Phase 2.0's prompt-callback hook — the four-choice
  modal that fires on `prompt`-effect rules lives here.
  See `process/phase2.1-chat-specs.md` for the full contract.

### 2.5 Deployment

- Local-first. **The default install requires no external services** —
  SQLite for entity records plus an in-process event bus. `uv run
  kolsch` works with nothing else installed or running.
- **The default install requires no LLM provider** *(Phase 1.2,
  UX-R4)* — `kolsch setup` creates a workspace and ships a `provider:
  fake` starter agent so the first run works without credentials.
  Real-LLM execution needs `kolsch connect <provider>` (either inside
  `setup` or any time after).
- **Storage matures with scale** (three tiers): Tier 1 (default) SQLite +
  in-process bus; Tier 2 SQLite + Redis (cross-process fan-out); Tier 3
  Postgres + Redis (volume/concurrency at scale). Postgres and Redis are
  opt-in backends, selected in `platform.yaml`, not requirements. See the
  Development & Testing Handbook §6 for the tier detail.
- Backends run via Podman/Docker (a compose file) when adopted; the
  default tier needs no containers at all.
- Async execution via FastAPI + Arq workers.
- Single user, single workspace. No multi-tenancy machinery.

### 2.6 Built-in tools (the standard library)

A deliberately minimal set:

- `read` — read a file.
- `write` — write a file.
- `edit` — edit a file with structured replacement.
- `bash` — run a shell command.

That is the entire built-in tool set in v1. Everything else is a declared
tool (user-supplied) or an MCP server tool.

---

## Part 3 — v1 EXPLICITLY EXCLUDES

Each item below is plausibly something a reader would expect to be in v1.
Each is explicitly NOT in v1. The line after each item names the phase
or context in which it returns.

### 3.1 Orchestration and multi-agent

- **The Workflow Engine.** No DAGs, no nodes, no fan-out/fan-in, no
  conditionals, no edges, no workflow definitions. Workflows are *Phase 2*.
- **Multi-agent workflows.** No mechanism for two agents to coordinate.
  An agent can hand off to a sub-agent via tool calls if the framework
  supports it, but there is no workflow-level construct. *Phase 2.*
- **Sub-workflows / nested workflows.** No `kind: sub_workflow` node type.
  *Phase 2+.*
- **The `author_workflow` capability.** The signature capability of the
  built-in agent (writing workflow YAML from a conversation's context)
  does not exist in v1 because workflows do not exist. *Phase 2.*
- **`run_*` event types and the `run` envelope field.** *Phase 2.*

### 3.2 UI and consumers

- **The IDE.** No React app, no visual builder, no trace viewer in v1.
  The IDE is its own track, specified separately. *Later phase.*
- **Channels.** The "channels" organizational concept is IDE vocabulary,
  not a core concept. Not in v1.
- **Messaging integrations** — Slack, Discord, WhatsApp, etc. *Out of
  scope for the core platform; possible later as a consumer that sits on
  the API + Event Bus, never as a core feature.*
- **Hosted/multi-tenant deployment.** v1 is single-user local-first.
  *Later phase, requires the Authorization provider to be real.*

### 3.3 Sharing and ecosystem

- **Marketplace/registry of bundles.** Sharing is git URLs in v1; there is
  no central registry. *Later phase.*
- **Cross-bundle dependency resolution.** A bundle may reference another
  bundle by name in its manifest, but v1 does not automatically install,
  fetch, or resolve transitive dependencies. The user installs what they
  need. *Later phase.*
- **Automatic dependency installation** (uv libraries, Linux packages,
  etc.). v1 *describes* requirements in a bundle's manifest; v1 does NOT
  install them. The user satisfies dependencies themselves. *Later phase.*
- **Asset content hashes and content-addressed references** (Option D
  layering of the asset-reference scheme). v1 uses namespaced paths
  (Option B); content hashes are not part of authoring or resolution.
  *Later phase, non-breaking upgrade.*

### 3.4 Provider Module sophistication

- **Provider selection policies.** v1 has no policy mechanism. Each
  Provider Module has exactly one registered provider. *Later phase, when
  multiple providers per module make selection meaningful.*
- **Semantic memory / vector retrieval.** v1 Memory provider is
  `recent_buffer` — recent turns, truncate when over budget. No vectors,
  no hybrid search, no `pgvector`. *Later phase, behind the Memory Provider
  contract.*
- **Hierarchical / extraction-pipeline memory** (Mem0-style, OpenClaw's
  four-layer). *Later phase.*
- **Workspace concept** (a mutable directory the agent owns alongside the
  session log). v1 does not have agent workspaces. The session log is the
  agent's only persistent state. *Later phase.*
- **Self-scheduling** — an agent scheduling its own future invocations.
  Requires an external scheduler/trigger. *Later phase.*
- **Webhook and cron triggers** for invocation. v1 invocations are CLI or
  API calls only. *Later phase.*
- **Real sandboxing** (Docker, strict containers, deny lists). v1's
  Sandbox provider is `native` — tools execute in-process. *Later phase,
  required for hosted/multi-tenant.*
- **Real authorization.** v1's Authorization provider is allow-all.
  *Later phase, required for multi-tenant.*
- **Secret backends beyond `.env`** — OS env, vaults, cloud secret
  managers. *Later phase.*
- **HITL (human-in-the-loop) Provider Module.** Not in v1 because there is
  no workflow construct to suspend on. *Phase 2+.*
- **No internal Event Bus subscribers in v1.** The Event Bus
  infrastructure is in place (Redis pub/sub, publish from the Agent
  Engine after every `append_event`), but v1 has no internal modules
  that subscribe to it. The bus is available for external consumers
  (a debug tool watching session activity, a third-party metrics
  exporter), but the v1 platform's own modules do not depend on it.
  Subscribers arrive in later phases: the IDE for live updates,
  the Workflow Engine for reaction-driven workflows, the Scheduler
  for time-triggered runs. v1 ships the publish side; the subscribe
  side is mechanism without consumers.

### 3.5 Branching, forks, and tree navigation

- **User-facing branch navigation.** No `/tree`-style command, no UI for
  selecting an earlier point and continuing. *Later phase.*
- **Fork** — creating a new session derived from an existing one's state.
  *Later phase.*
- **Clone** — duplicating an active branch into a new session.
  *Later phase.*
- **Branch summaries.** Pi's mechanism for summarizing the abandoned
  branch when navigating. Requires branching navigation. *Later phase.*

(Note: the tree machinery is in v1 — `parent_event_id` is the structural
ordering, leaf is derived. Retries naturally create siblings. What's
excluded is *user-facing* tree navigation, not the tree itself.)

### 3.6 Testing and quality

- **Dataset-based evaluation module.** Running an agent against a fixed
  set of inputs and scoring outputs. *Later phase. Noted as elevated by
  the research findings (model-dependent context tuning); should not stay
  deferred indefinitely.*
- **A/B testing of agent configurations.** *Later phase.*
- **Built-in LLM-as-judge evaluator.** *Later phase.*

### 3.7 Cost and resource controls

- **Cost cap enforcement** — halting a run mid-execution when costs exceed
  a threshold. v1 *records* cost on `model.response` events; v1 does NOT
  enforce caps. The field is reserved in `RuntimeSpec` for the value to be
  recorded but is not acted on. *Later phase.*
- **Run-, session-, and channel-level cost aggregation and visualization.*
  *Later phase, with the IDE.*
- **Rate limiting.** No per-user, per-agent, or per-tool rate limits.
  Provider-side rate limits handled only via framework's retry policy.
  *Later phase.*

### 3.8 Loops and advanced control flow

- **Unbounded loops.** v1 has no looping construct at all. Retries inside
  an agent's execution are framework-handled (CrewAI handles them) and
  appear as retry siblings in the tree. There is no user-defined loop.
- **Bounded loops with `max_iterations`.** The construct exists in the
  *Phase 2* workflow schema. *Phase 2.*
- **Conditional routing.** No agent-level "if/else" construct. *Phase 2,
  in the workflow schema.*

### 3.9 Observability beyond the log

- **Cross-session analytics** — "all my sessions over time, sorted by
  cost." v1 has index rows in Postgres but no dashboard, no aggregation
  service. Cross-cutting queries are SQL only. *Later phase, with the IDE.*
- **Metrics export** — Prometheus, OpenTelemetry, etc. *Later phase.*
- **Live monitoring dashboards.** *Later phase.*

### 3.10 Multi-language and platform breadth

- **Non-Python agent frameworks.** v1's Agent Engine wraps CrewAI;
  alternative implementations (LangGraph, raw LLM calls, OpenClaw as a
  subprocess) are valid under the contract but not shipped in v1.
  *Later phase, the contract permits them.*
- **Non-local deployment targets** — Kubernetes manifests, cloud-specific
  configurations. v1 ships Docker Compose. *Later phase.*

### 3.11 Refinements that change the schema

These are real future improvements that the schema and contracts already
anticipate (open items in the schema draft), explicitly NOT delivered in v1:

- **Resolver Option D** — merge directives (`$remove`, list strategies,
  template holes). v1 has Option B (JSON Merge Patch). *Later phase,
  non-breaking upgrade.*
- **Asset reference Option D** — layered path + UUID + content hash. v1
  has Option B (namespaced paths only). *Later phase, non-breaking.*
- **Object-storage-backed `file_ref`s.** v1 uses local paths. *Later phase.*
- **Lossless compression of the event log** (prefix dedup, etc.). v1
  stores plainly. *Later phase, implementation detail.*

---

## Part 4 — Things That Will Tempt Drift

These are items that have been discussed favorably in the conversation, are
not in v1, and are MOST LIKELY to be argued back into v1 mid-build. Each
needs to be explicitly held out.

### 4.1 The `author_workflow` capability

Discussed as the signature feature of the platform — the conversation
→ autonomy bridge. Genuinely important. But it requires the Workflow
Engine and the workflow schema to exist. **v1 ships without it.** The
built-in agent in v1 is a *configuration assistant* (helps the user write
agent YAML) — not a workflow authoring assistant. The full capability
arrives with Phase 2.

### 4.2 Channels

Discussed as the user's organizational unit. The IDE story makes them
important. **They are an IDE concept and v1 has no IDE.** v1 has sessions
as a flat list, no grouping.

### 4.3 The full assistant_node preset

Discussed as a Tier 4 node type (workspace + memory + compaction +
skills + scheduler). **v1 has the foundations** (skills, the crude memory
provider, compaction triggering) but does NOT ship a packaged
"assistant_node" preset, because nodes don't exist in v1 (workflows are
Phase 2). A user can configure an agent that has all the foundations
enabled, which is functionally similar.

### 4.4 Real memory

The conversation has detailed designs for semantic memory, hybrid search,
the memory survey's five mechanism families. **v1 ships `recent_buffer`
and nothing else.** The Memory Provider contract is correct and
forward-compatible; the v1 implementation is deliberately crude.

### 4.5 Testing and evaluation

The research found that context configuration is model-dependent and needs
empirical tuning, which elevates the testing module from "v2 nice-to-have"
to "v1 maybe-needed." **Held out of v1 anyway.** v1 ships without
evaluation tooling; the limitation is acknowledged and will be addressed
in the phase after v1 ships and we have real users to find out whether
they actually need it.

### 4.6 The IDE

It's the most user-facing thing and the most tempting to ship. **v1
deliberately ships without it.** The CLI and API are the v1 surface. The
IDE is its own development track that can begin in parallel but is not
gated on v1's release.

---

## Part 5 — What "v1 Ships" Concretely Means

A user, on their own machine, can:

1. Install the platform via `docker compose up`.
2. Author an agent config via YAML and Markdown files in a directory.
3. Run that agent from the CLI: `kolsch run <agent>` (name TBD).
4. Have a conversation with the agent, or invoke it single-shot.
5. The agent uses the configured tools (built-in or declared or MCP).
6. Every detail is logged to a JSONL file.
7. The session can be paused (close the CLI) and resumed
   (`kolsch resume <session>`).
8. The session log can be replayed byte-identically through the same
   Assembly Procedure version.
9. Optionally, the same operations are available via the FastAPI service
   for external integration.

That is the entire v1. Everything that doesn't directly contribute to that
list is out.

---

## Part 6 — How v1 Validates Itself

(Forward reference. The four-test plan from the conversation goes here in
the dedicated test plan section.)

- **Feasibility** — CrewAI gives clean events; the engine contract holds
  observably.
- **Soundness** — the architecture's choices produce better outputs than
  the naive shared-context baseline, *measurably*.
- **CrewAI Bet** — stress-tested through Tests 1–2.
- **Desirability** — three real users return unprompted.

---

## Part 7 — Open Items

1. **CLI command names.** Resolved (post-Phase-1): the project is named
   **Kolsch** and ships a single console script named `kolsch`.
2. **The minimum useful built-in tool set.** §2.6 lists
   read/write/edit/bash. Whether v1 also ships `grep`, `find`, `ls`,
   `tree` (commonly bundled alongside the four) needs a small
   decision. Leaning yes for parity, but technically they can be MCP
   tools or declared.
3. **The exact API surface.** §2.4 sketches "execute / execute_stream /
   session CRUD-lite / log retrieval." The precise endpoints are an API
   spec, not in this scope document.
4. **Bundles in v1** — installable yes, but the manifest format hasn't
   been specified beyond "declares dependencies and required env vars."
   Worth a small focused doc when the Asset Management contract is drafted.
