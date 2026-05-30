# Phase 1.2 Patch — Workspace Resolution & First-Run Setup

**Draft 0.1**

> A usability patch closing two gaps that surfaced once Phase 1.1
> shipped: (1) `kolsch` has no canonical workspace location, so users
> have to `cd` into a specific directory before any command works,
> and (2) standing up a fresh workspace requires hand-writing
> `platform.yaml`, running `connect`, and crafting an agent file
> before anything is actually runnable. New users hit a wall before
> the first agent reply.
>
> This patch specifies: (1) the workspace-resolution chain
> (`--root` → `$KOLSCH_WORKSPACE` → `~/Documents/kolsch` via
> `platformdirs`), (2) the new `kolsch setup` CLI command that
> scaffolds the workspace, walks first-provider connect, ships a
> starter agent, and verifies the install with a real run, and
> (3) the friendly missing-workspace error that points unhelpful
> stack traces at `kolsch setup` instead.

---

## Part 0 — Why This Patch Exists

After Phase 1.1, Kolsch can talk to real LLMs — but the first-run
experience is hostile:

- Every command needs `--root <somewhere>` or the user must `cd` into
  the right directory. There is no "just run `kolsch`" path.
- Standing up a workspace by hand means: `mkdir` six directories,
  write a 20-line `platform.yaml`, run `kolsch connect`, write an
  agent YAML, then finally `kolsch run`. Six steps before any reward.
- Errors from a missing workspace are stack traces about
  `FileNotFoundError: platform.yaml`, not actionable guidance.

This patch fills the gap. Headline design decisions:

1. **One canonical default location.** `~/Documents/kolsch` (via
   `platformdirs.user_documents_dir()`). It survives reinstalls, lives
   where users look for things, and is platform-correct for macOS,
   Linux (XDG), and Windows. No multi-workspace registry — users who
   want more than one workspace override per-invocation.
2. **Explicit resolution chain.** `--root` flag > `KOLSCH_WORKSPACE`
   env var > default. Each layer is independently documented and
   testable. The same chain is reused by the runtime, the server, and
   every CLI subcommand — there is one resolver function.
3. **A single first-run command.** `kolsch setup` is the one
   command a new user is told to run. It is idempotent, every step is
   skippable, and it ends with a verified-working agent. Setup does
   not duplicate logic that exists elsewhere — it calls `kolsch
   connect`, the existing folder-writer, the existing runtime — and
   wraps them in a guided flow.
4. **No new agent abstractions.** The starter "helper" agent is a
   plain agent file, materialized into the workspace bundles. Users
   can edit, copy, or delete it the same as any other agent. It is
   not magic.

---

## Part 1 — Workspace Resolution

### 1.1 The resolver

One function in one module. Lives in `src/platform_runtime/workspace.py`.

```python
# src/platform_runtime/workspace.py

from pathlib import Path
import os
import platformdirs

KOLSCH_ENV_VAR = "KOLSCH_WORKSPACE"
DEFAULT_DIR_NAME = "kolsch"


def resolve_workspace(cli_root: Path | None = None) -> Path:
    """Return the canonical workspace path for this invocation.

    Resolution order (highest precedence first):
      1. ``cli_root`` — typically from the CLI ``--root`` flag.
      2. ``$KOLSCH_WORKSPACE`` — the env-var override.
      3. ``<platformdirs.user_documents_dir()>/kolsch`` — the default.

    The returned path is NOT guaranteed to exist. Callers that need
    a populated workspace must check (or call :func:`kolsch setup`).
    """
    if cli_root is not None:
        return cli_root.expanduser().resolve()
    env = os.environ.get(KOLSCH_ENV_VAR)
    if env:
        return Path(env).expanduser().resolve()
    return Path(platformdirs.user_documents_dir(), DEFAULT_DIR_NAME).resolve()


def workspace_is_initialized(root: Path) -> bool:
    """A workspace is initialized iff ``platform.yaml`` exists at root."""
    return (root / "platform.yaml").is_file()
```

The function is pure (modulo `os.environ` read) and returns an absolute
`Path`. It is the only public entry point — no other module reaches
for `KOLSCH_WORKSPACE` or `platformdirs`.

### 1.2 Resolution order

| Source | When set | Notes |
|---|---|---|
| `--root <PATH>` (CLI flag) | Per invocation | Highest precedence; explicit user override. Already exists in `src/cli/main.py:55–60`; default value changes from `Path.cwd()` to `None`. |
| `KOLSCH_WORKSPACE` env var | Per shell session | For users who keep one workspace elsewhere but want it on every command. |
| `~/Documents/kolsch` (default) | Always | Via `platformdirs.user_documents_dir()`. Created by `kolsch setup`. |

The resolver does not consult the current working directory. Phase 1's
behavior of "use CWD" is removed — users who relied on it set
`KOLSCH_WORKSPACE=.` or pass `--root .` explicitly. (No silent fallback;
silent fallbacks are how users end up with stray sessions in random
directories.)

### 1.3 Callers

Every entry point that constructs a `PlatformRuntime` consults the
resolver:

- `src/cli/main.py` — the top-level parser. Default of `--root` becomes
  `None`; immediately after parsing, the dispatcher calls
  `resolve_workspace(args.root)` before any subcommand handler runs.
- `src/api/__init__.py` / `build_app` — server bootstrap. Reads the
  same env-var chain via the resolver.
- Tests — call `resolve_workspace(tmp_path)` to pin the workspace.

No other module imports `platformdirs` or reads `KOLSCH_WORKSPACE`.

### 1.4 The "uninitialized workspace" error

When a non-setup command runs against a path where `platform.yaml`
does not exist, the CLI emits one short, actionable message and exits
with code 3 (distinct from generic error code 1):

```
No Kolsch workspace at /Users/jane/Documents/kolsch.

Run `kolsch setup` to create one, or point KOLSCH_WORKSPACE at an
existing workspace.
```

No stack trace. The runtime never gets a chance to raise its own
"platform.yaml missing" error; the CLI intercepts before bootstrap.

This is gated by `workspace_is_initialized()` (§1.1), not by trying
to bootstrap and catching the failure.

---

## Part 2 — The `kolsch setup` Command

The one command a new user runs.

### 2.1 Contract

```bash
kolsch setup [--root <PATH>] [--non-interactive]
```

- Idempotent. Re-running detects existing state and offers to update,
  not overwrite.
- Interactive by default. Each step prompts; user can accept (`y`/
  Enter), decline (`n`), or skip (`s`).
- `--non-interactive` accepts defaults at every step and errors at the
  first prompt that needs a value (e.g., an API key). For CI scripts.
- Resolves the workspace via the chain in §1 — `--root` flag, then
  `KOLSCH_WORKSPACE`, then the platformdirs default.

### 2.2 The flow

Six steps. Each is independently skippable and re-runnable.

**Step A — Workspace location.**

```
Welcome to Kolsch.

Workspace location: /Users/jane/Documents/kolsch
  [Enter] use this  [c]hange  [s]kip (already set up)
>
```

- "Enter" (or `y`) → proceed with the resolved path.
- `c` → prompt for a new path. Re-resolve.
- `s` → if `platform.yaml` exists, skip to Step C. If it doesn't, the
  CLI explains and re-asks.

**Step B — Workspace skeleton.**

Creates (idempotently) the directory layout and a default `platform.yaml`.

```
Creating workspace at /Users/jane/Documents/kolsch
✓ bundles/        (your agents live here)
✓ sessions/       (event-log ground truth)
✓ content/        (assets agents can read/write)
✓ logs/           (structured platform logs)
✓ data/           (agent working files)
✓ .platform/      (entity DB + internals)
✓ platform.yaml   (default backends: SQLite + in-process bus)
✓ .env            (empty — provider keys land here)
```

The default `platform.yaml` is the v1 default tier — SQLite, in-process
bus, native sandbox, allow-all rule-based auth, dotenv secrets — with
`file_access.allowed_paths` pointing at the absolute workspace paths.

If `platform.yaml` already exists, the step says so and offers to leave
it alone or diff against the default.

**Step C — LLM provider.**

```
LLM provider
At least one provider is needed to actually run agents.

  [r] openrouter  — recommended (one key → most models + metadata)
  [c] opencode
  [s] skip — I'll connect a provider later

>
```

- `r` or `c` → hand off to the existing `kolsch connect <provider>`
  flow (Phase 1.1 Part 8). The setup command does not reimplement
  connect; it calls it.
- `s` → continue without a provider. Step E will skip the test run
  with a note.

If a provider is already connected, the step says so and offers to
connect another, refresh, or move on.

**Step D — Starter agent.**

```
Starter agent
A small "helper" agent so you can test the system immediately.

Bundle name [starter]:
Agent name  [helper]:
Model       [opus]:    ← suggested from your connected providers' aliases

  [y] create  [n] skip
>
```

- Writes `bundles/<bundle>/bundle.yaml` and
  `bundles/<bundle>/agents/<agent>.yaml`.
- The agent uses `io.mode: conversational`, a friendly default prompt
  ("You are a helpful assistant."), and the user-selected model alias.
- If no provider was connected in Step C, the model defaults to
  `provider: fake` (the Phase 1 fake LLM) so the user still has a
  runnable agent and can swap it later.

If the chosen file path already exists, the step says so and asks
whether to overwrite, pick a different name, or skip.

**Step E — Test run.**

```
Test run
Run the helper agent once to confirm everything works.

  [y] run  [n] skip
>
```

If `y`, the command effectively runs:

```
kolsch run starter/agents/helper --input content="hello"
```

…and reports success/failure. On failure, prints the failure cause
and a hint (most often `llm.secret_missing` → re-run the connect
flow). On success, prints the reply.

If Step C was skipped, this step is skipped automatically with a note
that the test run needs a connected provider (or `provider: fake`).

**Step F — You're ready.**

```
🎉 Kolsch is ready.

Try:
  kolsch run starter/agents/helper --input content="<your prompt>"
  kolsch sessions list
  kolsch --help

Workspace: /Users/jane/Documents/kolsch
Docs:      docs/tutorials/first-agent.md
```

### 2.3 Idempotency rules

- **Skip**, never silently overwrite, when a destination exists.
- Each step asks before writing anything. The first refusal aborts
  that step only — the rest of setup continues.
- A full abort (Ctrl-C) leaves the filesystem in whatever state the
  last completed step produced. Re-running picks up where the user
  left off.
- `setup` writes nothing outside the resolved workspace. The only
  exception is the provider's API key, which goes into the workspace's
  `.env` per Phase 1.1.

### 2.4 The starter agent file

Exact content materialized by Step D (with `<MODEL>` substituted from
the user's choice — `opus`, another alias, or `fake-model` if no
provider connected):

```yaml
# bundles/starter/agents/helper.yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_helper
name: helper
description: A friendly general-purpose helper agent.
model: <MODEL>
prompt: |
  You are a helpful assistant. Answer the user's question clearly and
  concisely. If the question is ambiguous, ask one clarifying question
  before answering.
io: { mode: conversational }
```

And `bundles/starter/bundle.yaml`:

```yaml
name: starter
version: "0.1"
description: Bundle created by `kolsch setup` to verify the install.
```

No special "starter" treatment in the runtime — these are plain files
the user can edit, copy as a template, or delete.

> **Phase 2.0 alignment note (TOOL-R12).** Phase 2.0 + 2.3 land on
> `bundles/kolsch/` rather than `bundles/starter/` — the
> Phase 2.0 patch ships four canonical builtin tool files at
> `bundles/kolsch/tools/` (per
> `docs/explanation/tools-subsystem.md` §4.5) and Phase 2.3
> formalises the bundle rename. **This Phase 1.2 spec is not
> retroactively edited.** If Phase 1.2 has already scaffolded
> `bundles/starter/` into existing workspaces, Phase 2.3's drift
> detection handles the migration (detects the legacy bundle,
> offers to rename it, leaves the YAML inside untouched). New
> Phase 1.2 implementations landing AFTER Phase 2.0 should write
> to `bundles/kolsch/` directly to avoid the migration cost.
> Phase 1.2 reviewers reading this note: the original
> `bundles/starter/` path remains correct for everything you
> built; do not retrofit.

### 2.5 `--non-interactive` semantics

Used by CI, smoke tests, and the integration-test fixture for setup.

- Step A: uses resolved default; no prompt.
- Step B: creates skeleton if missing; preserves existing files.
- Step C: skipped unless `KOLSCH_PROVIDER_KEY` env var is set (then
  connects the provider named in `KOLSCH_PROVIDER` — also from env).
  Documenting the env conventions is part of UX-R5.
- Step D: creates the starter agent with `provider: fake` if no
  provider connected.
- Step E: runs the test exchange.
- Step F: prints the banner.

Exit code 0 on full success, 4 on partial completion (e.g., skeleton
created but no provider connected). Distinct from runtime exit codes.

---

## Part 3 — Failure Modes

New error codes. Adds a new §4.14 "Workspace" to
`docs/explanation/error-model.md`.

| Code | Level | Detail | Where |
|---|---|---|---|
| `workspace.not_initialized` | Fatal | `{path, env_var_set}` | CLI dispatch (pre-bootstrap) |
| `workspace.permission_denied` | Fatal | `{path, operation}` | `setup` step B; runtime bootstrap |
| `setup.aborted` | Recoverable | `{step, reason}` | `setup` after a user Ctrl-C; informational only |
| `setup.starter_exists` | Recoverable | `{path}` | `setup` step D, when the target file already exists and user declines overwrite |

`workspace.not_initialized` is the **only** error users should ever
see from a missing workspace. The CLI catches it, formats the friendly
message per §1.4, and exits 3. The error class still exists so the
runtime can surface it programmatically (e.g., when the server is
booted against a missing workspace at startup).

---

## Part 4 — Required Spec Revisions

Mechanical edits to existing docs. Apply as a single revision pass.

### UX-R1. `pyproject.toml`

Add `platformdirs >= 4.2` to the runtime dependencies.

### UX-R2. `src/cli/main.py`

Change `--root` default from `Path.cwd()` to `None`. Update the help
text to: `"Workspace root. Default: $KOLSCH_WORKSPACE or
~/Documents/kolsch."`. No other code change at this layer; the
dispatcher in §1.3 handles resolution.

### UX-R3. `docs/explanation/v1-scope.md` §2.4

Add `kolsch setup` to the v1 CLI surface. Note that it is idempotent
and accepts `--non-interactive`.

### UX-R4. `docs/explanation/v1-scope.md` §2.5

Update deployment to read:

> Default install requires no LLM provider — `kolsch setup` creates
> a workspace and ships a `provider: fake` starter agent so the first
> run works without credentials. Real-LLM execution needs `kolsch
> connect <provider>` (either inside `setup` or any time after).

### UX-R5. `docs/explanation/asset-management-contract.md`

Add §1.4 "Workspace Resolution" covering: the chain in §1.2, the
`platformdirs` default, the `KOLSCH_WORKSPACE` env var, and the
`KOLSCH_PROVIDER` / `KOLSCH_PROVIDER_KEY` convention used by
`--non-interactive` setup.

### UX-R6. `docs/explanation/error-model.md` Part 4

Add §4.14 "Workspace" with the four codes from Part 3.

### UX-R7. `docs/reference/module-api-reference.md`

Add Part 11.6 "Workspace Resolution" documenting `resolve_workspace`,
`workspace_is_initialized`, and the resolution precedence. Update the
dependency matrix (Part 12) to show that the CLI and API both depend
on the workspace module.

### UX-R8. `docs/process/development-testing-handbook.md` §10

Add success criteria §10.13 "Workspace Resolution & Setup":

- `resolve_workspace` returns CLI > env > default in that order.
- The CLI emits the friendly missing-workspace message (not a stack
  trace) when run against an uninitialized path.
- `kolsch setup` is idempotent — running it twice leaves the
  workspace identical to the once-run state.
- `kolsch setup --non-interactive` produces a runnable workspace
  with the fake starter agent.
- No module outside `src/platform_runtime/workspace.py` imports
  `platformdirs` or reads `KOLSCH_WORKSPACE`.

### UX-R9. `docs/process/test-catalog.md`

Add Part 9.6 "Workspace Resolution & Setup (UX)" with cases:

- TC-UX-01: `resolve_workspace(--root)` returns the flag value.
- TC-UX-02: `resolve_workspace()` with `KOLSCH_WORKSPACE` set returns
  the env value.
- TC-UX-03: `resolve_workspace()` with neither set returns the
  `platformdirs`-derived default.
- TC-UX-04: CLI emits the friendly message on missing workspace and
  exits 3 (not a Python traceback).
- TC-UX-05: `kolsch setup --non-interactive --root <tmp>` produces a
  workspace passing `workspace_is_initialized`.
- TC-UX-06: `kolsch setup` re-run is idempotent — second run does
  not modify any file written by the first.
- TC-UX-07: `kolsch setup` with provider env vars set runs the
  connect flow and writes `.env` correctly.
- TC-UX-08: starter agent file lands at the expected path with the
  expected `model` field reflecting the user's provider choice
  (`fake-model` when no provider).
- TC-UX-09: ending setup at step D and re-running picks up at step D
  (skipping completed steps).
- TC-UX-10: `kolsch run starter/agents/helper` works against a
  setup-produced workspace with no further configuration.

### UX-R10. `AGENTS.md`

Add to "Hard rules":

> Workspace location is resolved by
> `platform_runtime.workspace.resolve_workspace`. No other module
> reads `KOLSCH_WORKSPACE` or constructs the default path. The CLI
> emits a friendly message (not a stack trace) when the workspace is
> uninitialized.

### UX-R11. `README.md`

Update the install / quickstart section to read:

```bash
# Install
uv tool install kolsch                # (or pipx / pip)

# First run
kolsch setup                          # walks you through it

# Use it
kolsch run starter/agents/helper --input content="hello"
```

### UX-R12. `docs/tutorials/first-agent.md`

Add a "0. First run" preface that points at `kolsch setup` and notes
the rest of the tutorial assumes the workspace lives at
`~/Documents/kolsch` (with a one-line override note for
`KOLSCH_WORKSPACE` / `--root`). Steps 1–9 stay; the "lay down a
workspace" step (1) is reframed as "what `kolsch setup` produced —
in case you skipped it or want to know what's there."

### UX-R13. New doc: `docs/how-to/workspace-and-setup.md`

A short how-to covering:

- Switching between workspaces (`KOLSCH_WORKSPACE`, `--root`).
- Moving an existing workspace (just `mv` it; nothing inside
  references absolute paths *outside* the workspace).
- Backing up a workspace (`tar` it; the JSONL is the ground truth).
- Resetting (`rm -rf .platform/entities.db && kolsch reconstruct`).

---

## Part 5 — Explicitly Deferred

1. **Workspace registry / "kolsch workspaces list".** Only one
   workspace is the active one per invocation; multi-workspace
   management is a Phase 3 hosted concern.
2. **Workspace migrations.** Schema versioning for `platform.yaml`
   exists; an automated upgrader does not. Manual upgrades for now.
3. **Cross-host workspaces (e.g., a workspace on a NAS).** Works
   today by setting `KOLSCH_WORKSPACE`; we don't ship special
   handling for path performance, file locking on network shares,
   etc.
4. **Setup wizard for tools/skills.** Phase 1.2 sets up *agents*.
   Tool and skill scaffolding via setup is a later patch when those
   subsystems have stabilized.
5. **A telemetry opt-in step in setup.** No telemetry exists; nothing
   to opt into. If telemetry is added later, that step is added to
   setup then.

---

## Part 6 — Open Items

1. **The "Documents" location on headless Linux.** `platformdirs`
   returns `$XDG_DOCUMENTS_DIR` if set, falling back to `~/Documents`,
   which a server install may not have. Acceptable: setup creates the
   directory if missing, and users who want a different location set
   `KOLSCH_WORKSPACE`. Flagged for visibility.
2. **Containerized installs (dev container, CI).** Inside containers
   without a real Documents directory, the default resolves to
   `/root/Documents/kolsch` (or similar). The dev container's
   `devcontainer.json` should set `KOLSCH_WORKSPACE=/workspace` so
   the in-container experience matches the live-mounted source. This
   is a `.devcontainer/` change, not a code change — flagged so the
   build plan covers it.
3. **The starter agent's prompt.** The default ("You are a helpful
   assistant…") is intentionally generic. Users may want
   prompts tuned to common use cases (summarizer, code reviewer,
   etc.). Out of scope for this patch — the agent file is plain YAML
   and easy to edit. Could ship a small prompt gallery later.
4. **CLI flag ergonomics for `--root`.** `--root` is the existing
   name. Some users may expect `--workspace`. Considered adding
   `--workspace` as a synonym; rejected — one canonical name is
   cleaner. If we hear consistent confusion, revisit.

---

## Part 7 — Status

This is **Phase 1.2 patch draft 0.1**. The patch:

- Closes the no-canonical-location gap (`kolsch` ran-from-anywhere
  works now).
- Closes the cold-start gap (`kolsch setup` is the one command a new
  user runs).
- Establishes the workspace-resolution module so future "where does
  X live" questions have one obvious answer.

**Apply in two passes:**

1. **Revisions.** UX-R1 through UX-R13 mechanically edit existing
   docs and create the one new how-to. Same pattern as the LLM-R
   pass.
2. **Implementation.** Add `src/platform_runtime/workspace.py` (the
   resolver), add `setup` to the CLI dispatcher, add the dotenv-aware
   provider-env helpers for `--non-interactive`, add TC-UX-* tests.
   One focused developer-agent branch, reviewed against §10.13 and
   Part 9.6.

After this patch lands, the experience from "install" to "first agent
reply" is one command. That is the bar.
