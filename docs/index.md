# Kolsch Documentation

Build agents, compose workflows, replay sessions. Everything is local, everything is reusable.

- **Agents** — a model, a prompt, and tools; inherit from any existing agent. [Extending an agent →](how-to/extending-an-agent.md)
- **Tools** — Python functions agents can call; write your own with a `@tool` decorator. [Write a tool →](how-to/write-a-tool.md)
- **Skills** — reusable prompt instructions that teach agents how to handle specific tasks. [Forcing a skill →](how-to/forcing-a-skill.md)
- **Workflows** — deterministic DAGs of agents, tools, and Python steps with branching and state. [Build a workflow →](how-to/build-a-workflow.md)
- **Bundles** — ship agents, tools, skills, and workflows together as one folder. [Workspace and setup →](how-to/workspace-and-setup.md)
- **Providers** — OpenRouter, OpenCode, and Claude Code; swap models without changing agent code. [Connect a provider →](how-to/connect-a-provider.md)
- **Event log** — every exchange is recorded; resume sessions, replay runs, audit tool calls. [Architecture →](architecture/index.md)
- **Permissions** — rule-based authorization with a four-choice approval modal for sensitive tools. [Permissions →](how-to/permissions.md)

## Quick example

```bash
uv tool install kolsch
kolsch setup
kolsch chat kolsch/agents/helper
```

The [first-agent tutorial](tutorials/first-agent.md) walks through these commands and shows what happens on disk.

## Documentation

**Get started**
- [First agent tutorial](tutorials/first-agent.md)
- [Connect a provider](how-to/connect-a-provider.md)
- [Workspace and setup](how-to/workspace-and-setup.md)

**Build**
- [Build a workflow](how-to/build-a-workflow.md)
- [Write a tool](how-to/write-a-tool.md)
- [Write a workflow node](how-to/write-a-workflow-node.md)
- [Agent templating](how-to/agent-templating.md)
- [Extending an agent](how-to/extending-an-agent.md)
- [Permissions](how-to/permissions.md)

**Understand**
- [Architecture](architecture/index.md)
- [Event log contract](explanation/event-log-contract.md)

**Look up**
- [Schemas reference](reference/schemas.md)
- [Workflow YAML reference](reference/workflow-yaml.md)
- [API reference](api/index.md)
