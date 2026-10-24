# Kolsch

[![ci](https://github.com/Signal-Vector/kolsch/actions/workflows/ci.yml/badge.svg)](https://github.com/Signal-Vector/kolsch/actions/workflows/ci.yml)
[![docs](https://github.com/Signal-Vector/kolsch/actions/workflows/docs.yml/badge.svg)](https://signal-vector.github.io/kolsch/)

Run and replay AI agents from your terminal.

Kolsch is a local-first agent runtime. Build agents with tools and skills, compose them into deterministic, repeatable workflows. Every component is reusable, every session replayable.

---

## Quickstart

Requires Python 3.12 or 3.13 (3.14 isn't supported yet). If uv picks 3.14, run `uv tool install --python 3.13 kolsch` instead.

```bash
uv tool install kolsch
kolsch setup                # walks you through it
kolsch chat kolsch/agents/helper
```

```
kolsch chat • helper • ses_abc123

You: hello

Helper: Hi! I'm your starter agent. I can read files, run commands,
and answer questions. What would you like to do?

You: ▍
```

## What you can do

- **Run agents locally** — no cloud service, no external database required
- **Compose deterministic workflows** — multi-step processes with branching, agent chaining, and state
- **Reuse everything** — agents, tools, skills, and workflows are portable components
- **Replay any session** — every exchange is recorded; resume conversations or trace what happened
- **Swap providers freely** — OpenRouter, OpenCode, Claude Code; change the model without changing the agent
- **Share as bundles** — one folder, one name; copy a directory to share an agent with its tools and workflows

## Key concepts

- **Agents** — a model, a prompt, and tools, defined in YAML. Agents can [inherit from other agents](docs/how-to/extending-an-agent.md).
- **Tools** — Python functions an agent can call. [Write your own](docs/how-to/write-a-tool.md) or use the built-ins.
- **Skills** — reusable prompt instructions. One skill works across many agents.
- **Workflows** — deterministic DAGs of agents, tools, and Python steps. 16 built-in node types; branch on booleans or route multi-way on a typed decision. [Build a workflow](docs/how-to/build-a-workflow.md).
- **Jev decisions** — fast, typed classify / score / yes-no judgements (TypeSafe AI's Jev, via OpenRouter) as the `jev_decide` agent tool and the `kolsch/nodes/jev` workflow node. [Use Jev](docs/how-to/use-jev.md).
- **Bundles** — a folder that ships everything together. One name, one copy, one share.

## Documentation

**Get started**
- [First agent tutorial](docs/tutorials/first-agent.md)
- [Connect a provider](docs/how-to/connect-a-provider.md)

**Build**
- [Build a workflow](docs/how-to/build-a-workflow.md)
- [Write a tool](docs/how-to/write-a-tool.md)
- [Use Jev for decisions](docs/how-to/use-jev.md)
- [Extend an agent](docs/how-to/extending-an-agent.md)
- [Permissions](docs/how-to/permissions.md)

**Understand**
- [Architecture](docs/architecture/index.md)
- [API reference](docs/api/index.md)

## Status

Kolsch is in early development. What ships today: the agent runtime, workflow engine, three LLM connectors (OpenRouter, OpenCode, Claude Code), and an MCP server.

Not yet: bundle registry (sharing means copying a folder), distributed execution (single-node only), web UI (CLI and HTTP API today).

Use it locally for replayable agent workflows. Not for multi-tenant SaaS.

## Development

```bash
./scripts/test.sh fast   # unit tests (~1s)
./scripts/test.sh slow   # component + integration + golden
./scripts/test.sh all    # everything
./scripts/test.sh cov    # CI-style with coverage
```

See [AGENTS.md](AGENTS.md) for the full development workflow, container setup, and commit gate.

## License

MIT — see [LICENSE](LICENSE).
