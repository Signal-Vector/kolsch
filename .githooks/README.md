# Team git hooks

Shared git hooks for the kolsch repo — currently only codanna index
refresh triggers.

## One-time setup (per clone)

```
git config core.hooksPath .githooks
```

That's it. Run once after `git clone`; git will fire hooks from this
directory instead of `.git/hooks/` (which isn't tracked and doesn't
propagate to teammates).

To verify:

```
git config --get core.hooksPath      # → .githooks
```

## What the hooks do

| Hook | Fires on | Action |
|---|---|---|
| `post-commit` | `git commit` | Rebuilds the codanna index in the background |
| `post-merge` | `git pull` / `git merge` | Same |
| `post-checkout` | branch switch (skips file checkouts) | Same |

All three use `codanna index -f` — the `-f` (force) flag is a
workaround for a codanna incremental bug (2026-07-15) where the plain
`codanna index` no-ops even when files have changed on disk. If that
bug is fixed upstream, drop `-f` for a much faster incremental
refresh.

Hooks run in the background via `nohup … &` so git operations return
immediately; logs land at `/tmp/codanna-reindex-kolsch.log` so a
failed rebuild is inspectable without spamming your terminal.

If `codanna` isn't on `PATH` the hooks exit cleanly (0) — safe to
have installed on machines that don't run codanna.

## Alternative: daemon mode

`codanna serve --watch` is the officially-supported real-time
file-watcher mode (config knobs live in `.codanna/settings.toml
[file_watch]`). Handles uncommitted edits too. If you prefer that,
the hooks above are complementary — safe to run both.
