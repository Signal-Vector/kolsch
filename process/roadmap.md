# Kolsch Roadmap

The full sequence of phases from "single-agent foundation" to
"usable product" and beyond. Where this differs from
`docs/explanation/architecture-roadmap.md`: that doc is about the
architecture (what gets built); this doc is about the order and
status (when, and how far along). Update this file whenever a
phase changes status or new work is scheduled.

---

## Where we are right now

*(Updated 2026-06-08 after the recent design + ship push. Status table refreshed to match shipped state.)*

| Phase | Status | Implementation |
|---|---|---|
| 1 — Single-agent foundation | shipped | merged through May 28 |
| 1.1 — LLM providers | shipped | `build_llm` + OpenRouter/OpenCode connectors + `kolsch connect` family; bug-litellm-model-routing fix shipped |
| 1.2 — Workspace + setup | shipped | `kolsch setup`, `KOLSCH_WORKSPACE`, `platformdirs` default; `qol-setup-evolution-and-defaults` Steps 2-8 shipped (migration framework + per-artifact migrators + drift detector); `bug-model-line-placeholder-collides-with-jinja` fix shipped (`5411f85`) |
| 1.3 — Model fallback chains | shipped | `c2b7dc2` — full Phase 1.3 implementation |
| 2.0 — Tools + permissions | shipped | four builtin tools + `prompt` effect + workspace-only defaults; chat-permissions migration shipped (`ba4673f`); `qol-tool-permission-discovery-uniform-with-stacking` adds modal-on-no-rule + bundle-level permissions (`8351a55`); `qol-permission-system-human-readable` rewrites the rule grammar (three-field schema, glob, `/perm` slash) (`3c392e2`) |
| 2.1 — `kolsch chat` REPL | shipped | All of 24a-24e (`91cf631`..`5c0018a`); deny/esc cancellation (`e7e1b35`); capsule layout (`f669246`); chat-deny + esc behaviors shipped |
| 2.2 — Agent envelope | shipped | `ff70f6a` Phase 2.2 base + `edaf4bd` extends-parent render path with 28-case EXT-CORPUS |
| 2.3 — Canonical kolsch bundle | **partially shipped** | bundle infrastructure + helper agent (as parent template) shipped; **OpenClaw + Pi reference agents still pending** |
| 2.4 — Skills subsystem | shipped | `2f1727d` — `kolsch skills` CLI family + three canonical skills (`write-python-test`, `review-pr-diff`, `commit-from-staged`) + `/skill` slash + `--skill` flag for force-invoke. **2.4.2 (skill-creating skill) deferred to post-Phase-3.x** |
| 2.5 — Tutorials and reference docs | **partially shipped** | `docs/tutorials/first-agent.md` exists; remaining tutorials (creating a bundle, writing an agent, writing a tool, writing a skill, connecting a provider) still to write. **No longer blocked** — every dependency phase has shipped |

**Cross-cutting:**
- 15+ bug docs in `process/bug-*.md`, mostly closed.
- 15+ QoL docs in `process/qol-*.md`, mostly shipped or stubbed.
- Engine boundary classifier shipped (`bd1f529` + `9f3c0a6`); engine no longer surfaces transport timeouts as `platform.internal_error`.
- Tool output truncation + persistence shipped (`2deacfe`): bash overflow → tmp file; `read_file` line-based pagination.
- Opencode connector timeout bumped 30s → 180s (`652b72d`); session replay tool shipped (`497ef61`).
- Unified ref convention shipped (`5d744f3`): `kolsch/bash` form; `workspace:` implicit default.
- `schema_version` + `assembly_version` optional (`48df720`).

---

## What's left to reach a "usable product"

After the recent ship push, only three pieces stand between the
current state and the "minimum viable usable product" definition
in the next section. Ordered by suggested sequence.

### Phase 2.3 — Canonical kolsch bundle (OpenClaw + Pi remaining)
**Status:** bundle infrastructure + helper agent shipped. Only
the OpenClaw + Pi reference agents remain.

**Shipped portion**:
- `bundles/kolsch/` exists with `bundle.yaml` + `tools/` + `agents/helper.yaml`.
- The drift-detection mechanism shipped in
  [qol-setup-evolution-and-defaults.md](qol-setup-evolution-and-defaults.md)
  Steps 2-8 — shared migration utility, three per-artifact
  migrators (`platform.yaml` schema, authz rules, bundle drift),
  orchestrator integration in `kolsch setup`.
- `helper.yaml` rewritten as a Jinja-block parent template
  (post-extends-parent fix) so child agents can fill `{% block
  role %}` and `{% block special_instructions %}`.

**Remaining**: author OpenClaw + Pi as additional canonical
agents under `bundles/kolsch/agents/`. They use the same
envelope features (`extends:`, `context_files:`, Jinja blocks)
that helper now demonstrates; the drift detector picks them up
on the next setup re-run. Estimated effort: ~1-2 days per
agent.

### Phase 2.4 — Skills subsystem buildout
**Status: SHIPPED** at commit `2f1727d`.

Phase 2.4.0 + 2.4.1 work consolidated into a single ship:
- `kolsch skills` CLI family: `list`, `show`, `which`, `new`,
  `validate`, `install`, `remove`.
- Three canonical skills shipped under `bundles/kolsch/skills/`:
  `write-python-test`, `review-pr-diff`, `commit-from-staged`.
- Force-invoke surface: `/skill <ref>` slash in chat;
  `--skill <ref>` flag on `kolsch run`.

**Deferred to post-Phase-3.x**: Phase 2.4.2 — the
"skill-creating skill" meta-capability. Depends on workflow
patterns; tracked at `phase2.4-skills-build.md` Part 5.

### Phase 2.5 — Tutorials and reference docs
**Status:** `docs/tutorials/first-agent.md` exists; remaining
tutorials still to write. **No longer blocked** — every
dependency phase has shipped (1.x, 2.0, 2.1, 2.2, 2.4 all done;
2.3's helper template + permissions system + skills CLI all
landed).

Tutorials needed:
- **Creating your first bundle** — how to organize agents, tools, skills.
- **Writing an agent** — extending the helper, customizing the prompt with the variable API, adding context_files. (Recent: `docs/how-to/extending-an-agent.md` from the extends-parent fix demonstrates the inheritance side; tutorial form for the full authoring story still to write.)
- **Writing a tool** — `@tool` decorator + permissions API; pattern from existing `bundles/kolsch/tools/*` as examples.
- **Writing a skill** — pattern + authoring guide. Phase 2.4 ships three canonical skills as examples.
- **Connecting an LLM provider** — already covered in `first-agent.md`'s step 8, but a standalone deep-dive.

How-to guides (Diátaxis recipes) for specific tasks:
- **Tightening permissions** for an agent. (Recent: `docs/how-to/permissions.md` updated by the human-readable permission system QoL.)
- **Sharing context across agents** via inheritance. (Recent: `docs/how-to/extending-an-agent.md` covers this.)
- **Resuming and replaying sessions.**
- **Updating to a new kolsch version** — what `kolsch setup` re-run does, how the drift handling works.

Reference docs are mostly in place via `docs/reference/` but need
an audit pass for completeness.

Sequencing: roll in parallel with Phase 2.3 OpenClaw + Pi
authoring; the new agents become tutorial examples.

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

Mapping to phases (current state):

| Requirement | State |
|---|---|
| Install + setup in 5 min | ✓ done (1.0, 1.1, 1.2 shipped) |
| Working agent + real LLM | ✓ done (1.1 + 1.2 + 2.0 + 1.3 shipped) |
| Conversation + tools | ✓ done (2.0 + 2.1 + 2.2 shipped) |
| Permission discipline | ✓ done (2.0 + 2.2 + human-readable permission system QoL shipped) |
| Skills authoring | ✓ done (Phase 2.4 shipped: CLI + three canonical skills + force-invoke) |
| Tutorials | **partial** — first-agent.md done; full tutorial set still to write (Phase 2.5) |
| Reference agents beyond helper | **partial** — Phase 2.3 OpenClaw + Pi still pending |
| Workflows | **not started** — Phase 3.0-3.5 still undefined design surface |
| Helpful errors | ✓ substantially improved this design push (engine boundary classifier; chat denial capsules; clear migration errors) |

So the **minimum viable usable product** has shrunk to three
concrete deliverables:

1. **Phase 2.3** — author OpenClaw + Pi reference agents (~1-2 days each).
2. **Phase 2.5** — write the four remaining tutorials and the four how-to recipes (writing effort).
3. **Phase 3.0 + 3.1** — workflow asset type + sequential execution (multi-week; the big undefined territory).

Everything before this point is shipped. Everything after
(Phase 4 memory buildout, Phase 5 IDE, Phase 6 Textual polish,
Phase 7 hosted) is **delight + scale**, not **usability**.

---

## Suggested order

```
Phases 1.x + 2.0 + 2.1 + 2.2 + 2.4 — all shipped ✓
   │
   ▼
Current ─ Phase 2.3 OpenClaw + Pi reference agents (~1-2 days each)
   │      + Phase 2.5 tutorial set (writing effort; rolls in parallel)
   │      + bug-crewai-output-leak-during-retries (independent; ship any time)
   │
   ▼
       ─── MINIMUM USABLE PRODUCT line ─── (after 2.3 + 2.5 land)
   │
   ▼
Phase 3.0 (workflow asset type + parser)
   │
   ▼
Phase 3.1 (sequential nodes — first runnable workflows)
   │
   ▼
Phase 3.2 (multi-thread engine support — hardest sub-phase)
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
- Tutorials (Phase 2.5) roll in parallel with Phase 2.3 OpenClaw + Pi
  authoring; the new agents become tutorial examples.
- Phase 4 (memory) is largely independent of Phase 5 (IDE); can be
  scheduled flexibly.
- Phase 6 (Textual polish) is fully independent of Phase 5 (IDE);
  pick based on user-feedback priority.

## Deferred design work (stub specs filed)

Small follow-ups captured during recent QoL closures. Each has a
stub spec under `process/`; pick up when the trigger condition
surfaces:

- `qol-action-namespace-bundle-scoped.md` — migrate action
  namespaces from `tool.bash` → `tool.kolsch.bash`. Trigger:
  collision between two bundles shipping a tool with the same
  name.
- `qol-tool-permissions-binding-unused.md` — wire
  `PermissionsBinding.bind(...)` so tool bodies can do
  `ctx.permissions.check(...)`. Trigger: a tool author tries the
  in-body API.
- `qol-zero-config-first-run-experience.md` — decide what
  `default_platform_payload.defaults.model` ships as. Trigger:
  the next first-time UX conversation.
- `qol-ship-canonical-kolsch-agents-as-package-resources.md` —
  make `kolsch:agents/...` actually resolve. Trigger: cross-
  install agent inheritance becomes desirable.
- `qol-skills-jinja-variable-symmetric-with-tools.md` — symmetric
  `{{ skills }}` Jinja var. Trigger: authors complain about the
  post-append asymmetry.
- `bug-framework-cancellation-mid-call-latency.md` +
  `bug-mid-tool-cancellation-latency.md` — mid-tool Esc
  responsiveness. Trigger: a user complains.

None blocking. Stubs preserve design context so a future agent
can pick up cold.

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
