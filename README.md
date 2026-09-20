# Kolsch

[![ci](https://github.com/Signal-Vector/kolsch/actions/workflows/ci.yml/badge.svg)](https://github.com/Signal-Vector/kolsch/actions/workflows/ci.yml)
[![docs](https://github.com/Signal-Vector/kolsch/actions/workflows/docs.yml/badge.svg)](https://signal-vector.github.io/kolsch/)

Kolsch is a local-first, Python-native agent harness. Define an
agent in YAML, run it from a CLI, an HTTP API, or a workflow step —
every exchange is captured to an append-only JSONL event log you can
replay and resume byte-for-byte (see the
[event log contract](docs/explanation/event-log-contract.md)).

Two things anchor the design:

- **Bundles.** An agent, the tools it calls, the skills it loads, and
  the workflows that orchestrate it ship together as one folder with
  one name. Extend someone else's agent, reuse someone else's tool,
  share the whole thing by copying a directory.
- **DAG workflows.** Deterministic multi-step processes — not just
  single-agent chat — where each step can call an agent, run Python,
  read or write state, or invoke another workflow.

---

## Status

Early but real. What ships today: the single-agent foundation, the
workflow engine (agents and Python steps composed into DAGs), three
framework adapters (a built-in minimal loop by default, opt-in CrewAI,
and an external-harness subprocess adapter), three LLM connectors
(OpenRouter, OpenCode, and Claude Code), and an MCP operator server.
Still moving: agent and workflow YAML shapes may still change between
releases, so expect to update files when you upgrade. Sharing a
bundle (a packaged set of agents, tools, and workflows) today means
copying a directory; there's no registry yet. Use it locally — not
for a multi-tenant SaaS.

## Design at a glance

- **Local-first, zero-dependency default.** Runs on SQLite + an
  in-process event bus. No external services required to start. Postgres
  and Redis are opt-in scale-up backends.
- **Event-sourced.** Per-session JSONL logs are ground truth; every other
  datastore is rebuildable from them.
- **Modular behind contracts.** Engine, providers (memory, secrets,
  sandbox, authorization), and subsystems (asset management, store, bus,
  tools, skills) each sit behind a contract so implementations can be
  swapped or extended without rewrites.
- **Engine as recorder, framework as driver.** The Agent Engine wraps a
  framework adapter and records its activity as events; it doesn't
  reimplement the agent loop. The default is a built-in minimal loop;
  CrewAI is an opt-in alternative; and a subprocess adapter drives an
  external harness (e.g. the Claude Code CLI) through the same contract.

## Install and first run

```bash
# Install
uv tool install kolsch                # (or pipx / pip)

# First run — scaffold the workspace
kolsch setup                          # walks you through it

# Day 2 — talk to the starter agent
kolsch chat kolsch/agents/helper      # streaming REPL: tokens, tool calls, /commands

# Or one-shot
kolsch run kolsch/agents/helper --input content="hello"
```

`kolsch/agents/helper` is a bundle reference: `kolsch` is the
starter bundle `setup` installed, `agents/helper` is the agent
inside it. Every agent, tool, skill, and workflow resolves the same
way — `<bundle>/<type>/<name>`.

`kolsch chat` is the recommended day-2 command: a streaming
conversation against the agent (token-by-token rendering,
collapsible tool-call blocks, a four-choice approval modal for
`prompt`-effect rules, slash commands like `/exit` / `/clear` /
`/model`, and `--session <id>` to resume an earlier
conversation). Everyday recipes live in
`docs/how-to/chatting-effectively.md`.

`kolsch setup` scaffolds a workspace at `~/Documents/kolsch` (override
with `--root` or `$KOLSCH_WORKSPACE`), optionally connects an LLM
provider, and ships a starter agent so the first run works without
credentials.

- **If you connect a provider during setup**, the starter agent uses
  its first model — real answers from the first run.
- **If you skip the provider step**, the starter agent ships with the
  built-in **mock LLM** (echoes your input back). `kolsch setup`
  prints a clearly-labelled notice so you don't mistake echoes for
  real replies. Connect a provider any time via `kolsch connect` and
  update the agent's `model:` field.

The helper agent ships with four canonical tools — `read_file`,
`write_file`, `list_files`, and `bash` — that the model can call
inside the workspace. `read_file` and `list_files` run silently
within `file_access.allowed_paths`; `write_file` and `bash` default
to `prompt` so the chat surface asks before the agent modifies your
machine. `kolsch permissions {list, add, remove, show}` edits the
rule list out of chat; see `docs/how-to/permissions.md`.

Find available models with `kolsch models` (all providers) or
`kolsch models <provider>`. See `docs/how-to/workspace-and-setup.md`
for switching between workspaces and `docs/how-to/find-models.md`
for the model-discovery recipes.

## Documentation

The full documentation renders at
[signal-vector.github.io/kolsch](https://signal-vector.github.io/kolsch/).
Fastest way in from here:

- **New to Kolsch?** [First agent](docs/tutorials/first-agent.md) (a
  10-minute walkthrough) or
  [Connect a provider](docs/how-to/connect-a-provider.md).
- **Building something?** [Build a workflow](docs/how-to/build-a-workflow.md),
  [Write a tool](docs/how-to/write-a-tool.md),
  [Extending an agent](docs/how-to/extending-an-agent.md).
- **Understanding how it works?**
  [Architecture overview](docs/architecture/index.md),
  [Event log contract](docs/explanation/event-log-contract.md).
- **Looking something up?** [Schemas](docs/reference/schemas.md),
  [Workflow YAML](docs/reference/workflow-yaml.md),
  [API reference](docs/api/index.md).

Everything above lives under [`docs/`](docs/index.md), organized by
the [Diátaxis](https://diataxis.fr/) framework: tutorials to learn,
how-to guides to build, architecture/explanation to understand,
reference to look up.

If you are an AI agent working on this codebase, read
[`AGENTS.md`](AGENTS.md) first.

## Development

### Running tests

`scripts/test.sh` is the canonical entry point — it splits the suites
by their parallelism profile so each one runs in the cheapest way:

```bash
./scripts/test.sh fast   # tests/unit only, serial (~1s)
./scripts/test.sh slow   # component + integration + golden, -n auto
./scripts/test.sh all    # fast then slow
./scripts/test.sh cov    # CI-style invocation with coverage on
```

Why split: `tests/unit` is fast serial and xdist adds ~2s of worker
startup overhead. `tests/component` is dominated by ~10 CLI-chat tests
where each takes 15-40s, and parallelism is a 12× win there. A single
`-n auto` over both is the worst of both.

Extra args pass through to pytest, so `./scripts/test.sh fast -k some_name`
and `./scripts/test.sh slow tests/component/test_foo.py` both work.

Coverage is **not** in the default pytest `addopts` (it triples wall
time and the `fail_under=85` floor makes partial-suite runs fail).
The `cov` mode adds it back when you want to reproduce CI numbers.

## License

MIT — see [LICENSE](LICENSE).
