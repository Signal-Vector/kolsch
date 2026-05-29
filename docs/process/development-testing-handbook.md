# Development & Testing Handbook

**Draft 0.1**

> How we build and verify this codebase. Engineering principles, Python
> practices, tooling, linting, git conventions, documentation discipline,
> and the full testing strategy — methodology, scaffolding, process, and
> per-subsystem success criteria.
>
> This is the document a new contributor (human or agent) reads to know
> how to work on the project. For how an AI agent specifically operates
> on this codebase, see `AGENTS.md` at the repo root.

---

## Part 1 — Engineering Principles

Five principles that govern every decision in the codebase.

### 1.1 Pragmatic, modular, refactorable

Choose concrete implementations and ship them. Don't abstract for the
sake of abstraction. But put concrete choices behind contracts so the
next phase can swap or extend without rewriting. (This is the same
principle that governs the architecture; it governs the code too.)

### 1.2 Mature with scale

Start with the simplest thing that works for a single-user local
install. Add infrastructure only when scale demands it. The clearest
example: the platform runs on **SQLite with an in-process event bus by
default** — zero external services. Postgres and Redis are opt-in
backends adopted when data volume or multi-process concurrency genuinely
require them. (See §6 for the storage tiers.) The same philosophy
applies everywhere: don't make the user run a database cluster to try
out one agent.

### 1.3 Filesystem is ground truth

Events on disk (JSONL) are the durable record. Every other datastore —
SQLite entity records, caches, in-memory state — is rebuildable from the
log. Code must honor this: no state that can't be reconstructed from the
event log.

### 1.4 The event log is the spine

Most of the system's correctness reduces to "the right events were
emitted, in the right order, with the right content." Code that emits
events is the code that matters most. Tests assert against event
sequences. Changes to event-emitting code are the changes to scrutinize
hardest.

### 1.5 Readable beats clever

This is a codebase others will extend (and that agents will modify).
Favor clarity over cleverness. A junior contributor or an AI agent
should be able to read a module and understand it without tracing
through indirection. Explicit over implicit, named over anonymous,
boring over surprising.

### 1.6 The container is the canonical environment

Development happens *inside* a container, not on the host. The container
defines the one true environment — Python version, system libraries,
tools, dependencies — and it is bit-for-bit identical to what CI runs.
The host's job is to edit files; the container's job is to run
everything (linting, type-checking, tests, the platform itself).

This eliminates "works on my machine": what passes locally passes in CI,
because they are the same environment. Source code is live-mounted into
the container (so editing is instant, no rebuild), but execution is
always in-container. See §8.2 for the dev/test container model and §9
for how the inner loop and the commit gate use it.

---

## Part 2 — Python Practices

### 2.1 The baseline

- **Python 3.12+.** Use modern syntax: structural pattern matching where
  it clarifies, `|` union types, built-in generics (`list[str]` not
  `List[str]`).
- **Type hints everywhere.** Every function signature, every class
  attribute. No untyped public functions. `mypy --strict` passes (§4).
- **Pydantic v2 for all data models.** Config, events, entity records,
  API request/response shapes. Pydantic gives validation, serialization,
  and schema export for free. Don't hand-roll dataclasses where a
  Pydantic model fits.
- **Async-first.** I/O-touching code is `async`. Pure logic is sync.
  Don't mix blocking I/O into async paths (it stalls the event loop).
- **Dependency injection over globals.** Modules receive their
  dependencies (the store, the bus, providers) as constructor arguments
  or function parameters. No module-level singletons reaching across the
  codebase. This is what makes testing with mocks straightforward.

### 2.2 Structure

- **One module, one responsibility.** A file should do one thing. The
  Memory provider's `recent_buffer` variant is one module; the Tools
  dispatch logic is another.
- **Contracts as Protocols or ABCs.** Each subsystem's public contract
  (per the Module API Reference) is expressed as a `typing.Protocol` or
  abstract base class. Implementations conform; tests mock against the
  protocol.
- **Errors are PlatformError subclasses.** Per the Error Model spec.
  Never raise bare `Exception`. Never let a non-PlatformError escape a
  public method (wrap it).
- **No circular imports.** The dependency matrix (Module API Reference
  §12) defines the call graph; imports follow it. The Kernel imports
  nothing platform-specific; the Agent Engine imports much. If you find
  a circular import, the module boundaries are wrong.

### 2.3 What to avoid

- No `from module import *`.
- No mutable default arguments.
- No bare `except:` — catch specific exceptions.
- No business logic in `__init__.py` files (they're for exports).
- No `print()` for diagnostics — use the logging framework (§7.4).
- No swallowed exceptions — if you catch and don't re-raise, log why.

---

## Part 3 — Tooling

The committed stack. These are not suggestions; they're the project's
tools.

### 3.1 `uv` — package and environment management

[uv](https://github.com/astral-sh/uv) manages the virtualenv,
dependencies, and lockfile.

- `uv sync` — create/update the environment from the lockfile.
- `uv add <package>` — add a dependency (updates `pyproject.toml` and
  `uv.lock`).
- `uv add --group dev <package>` — add a dev-only dependency.
- `uv run <command>` — run a command in the project environment.
- `uv.lock` is committed. The environment is reproducible from it.

Dependency groups:
- `dev` — linting, type-checking, pre-commit.
- `test` — pytest and testing infrastructure.
- `docs` — documentation tooling.

The default (non-grouped) dependencies are the platform's runtime
requirements only — kept minimal so the install is light.

### 3.2 `ruff` — linting and formatting

[ruff](https://github.com/astral-sh/ruff) replaces flake8, isort, black,
and most of pylint. One fast tool.

- `uv run ruff check .` — lint.
- `uv run ruff check --fix .` — lint and auto-fix.
- `uv run ruff format .` — format.

Configured strict in `pyproject.toml`: a broad rule set enabled
(pycodestyle, pyflakes, isort, bugbear, comprehensions, pyupgrade,
and more). The config is part of the repo; don't weaken it without
discussion.

### 3.3 `mypy` — type checking

`mypy --strict` with the Pydantic plugin enabled.

- `uv run mypy .` — type-check the whole project.

Strict mode means: no implicit `Any`, no untyped defs, no untyped
calls, warns on unreachable code, etc. Type checking is not optional
and not a separate "later" pass — it runs on every change (§4).

### 3.4 `pytest` — testing

[pytest](https://docs.pytest.org/) with:
- `pytest-asyncio` — async test support.
- `pytest-cov` — coverage measurement.

- `uv run pytest` — run the suite.
- `uv run pytest tests/unit` — run one layer.
- `uv run pytest -k "memory"` — run tests matching a pattern.
- `uv run pytest --cov` — with coverage.

### 3.5 `pre-commit` — enforce checks before commit

[pre-commit](https://pre-commit.com/) runs ruff and mypy on changed
files before each commit. Installed via `uv run pre-commit install`.
This makes "lint every change" mechanical rather than a discipline
someone has to remember.

### 3.6 Containers — Podman (Docker-compatible)

Infrastructure for tests and local scale-up runs in containers. The
project is written against the Docker-compatible container API, so it
works identically with **Podman** and **Docker**.

**On this project's primary development machine, use Podman explicitly.**
Documentation examples show `podman` and `podman-compose`. Docker users
can substitute `docker` / `docker compose` — the commands and files are
identical (many contributors alias `docker=podman`).

Containers are not just for test infrastructure — **the entire
development environment is containerized** (per principle §1.6). A
`devcontainer.json` defines the canonical environment; contributors
develop inside it. See §8.2 for the full dev/test container model.

---

## Part 4 — Linting & Formatting: Every Change, Every Time

The rule: **code is linted, formatted, and type-checked on every change.**
Not at PR time, not "before release" — every change.

Enforced at three points:

1. **Editor (inside the dev container).** The editor attaches to the dev
   container via `devcontainer.json`, so ruff-on-save and inline mypy run
   in the canonical environment. (Setup notes in `docs/how-to/`.)
2. **Pre-commit hook.** `pre-commit` runs ruff + mypy on staged files
   before the commit completes. A failing check blocks the commit.
3. **CI.** Every push runs the full `ruff check`, `ruff format --check`,
   and `mypy` across the whole project. A failure blocks the merge.

The three layers are redundant on purpose: the editor gives instant
feedback, pre-commit catches what the editor missed, CI catches what
slipped past pre-commit (e.g., someone bypassed the hook).

No merge proceeds with lint, format, or type errors. This is a hard
gate.

---

## Part 5 — Git & Commits

### 5.1 Branch model

- `main` is always releasable. Lint, types, and tests pass on `main` at
  all times.
- Work happens on short-lived feature branches off `main`.
- Branches merge via pull request after CI passes and review.

Keep branches small. A branch that touches one subsystem and its tests
is reviewable; a branch that rewrites half the platform is not.

### 5.2 Commit message guideline

Use [Conventional Commits](https://www.conventionalcommits.org/) — a
small, well-known convention that makes history readable and enables
tooling (changelog generation later).

Format:

```
<type>(<scope>): <short summary>

<optional body explaining what and why, not how>

<optional footer: breaking changes, issue refs>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`.

Scope: the subsystem or area — `kernel`, `store`, `engine`, `tools`,
`memory`, `docs`, etc.

Examples:

```
feat(memory): add recent_buffer tag filtering

fix(store): correct parent_event_id under concurrent append

test(engine): add golden event-sequence test for tool failure

docs(tools): how-to guide for writing a custom tool
```

Rules:
- Summary in imperative mood ("add", not "added"), under ~70 chars.
- The body explains *what changed and why* — the diff shows *how*.
- One logical change per commit. Don't bundle a refactor with a feature.
- Reference issues in the footer (`Refs: #42`) when relevant.

### 5.3 What not to commit

- Secrets (`.env` is gitignored; never commit real secret values).
- Generated artifacts (the SQLite db, session logs, content store,
  `.platform/` caches — all gitignored).
- Large binary files.
- Commented-out code (delete it; git remembers).

---

## Part 6 — Storage Tiers (Mature With Scale)

The platform's datastores are modular backends, selected in
`platform.yaml`. Start minimal; scale up only when needed.

### 6.1 The three tiers

**Tier 1 — SQLite + in-process bus (default).**
- Entity records (Session, Thread) → SQLite at
  `.platform/entities.db`.
- Event bus → in-process asyncio pub/sub. No external broker.
- Ground truth → filesystem JSONL (always, all tiers).
- **Zero external services.** `uv run kolsch` works with nothing
  else installed or running.

**Tier 2 — SQLite + Redis.**
- Entity records → still SQLite.
- Event bus → Redis (for cross-process delivery, real-time fan-out to
  the IDE or external subscribers).
- Adopted when: you have actual subscribers needing cross-process
  events.

**Tier 3 — Postgres + Redis.**
- Entity records → Postgres (when SQLite's single-writer model or data
  volume becomes a real constraint).
- Event bus → Redis.
- Adopted when: scale genuinely demands it (Phase 3 / hosted).

### 6.2 Why this is clean

Both datastores already sit behind contracts:
- The Session/Run Store contract (`create_session`, `append_event`,
  etc.) doesn't mention the backend. SQLite and Postgres are
  implementations of the same contract.
- The Event Bus contract (`publish`, `subscribe`) doesn't mention
  Redis. The in-process bus and the Redis bus are implementations of
  the same contract.

Backend selection is configuration, not code change. Tests run against
the default tier (SQLite + in-process) for speed; the Postgres and Redis
backends have their own component tests against containerized instances
(§8).

### 6.3 Configuration

```yaml
storage:
  entity_backend: sqlite          # sqlite | postgres
  entity_path: ".platform/entities.db"
  # entity_dsn: "postgresql://..."   # when entity_backend: postgres

event_bus:
  backend: in_process             # in_process | redis
  # redis_url: "redis://localhost:6379"   # when backend: redis
```

Default values shown. A fresh install needs no changes to run.

---

## Part 7 — Documentation Discipline

### 7.1 Write as we go

Documentation is written *as code lands*, not after. A PR that adds a
feature includes the docs for that feature. A PR without its docs is
incomplete. This keeps documentation from rotting into a someday-task.

### 7.2 The `docs/` folder, Diátaxis-structured

We adopt the [Diátaxis](https://diataxis.fr/) framework (the same model
the Django documentation uses). Four documentation types, each serving
a distinct need, in distinct directories:

```
docs/
├── tutorials/          — learning-oriented; "build your first agent"
├── how-to/             — task-oriented; "how to write a custom tool"
├── reference/          — information-oriented; API, config schema, events
├── explanation/        — understanding-oriented; architecture, rationale
└── index.md            — entry point, links to the four sections
```

**Tutorials** — take a newcomer by the hand through a complete, working
example. Guaranteed to succeed if followed. "Build and run your first
agent."

**How-to guides** — solve a specific task for someone who already knows
the basics. "How to add a custom tool." "How to write a new memory
variant." "How to switch to the Postgres backend."

**Reference** — dry, complete, accurate. The config schema reference,
the event type catalog, the Module API Reference. Generated from or
mirrored with the code where possible.

**Explanation** — the why. Architectural decisions, the event-sourcing
rationale, the phase roadmap. Much of the existing spec set is
explanation-type documentation.

### 7.3 Mapping existing specs onto the structure

The current spec documents map mostly to **explanation** and
**reference**:
- The architecture roadmap, the contract specs, the audit → explanation.
- The Module API Reference, the schema spec, the error catalog →
  reference.

As implementation proceeds, we add **tutorials** and **how-to** guides
(which the spec set currently lacks) so that someone can actually *use*
and *extend* the platform, not just understand its design.

### 7.4 Logging is documentation too

Diagnostic logging (via Python's `logging`, written to `logs/` per the
filesystem layout) documents runtime behavior. Log at sensible levels:
`DEBUG` for development detail, `INFO` for lifecycle events, `WARNING`
for recoverable issues, `ERROR` for failures. Logs are not the event
log (that's the JSONL ground truth) — logs are operational diagnostics.

---

## Part 8 — Testing Philosophy & Scaffolding

### 8.1 The four layers

A testing pyramid shaped for this system.

**Layer 1 — Unit tests.** Pure logic, no I/O, no containers. Fast
(<5 seconds for the whole layer). Run constantly during development.

Best targets: the Kernel/Resolver (validators, merge algorithm), ref
parsing, path-policy evaluation, error serialization, the Assembly
Procedure's pure transformations.

**Layer 2 — Component tests.** One subsystem with its real backing
(temp filesystem, SQLite, in-process bus) but mocked neighbors. Verify
a subsystem honors its contract in isolation. Fast-ish (<1 minute).

Best targets: the Session/Run Store against a temp dir + SQLite; the
Memory provider against a mock store; the Tools subsystem against mock
providers.

**Layer 3 — Integration tests.** Multiple real subsystems together,
mocked only at the LLM boundary (the **Fake LLM**, §8.3) and using
**real CrewAI** underneath. This is where contract mismatches surface.
Minutes.

Best targets: the engine + store + memory + tools executing a full
exchange with a scripted Fake LLM; force-invoke flows; error
propagation across modules.

**Layer 4 — End-to-end tests.** The whole platform, real infrastructure
(containerized), real LLM. Slow, nondeterministic, few. The "does it
actually work" tests. Run before releases, not on every change.

Best targets: the four overall validation tests (Feasibility, Soundness,
Robustness, Desirability) from the test plan.

### 8.2 The Containerized Development Environment

Per principle §1.6, **the entire development environment is
containerized**. The container is the canonical environment — bit-for-bit
identical to CI. The host edits files; the container runs everything.
We use Podman (Docker-compatible).

There are two container configurations, for two distinct purposes.

#### 8.2.1 The dev container (`compose.dev.yml` + `devcontainer.json`)

The fast inner loop. Optimized so that **code changes never require a
rebuild** — source is live-mounted, not baked in.

Volume strategy:
- **Source code** (`src/`, `tests/`, `docs/`) — live-mounted read-write.
  Editing a file on the host reflects instantly inside the container; no
  rebuild.
- **The uv-managed virtualenv** — a *named volume*, so dependencies
  persist across container restarts and `uv sync` doesn't re-install
  every time.
- **The uv cache** — a *named volume*, so package downloads are cached
  across rebuilds.
- **Test/runtime artifacts** (`.platform/`, coverage data) — ephemeral
  or a throwaway volume; not the host's working tree.

What stays baked into the image (rebuilt only when the *environment*
changes — new dependency, Python bump, new system lib):
- Base Python, system libraries, uv itself, the OS-level toolchain.

The image needs rebuilding only when `pyproject.toml`/`uv.lock` or the
`Containerfile` changes — not when you edit code.

`devcontainer.json` (the open devcontainer standard) points the editor
at this container. VS Code's Dev Containers support is the primary,
best-supported path; the editor runs *inside* the container, so ruff,
mypy, pytest, and autocomplete all use the canonical environment
natively. (Editor-agnostic fallback: `podman-compose exec` into the
running dev container — see §8.2.3.)

Bring it up once:
```bash
podman-compose -f compose.dev.yml up -d --build   # build + start; rare
# (or: open the folder in VS Code → "Reopen in Container")
```

Thereafter, edit on the host; run checks inside the running container
(see §9.1). No rebuild for code changes.

#### 8.2.2 The test/CI container (`compose.test.yml`)

The commit gate and CI configuration. Deliberately the opposite of the
dev container's optimization: **a clean rebuild, fresh state, full
suite.** This is what guarantees a commit was verified against the
canonical environment, not against a dev container that has drifted over
days of work.

`compose.test.yml` defines:
- An `app` service — the platform, built clean (`--no-cache` on the app
  layer), source COPIED in (not mounted), so the test runs against
  exactly what's committed.
- A `postgres` service and a `redis` service — fresh instances for the
  backend-variant component tests.
- The `app` service's command runs the full gate: ruff check, ruff
  format check, mypy, then unit + component + integration tests.

This same compose file is what CI runs. Local commit verification and CI
are therefore *identical* — "passes at commit" means "passes in CI."

#### 8.2.3 Test infrastructure for backend variants

Most tests run against the default tier (SQLite + in-process bus) and
need **no external services**. Only the Postgres/Redis backend component
tests need containers — and `compose.test.yml` provides them.

For running just the backend tests locally against the dev container,
two options:

**Option A — `testcontainers` (used in CI-style runs).** The
`testcontainers-python` library manages Postgres/Redis container
lifecycle within the test session. It drives Podman via the
Docker-compatible socket:
```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST="unix://$(podman info --format '{{.Host.RemoteSocket.Path}}')"
export TESTCONTAINERS_RYUK_DISABLED=true   # Ryuk reaper is Docker-specific
```

**Option B — explicit compose (simpler mental model).**
```bash
podman-compose -f compose.test.yml up -d postgres redis
podman-compose exec dev uv run pytest tests/component/postgres
podman-compose -f compose.test.yml down -v
```

Recommendation: testcontainers within the `compose.test.yml` flow for
CI; explicit compose for inspectable local backend runs. The everyday
inner loop (default tier) needs none of this.

### 8.3 The Fake LLM — the most important test fixture

We must NOT burn tokens on testing. Integration tests use a **Fake LLM**:
a model stub that returns scripted responses, including scripted tool
calls, so tests are deterministic and free.

The Fake LLM:
- Implements the same interface the real model client exposes.
- Is configured per-test with a script: a sequence of responses to
  return for successive model calls.
- Can script tool-call requests (so the engine's tool dispatch is
  exercised), plain text responses, and error responses (so retry and
  failure paths are exercised).
- Records what it was asked (the assembled context) so tests can assert
  on what the model *would* have seen — this is how we test the
  Materialization Rule without a real model.

Example shape:

```python
fake_llm = FakeLLM(script=[
    ToolCallResponse(tool="read", args={"path": "data/x.md"}),
    TextResponse("Based on the file, the answer is 42."),
])
```

The Fake LLM runs *underneath real CrewAI* in integration tests — CrewAI
drives its real agent loop, but the model calls resolve to scripted
responses. This validates the actual CrewAI integration (the genuine
risk) while staying deterministic and free.

### 8.4 Golden event-log tests — measuring change impact

The single most useful technique for an event-sourced system.

A golden test:
1. Runs a scenario (with the Fake LLM) that produces a sequence of
   events.
2. Normalizes nondeterministic fields (UUIDs → stable placeholders,
   timestamps → fixed values).
3. Compares the normalized event sequence to a stored "golden" file.
4. Asserts they match.

When you change engine code, the golden diff **is** the impact
measurement. You see exactly which events changed, in what way. A change
that shouldn't affect output produces no golden diff; a change that does
shows precisely what moved.

Golden files live in `tests/golden/`. Updating them is deliberate:
`uv run pytest --update-golden` regenerates, and the regenerated files
are reviewed in the PR diff like any other change. An unexpected golden
diff in review is a red flag.

This makes "measure the impact of a code modification" concrete and
visible, which is exactly the development experience we want.

### 8.5 Fixtures and factories

Reusable builders reduce test boilerplate:
- `make_agent_config(**overrides)` — a valid ResolvedAgentConfig with
  sensible defaults, override any field.
- `make_event(type, **data)` — a well-formed event.
- `make_session()` — a session with a temp JSONL.
- `temp_platform_root()` — a fixture yielding a fresh platform-root
  temp directory with the standard layout.

These live in `tests/fixtures/` and `conftest.py`. A test should rarely
construct raw data by hand.

### 8.6 Event-sequence assertion helpers

Since correctness is largely about emitted events, dedicated assertions:
- `assert_event_sequence(events, [expected_types])` — the right event
  types in the right order.
- `assert_event_content(event, **expected)` — an event's data matches.
- `assert_materialization(events, fake_llm)` — the content the Fake LLM
  was asked to process matches what the events captured (the
  Materialization Rule check).

These make event-centric tests read clearly instead of drowning in
manual dict comparisons.

---

## Part 9 — Testing Process

### 9.1 The inner loop (inside the dev container, no rebuild)

The everyday loop runs *inside the running dev container* against
live-mounted source — so it's fast (no rebuild) but still in the
canonical environment. Via the devcontainer-attached editor, these run
transparently; the editor-agnostic equivalent is `podman-compose exec`:

```bash
# Inside the dev container (editor terminal) or via exec:
podman-compose exec dev uv run ruff format .
podman-compose exec dev uv run ruff check --fix .
podman-compose exec dev uv run mypy .
podman-compose exec dev uv run pytest tests/unit          # seconds
podman-compose exec dev uv run pytest tests/component     # ~1 min
podman-compose exec dev uv run pytest tests/integration   # minutes
```

Feedback tiers, fast to slow:

| Layer | Speed | When run | Infra |
|-------|-------|----------|-------|
| Unit | <5s | On save, constantly | None (in dev container) |
| Component | <1min | Frequently | SQLite, temp FS (+ containers for PG/Redis variants) |
| Integration | minutes | Before commit | Fake LLM + real CrewAI |
| E2E | slow | Before release | Real LLM, containers |

Because source is live-mounted, **no rebuild happens for code changes** —
edits are picked up instantly by the next in-container test run. The
image rebuilds only when the environment changes (`pyproject.toml`/
`uv.lock`/`Containerfile`).

### 9.2 The commit gate (clean rebuild, full suite)

At commit time, the gate does NOT trust the possibly-drifted dev
container. It performs a **clean rebuild and runs the full suite inside
the freshly built image** — via `compose.test.yml` (§8.2.2). This is
what guarantees the commit is verified against the canonical
environment.

Triggered by the pre-commit hook (or `just test-commit`):

```bash
podman-compose -f compose.test.yml down -v             # drop prior state/volumes
podman-compose -f compose.test.yml build --no-cache app  # clean app rebuild
podman-compose -f compose.test.yml up \
    --abort-on-container-exit --exit-code-from app
# The app service runs: ruff check, ruff format --check, mypy,
# then unit + component + integration tests, then the coverage check.
# Nonzero exit blocks the commit.
```

Why a clean rebuild at the gate: the dev container accumulates state
over days (stray installed packages, leftover artifacts, cached
imports). A clean rebuild catches the classic bugs — a dependency used
but not added to `pyproject.toml`, a test that only passed because of
local state, an import that worked only because of a manual `pip install`
someone forgot about. The commit must pass in a pristine environment.

### 9.3 CI runs the same gate

CI runs the **identical `compose.test.yml` flow** — clean rebuild, full
suite. Because the commit gate and CI use the same containerized
configuration, "passes at commit" and "passes in CI" are the same
statement. No environmental divergence between local and CI.

CI order (failing fast), all inside the freshly built `app` container:
1. `ruff check` + `ruff format --check`.
2. `mypy`.
3. Unit tests.
4. Component tests (containerized Postgres + Redis for non-default
   backends).
5. Integration tests (Fake LLM + real CrewAI).
6. Coverage check (§11).

A merge to `main` is blocked unless all pass. E2E tests run on a separate
schedule (nightly or pre-release), not every push, because they cost
tokens and time.

### 9.3.1 What blocks a merge

- Any lint, format, or type error.
- Any failing unit, component, or integration test.
- A coverage decrease beyond the allowed delta (§11).
- An unexplained golden-file change (golden diffs must be intentional
  and reviewed).
- Missing documentation for a user-facing change.
- A commit that was not verified through the clean-rebuild gate
  (e.g., pre-commit bypassed) — CI re-runs the gate and will catch it.

### 9.4 Measuring the impact of a change

The development experience we want — easy to measure the impact of a
modification — comes from:
1. **Fast unit feedback** (seconds) shows logic-level impact instantly.
2. **Golden event tests** show behavioral impact precisely (which
   events changed).
3. **Coverage delta** shows whether a change left new code untested.
4. **Optional benchmark tests** (§9.5) show performance impact on hot
   paths.

Together these mean a contributor (or agent) sees the full impact of a
change quickly and concretely.

### 9.5 Benchmark tests (optional tier)

For performance-sensitive paths — memory cache rebuild, log reads,
context assembly — micro-benchmarks track whether a change made
something slower. Run on demand, not every push. Results tracked over
time so regressions are visible.

---

## Part 10 — Per-Subsystem Success Criteria

What "working correctly" means for each subsystem — concrete and
measurable. These supersede the per-module test *categories* in the
test plan; the test plan retains the four overall validation tests.

### 10.1 Kernel / Resolver

- `resolve` produces byte-identical output for identical input across
  1000 repeated runs (determinism).
- Passes 100% of the published RFC 7386 merge test vectors.
- Given a config with N independent validation errors, `resolve_and_
  validate` returns all N (not fewer), verified with crafted multi-error
  inputs.
- Runs with zero I/O — verified by running the full kernel test suite
  with filesystem and network access denied.

### 10.2 Asset Management

- A catalog built from a fixture of K bundles registers exactly the
  expected assets (count and refs match).
- Duplicate bundle names cause a fatal startup error (not silent
  last-wins).
- Shorthand resolution: unambiguous resolves correctly; ambiguous raises
  with all candidates named (verified with a 2-candidate fixture).
- Prompt refs are pre-resolved to content in the resolved config; tool
  and skill refs are NOT (verified by inspecting the resolved config).
- `platform.yaml` sections dispatch to the right subsystems (verified
  by a config with distinct per-section markers).

### 10.3 Session/Run Store

- 100 concurrent append attempts from 10 processes all persist, with no
  corruption and no lost events, verified by reading back and counting.
- `parent_event_id` of every appended event equals the prior leaf,
  verified across a 1000-event sequence.
- Killing the process mid-write leaves a recoverable log (next open
  truncates a partial final line cleanly; all complete prior events
  intact).
- `reconstruct_from_filesystem` rebuilds entity records matching the
  original set, verified by wiping the entity DB and reconstructing.
- Works identically on SQLite (default) and Postgres backends —
  the same component test suite passes against both.

### 10.4 Event Bus

- A published event reaches all matching subscribers and no
  non-matching ones (verified with overlapping topic patterns).
- A subscriber that raises does not prevent delivery to others and is
  not removed (verified with a deliberately-raising handler).
- The in-process bus (default) and Redis bus pass the same component
  test suite.
- Bus publish failure (simulated) does not raise into the engine's
  execution path.

### 10.5 Agent Engine

- For a scripted Fake-LLM exchange, the emitted event sequence matches
  the golden file exactly (modulo normalized nondeterministic fields).
- The Materialization check passes: the content the Fake LLM was asked
  to process equals what the events captured, for at least 3 diverse
  scenarios.
- Diagnostic errors (tool failure) do NOT end the exchange; Fatal errors
  do — verified by scripting each.
- Killing and resuming mid-session reconstructs correctly and the next
  exchange proceeds (statelessness across calls).
- Authorization denial at pre-flight emits `thread.exchange_failed` and
  raises, with no further events.

### 10.6 Memory Provider

- Cache rebuilt from log replay produces identical retrieval results to
  the pre-restart cache (verified by snapshotting retrievals before and
  after a simulated restart).
- Writes are tagged with the configured `write_tag`; the user cannot
  override it at write time (verified by attempting override).
- A `write_tag` containing a wildcard is rejected at config validation.
- Items from session A never appear in session B's retrievals (verified
  with two sessions).

### 10.7 Secrets / Sandbox / Authorization

- Secrets: `resolve` returns the value; the value never appears in any
  log or event (verified by scanning captured logs/events for the
  secret).
- Sandbox (native): result and exceptions pass through unchanged
  (verified by comparing direct call vs. sandboxed call).
- Authorization: first-match-wins and default-deny verified with crafted
  rule sets; regex is fullmatch (`tool.b` does NOT match `tool.bash`).

### 10.8 Tools Subsystem

- Tool dispatch order is validate → authz → secrets → sandbox → execute,
  verified by instrumenting each step and asserting call order.
- Args failing the Pydantic schema produce `tool.invalid_arguments`
  before the tool function runs.
- A tool raising produces `tool.execution_failed` (Diagnostic); the
  exchange continues.
- Builtin read/write/edit honor the path policy; bash bypasses it
  (verified explicitly, documenting the known limitation).

### 10.9 Skills Subsystem

- Enabled skills' descriptions appear in the system-prompt listing;
  `disable-model-invocation` skills are excluded from the listing but
  still force-invocable.
- Force-invoke produces a synthetic `tool.called`/`tool.result` for
  `read` on the SKILL.md path, verified in the event sequence.
- A skill ref not in the enabled list cannot be force-invoked (raises
  `skill.forced_not_enabled`).

### 10.10 Error Model

- Every `PlatformError` subclass serializes to the canonical envelope
  (verified by iterating all subclasses).
- The `__cause__` chain serializes recursively as `cause` (verified with
  a 3-deep wrap).
- Every `type` string emitted anywhere is in the catalog (verified by
  scanning emitted errors against the registered set).

### 10.11 File Handling

- Path policy: denies override allows; default-deny for unmatched;
  symlink-to-denied is denied (verified with crafted paths and a
  symlink).
- Adding `.env` to `allowed_paths` does NOT grant access (deny wins).
- Tools never write to `content/` (verified by code inspection plus a
  runtime guard in tests).

### 10.12 LLM Provider Configuration (Phase 1.1, LLM-R7)

- The registry rejects duplicate registration — calling
  `register(type, cls)` twice for the same `type` raises a
  `PlatformError` rather than silently overwriting.
- `build_llm` resolves the full chain: alias → canonical
  `{provider, model}` → connector lookup via the registry →
  `crewai.LLM` construction.
- A connected `MetadataSourceConnector` (only OpenRouter in v1.1)
  augments OTHER providers' `discover_models` results during the
  connect flow — verified by connecting OpenRouter, then connecting
  OpenCode, and asserting OpenCode's models gain context_length /
  supports_tools fields from the OpenRouter catalog.
- Provider connectors register independently — adding a new
  connector requires no changes outside `src/llm/providers/` (one
  new file + one line in `__init__.py`).
- **API key values never appear in logs, events, or
  `platform.yaml`** — only the secret NAMES do. Verified by
  inspecting `platform.yaml` after a `connect` flow and by the
  Materialization Rule audit on emitted events.

---

## Part 11 — Coverage & Quality Gates

### 11.1 Coverage targets

- **Overall line coverage ≥ 85%.** A floor, not a ceiling.
- **Branch coverage ≥ 80%.**
- **The Kernel/Resolver ≥ 95%** — it's pure logic with no excuse for
  gaps.
- **Per-PR: coverage must not decrease** beyond a small allowed delta
  (e.g., -0.5%). A PR that adds untested code is flagged.

Coverage is necessary but not sufficient — 100% coverage with weak
assertions is worse than 85% with strong ones. Coverage measures
*exercise*, not *verification*. Reviewers check that tests actually
assert meaningful behavior, not just execute lines.

### 11.2 Coverage diffing per PR

CI reports the coverage delta on each PR: total coverage, and which
new/changed lines are uncovered. This makes "did this change leave
something untested" visible at review time.

### 11.3 The quality gates, summarized

A change reaches `main` only if:
1. Lint + format clean (ruff).
2. Types clean (mypy --strict).
3. Unit + component + integration tests pass.
4. Coverage ≥ targets and not decreased beyond delta.
5. Golden diffs (if any) are intentional and reviewed.
6. Docs updated for user-facing changes.
7. Reviewed and approved.

These gates are the contract between contributors. They keep `main`
always-releasable and make the impact of every change measurable.

---

## Part 12 — Open Items

1. **Devcontainer and compose files.** The concrete `devcontainer.json`,
   `compose.dev.yml`, `compose.test.yml`, and `Containerfile` are
   implementation artifacts to be authored at project setup. This
   handbook specifies their roles (§8.2); the files themselves come with
   the initial scaffolding. Editor setup beyond VS Code Dev Containers
   (PyCharm's remote interpreter, Neovim in-container) documented in
   `docs/how-to/editor-setup.md`.

2. **CI platform specifics.** This handbook specifies the gates
   tool-agnostically and mandates that CI run the same
   `compose.test.yml` flow as the commit gate. The concrete CI config
   (GitHub Actions / GitLab CI) that invokes that flow is a separate
   implementation artifact.

3. **Benchmark baselines.** The benchmark tier (§9.5) needs initial
   baselines once there's code to measure.

4. **Mutation testing (future).** Coverage measures exercise; mutation
   testing (e.g., `mutmut`) measures assertion strength. Worth
   considering once the suite is mature.

5. **The Fake LLM's fidelity to real providers.** The Fake LLM mimics
   the model client interface; keeping it faithful as real provider
   APIs evolve is ongoing work. A periodic "Fake vs. real" reconciliation
   test (token-budgeted) guards against drift.

6. **Dev container rebuild ergonomics.** When `uv.lock` changes, the dev
   container needs a rebuild to pick up new dependencies. Worth a small
   helper (a `just` recipe or a devcontainer `postCreateCommand`) that
   detects lockfile changes and prompts/triggers the rebuild, so
   contributors don't run stale environments after a `git pull`.
