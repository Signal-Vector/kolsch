# Kolsch

Kolsch is a local Python runtime where the agents you build and the
workflows that call them share one context, one event log, one
package format.

---

## What it is

- **Agents.** Configurations you inherit from, extend, and pair with
  your own tools and instructions.
- **Workflows.** YAML files that describe a graph of steps. Steps
  can call agents, run Python, read and write state, or invoke other
  workflows.

Both live in the same catalog. Both run on the same local runtime.
Every session lands as a plain JSONL log on disk.

## Where it sits

Agent tools handle open-ended one-to-one chat but not multi-step
orchestration. Workflow tools handle deterministic pipelines but
know nothing about the person running them. Kolsch is one runtime
for both.

|  | Workflow tools (n8n, CrewAI) | Agent chat tools | Kolsch |
|---|---|---|---|
| Multi-step orchestration | Yes | No | Yes |
| Agent context that travels between steps | No | Yes | Yes |
| Extend an agent you already built | No | Partial | Yes |
| Reuse the same tool across agents and workflows | Partial | No | Yes |
| Resume a conversation from last week | No | Partial | Yes |
| Runs locally with no external services | Partial | No | Yes |

## Bundles

A bundle is a shippable capability. You get a working agent, the
tools it calls, the skills it loads, the workflows that orchestrate
it, and the permission rules that govern all of it — as one folder
with one name.

That means:

- **Extend someone else's agent.** Point `extends:` at another
  bundle's agent and inherit its instructions, tools, and skills.
  Override what you want, keep the rest.
- **Reuse tools across agents.** A tool written in one bundle is
  callable from any agent in any bundle, subject to permissions.
- **Bundles don't share permissions.** A new bundle's tools don't
  inherit rules you granted for other tools — installing one can't
  silently widen what your existing agents can do.
- **One reference shape for everything.** Agents, tools, skills,
  workflows, and nodes all resolve as `<bundle>/<type>/<name>` —
  e.g. `kolsch/agents/helper`.

```
bundles/my-bundle/
├── bundle.yaml       # name, version, permissions
├── agents/           # *.yaml
├── tools/            # @tool Python modules
├── skills/           # SKILL.md folders
├── workflows/        # YAML DAGs
└── nodes/            # workflow node handlers
```

`kolsch setup` drops a starter bundle in. Adding another today means
copying a directory into `bundles/` — git clone, tar, cp.

## Principles

- **Extend and compose.** Inherit from any agent, reuse any tool,
  build on any bundle. Your work stacks on top of what already
  exists instead of forking it.
- **Strictly typed configuration.** Mistakes in a YAML file surface
  at load time with the filename, the field, and a Fix: line — not
  as a stack trace three steps into a run. See
  [Schemas](reference/schemas.md).
- **Workflows compose your agents, tools, and bundle assets.** A
  workflow step can call any agent you've built, any tool you've
  written, and any asset any bundle exposes — all under the same
  reference shape.
- **Runs on your machine.** No cloud service, no external database
  required. See [Workspace and setup](how-to/workspace-and-setup.md).
- **Resume any conversation, replay any run.** Every turn, tool
  call, and node fire is recorded, so you can pick a session back
  up or audit exactly what happened. See
  [Event log contract](explanation/event-log-contract.md).

## Where it is in development

Kolsch is in early development. What's not yet in place:

- Single-node executor. No distributed runtime.
- One thread per session. No user-visible branching.
- YAML shapes may still shift.
- No bundle registry. Sharing means copying the folder.

Use it for local, replayable, YAML-defined agents and workflows. Not
for a multi-tenant SaaS.

Kolsch is MIT-licensed; see the `LICENSE` file in the repository
root.

## Get started in 15 minutes

```bash
uv tool install kolsch
kolsch setup
kolsch chat kolsch/agents/helper
```

The [first-agent tutorial](tutorials/first-agent.md) walks the same
three commands and shows the JSONL.

---

## Documentation

### Get running

- [First agent](tutorials/first-agent.md)
- [Connect a provider](how-to/connect-a-provider.md)
- [Workspace and setup](how-to/workspace-and-setup.md)

### Build

- [Build a workflow](how-to/build-a-workflow.md)
- [Write a tool](how-to/write-a-tool.md)
- [Write a workflow node](how-to/write-a-workflow-node.md)
- [Agent templating](how-to/agent-templating.md)
- [Extending an agent](how-to/extending-an-agent.md)
- [Permissions](how-to/permissions.md)

### Understand

- [Architecture](architecture/index.md)
- [Event log contract](explanation/event-log-contract.md)
- [Framework protocol design](explanation/framework-protocol-design.md)

### Look up

- [Schemas reference](reference/schemas.md)
- [Workflow YAML reference](reference/workflow-yaml.md)
- [Vocabulary](process/vocabulary.md)
- [API reference](api/index.md)
