# Kolsch Roadmap

The full sequence of phases from "single-agent foundation" to
"usable product" and beyond. Where this differs from
`docs/explanation/architecture-roadmap.md`: that doc is about the
architecture (what gets built); this doc is about the order and
status (when, and how far along). Update this file whenever a
phase changes status or new work is scheduled.

---

## Where we are right now

| Phase | Status | Spec | Implementation |
|---|---|---|---|
| 1 — Single-agent foundation | shipped | various Phase 1 contracts in `docs/explanation/` | merged through May 28 |
| 1.1 — LLM providers | spec'd + partially shipped | `phase1.1-llm-specs.md`, `phase1.1-llm-provider.md` | `build_llm` + OpenRouter/OpenCode connectors + `kolsch connect` family in main; bug-litellm-model-routing fix in progress |
| 1.2 — Workspace + setup | spec'd + shipped (qol-setup absorbed) | `phase1.2-usability-specs.md`, `phase1.2-usability-build.md` | `kolsch setup`, `KOLSCH_WORKSPACE`, `platformdirs` default in main; bug-setup-test-run-failure-handling open. **qol-setup-evolution-and-defaults Steps 2-8 shipped**: shared migration utility (`src/cli/_migrations/`), platform.yaml + authz + bundle drift migrators, `defaults` block + resolver fallback, helper agent rewritten, orchestrator integration with KEEP-no-double-prompt regression coverage. Re-runs are now migration-aware end-to-end. |
| 1.3 — Model fallback chains | spec only | `phase1.3-fallback-specs.md`, `phase1.3-fallback-build.md` | not built |
| 2.0 — Tools + permissions | spec'd + partially shipped | `phase2.0-tools-specs.md`, `phase2.0-tools-build.md` | four builtin tools + `prompt` effect + workspace-only defaults in main; chat-permissions migration PRs uncommitted on working tree (E-BUG-CREWAI-002 merge gap) |
| 2.1 — `kolsch chat` REPL | spec'd + partially shipped | `phase2.1-chat-specs.md`, `phase2.1-chat-build.md` | Step 24a scaffold only; 24b–24e not built |
| 2.2 — Agent envelope | spec only (just drafted) | `phase2.2-agent-envelope-specs.md`, `phase2.2-agent-envelope-build.md` | not built |
| Rename `pi-platform` → `kolsch` | mostly complete | `delegated-imagining-newell.md` in .claude/plans | applied to all new docs; legacy refs handled |

**Cross-cutting:**
- ~8 bug docs in `process/bug-*.md`, mix of closed/open.
- 3 QoL docs in `process/qol-*.md`, mix of shipped/spec'd.
- F5 fix (renderer's `_THINKING_INDICATOR` removal) folded into `qol-chat-capsule-layout.md`; supersedes PR G2 of the CrewAI-stdout bug.

---

## What's left to reach a "usable product"

Five remaining areas. Each is a phase or a phase family. Ordered by
the suggested sequence (with dependencies called out below).

### Phase 2.3 — Single `kolsch` bundle + drift detection
**Status:** drift mechanism shipped via qol-setup; Phase 2.3 shrinks
to "ship OpenClaw + Pi as additional canonical agents."

Replaces Phase 1.2's `bundles/starter/` with a canonical
`bundles/kolsch/` containing helper, OpenClaw, and Pi reference
agents. On `kolsch setup` re-run, detect drift between local files
and the canonical bundle; offer per-file `[k]eep / [c]anonical / [d]iff / [s]kip`
choices with timestamped backups.

Depends on: 2.2 (the bundled agents use `extends:` and
`context_files:` from Phase 2.2). Best landed AFTER 2.2 ships so
the reference agents demonstrate the envelope.

> **Status amended (shipped portion):** the drift-detection
> mechanism is implemented in
> [qol-setup-evolution-and-defaults.md](qol-setup-evolution-and-defaults.md)
> Steps 2-8 — shared migration utility, three per-artifact
> migrators (`platform.yaml` schema, authz rules, bundle
> drift), orchestrator integration in `kolsch setup`. The
> single-bundle rename (`starter` → `kolsch`) and the rewritten
> helper agent landed alongside.
> **Phase 2.3 remaining work:** ship OpenClaw + Pi as
> additional canonical agents under `bundles/kolsch/agents/`.
> They'll use the same envelope features (`extends:`,
> `context_files:`, Jinja prompts) that helper now demonstrates;
> the drift detector picks them up for free on the next setup
> re-run after they ship.

### Phase 2.4 — Skills subsystem buildout
**Status:** Phase 1 contracts in `docs/explanation/skills-subsystem.md`;
mechanics shipped; no canonical skills exist; no test discipline yet.

Three sub-phases likely:
- **2.4.0** — Audit and harden the skills subsystem: confirm Phase 1's
  contracts still hold, add missing tests, document the skill
  authoring pattern. No new features; just verification.
- **2.4.1** — Ship a small set of canonical skills as part of the
  `bundles/kolsch/` bundle (Phase 2.3). What set? At minimum, the
  ones that earned skills their own subsystem in the contracts:
  prompt-extraction, response formatting, retry/recovery scaffolds.
- **2.4.2** — "Skill-creating skill" / "skill author skill": a
  meta-skill that helps a user create their own skills via
  conversation. The agent uses it as a generator. Possible only
  after 2.4.1 sets the pattern.

Depends on: 2.0 (tools), 2.2 (agent envelope — skills' context
injection), 2.3 (bundle structure).

### Phase 2.5 — Tutorials and reference docs
**Status:** `docs/tutorials/first-agent.md` exists; rest of the
content is empty.

Documentation phase. Not code, but blocking for a "usable product."
Tutorials needed:
- **Creating your first bundle** — how to organize agents, tools, skills.
- **Writing an agent** — extending the helper, customizing the prompt with the variable API, adding context_files.
- **Writing a tool** — `@tool` decorator + permissions API; pattern from existing `bundles/kolsch/tools/*` as examples.
- **Writing a skill** — once 2.4 lands; the pattern + authoring guide.
- **Connecting an LLM provider** — already covered in `first-agent.md`'s step 8, but a standalone deep-dive.

How-to guides (Diátaxis recipes) for specific tasks:
- **Tightening permissions** for an agent.
- **Sharing context across agents** via inheritance.
- **Resuming and replaying sessions.**
- **Updating to a new kolsch version** — what `kolsch setup` re-run does, how the drift handling works.

Reference docs are mostly in place via `docs/reference/` but need
an audit pass for completeness.

Depends on: 2.0–2.4 (each tutorial exercises one of those
capabilities). Realistically rolls in parallel with the phase it
documents.

---

### Phase 3 — Workflows
**Status:** entirely undefined. Architecture-roadmap §3 sketched it
at a high level; needs concrete specs.

The largest single body of work after Phase 2. Sub-phases will
likely be:

- **3.0 — Workflow asset type + parser.** YAML schema for workflows;
  parsing into a typed `ResolvedWorkflowConfig`; storage as a new
  asset type in the bundle catalog. No execution yet.
- **3.1 — Workflow Engine: sequential nodes.** A workflow with a
  list of agent-invocation nodes runs them in order; each node's
  output becomes the next node's input via the IOSpec contract
  from Phase 1. Emits `workflow.started`, `node.dispatched`,
  `node.completed`, `workflow.completed` events.
- **3.2 — Multi-thread engine support.** Each parallel workflow
  branch spawns its own thread. Phase 1 entity/event-tree
  mechanics support multi-thread but the engine pre-flight assumes
  single-thread-per-session; this lifts that.
- **3.3 — Parallel + conditional node types.** Fan-out / fan-in via
  `parallel:` blocks; `if:` blocks for routing. Builds on 3.2.
- **3.4 — Sub-workflows.** A node that invokes another workflow.
  Composition without recursion limits.
- **3.5 — `author_workflow` meta-capability.** The signature Phase
  3 feature per `v1-scope.md`: a built-in agent that writes
  workflow YAML from a conversation. Possible only after 3.0–3.4.
- **3.6 — Workflow tutorials and docs.** Walkthroughs of building
  the first workflow, the workflow design patterns, the testing
  strategies. Parallel-rolls with 3.0–3.5.

Workflow node types worth scoping during 3.0:
- **Agent invocation** — call an agent with inputs.
- **Conditional** — `if/else` based on prior node output.
- **Parallel** — fan-out a list into parallel agent calls.
- **Sub-workflow** — invoke another workflow.
- **HITL** — human-in-the-loop approval gate (depends on the HITL
  provider; might land as Phase 3.7 or later).
- **Map** — apply an agent across a list (variant of parallel).
- **Reduce** — combine results from a map.

Depends on: Phase 2 complete (agents need to be expressive enough
to be workflow nodes). Multi-thread engine work (3.2) is the
hardest sub-phase technically.

---

### Phase 4 — Memory system buildout
**Status:** Phase 1 has the `recent_buffer` memory provider variant
shipped; everything else is open.

Sub-phases:

- **4.0 — Semantic memory variant.** A vector-store-backed memory
  provider that does similarity retrieval. Registers as a new
  variant under the existing Memory Provider's registry. Config
  fields: embedding model, vector store backend (could be Mem0 or
  pgvector or chromadb — pick at spec time).
- **4.1 — Off-the-shelf integration.** Wrapping an existing library
  (Mem0 is the leading candidate per
  `docs/explanation/memory-provider-contract.md:550`). Reduces
  what we maintain in-house; trades for an external dependency.
- **4.2 — Context compaction.** When a conversation approaches the
  model's context limit, summarize older exchanges into a
  `memory.compaction` event that lives in the JSONL. Replay
  honors the compaction (so re-runs are deterministic). Cross-
  cutting with the chat renderer — compactions might surface as
  a system notice ("Compacted N exchanges into a summary").
- **4.3 — Workflow-scoped memory.** When a workflow's first agent
  writes a memo, the third agent in the same workflow can read it.
  Depends on Phase 3.0–3.4.
- **4.4 — Cross-session memory.** A persistent store that survives
  across sessions; the agent can recall yesterday's conversation.
  Depends on 4.1 (semantic memory makes this useful) and tight
  privacy/scope rules.

Depends on: Phase 1's Memory Provider contract (in place). Some
sub-phases also depend on Phase 3 for workflow-scoped memory.

---

### Phase 5 — IDE
**Status:** entirely undefined. Architecture-roadmap §3.4 sketches
three views (agent designer, workflow designer, session inspector).

Largest end-of-Phase-2 work; needs its own multi-phase decomposition.
Likely sub-phases:

- **5.0 — Server / runner split.** Pre-IDE preparation. The CLI/API
  surface gets restructured so the IDE can talk to a long-running
  server. The architecture-roadmap §4's Seam 8 covers the shape.
  Possibly merged into Phase 6 (hosted) if convenient.
- **5.1 — Session inspector.** Browser app showing live sessions:
  event timeline, tool-call expansion, replay-any-event-position.
  The simplest IDE view — read-only, no editing. Builds on
  Phase 1's event log.
- **5.2 — Agent designer.** YAML-aware editor with schema
  autocomplete, live validation, prompt preview. Could also offer
  a graph-style editor. Read/write on agent files in the workspace.
- **5.3 — Workflow designer.** Visual graph editor for workflows.
  Nodes are agents; edges are data flow per IOSpec. Read/write on
  workflow YAML in the workspace. Depends on Phase 3 (workflows
  exist).
- **5.4 — Live updates via Event Bus bridge.** SSE/WebSocket bridge
  so the IDE sees session events in real time. The Phase 1 Event
  Bus has the publish side; this adds the subscribe side.

Depends on: Phases 2 and 3 mostly stabilized. IDE is the user-
facing surface that everything else has built up to.

---

### Phase 6 — TUI polish (Textual rebuild of `kolsch chat`)
**Status:** referenced in `qol-chat-capsule-layout.md` and `phase2.1`
as the deferred "nice and responsive" rebuild.

Rebuilds `kolsch chat` on Textual instead of prompt_toolkit + Rich.
Trade: gives up terminal-native scrolling and selection (per Phase
2.1's §2.4 commitment) in exchange for richer layout, mouse
support, themes, pinned regions.

Sub-phases probably:
- **6.0 — Textual scaffold + chat re-implementation.** Match the
  capsule layout from `qol-chat-capsule-layout.md` but in Textual.
- **6.1 — Layout enhancements:** pinned title bar, side panel for
  session list / agent list, mouse interactions.
- **6.2 — Selection mode.** A keyboard toggle to enter selection
  mode for copy-paste (since native selection doesn't work in
  alt-screen).

Depends on: Phase 2.1 stable + the capsule QoL layout shipped (so
Textual targets a known final design, not a moving one).

Can ship after Phase 5 (IDE) or before — they're independent and
optional. Many users may be fine with the prompt_toolkit + Rich
chat for a long time; Textual is an experience upgrade.

---

### Phase 7 — Hosted / multi-tenant
**Status:** Phase 1-2's "Phase 3" from architecture-roadmap.md §4.

The final platform-architectural shift. Sub-phases will be
significant; spec at the time. Includes:
- Real Authorization with principal identities (not allow-all).
- Server / runner separation (might land partially in 5.0).
- Multi-user session isolation.
- Resource quotas and rate limiting.
- Possibly: hosted credential management (vault integration —
  builds on Phase 1.1's `vault` secrets variant).

Depends on: everything else mostly done. This is the "we're a
product" phase; everything before is single-user / local-first.

---

## Definition of "usable product"

The line where a new user can:
1. Install kolsch in five minutes.
2. Run `kolsch setup` and get a working agent that responds with
   real LLM output.
3. Have a conversation with the agent that involves tools (read
   files, run shell commands).
4. Trust that the agent doesn't bypass permissions or leak data.
5. Read tutorials that explain how to extend with their own
   agents, tools, skills.
6. Compose simple workflows from agents.
7. Recover from errors with helpful messages, not stack traces.

Mapping to phases:

| Requirement | Needed phases |
|---|---|
| Install + setup in 5 min | 1.0, 1.1, 1.2 (mostly there; setup bugs being fixed) |
| Working agent + real LLM | 1.1 + 1.2 + 2.0 (mostly there; 1.3 fallback nice-to-have) |
| Conversation + tools | 2.0 + 2.1 + 2.2 (2.0 done; 2.1 partial; 2.2 spec'd) |
| Permission discipline | 2.0 + 2.2 (composition layer) |
| Tutorials | 2.5 |
| Workflows | 3.0–3.5 |
| Helpful errors | cross-cutting; bug-doc discipline addresses each issue |

So the **minimum viable usable product** is **Phase 2 complete + Phase 2.5 tutorials + minimal Phase 3 (workflows)**:

- Phases 1.0 / 1.1 / 1.2 — done or close.
- Phase 1.3 (fallback) — desirable for reliability but not blocking.
- Phase 2.0 / 2.1 / 2.2 — completes the agent experience.
- Phase 2.3 (bundle) — ships reference agents.
- Phase 2.4.0 + 2.4.1 (skills basics) — completes the "everything works" story.
- Phase 2.5 (tutorials) — without docs, users can't onboard.
- Phase 3.0 + 3.1 (workflow basics) — enables the "compose multi-step" use case that justifies the platform existing.

Everything after that (memory, IDE, hosted, Textual polish) is
**delight + scale**, not **usability**.

---

## Suggested order

```
Current ─ Phase 1.x bug closeouts (litellm routing, setup failure handling)
   │
   ▼
Phase 2.1 finish (Steps 24b–24e) + chat-permissions migration PRs
   │
   ▼
Phase 2.2 (agent envelope; 15-step build plan)
   │
   ├──── Phase 2.5 (tutorials — rolls in parallel)
   │
   ▼
Phase 2.3 (kolsch bundle with helper + OpenClaw + Pi using 2.2's envelope)
   │
   ▼
Phase 2.4.0 (skills audit + hardening)
   │
   ▼
Phase 2.4.1 (canonical skills shipped in kolsch bundle)
   │
   ▼
Phase 1.3 (model fallback — reliability for production-grade use)
   │
   ▼
       ─── MINIMUM USABLE PRODUCT line ───
   │
   ▼
Phase 3.0 (workflow asset type + parser)
   │
   ▼
Phase 3.1 (sequential nodes — first runnable workflows)
   │
   ▼
Phase 3.2 (multi-thread engine support)
   │
   ▼
Phase 3.3 + 3.4 (parallel/conditional + sub-workflows)
   │
   ▼
Phase 3.5 + 3.6 (author_workflow meta-agent + workflow docs)
   │
   ▼
Phase 2.4.2 (skill-creating skill — depends on workflow patterns)
   │
   ▼
       ─── COMPOSABLE PRODUCT line ───
   │
   ├──── Phase 4 (memory + compaction — rolls anytime after 3.4)
   │
   ▼
Phase 5 (IDE — multi-sub-phase)
   │
   ▼
Phase 6 (Textual TUI polish — optional, parallel-rollable)
   │
   ▼
Phase 7 (hosted / multi-tenant)
   │
   ▼
       ─── PRODUCT-AT-SCALE line ───
```

Reasonable parallelism opportunities:
- Tutorials (Phase 2.5) roll in parallel with whatever phase is
  shipping content for them.
- Phase 4 (memory) is largely independent of Phase 5 (IDE); can be
  scheduled flexibly.
- Phase 6 (Textual polish) is fully independent of Phase 5 (IDE);
  pick based on user-feedback priority.

---

## How to use this doc

- **When a phase changes status** (planned → spec'd → shipped),
  update the status table at the top.
- **When a new phase is identified** (a bug or feature that warrants
  its own phase), add a section in the "what's left" area with
  status `discussed`.
- **When phase decomposition firms up** (sub-phases get sub-spec'd),
  add the sub-phase list under the parent. Keep it concise — point
  at spec docs for details.
- **When dependencies shift** (e.g., a phase that was blocking
  becomes optional), update the dependency notes and the suggested
  order graph.

This is the master pointer. Specific spec/build docs live in
`process/phaseX.Y-*-specs.md` and
`process/phaseX.Y-*-build.md` and are linked from the status
table above.

---

## Cross-cutting concerns (always-on)

These don't have phase numbers; they apply throughout:

- **Per-step PR discipline.** Each step of a phase ships as its own
  PR. The merge gap from E-BUG-CREWAI-002 D3 (multiple PRs
  bundled as uncommitted working-tree changes) is a recurring
  process problem; the discipline of one-commit-per-step prevents
  it.
- **Bug docs as the first response.** When a bug surfaces, file
  `process/bug-<short-name>.md` with hypotheses, investigation
  plan, dev/reviewer prompts. Pattern proven across the conversation.
- **Coordinator escalation log** at the bottom of every build doc.
  Devs and reviewers log unexpected findings as structured
  escalations; coordinator records resolutions inline.
- **"Coordinator approves the strategy before code lands"** for
  investigations. The STOP gate after Phase A writeups prevents
  speculative fixes. Pattern from bug-crewai-stdout-leak-investigation.md.
- **AGENTS.md hard rules** accumulate the project's discipline.
  Worth re-reading when a phase ships a new contract; sometimes
  new rules belong there.
- **Diátaxis documentation structure** (tutorials / how-to /
  reference / explanation) — every phase that adds a feature
  should add the corresponding doc forms. The Phase 2.5 tutorial
  phase is the catch-up moment; ongoing work should not let docs
  fall behind.
