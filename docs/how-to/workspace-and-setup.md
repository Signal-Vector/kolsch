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

Prints up to five sections:

1. **Missing tool files** — present in current source, absent from
   your workspace.
2. **Modified tool files** — sha256 differs from the recorded
   `.kolsch/bundle-baseline.json`. Skipped with a one-line note when
   no baseline exists (older workspaces installed before qol-089).
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

Exit code is `0` when clean, `1` when any drift was found.

## Repairing drift — `kolsch setup --sync-bundle`

Once `kolsch doctor` shows drift, repair it:

```bash
kolsch setup --sync-bundle          # interactive prompt per changed file
kolsch setup --sync-bundle --yes    # non-interactive; safe defaults
kolsch setup --sync-bundle --dry-run  # preview only; write nothing
```

Sync classifies every file in `bundles/kolsch/` into four buckets:

- **unchanged** — sha256 matches baseline → **overwrite** with the
  source copy.
- **locally-modified** — sha256 differs from baseline → **prompt**
  per file (`[k]eep local / [o]verwrite / [d]iff`). Non-interactive
  runs default to keep-local and exit non-zero as a partial-success
  signal.
- **added-at-source** — new file in source → **copy in**.
- **retired-at-source** — file in workspace no longer in source →
  **prompt** (`[k]eep / [d]elete`). Non-interactive default is
  keep + skip.

After the file-level pass, the migrator's YAML rewrites are applied
to `bundles/kolsch/agents/*.yaml` and `platform.yaml`. `.kolsch/bundle-baseline.json`
is refreshed. Files under `bundles/<your-bundle>/**` are **never
written** — the `kolsch doctor` report is where user-owned drift
gets surfaced.

`--yes` and `--no` are symmetric knobs for scripting — both use the
safe-default answers (keep-local for modified, keep for
retired-at-source). Use whichever reads better in your automation.

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

## Where to read next

- Tutorial: [first-agent.md](../tutorials/first-agent.md) — the
  guided tour of what a workspace gets you.
- Spec: [asset-management-contract.md §1.4](../explanation/asset-management-contract.md)
  — the resolution rules in normative form.
- Reference: [API: Platform Runtime](../api/index.md#platform-runtime)
  — `resolve_workspace` and `workspace_is_initialized` signatures.
