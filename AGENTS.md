# AGENTS.md

> Operating instructions for AI agents working on this codebase. Humans:
> see `README.md` for the project overview and `docs/` for full
> engineering practices. This file is the agent's quick-start contract —
> read it before making changes.

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
   If you need to change a contract, update the reference in the same
   change and flag it in your summary.

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
podman-compose exec dev uv run pytest tests/integration   # minutes (Mock LLM + real CrewAI)

# If you changed engine behavior, check golden diffs:
podman-compose exec dev uv run pytest tests/golden
#   Unexpected golden diff = your change altered emitted events.
#   If intentional: ... uv run pytest --update-golden, then review the diff.

# Full gate: clean rebuild + whole suite + coverage (the pre-push hook
# runs this on a push to `main`). Do NOT trust the dev container for the
# final check — rebuild clean. Easiest is the wrapper:
scripts/commit-gate            # cached build
scripts/commit-gate --clean    # pristine --no-cache rebuild
# ...which is equivalent to:
podman-compose -f compose.test.yml down -v
podman-compose -f compose.test.yml build --no-cache app
podman-compose -f compose.test.yml up --abort-on-container-exit --exit-code-from app

# Commit using Conventional Commits (only after the gate passes):
git commit -m "fix(store): correct parent_event_id under concurrent append"
```

The inner loop is fast because source is live-mounted — no rebuild for
code edits. The full gate does a clean rebuild so what you commit is
verified against a pristine environment (catching "forgot to add the
dependency to pyproject.toml" and local-state bugs).

### Two-tier gate (qol-125)

The test gate is split into two scopes of the **same**
`compose.test.yml` container (both run ruff → ruff-format → mypy →
chat-hex lint first, fail-fast), selected by `GATE_SCOPE`:

- **`full`** (default) — the whole `tests/` tree WITH coverage and the
  **85% floor**. This is the hard landing gate. It runs **locally** at
  push-to-`main` via the `.githooks/pre-push` hook (which calls
  `scripts/commit-gate`), and blocks the push on any failure. Run it by
  hand anytime with `scripts/commit-gate` (cached) / `--clean` (pristine).
- **`sanity`** — the whole tree **minus** the two heavy dirs
  (`tests/cli`, `tests/component`), **no** coverage. This is what
  **GitHub CI** runs on every push (~3 min vs ~16 min). `tests/golden`
  and `tests/security` DO run in sanity — only the two heavy tiers and
  the coverage floor are dropped.

Development reaches `main` via local `git merge` + `git push origin
main`, so the pre-push hook is the enforcement point. One-time setup per
clone is the same `git config core.hooksPath .githooks` documented in
`.githooks/README.md` — without it, local pushes to `main` are ungated.

### Risks & Non-Guarantees (accepted)

The full gate is **local-only** — there is deliberately **no GitHub
backstop**. Known, accepted consequences:

1. **`git push --no-verify` skips the full gate.** A `--no-verify` push
   to `main` lands unvalidated. Intended escape hatch, not a bug.
2. **A fresh clone / second machine has no enforcement** until
   `git config core.hooksPath .githooks` is run. Until then, pushes to
   `main` are ungated locally.
3. **The 85% coverage floor and the two heavy tiers (`cli`,
   `component`) are not on GitHub.** `golden` and `security` still run on
   GitHub every push; the coverage floor and the two heavy tiers do not.
   A coverage regression or a `cli`/`component` regression is invisible
   on GitHub until the local full gate runs at push-to-`main`.
4. **The `pull_request` trigger runs only the sanity subset.** If code
   ever reaches `main` via a GitHub PR merge (rather than a local push),
   the full gate never fires and no branch protection requires the hook.
5. **CI green now means "sanity passed," not "full gate passed."** Do
   not treat a green `ci` badge as "safe to build on."

If the gate blocks a push, fix the issue rather than bypassing.

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
- **Never burn tokens in tests.** Use the Mock LLM. Real-LLM tests are
  the E2E layer only, run deliberately, never on every change.
- **Document as you go.** A user-facing change includes its docs in the
  same change. See the Diátaxis structure in `docs/`.
- **Default to SQLite + in-process bus.** Don't introduce hard
  dependencies on Postgres or Redis; they are opt-in scale-up backends
  behind contracts.
- **LLM provider config lives in `platform.yaml`'s `llm_providers`
  section**, populated via `kolsch connect <provider>`. Never embed
  API keys in agent configs or in code paths other than the
  connector's `build_llm` method.
- **Workspace location is resolved by
  `platform_runtime.workspace.resolve_workspace`.** No other module
  reads `KOLSCH_WORKSPACE` or constructs the default path. The CLI
  emits a friendly message (not a stack trace) when the workspace
  is uninitialized.
- **Every new `model.*` error code MUST declare Recoverable or
  Fatal in the same change that introduces it,** and update the
  fallback trigger table in `docs/explanation/error-model.md`
  §4.15 (the canonical "does this trigger fallback" surface).
  The classification is what the engine's
  `is_fallback_triggering` predicate consults; a new code with
  no declared level silently defaults to "no fallback", which
  is wrong-by-default for any genuinely transient condition.

- **Tools shipped by the platform live in `bundles/kolsch/tools/`
  as plain Python files.** Never embed a "builtin tool" in the
  engine or runtime; the file IS the tool, and users can read it,
  copy it as a template, or swap it. The Tools subsystem catalog
  discovers them via the same scan path that finds any
  bundle-supplied declared tool. If you find yourself adding a
  case branch like `if tool_name == "read_file": …` inside
  `src/engine/` or `src/tools/`, you have violated this rule —
  add the tool file instead.
- **The production Authorization rule set must end with a
  default-deny tail.** The `allow_all_test` variant is for tests
  only — it is registered ONLY when `KOLSCH_TEST_BUILD=1` and
  production builds refuse to load it. Removing
  `rl_default_deny` from `platform.yaml` directly OR via
  `kolsch permissions remove` is rejected (the runtime
  bootstrap refuses to load a rule set without the tail; the
  CLI raises `authz.protected_rule`).
- **Every new CLI command or subcommand requires a smoke test
  in `tests/component/test_cli_smoke.py`.** No exceptions. The
  smoke test invokes the CLI in a fresh subprocess
  (`subprocess.run([sys.executable, "-m", "cli", ...])`) — NOT
  via in-process `cli.main(argv)` — because the point is to
  catch the class of parser-to-dispatcher wiring bugs that only
  surface when the CLI is actually spawned (the
  `bug-sessions-namespace-attr-error.md` crash being exhibit A).
  Coverage is mechanically enforced by
  `test_smoke_coverage_enumerates_every_top_level_subcommand`:
  add a command to `cli.main._DISPATCH` without adding it to the
  smoke module's `_TOP_LEVEL_SUBCOMMANDS` tuple and the test
  refuses to pass. The catalog reference is Part 11.6.1. **CI runs the
  suite with `pytest -n auto`** (pytest-xdist) to keep it
  under 10s — see Part 11.6.1 for the rationale; serial
  invocation is ~36s.
- **The chat renderer is a pure consumer of the engine's event
  stream.** The renderer MUST NOT invent UI state that isn't
  derivable from events. If a UI element needs information the
  events don't carry, the engine grows the event; the chat
  doesn't fork state. The four-choice approval modal that
  consumes Phase 2.0's prompt callback is the canonical example:
  it returns its decision through the callback's tuple shape so
  Phase 2.0's authz provider — not the chat — appends the new
  rule and rewrites `platform.yaml`. The chat also MUST NOT
  enter full-screen / alternate-screen mode. It renders
  **inline** (via Textual's `App(inline=True)` today) so
  terminal-native scrolling and selection keep working.
  TC-CHAT-18 enforces this by AST-parsing every `.py` file
  under `src/cli/chat/` and rejecting any `App(...).run(...)`
  or `App(...).run_async(...)` call whose kwargs do not include
  `inline=True`, and by continuing to reject the legacy
  `Application.run(full_screen=True)` idiom. Approvals are the
  one carve-out to the "pure consumer" rule: the four-choice
  callback registered via `authz.register_prompt_callback`
  fires from a worker and mounts an inline widget; this is a
  pre-existing carve-out documented at `src/cli/_modal.py:168`.
- **Agent prompts are Jinja2 templates.** Variable names are
  resolved via the standardized API (`tools`, `docs`,
  `workspace`, `agent`, `platform`) defined in
  `process/phase2.2-agent-envelope-specs.md` §4. Typo'd variables
  raise `config.template_render_failed` at agent-load
  (StrictUndefined). The five top-level variables expose the
  standardized method family (`.list()`, `.summary()`,
  `.names()`, `.get(name)`, `.count` where the underlying object
  is list-shaped); see Schemas §A.11 for the full API. Plain-text
  prompts (no `{{`, `{%`) render byte-identical to themselves —
  every Phase 1.x agent continues to work unchanged.
- **Never put secrets in `platform.yaml`.** The `platform`
  variable in agent prompts can read it (via the Jinja render
  context), so anything in `platform.yaml` is reachable from
  every agent the workspace runs. `platform.yaml` is for
  non-sensitive configuration only: provider topology, defaults,
  authz rules, file-access lists, workspace_summary text. Secrets
  live in `.env` per Phase 1.1's `dotenv` provider; agent code
  reaches them via `ctx.secrets` after the Secrets provider
  resolves names listed in `@tool(secrets_needed=[...])`.
- **Multi-spec refactors require a composition invariant and a
  live-runtime gate at each milestone.** Component-scoped specs
  cannot ship a working feature on their own — the seams between
  them (bus subscription, service-registry injection, adapter
  handler registration) are load-bearing and easy to leave
  unwired. Every refactor spanning ≥ 2 specs owes:
    (a) an invariant in `process/design-decisions-ledger.md` of
        the form _"no spec in this refactor ships until a real
        Pilot ChatApp (or equivalent live-runtime harness) proves
        the composed pipeline surfaces the feature end-to-end"_;
    (b) a **milestone smoke check** — actually boot the shipped
        surface and use the feature — before flipping the
        milestone gate. Green component tests are necessary, not
        sufficient. If your tests use a `_replay_*` /
        `_drain_events_manually` / `handle_directly(...)`
        scaffold to simulate the live path, that scaffold is a
        documented gap, not a shortcut — promote it to a blocker
        and wire the live path instead of shipping around it.
    Exhibit A: the 19-spec Textual chat refactor (specs
    001-019, 2026-07). Shipped green with 2713 passing tests, but
    invoking a workflow in the running chat produced no
    `WorkflowCard` because two seams (tool-dispatch
    `chat_services=` forwarding, runtime bus subscription) were
    never wired. Bug-020 documents the fix; this rule prevents
    the recurrence.


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
│   ├── integration/           ← multi-subsystem + Mock LLM + real CrewAI
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
- Which contracts (if any) you touched — and confirm the reference
  was updated.
- Test results: which layers ran, pass/fail.
- Any golden diffs and whether they were intentional.
- Any new docs added.
- Anything you couldn't resolve or that needs human judgment.

Keep the summary concrete and short. The diff shows *how*; your summary
explains *what and why*.

---

## If you're unsure

- **Contract ambiguity** → `docs/reference/module-api-reference.md`
  is the arbiter; if it's silent, flag it rather than guessing.
- **Where something lives** → query Codanna.
- **Whether a change is safe** → check the dependency matrix and run the
  integration + golden tests.
- **Anything touching child safety, security boundaries, or auth** →
  surface it explicitly for human review; don't quietly decide.

Small, well-tested, well-documented changes that honor the contracts.
That's the job.
