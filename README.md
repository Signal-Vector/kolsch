# Kolsch

A local-first, definition-orchestrated multi-agent workflow platform.
YAML is the source of truth; a Python/CrewAI backend executes agents;
everything is captured in an append-only event log (JSONL) that serves
as ground truth.

**Phase 1 (current): single-agent foundation.** Define an agent in YAML,
run it via CLI or HTTP API, watch it work through tool use, get a
result — fully replayable, inspectable, and resumable.

---

## Status

This repository currently holds the **complete Phase 1 specification
suite** — the design, contracts, API reference, engineering practices,
and test strategy that implementation is built against. Code lands on
top of these specs.

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
- **Engine as recorder, framework as driver.** The Agent Engine wraps
  CrewAI and records its activity as events; it doesn't reimplement the
  agent loop.

## Install and first run

```bash
# Install
uv tool install kolsch                # (or pipx / pip)

# First run — scaffold the workspace
kolsch setup                          # walks you through it

# Day 2 — talk to the starter agent
kolsch chat kolsch/agents/helper      # streaming REPL: tokens, tool calls, /commands

# Or one-shot
kolsch run starter/agents/helper --input content="hello"
```

`kolsch chat` is the recommended day-2 command: a streaming
conversation against the agent (token-by-token rendering,
collapsible tool-call blocks, a four-choice approval modal for
`prompt`-effect rules, slash commands like `/exit` / `/clear` /
`/model`, and `--session <id>` to resume an earlier
conversation). The full contract is in
`docs/process/phase2.1-chat-specs.md`; everyday recipes live in
`docs/how-to/chatting-effectively.md`.

`kolsch setup` scaffolds a workspace at `~/Documents/kolsch` (override
with `--root` or `$KOLSCH_WORKSPACE`), optionally connects an LLM
provider, and ships a starter agent so the first run works without
credentials.

- **If you connect a provider during setup**, the starter agent uses
  its first model — real answers from the first run.
- **If you skip the provider step**, the starter agent ships with the
  built-in **fake LLM** (echoes your input back). `kolsch run` prints
  a clearly-labelled warning to stderr so you don't mistake echoes
  for real replies. Connect a provider any time via `kolsch connect`
  and update the agent's `model:` field.

The helper agent ships with four canonical tools — `read_file`,
`write_file`, `list_files`, and `bash` — that the model can call
inside the workspace. `read_file` and `list_files` run silently
within `file_access.allowed_paths`; `write_file` and `bash` default
to `prompt` so the chat surface (Phase 2.1) asks before the agent
modifies your machine. `kolsch permissions {list, add, remove}`
edits the rule list out of chat; see `docs/how-to/permissions.md`.

Find available models with `kolsch models` (all providers) or
`kolsch models <provider>`. See `docs/how-to/workspace-and-setup.md`
for switching between workspaces and `docs/how-to/find-models.md`
for the model-discovery recipes.

## Where to start reading

Start with the documentation index: [`docs/index.md`](docs/index.md).

For the recommended reading order and a topical index of every concept,
see the [master index](docs/reference/master-index.md).

If you are an AI agent working on this codebase, read
[`AGENTS.md`](AGENTS.md) first.

## Documentation structure

Documentation follows the [Diátaxis](https://diataxis.fr/) framework:

- **`docs/explanation/`** — architecture and rationale (the specs).
- **`docs/reference/`** — schemas, the module API reference, the master
  index.
- **`docs/process/`** — engineering practices, testing strategy, the
  test catalog.
- **`docs/how-to/`** — task guides (added during implementation).
- **`docs/tutorials/`** — learning paths (added during implementation).

## Development

### Running tests

`scripts/test.sh` is the canonical entry point — it splits the suites
by their parallelism profile so each one runs in the cheapest way:

```bash
./scripts/test.sh fast   # tests/unit only, serial (~1s)
./scripts/test.sh slow   # component + integration + golden, -n auto (~32s)
./scripts/test.sh all    # fast then slow (~34s total for ~1,900 tests)
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

TBD.
