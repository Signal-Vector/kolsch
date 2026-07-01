# Kolsch

Kolsch is a local-first harness for agents that get sharper with
use, paired with a workflow layer for the parts that shouldn't
drift.

Kolsch is two things under one runtime. It is a harness for agents
you build, extend, and re-run against your own context. It is a
YAML-defined workflow layer for the steps you want to pin down and
repeat. Both sit on the same event log, the same asset catalog, and
the same local SQLite store, so an agent you shaped last week is the
same agent a workflow calls tomorrow.

The design comes from a simple observation. Agent tools tend to be
good at open-ended one-to-one work and bad at multi-step
orchestration. Workflow tools tend to be good at deterministic
pipelines and blind to who is using them or what they already know.
Kolsch treats these as one problem. Agents accumulate skills, tools,
and prompts inside bundles you own. Workflows call those agents as
nodes when a job has enough shape to warrant a DAG.

The harness angle is deliberate. Agents in Kolsch are not one-shot
prompts. They are configurations you inherit from, extend, and pair
with your own tools and skills. A bundle is a namespaced folder that
co-locates agents, tools, skills, workflows, nodes, and prompts
under one manifest, with its own permission layer. You add to it
over time. Sessions are event-sourced JSONL logs on disk, so
resuming a conversation or replaying a run is a file read, not a
service call.

This lands between two camps. Workflow platforms like n8n and CrewAI
make it easy to wire steps together but treat the operator as a
stateless caller — there is no built-in notion of your agents, your
skills, or your accumulated context traveling with the work.
Agent-forward services in the Hermes shape do the opposite: they
hold a rich one-to-one exchange but stop short of multi-step,
branching, deterministic orchestration you can schedule against and
reason about. Kolsch keeps both. An agent you have shaped, with the
tools and skills it has picked up, is the same unit a workflow node
invokes. The workflow gives you determinism where you need it. The
agent gives you judgment where you don't. Neither side has to
pretend the other doesn't exist.

## Key principles

Five load-bearing choices worth understanding before committing time:

- **Bundles are first-class packages, not just skill folders.** A
  bundle is a namespaced directory that co-locates agents, tools,
  skills, workflows, nodes, and prompts under one manifest with its
  own permission layer. Every asset gets a canonical
  `<bundle>/<type>/<name>` ref, so shipping a capability means
  shipping the folder — no registry, no glue code. See
  [Assets](architecture/assets.md).
- **Workflows are YAML-declared, DAG-based, and event-sourced.**
  Multi-step automation is authored as a nodes-and-edges YAML file
  with explicit data binding — no scheduler, no hidden state, and
  every node's inputs, outputs, and errors land in the event log.
  The same file runs the same way on your laptop, in CI, and inside
  another agent's tool call. See the
  [workflow foundation](explanation/workflow-foundation.md).
- **Python-first and strictly typed at every boundary.** Tools,
  nodes, and manifests are Pydantic v2 models with
  `ConfigDict(extra='forbid')`, checked by `mypy --strict` and the
  full ruff ANN rule set. Configuration errors surface at load time
  with a filename and a Fix: line, not three tool calls later. See
  the [schemas reference](reference/schemas.md).
- **Local-first, with multi-workspace by cd.** The default install
  runs on SQLite and an in-process event bus with zero external
  services; Postgres and Redis are opt-in for scale, not
  prerequisites. A workspace is just a directory — switch between
  them with `--root`, `$KOLSCH_WORKSPACE`, or a shell export, no
  registry required. See
  [Workspace and setup](how-to/workspace-and-setup.md).
- **The event log is the ground truth.** Every user turn, tool call,
  node fire, and framework exchange is appended to a JSONL log that
  is replayable and diffable — the SQLite entity DB is a derived
  index, never the source of record. Sessions resume, workflows
  fork, and audits reconstruct exactly what happened from the same
  append-only stream. See the
  [event log contract](explanation/event-log-contract.md).

## Bundles

A bundle is a namespaced package that co-locates agents, tools,
skills, workflows, nodes, and prompts under one manifest — with its
own permission layer. Traditional skills are a single folder of
instructions; a bundle owns six asset types and the authz rules that
govern them, so an agent, the tool it calls, and the workflow that
orchestrates it all ship and version together. `kolsch setup` drops
a starter bundle into your workspace; adding another today means
copying a directory into `bundles/` — git clone, tar, cp, whatever
gets the folder there.

```
bundles/my-bundle/
├── bundle.yaml       # name, version, permissions
├── agents/           # *.yaml
├── tools/            # @tool Python modules
├── skills/           # SKILL.md folders
├── workflows/        # YAML DAGs
├── nodes/            # workflow node handlers
└── prompts/
```

## Where it is in development

Kolsch is in early development. What that means concretely:

- **Single-node executor.** Workflows run on one machine, one
  process. No distributed runtime.
- **One thread per session.** The event tree supports branching
  structurally, but user-visible branching isn't shipping yet.
- **CrewAI is the only real framework adapter.** The Protocol is
  designed for others; none are written.
- **YAML shapes may still shift.** The [Reference](reference/schemas.md)
  tab is the arbiter of the current shape.
- **The Python API isn't stable.** Public surface may change; the
  contracts in `docs/explanation/` are the load-bearing invariants.
- **No bundle registry.** Sharing a bundle today means shipping the
  folder — no `kolsch install`, no marketplace.
- **License is TBD.**

None of that stops you from using Kolsch today for its intended
shape — a solo developer or small team running local, replayable,
YAML-defined agents. It does mean: don't build a multi-tenant SaaS
on it yet.

## Get started in 15 minutes

If you have Python and a terminal:

```bash
uv tool install kolsch
kolsch setup
kolsch chat kolsch/helper
```

`kolsch setup` walks a provider prompt (OpenRouter recommended) and
scaffolds the starter bundle. `kolsch chat kolsch/helper` opens a
streaming REPL against the starter agent; the session lands as a
JSONL file under `.kolsch/sessions/`.

The [First agent tutorial](tutorials/first-agent.md) is the guided
version of the same three commands, with a walk through what the
JSONL looks like and how to resume a session.

---

## Documentation

### Get running

- [First agent](tutorials/first-agent.md) — the 15-minute walkthrough.
- [Connect a provider](how-to/connect-a-provider.md) — OpenRouter or
  OpenCode from an empty workspace.
- [Workspace and setup](how-to/workspace-and-setup.md) — what a
  workspace holds, switching between them, scripting for CI.

### Build

- [Build a workflow](how-to/build-a-workflow.md) — wire the 14
  built-in node types into a DAG.
- [Write a tool](how-to/write-a-tool.md) — author a `@tool`-decorated
  Python function agents can call.
- [Write a workflow node](how-to/write-a-workflow-node.md) — author
  a `@node`-decorated Python step.
- [Agent templating](how-to/agent-templating.md) — the six top-level
  variables in agent prompts.
- [Extending an agent](how-to/extending-an-agent.md) — inherit via
  `extends:`.
- [Permissions](how-to/permissions.md) — the three-layer authz
  stack.
- [Chatting effectively](how-to/chatting-effectively.md),
  [Configuring fallbacks](how-to/configure-fallbacks.md),
  [Forcing a skill](how-to/forcing-a-skill.md),
  [Finding models](how-to/find-models.md).

### Understand

- [Architecture](architecture/index.md) — the map, plus one page per
  subsystem (Agent Engine, Workflows, Tools, Skills, Assets, Kernel,
  Templating, Store, Bus, Providers, LLM Connectors, Files, Schemas,
  Errors, Platform Runtime, CLI, API).
- [Event log contract](explanation/event-log-contract.md) — assembly
  procedure, materialization rule, reconstruction guarantee.
- [Framework protocol design](explanation/framework-protocol-design.md)
  — why the engine wraps a Protocol, not a library.

### Look up

- [Schemas reference](reference/schemas.md) — every Pydantic type,
  generated from source.
- [Workflow YAML reference](reference/workflow-yaml.md) — every
  built-in node type with config schema, binds, state, and return
  shape.
- [Vocabulary](process/vocabulary.md) — which term to pick when two
  synonyms compete.
- [Documentation conventions](process/documentation-conventions.md)
  — the Google-style docstring rules.
- [API reference](api/index.md) — auto-generated from source
  docstrings.
