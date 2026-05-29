# AGENTS.md

> Operating instructions for AI agents working on this codebase. Humans:
> see `README.md` for the project overview and
> `docs/development-testing-handbook.md` for full engineering practices.
> This file is the agent's quick-start contract — read it before making
> changes.

---

## What this project is

**Kolsch** — a local-first, definition-orchestrated multi-agent workflow
platform. YAML is the source of truth; a Python/CrewAI backend executes
agents; everything is captured in an append-only event log (JSONL) that
is the ground truth. Phase 1 (current) is the single-agent foundation.

Read `docs/explanation/` for architecture and `docs/reference/` for the
Module API Reference and schemas before changing anything non-trivial.

---

## Use the code index — do not grep-and-hope

This project is indexed with **Codanna**, a local code-intelligence MCP
server. It understands the code's structure (functions, call graphs,
implementations) and supports semantic search. **Prefer Codanna queries
over raw grep** for navigating and understanding the codebase.

Typical queries:
- "Where is `append_event` called?" → call graph, not string matches.
- "Find the memory cache reconstruction logic" → semantic search by
  intent.
- "What implements the Store contract?" → implementation lookup.

Codanna also indexes the `docs/` folder, so you can query documentation
alongside code:
```
codanna mcp search_documents query:"how does context assembly work"
```

The index hot-reloads as code changes (`--watch`). When you need to
understand how something connects, query the index first. This is faster
and more accurate than reading files top-to-bottom or grepping for
names.

If the index seems stale, it can be refreshed; check
`docs/how-to/codanna.md` for refresh commands and setup.

---

## Before you change code

1. **Understand the contract.** Every subsystem has a public API defined
   in `docs/reference/module-api-reference.md`. Changes must honor it.
   If you need to change a contract, update both the reference AND the
   originating spec, and flag it in your summary.

2. **Find the affected area via Codanna.** Query the index for the
   functions and call sites you'll touch. Know what depends on what
   before editing.

3. **Check the dependency matrix** (Module API Reference §12) to see
   which modules call the one you're changing.

---

## The workflow for every change

Development happens **inside the dev container** — it's the canonical
environment, identical to CI. The host edits files (live-mounted); the
container runs everything. If your editor supports Dev Containers
(`devcontainer.json` is provided), "Reopen in Container" and work
normally. Otherwise, exec into the running container.

```bash
# One-time (or when pyproject.toml / uv.lock / Containerfile changes):
podman-compose -f compose.dev.yml up -d --build

# Edit code on the host — it's live-mounted, so no rebuild for code changes.

# Run checks INSIDE the dev container against your live edits:
podman-compose exec dev uv run ruff format .
podman-compose exec dev uv run ruff check --fix .
podman-compose exec dev uv run mypy .
podman-compose exec dev uv run pytest tests/unit          # seconds
podman-compose exec dev uv run pytest tests/component     # ~1 min
podman-compose exec dev uv run pytest tests/integration   # minutes (Fake LLM + real CrewAI)

# If you changed engine behavior, check golden diffs:
podman-compose exec dev uv run pytest tests/golden
#   Unexpected golden diff = your change altered emitted events.
#   If intentional: ... uv run pytest --update-golden, then review the diff.

# Commit gate: clean rebuild + full suite (pre-commit hook runs this).
# Do NOT trust the dev container for the final check — rebuild clean:
podman-compose -f compose.test.yml down -v
podman-compose -f compose.test.yml build --no-cache app
podman-compose -f compose.test.yml up --abort-on-container-exit --exit-code-from app

# Commit using Conventional Commits (only after the gate passes):
git commit -m "fix(store): correct parent_event_id under concurrent append"
```

The inner loop is fast because source is live-mounted — no rebuild for
code edits. The commit gate does a clean rebuild so what you commit is
verified against a pristine environment (catching "forgot to add the
dependency to pyproject.toml" and local-state bugs). CI runs the exact
same `compose.test.yml` flow, so passing the gate locally means passing
in CI.

Pre-commit hooks run the gate automatically; if blocked, fix the issue
rather than bypassing.

---

## Hard rules

These are non-negotiable. Violating them blocks merges.

- **Type hints everywhere.** `mypy --strict` must pass. No untyped public
  functions, no implicit `Any`.
- **No bare exceptions.** Raise `PlatformError` subclasses (see
  `docs/reference/error-catalog.md`). Never let a non-PlatformError
  escape a public method — wrap it.
- **The filesystem JSONL is ground truth.** Never add state that can't be
  rebuilt from the event log.
- **Tests assert on events.** For anything touching the event log, use
  the event-sequence assertion helpers and add/maintain golden tests.
- **Never burn tokens in tests.** Use the Fake LLM. Real-LLM tests are
  the E2E layer only, run deliberately, never on every change.
- **Document as you go.** A user-facing change includes its docs in the
  same change. See the Diátaxis structure in `docs/`.
- **Default to SQLite + in-process bus.** Don't introduce hard
  dependencies on Postgres or Redis; they are opt-in scale-up backends
  behind contracts.
- **LLM provider config lives in `platform.yaml`'s `llm_providers`
  section**, populated via `kolsch connect <provider>`. Never embed
  API keys in agent configs or in code paths other than the
  connector's `build_llm` method. *(Phase 1.1, LLM-R11.)*

---

## Containers: use Podman, develop inside the container

**On this machine, use `podman` explicitly** (not `docker`). The project
is Docker-compatible, so files and commands are identical, but invoke
Podman.

The dev environment is fully containerized — it's the canonical
environment (see the workflow above). You develop *inside* the dev
container (`compose.dev.yml` / `devcontainer.json`), with source
live-mounted so code edits need no rebuild.

The commit gate and CI use `compose.test.yml` — a clean rebuild with
fresh Postgres + Redis for the backend-variant tests. Most tests run
against the default tier (SQLite + in-process bus) and need no external
services; only the Postgres/Redis backend component tests do.

---

## Project layout

```
.
├── AGENTS.md                  ← you are here
├── README.md                  ← human overview
├── pyproject.toml             ← deps, tool config (ruff, mypy, pytest)
├── uv.lock                    ← committed lockfile
├── .devcontainer/
│   └── devcontainer.json      ← canonical dev environment (editor attaches here)
├── compose.dev.yml            ← dev container: live-mounted source, cached venv
├── compose.test.yml           ← commit/CI: clean rebuild, fresh PG + Redis
├── Containerfile              ← the image definition (Podman/Docker)
├── src/                       ← the platform code
│   ├── kernel/                ← pure config resolver (no I/O)
│   ├── assets/                ← asset mgmt, platform bootstrap
│   ├── store/                 ← session/run store (SQLite | Postgres)
│   ├── bus/                   ← event bus (in_process | redis)
│   ├── engine/                ← agent engine (wraps CrewAI)
│   ├── providers/             ← memory, secrets, sandbox, authz
│   ├── tools/                 ← tools subsystem + builtins
│   ├── skills/                ← skills subsystem
│   └── errors/                ← PlatformError hierarchy + catalog
├── tests/
│   ├── unit/                  ← pure, fast, no I/O
│   ├── component/             ← one subsystem + real backing
│   ├── integration/           ← multi-subsystem + Fake LLM + real CrewAI
│   ├── e2e/                   ← whole platform, real LLM (deliberate)
│   ├── golden/                ← golden event-sequence files
│   ├── fixtures/              ← factories and builders
│   └── conftest.py
└── docs/                      ← Diátaxis-structured documentation
    ├── tutorials/
    ├── how-to/
    ├── reference/
    └── explanation/
```

(Layout is the intended target; adapt as the codebase materializes.)

---

## When you finish a task

In your summary, report:
- What changed and why (one paragraph).
- Which contracts (if any) you touched — and confirm the reference +
  spec were updated.
- Test results: which layers ran, pass/fail.
- Any golden diffs and whether they were intentional.
- Any new docs added.
- Anything you couldn't resolve or that needs human judgment.

Keep the summary concrete and short. The diff shows *how*; your summary
explains *what and why*.

---

## If you're unsure

- **Contract ambiguity** → the originating spec in `docs/` is the
  arbiter; if it's silent, flag it rather than guessing.
- **Where something lives** → query Codanna.
- **Whether a change is safe** → check the dependency matrix and run the
  integration + golden tests.
- **Anything touching child safety, security boundaries, or auth** →
  surface it explicitly for human review; don't quietly decide.

Small, well-tested, well-documented changes that honor the contracts.
That's the job.
