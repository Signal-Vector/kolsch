# Team git hooks

Shared git hooks for the kolsch repo — codanna index refresh triggers
plus a `pre-push` gate for pushes to `main`.

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
| `pre-push` | `git push` to `refs/heads/main` | Runs the FULL local landing gate (`scripts/commit-gate`) and blocks the push if it fails. **Dormant:** `main` is protected by a GitHub ruleset and only accepts changes through PRs, so direct pushes are rejected and this hook no longer fires in practice. Run `scripts/commit-gate` by hand before opening a PR (see `AGENTS.md` → "Landing a change"). |

The three codanna `post-*` hooks use `codanna index -f` — the `-f`
(force) flag is a
workaround for a codanna incremental bug (2026-07-15) where the plain
`codanna index` no-ops even when files have changed on disk. If that
bug is fixed upstream, drop `-f` for a much faster incremental
refresh.

Hooks run in the background via `nohup … &` so git operations return
immediately; logs land at `/tmp/codanna-reindex-kolsch.log` so a
failed rebuild is inspectable without spamming your terminal.

If `codanna` isn't on `PATH` the hooks exit cleanly (0) — safe to
have installed on machines that don't run codanna.

## The `pre-push` gate (qol-125)

`pre-push` runs the **full** local landing gate — a clean
`compose.test.yml` build then ruff → ruff-format → mypy → chat-hex lint
→ the whole `tests/` tree WITH coverage (85% floor) — via
`scripts/commit-gate`. It fires **only** when `refs/heads/main` is among
the pushed refs and the local sha is not all-zeros (an all-zeros sha is a
branch *delete* — nothing to validate). Pushes to any other branch, and
branch deletes, are a no-op. On gate failure the hook exits non-zero and
git aborts the push.

GitHub CI runs a faster **sanity** subset on every push (the whole tree
minus the two heavy dirs, no coverage), so the full gate is enforced
**locally** here. `git push --no-verify` skips it. See `AGENTS.md` →
Risks & Non-Guarantees for the accepted tradeoffs (no GitHub backstop).

## Alternative: daemon mode

`codanna serve --watch` is the officially-supported real-time
file-watcher mode (config knobs live in `.codanna/settings.toml
[file_watch]`). Handles uncommitted edits too. If you prefer that,
the hooks above are complementary — safe to run both.
