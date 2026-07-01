# Kolsch

Local-first, YAML-driven multi-agent runtime. Define an agent in a
YAML file. Run it locally on your machine. Every step lands on a
JSONL event log you can read.

Under the hood: a Python engine, an in-process event bus, a SQLite
entity store, and a first-class DAG workflow layer. LLM calls go
through pluggable connectors (OpenRouter, OpenCode). CrewAI is the
framework adapter today; the engine talks to a `Framework` Protocol,
so the framework is swappable.

Kolsch is a runtime plus a YAML config format plus a JSONL event
log. It isn't a hosted service, a managed cloud, or a UI. It isn't
a replacement for CrewAI — it wraps CrewAI. It isn't a workflow
engine competing with n8n at scale — it's a workflow layer for
multi-agent chains that keeps everything replayable on disk.

## Key principles

Four load-bearing choices worth understanding before committing time:

- **The event log is the ground truth.** Every model call, tool
  invocation, and workflow step lands on a JSONL file per session.
  The SQLite entity store is a rebuildable index of that log; drop
  it and the log reconstructs the world. See the
  [event log contract](explanation/event-log-contract.md).
- **YAML is the source of truth for configuration.** Agents, tools,
  skills, and workflows are files under `bundles/<bundle>/`. Nothing
  hidden in a DB, nothing baked into code paths. See
  [Assets](architecture/assets.md).
- **Local-first.** SQLite plus an in-process event bus by default.
  No cloud service required to run; no phone-home. Postgres and
  Redis are contract-level extension points, not shipping backends
  today.
- **The framework is swappable behind a Protocol.** The engine
  depends on the `Framework` interface, not on CrewAI directly.
  CrewAI is one implementation; a `FakeFramework` ships for tests
  and offline runs. See the
  [framework protocol design](explanation/framework-protocol-design.md).

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
