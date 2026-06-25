# QoL: Workflow run storage stats + list commands

**Status:** Proposed (Phase 3.x follow-up after Phase 3.0 lands)
**Effort:** ~1-2 days
**Owner:** TBD
**Depends on:** Phase 3.0 + Phase 3.1 executor (so there are real run JSONLs on disk to inspect)

---

## Background

Phase 3.0 master spec §2.3 (per Round 4 locked decision #14) establishes the workflow run retention model:

- Workflow run event logs are plain JSONL files at `runs/<run_id>/events.jsonl` (or the existing on-disk convention).
- "Forever retention" means **the engine will not auto-prune anything**. Once a run finishes, its event log sits on disk until an operator deletes it.
- Operators retain full control via standard filesystem tools (`rm`, `find -mtime`, `tar`, etc.).
- Fork-from-event implication: if the parent run's JSONL is deleted, fork attempts raise `WorkflowRunNotFound`.
- SaaS/multi-tenant retention cost concerns are explicitly deferred to Phase 4+ multi-tenancy work.

That model is sound, but it leaves operators with no built-in visibility. Today they would have to `du -sh runs/` and `ls -la runs/` and stitch together which directories are stale runs vs. in-flight runs vs. forks. This QoL ships first-class kolsch CLI commands so operators can SEE what's on disk and make informed delete decisions.

## Scope

### In scope

- `kolsch workflows list` — lists runs with `run_id`, start timestamp, age, on-disk size, status (`running` | `completed` | `failed` | `cancelled`), and fork-relationship (parent `run_id` if forked).
- `kolsch workflows list --older-than 90d` — age filter (accepts `d`, `h`, `m` suffixes; e.g., `30d`, `12h`).
- `kolsch workflows stats` — summary: total run count, total size on disk, oldest run, newest run, breakdown by status.
- `kolsch workflows show <run_id>` — augments the existing/in-flight executor-side `show` command (Phase 3.1) with on-disk size and age fields. (If `show` does not yet exist when this QoL lands, scope it here as a thin wrapper that prints the same fields list mode shows, plus the event-log path.)
- All commands honor `--format text|json` per the cross-cutting CLI convention.

### Out of scope

- **Auto-pruning.** The engine never prunes; this QoL stays read-only. Deletion is a manual `rm -rf runs/<run_id>` or operator-scripted.
- **Archival** (compression, cold-storage move, tarball-and-ship). Separate future QoL if real operator demand surfaces.
- **Cost analysis.** Token and dollar cost is an agent-layer concern per Round 4 locked decision #16; this QoL does not aggregate cost across runs.
- **Multi-tenant retention policy** (per-tenant quotas, eviction). Deferred to Phase 4+ multi-tenancy work.
- **Rewriting / repairing event JSONLs.** Read-only inspection only.

## Why this is a QoL, not a feature

Phase 3.0 §2.3 already establishes the file-based retention model and the operator-controlled deletion contract. This QoL changes no semantics — it just gives operators visibility into what already exists on disk. The engine's behavior is unchanged: it still never prunes, it still writes to the same on-disk layout, fork-from-event still raises `WorkflowRunNotFound` when the parent is gone.

## CLI shape

```
kolsch workflows list [--older-than DURATION] [--status STATUS] [--format text|json]
kolsch workflows stats [--format text|json]
kolsch workflows show <run_id> [--format text|json]
```

- `--older-than DURATION` — accepts `<n>d`, `<n>h`, `<n>m` (90 days, 12 hours, 30 minutes). Filters to runs whose start timestamp is older than `now - DURATION`.
- `--status STATUS` — optional filter; one of `running` | `completed` | `failed` | `cancelled`.
- `--format json` envelopes per the cross-cutting CLI convention:
  - `list`: `{command: "workflows list", status: "ok", data: {runs: [{run_id, started_at, age_seconds, size_bytes, status, parent_run_id}, ...]}}`
  - `stats`: `{command: "workflows stats", status: "ok", data: {total_runs, total_size_bytes, oldest_run_id, oldest_started_at, newest_run_id, newest_started_at, by_status: {running, completed, failed, cancelled}}}`
  - `show`: `{command: "workflows show", status: "ok", data: {run_id, started_at, finished_at, age_seconds, size_bytes, status, parent_run_id, events_jsonl_path}}`

## Status detection

A run's status is derived from its event log:

- **`running`** — no terminal event (`workflow.completed`, `workflow.failed`, `workflow.cancelled`) AND mtime within recent activity window (default 60s).
- **`completed`** / **`failed`** / **`cancelled`** — last event in the JSONL is the corresponding terminal event.
- **Inferred-stale**: no terminal event AND mtime older than the activity window — surfaced as `running` with an `is_stale: true` flag in JSON output (and a `(stale)` annotation in text output), so operators can spot crashed runs and decide whether to delete.

## Acceptance Criteria

1. `kolsch workflows list` exists as a subcommand; visible in `kolsch --help` and `kolsch workflows --help`.
2. `list` walks the runs directory and reports each run's `run_id`, `started_at`, age, on-disk size, status, and `parent_run_id` (when forked).
3. `kolsch workflows stats` reports total run count, total size on disk, oldest run, newest run, and per-status breakdown.
4. `--older-than DURATION` filters `list` output correctly (a run started 91 days ago appears under `--older-than 90d`; a run started 89 days ago does not).
5. `--status STATUS` filters `list` output to the requested status.
6. `--format json` envelope conforms to the cross-cutting CLI convention for all three commands.
7. Stale-running runs (no terminal event, mtime older than activity window) are flagged (`is_stale: true` in JSON, `(stale)` annotation in text).
8. Live-run safety: commands do not block, lock, or rewrite event JSONLs. Reads are non-blocking; partial last lines are tolerated.
9. Tests: at least one fixture per run status (running, completed, failed, cancelled, stale); per-command golden-output tests for both `text` and `json` modes; `--older-than` boundary test.

## Effort estimate

- Run-directory walker + per-run status/size/age extraction: ~4-6 hours
- CLI wiring (`list`, `stats`, `show`, flags): ~2-3 hours
- Tests (fixtures + golden outputs + boundary cases): ~3-4 hours
- Docs (operator-facing section in `docs/workflows.md` covering "managing on-disk runs"): ~1 hour
- **Total: ~1.5-2 days**

## Risks

- **File scan cost on very large run dirs.** Walking thousands of run directories and reading the last line of each JSONL can be slow. Mitigation: cache the terminal event by tailing only the last few KB of each JSONL; document expected performance at 10k runs.
- **Race with active run writes.** An in-flight run is appending to its JSONL while `list` reads it. Mitigation: mtime check + tolerate truncated last lines (skip on JSON decode error); never lock or write.
- **mtime as proxy for "started_at"** — directory mtime can be touched by unrelated operations. Mitigation: prefer the first event's `ts` field over filesystem mtime when reading the JSONL header.
- **Disk-size accounting** — `size_bytes` should be the run directory's recursive size (event log + any sidecar files the executor writes). Use `os.walk` + `os.path.getsize`; document.

## Cross-references

- Master workflow spec `process/phase3.0-workflow-spec.md` §2.3 — forever-retention "files on disk; operator-controlled" model (Round 4 locked decision #14)
- Master workflow spec §20 — open questions including the deferred SaaS multi-tenancy retention concern
- The cross-cutting CLI convention: `process/qol-cli-format-output-rollout.md`
- Round 4 locked decision #16 — cost telemetry is out of scope (agent-layer concern), and is explicitly NOT folded into this storage-stats QoL
