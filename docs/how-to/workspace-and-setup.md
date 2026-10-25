# Workspace and Setup

Short recipes for the workspace operations that come up after `kolsch
setup` has run once. For *why* the workspace works this way, read the
Asset Management contract §1.4. For the install / first-run path, read
the [first-agent tutorial](../tutorials/first-agent.md).

## Where does my workspace live?

Resolution order (highest precedence first), implemented by
`platform_runtime.workspace.resolve_workspace`:

1. `--root <PATH>` on any `kolsch` command.
2. `$KOLSCH_WORKSPACE` in the shell environment.
3. `<platformdirs.user_documents_dir()>/kolsch` — typically
   `~/Documents/kolsch` on macOS and Linux, `%USERPROFILE%/Documents/kolsch`
   on Windows.

Confirm where any specific invocation is pointed:

```bash
kolsch --root . providers     # uses the current directory
KOLSCH_WORKSPACE=/srv/kolsch kolsch providers
kolsch providers               # uses the default
```

The CLI never falls back to the current working directory silently.
If you want CWD-rooted behaviour, set `KOLSCH_WORKSPACE=.` or pass
`--root .`.

## Switching between workspaces

Use whichever lever fits the lifetime of the switch:

- **For one command** — pass `--root <path>` on that command.
  Useful when you keep multiple workspaces and want to glance at one.
- **For one shell session** — `export KOLSCH_WORKSPACE=<path>`. Every
  subsequent command in that shell uses it.
- **Permanently** — add the export to your shell rc, OR `mv` the
  default workspace to the location you actually want and set
  `KOLSCH_WORKSPACE` there.

Only one workspace is "active" per invocation; there is no
multi-workspace registry. A "kolsch workspaces list" command is a
future hosted-mode concern.

## Moving an existing workspace

`mv` the directory. Nothing inside the workspace references absolute
paths *outside* it — `platform.yaml`'s `file_access.allowed_paths`
are relative; the session log uses relative paths; the SQLite entity
DB uses relative paths.

```bash
mv ~/Documents/kolsch /srv/kolsch
export KOLSCH_WORKSPACE=/srv/kolsch
kolsch sessions list           # everything still works
```

If you forget the `export`, the next bare `kolsch` invocation tries
`~/Documents/kolsch`, finds nothing there, and prints the friendly
"run `kolsch setup`" message instead of a stack trace.

## Backing up a workspace

The on-disk JSONL session logs are ground truth. The SQLite entity DB
is an index that `kolsch reconstruct` can rebuild from the log. So a
backup is simply:

```bash
tar -czf kolsch-backup-$(date +%F).tgz -C ~/Documents kolsch
```

If you're tight on space, you can exclude `.platform/` (entity DB +
internals) and the next `kolsch` invocation rebuilds it on demand —
but excluding `sessions/` would lose history irrecoverably.

## Resetting a workspace

To clear the entity DB but keep the session history:

```bash
rm ~/Documents/kolsch/.platform/entities.db
kolsch reconstruct             # walks sessions/, rebuilds the DB
```

To wipe everything and start fresh:

```bash
rm -rf ~/Documents/kolsch
kolsch setup                   # scaffolds a new one
```

To remove a single provider without touching the rest:

```bash
kolsch disconnect <provider>   # updates platform.yaml + .env
```

## Upgrading an existing workspace

A newer kolsch ships new starter files (tools, agents, workflows) and,
sometimes, new default permission rules. Re-running `kolsch setup` on
an existing workspace does not install them by itself — it tells you,
right after `✓ starter agent already at …`:

```text
  ⚠ workspace bundle is behind kolsch 0.12.0: `--sync-bundle` would copy 24 new file(s), ask about 11 file(s) that differ from this version (kept unless you overwrite).
    1 missing default permission rule: kolsch/tools/jev_decide → prompt (sends workspace content to a third party (OpenRouter))
    Run `kolsch setup --sync-bundle` to review and apply (`--dry-run` to preview).
```

An interactive re-run then asks **Review and apply now?** — yes runs
the [`--sync-bundle`](#repairing-drift-kolsch-setup-sync-bundle) flow
right there, before setup's test run, so the test uses the upgraded
bundle. `--non-interactive` only prints the notice. Nothing is written
unless you say yes, and the notice disappears once the workspace is in
sync. The notice counts everything sync would act on: files to copy,
refresh or remove, retired tool refs to rewrite (in
`bundles/kolsch/agents/*.yaml` and `platform.yaml`), files it would
ask about, and missing default rules.

Files that differ from source are only cleared by an interactive
`--sync-bundle`: `[o]verwrite` them, or `[k]eep` them. A keep is
remembered in `.kolsch/bundle-kept.json` against that exact file and
that exact shipped version, so sync doesn't ask again and the notice
and `kolsch doctor`'s drift leave it out (doctor lists it under
**Kept local**, informational) — until you edit the file or a kolsch
upgrade changes the shipped version, when it's asked about again.
`--yes` and `--no` keep files without remembering, so those runs keep
exiting 1 and the files keep showing.

`kolsch doctor`, `kolsch setup --sync-bundle --dry-run` and this notice
are built from the same planners, so they agree on what sync would do.

## Diagnosing bundle drift — `kolsch doctor`

The starter bundle at `bundles/kolsch/` is shipped by `kolsch setup`
from a packaged template. Over time the source may add new tools,
rename existing ones, or retire ones you still have on disk — and
`platform.yaml` + the starter agent YAMLs may still reference the
retired names.

`kolsch doctor` is a **read-only diagnostic** that reports drift.
Writes nothing.

```bash
kolsch doctor
```

Prints up to six sections:

1. **Missing tool files** — present in current source, absent from
   your workspace.
2. **Modified tool files** — differ from the current source, and the
   sha256 differs from the recorded `.kolsch/bundle-baseline.json`. A
   file identical to source is never listed. Files the baseline has no
   entry for are listed separately ("differs from source, no baseline
   entry"). With no baseline
   (workspaces installed before qol-089) the section is labelled
   **Modified vs source** and lists every shipped file that differs
   from the current source: a local edit or an older shipped version.
   The next `kolsch setup --sync-bundle` records the baseline.
3. **Retired-at-source tool files** — present in your workspace but
   no longer in source (e.g. `fetch_website.py` after the
   `fetch_url` / `browse_url` split).
4. **Retired-tool refs in kolsch-owned YAMLs** —
   `bundles/kolsch/agents/*.yaml` + `platform.yaml` still reference
   tools the migrator's manifest lists as retired. Each entry
   includes the proposed rewrite.
5. **Retired-tool refs in user-owned YAMLs** — same, but in
   `bundles/<your-bundle>/**`. `kolsch doctor` surfaces them; it
   never rewrites user code.
6. **Missing default permission rules** — default rules shipped since
   the workspace was set up. Missing `prompt` / `deny` defaults count
   as drift (`--sync-bundle` adds them); missing `allow` defaults are
   informational, with the `kolsch permissions add` command. See
   [New default rules on an existing workspace](permissions.md#new-default-rules-on-an-existing-workspace).

Plus, only when there's a finding, an advisory **Authorization**
section: `platform.yaml`'s rule list has no default-deny tail, or has
it somewhere other than last (or its `authorization` shape can't be
read). See
[If your rule list has no default-deny tail](permissions.md#if-your-rule-list-has-no-default-deny-tail).

Always, an advisory **Agent write access** section: the paths agent
file tools can never write (`platform.yaml`, `.kolsch/`, `.env`,
`.platform/`), plus grouped warnings for platform `allow` rules for
`bash`, `write_file` allows whose pattern points into `bundles/`, and
other `write_file` allows whose pattern uses a glob character (`*`,
`?`, `[`), which also matches `/` and `.`. See
[What agents can never change](permissions.md#what-agents-can-never-change-and-what-they-still-can).

`__pycache__/` directories and `.DS_Store` files are ignored
everywhere: they are never shipped, so they are not reported as
retired.

Exit code is `0` when clean, `1` when any drift was found. The
Authorization and Agent write access sections never change the exit
code. When there is drift, the report ends by pointing at
`kolsch setup --sync-bundle`.

## Repairing drift — `kolsch setup --sync-bundle`

Once `kolsch doctor` shows drift, repair it:

```bash
kolsch setup --sync-bundle          # interactive prompt per changed file
kolsch setup --sync-bundle --yes    # non-interactive; safe defaults
kolsch setup --sync-bundle --dry-run  # preview only; write nothing
```

Sync classifies every file in `bundles/kolsch/` into four buckets:

- **unchanged** — identical to the source copy, or sha256 matches the
  baseline (an older shipped version you never edited) → **overwrite**
  with the source copy.
- **locally-modified** — differs from source, and sha256 differs from
  the baseline (or the baseline has no entry for it) → **prompt** per
  file (`[k]eep local / [o]verwrite / [d]iff`). An interactive `[k]eep`
  is remembered until the file or the shipped version changes (see
  [Upgrading an existing workspace](#upgrading-an-existing-workspace)).
  `--yes` and `--no` keep the local file without remembering and exit
  non-zero as a partial-success signal.
- **added-at-source** — new file in source → **copy in** (`--no`
  skips it).
- **retired-at-source** — file in `bundles/kolsch/` that source
  doesn't ship → **prompt** (`[k]eep / [d]elete`). That covers both a
  file kolsch shipped and later retired and a file you added yourself,
  so `--yes` deletes it only when the baseline proves kolsch shipped it
  and you never edited it (it has an entry and the sha256 matches);
  anything else is kept. `--no` always keeps. An interactive `[k]eep`
  is remembered, as for modified files.

When a run keeps anything, it ends with
`N item(s) left unresolved — exit 1 (partial).` — under `--yes` /
`--no` it adds that a run without those flags reviews each item (with
`[d]iff`). Exit `1` here is the partial-success signal, not a crash.

**No baseline?** Workspaces installed before qol-089 have no
`.kolsch/bundle-baseline.json`. Sync still runs: it prints
`no baseline recorded — classifying against current source.`, treats
every file identical to source as unchanged and every other shipped
file as locally modified (so it asks before touching it), then
records a baseline.

After the file-level pass, the migrator's YAML rewrites are applied
to `bundles/kolsch/agents/*.yaml` and `platform.yaml`. Then
`.kolsch/bundle-baseline.json` is updated — even when the run was
partial — with an entry for every file sync copied, overwrote,
rewrote or found identical to source. A file you kept local keeps its
old entry (or stays unrecorded), so the next sync asks about it
again and nothing else. A file the migrator rewrote is recorded only
if you had not edited it; an edited one keeps its old entry, so your
edit is never refreshed away later. Files under `bundles/<your-bundle>/**` are
**never written** — the `kolsch doctor` report is where user-owned
drift gets surfaced.

Then sync checks `platform.yaml` for **default permission rules**
shipped since the workspace was set up (for example the `prompt` rule
for `kolsch/tools/jev_decide`). It offers the missing `prompt` / `deny`
defaults with a one-line reason each (`[a]dd all / [c]hoose each /
[s]kip`; `--yes` adds them, `--no` and `--dry-run` add nothing), adds
the accepted ones directly before the default-deny tail (or at the end
if there is none), and records each decision in
`.kolsch/default-rules.json` so a default is offered only once.
Missing `allow` defaults are only reported, with the
`kolsch permissions add` command. Without `--yes` / `--no`, sync asks
on the terminal; if stdin isn't a terminal it stops with an `error:`
line (exit 2) telling you to pass one of them. See
[New default rules on an existing workspace](permissions.md#new-default-rules-on-an-existing-workspace).

## When the workspace doesn't exist

Any non-setup command run against an uninitialised path produces:

```
No Kolsch workspace at /Users/jane/Documents/kolsch.

Run `kolsch setup` to create one, or point KOLSCH_WORKSPACE at an
existing workspace.
```

Exit code is `3` (distinct from generic error code `1`). No stack
trace, no runtime bootstrap — the CLI catches the missing workspace
before any subsystem boots. The behaviour is gated by
`workspace_is_initialized()` in `platform_runtime.workspace`.

## Fetching URLs

Two tools ship for pulling web pages:

- **`fetch_url`** — light, always available. Uses `httpx` +
  `trafilatura`. Good for static or server-rendered pages, JSON
  APIs, RSS/XML, and small binary payloads. Adds under 5 MB of
  dependencies, no headless browser, no Chromium download.
- **`browse_url`** — heavy, opt-in via the `kolsch[browse]` extra.
  Uses a real headless Chromium via Playwright and executes page
  JavaScript before capture. Reach for it when `fetch_url` returns
  a page shell without the content the model needs — the classic
  single-page-app case where the DOM is hydrated after
  `DOMContentLoaded`.

Start with `fetch_url`. If the page returns a shell
(`<div id="root"></div>` and nothing meaningful in the body), swap
to `browse_url`. Both are gated by an in-chat prompt by default
(actions `kolsch/tools/fetch_url` and `kolsch/tools/browse_url`); see
[Permissions](permissions.md) for how to tighten or loosen that
gate.

## Where the starter agent's model comes from

`kolsch setup` picks a model for the starter `kolsch/agents/helper` agent
and writes it to **`platform.yaml`'s `defaults.model`** — not to the
agent file. The helper's `bundles/kolsch/agents/helper.yaml`
intentionally has no `model:` field; the kernel's defaults
composition supplies the value at agent-load (the lowest-priority
layer, beneath any explicit `model:` on the agent itself).

What setup writes depends on what's connected:

- **No provider connected** → `defaults.model: {provider: mock,
  model: mock-model}`. The agent echoes input; useful for offline
  smoke tests.
- **One or more providers connected with `model_aliases`** →
  `defaults.model: <alias>` (preferring `opus` → `smart` → `cheap`).
- **Provider connected without aliases** → `defaults.model:
  {provider: <name>, model: <first-discovered-model>}`.

To change the model afterwards, either:

1. Edit `platform.yaml`'s `defaults.model` directly — affects every
   agent in the workspace that doesn't pin its own.
2. Add a `model:` field to a specific agent's YAML — overrides
   `defaults.model` for that agent only.

The setup-time `{{MODEL_LINE}}` placeholder mechanism was removed
in E-BUG-MODELLINE-001 (it collided with Phase 2.2 Jinja syntax in
agent YAMLs). The starter file now ships as plain, unprocessed YAML.

### Enabling `browse_url`

--8<-- "docs/_includes/browse-install.md"

Chromium is a ~150 MB download and is NOT installed
automatically — mid-chat downloads of that size are user-hostile,
so kolsch surfaces the missing binary as an actionable error
rather than a silent install.

## Scripted setup (CI, devcontainers)

`kolsch setup --non-interactive` skips every prompt and uses the
resolved-default workspace path. Two env vars wire a provider
without prompting:

- `KOLSCH_PROVIDER` — provider name (e.g., `openrouter`).
- `KOLSCH_PROVIDER_KEY` — API key value; lands in `.env`, never in
  `platform.yaml`.

```bash
# CI smoke
KOLSCH_PROVIDER=openrouter \
KOLSCH_PROVIDER_KEY="$OPENROUTER_API_KEY" \
  kolsch setup --non-interactive --root /tmp/kolsch-ci

kolsch run kolsch/agents/helper --input content="hello" \
  --root /tmp/kolsch-ci
```

Exit codes from setup: `0` on full success, `4` on partial
completion (e.g., skeleton created but no provider connected — the
mock starter agent still works). These are distinct from runtime
exit codes.

## If the setup test run fails

Setup's last step runs the starter agent once. If that exchange
fails, setup exits `4` and prints a "completed partially" banner
instead of "Kolsch is ready". The banner shows:

- whether the workspace and the starter agent were created on this
  run or were already there;
- the failed run's session id (`Session:`);
- the error code and message recorded in that session's event log,
  plus a one-line excerpt of the underlying error (`Detail:`) when
  there is one.

The first "Next steps" entry is the exact command to open that
session's full event log:

```bash
kolsch sessions events <the printed session id>
```

If the test run could not start at all — for example the agent's
config did not resolve — the banner's `Cause:` line says "the test run
did not start" and the next step is `kolsch doctor`, since no session
was created.

Fix the cause (for example, re-run `kolsch connect <provider>` for a
bad key), then re-run `kolsch setup` (it is idempotent and skips what
is already done) or run the agent directly with `kolsch run`.

## When a run fails

A failed `kolsch run` exits `2` and prints a short block: the error
code and message, a one-line cause, the session id, and the command to
inspect it:

```bash
kolsch sessions events <the printed session id>   # the full event log of the failed run
```

For more detail:

- `kolsch -v run …` adds third-party log records (CrewAI, the OpenAI
  SDK, httpx) and the Python traceback to stderr. `-v` goes before the
  command.
- `<workspace>/logs/kolsch.log` always has every log record at INFO and
  above, including tracebacks, whether or not you passed `-v`. It
  rotates at 5 MB (3 backups). kolsch never logs API keys or request
  headers; provider error bodies are logged verbatim (some providers
  echo a masked key fragment).

See [CLI: Failure output and logging](../architecture/cli.md#failure-output-and-logging)
for the exact format.

## Where to read next

- Tutorial: [first-agent.md](../tutorials/first-agent.md) — the
  guided tour of what a workspace gets you.
- Spec: [asset-management-contract.md §1.4](../explanation/asset-management-contract.md)
  — the resolution rules in normative form.
- Reference: [API: Platform Runtime](../api/index.md#platform-runtime)
  — `resolve_workspace` and `workspace_is_initialized` signatures.
