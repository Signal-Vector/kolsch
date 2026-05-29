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

## License

TBD.
