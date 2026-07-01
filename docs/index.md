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
| Agent context that travels | No | Yes | Yes |
| Determinism where you need it | Yes | No | Yes |
| Judgment where you don't | No | Yes | Yes |

## Bundles

A bundle is a folder that groups agents, tools, skills, workflows,
nodes, and prompts under one manifest with its own permission rules.
Six kinds of assets under one namespace, so the agent, the tool it
calls, and the workflow that orchestrates it ship and version
together.

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

`kolsch setup` drops a starter bundle in. Adding another today means
copying a directory into `bundles/` — git clone, tar, cp.

## Principles

- **Bundles are packages, not just skill folders.** Every asset gets
  a `<bundle>/<type>/<name>` reference. See
  [Assets](architecture/assets.md).
- **Workflows are YAML graphs.** No scheduler. No hidden state.
  Every step's inputs, outputs, and errors land in the event log.
  See [Workflow foundation](explanation/workflow-foundation.md).
- **Python-first, strictly typed.** Configuration errors surface at
  load time with a filename and a Fix: line. See
  [Schemas](reference/schemas.md).
- **Local-first, multi-workspace by cd.** SQLite plus an in-process
  bus, zero external services. Postgres and Redis are opt-in. See
  [Workspace and setup](how-to/workspace-and-setup.md).
- **The event log is the source of record.** Every turn, tool call,
  and node fire appends to a JSONL log. SQLite is a derived index.
  See [Event log contract](explanation/event-log-contract.md).

## Where it is in development

Kolsch is in early development. What's not yet in place:

- Single-node executor. No distributed runtime.
- One thread per session. No user-visible branching.
- CrewAI is the only framework adapter.
- YAML shapes may still shift.
- No bundle registry. Sharing means copying the folder.
- License is TBD.

Use it for local, replayable, YAML-defined agents. Not for a
multi-tenant SaaS.

## Get started in 15 minutes

```bash
uv tool install kolsch
kolsch setup
kolsch chat kolsch/helper
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
