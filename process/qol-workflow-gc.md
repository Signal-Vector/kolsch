# QoL: kolsch workflows gc — operator-controlled run retention

**Status:** Proposed (Phase 3.x deliverable; specific phase TBD in 3.1+ planning)
**Effort:** ~1-2 days
**Owner:** TBD
**Depends on:** Phase 3.0 (specifically Round 4 locked decision #14 — the file-based retention model) + Phase 3.1 executor (so there are real workflow run JSONLs on disk to GC)

---

## Background

Phase 3.0 master spec §2.3 (per Round 4 locked decision #14) establishes the workflow run retention model:

- Workflow run event logs live as plain files at `runs/<run_id>/` on disk.
- The engine does NOT auto-prune anything. Once a run finishes, its directory sits on disk until an operator removes it.
- Operators retain full control over retention.
- Fork-from-event implication: if a parent run's JSONL is deleted, fork attempts raise `WorkflowRunNotFound`.

Round 5 locked decision H6 commits to ship a first-class CLI tool that lets operators select and delete old runs with safe defaults — specifically: protecting fork-chain integrity so a GC pass doesn't silently break the ability to fork from a still-referenced parent.

This QoL pairs with `qol-workflow-storage-stats.md` (read-only inspection: `list` / `stats` / `show`). Where that QoL lets operators SEE what's on disk, this one lets them PRUNE what's on disk.

## Scope

### In scope

- `kolsch workflows gc --older-than <duration>` — selects runs whose start timestamp is older than `now - DURATION` (e.g., `90d`, `30d`, `12h`).
- `--dry-run` — prints what would be deleted (run_id, age, size) without mutating the filesystem.
- `--keep-forked` — preserves any run that is referenced as a parent (i.e., another run on disk holds a `parent:` pointer to it). Avoids breaking fork chains.
- Reports: count of runs deleted, total bytes freed, count of runs preserved due to `--keep-forked`.
- `--format text|json` envelope per the cross-cutting CLI convention (`qol-cli-format-output-rollout.md`).

### Out of scope

- **Auto-pruning.** The engine never prunes; this remains a strictly operator-driven command.
- **Archival or compression.** No tarball-and-ship, no cold-storage move. Separate future QoL if real operator demand surfaces.
- **Per-trace retention.** Preserving specific `trace_id` chains across runs is a separate feature; this QoL filters by age and fork-relationship only.
- **Multi-tenant retention policy** (per-tenant quotas, eviction rules). Deferred to Phase 4+ multi-tenancy work.

## Why this is a QoL, not a feature

Phase 3.0 §2.3 already establishes the file-based retention model and the operator-controlled deletion contract. This QoL changes no engine semantics — it ships the operator-facing tool that makes that contract manageable in practice. The engine still never prunes; it still writes to the same on-disk layout; fork-from-event still raises `WorkflowRunNotFound` when the parent is gone. The GC command is a convenience wrapper over what an operator could otherwise script with `find runs/ -mtime +90 -exec rm -rf {} \;`, plus the fork-chain safety net.

## CLI shape

```
kolsch workflows gc --older-than <duration> [--dry-run] [--keep-forked] [--format text|json]
```

- `--older-than DURATION` — required. Accepts `<n>d`, `<n>h` (e.g., `90d`, `12h`). Selects runs whose start timestamp is older than `now - DURATION`.
- `--dry-run` — prints planned deletions without touching the filesystem.
- `--keep-forked` — walks `parent:` pointers across all on-disk runs and excludes any run that is still referenced as a parent.
- `--format json` envelope:
  ```
  {
    "command": "workflows gc",
    "status": "ok",
    "data": {
      "runs_selected": <int>,
      "runs_deleted": <int>,
      "bytes_freed": <int>,
      "runs_preserved_forked": <int>
    }
  }
  ```
  When `--dry-run` is set, `runs_deleted` is `0` and `bytes_freed` reports the would-be-freed size; `runs_selected` reflects what would be deleted.

## Acceptance Criteria

1. `kolsch workflows gc` exists as a subcommand; visible in `kolsch --help` and `kolsch workflows --help`.
2. `--older-than` accepts duration strings (`Nd`, `Nh`) and rejects malformed values with a clear error.
3. Walks `runs/<run_id>/` directories; reads each run's metadata (first event in the JSONL) to determine `started_at` and any `parent:` pointer.
4. `--keep-forked` walks `parent:` pointers across all on-disk runs and excludes any run that is still referenced as a parent of another existing run.
5. `--dry-run` prints what would be deleted (with per-run sizes) and performs no filesystem mutation; idempotent.
6. `--format json` envelope conforms to the cross-cutting CLI convention with the fields listed above.
7. Tests:
   - `--older-than` precision boundary (a run started 91 days ago is selected by `--older-than 90d`; a run started 89 days ago is not).
   - `--dry-run` performs no filesystem mutation (post-run directory listing is byte-identical to pre-run).
   - `--keep-forked` correctness (a 100-day-old parent referenced by a 1-day-old child is preserved; the same parent with no living child is deleted).
8. Live-run safety: GC never deletes a run whose status is `running` or whose JSONL mtime is within the last 5 minutes (see Risks).

## Effort estimate

- CLI command + run-directory walker + age filter: ~3-4 hours
- `--keep-forked` fork-chain walker (build parent-pointer index across all runs, mark survivors): ~2-3 hours
- Tests (fixtures for selection boundary, dry-run no-mutation, fork-chain preservation): ~3-4 hours
- Docs (operator-facing section in `docs/workflows.md` covering GC usage and the fork-chain safety net): ~1 hour
- **Total: ~1.5-2 days**

## Risks

- **Scan cost on very large run dirs.** Walking thousands of run directories twice (once to determine age, once to build the parent-pointer index) is acceptable for a once-per-period operator-driven command. If real workloads hit pathological sizes, consider a `--limit` flag in a follow-up.
- **Race with active run writes.** A GC pass that runs while the executor is appending to an in-flight run's JSONL must not delete that run. Mitigation: skip any run whose status is `running` (terminal event not yet observed) OR whose JSONL mtime was updated within the last 5 minutes. Document the 5-minute guard.
- **`--keep-forked` dependency graph cost.** Building the parent-pointer index requires reading the head of every on-disk run's JSONL. Cache the index once per GC invocation; don't re-read per candidate. Document expected performance at 10k runs.
- **Operator confusion between `--dry-run` and a real GC.** Text output should LOUDLY label dry-run mode (e.g., a `DRY-RUN` header banner) so the operator does not mistake the report for a completed action.

## Cross-references

- Master workflow spec `process/phase3.0-workflow-spec.md` §2.3 — file-based retention; "files on disk; operator-controlled" model (Round 4 locked decision #14; Round 5 item H6 commits to shipping this CLI tool)
- Companion QoL: `process/qol-workflow-storage-stats.md` — read-only inspection (`list` / `stats` / `show`); GC is the corresponding write-side tool
- The cross-cutting CLI convention: `process/qol-cli-format-output-rollout.md`
