# QoL: Remove 1-seg ref shorthand; codemod existing YAML to 3-seg

**Status:** Proposed (depends on Phase 3.0 landing first)
**Effort:** ~1-2 days
**Owner:** TBD
**Depends on:** Phase 3.0 (master workflow spec §13.1 — "3-seg refs required EVERYWHERE; no 1-seg shorthand")

---

## Background

Phase 3.0 locked Round 3 decision R3 #5: **all asset refs are full 3-segment** (`<bundle>/<asset_type>/<name>`). The 1-segment shorthand (e.g., `bash` for `kolsch/tools/bash`) is REMOVED universally.

Rationale:
- Built-in workflow node types live at `kolsch/nodes/<name>` and require full 3-seg refs
- Removing 1-seg shorthand frees the 14 built-in type names as legal bundle names (a bundle named `bash` is now fine; its assets reference via full 3-seg `bash/nodes/<name>`)
- One canonical form everywhere — no precedence rules, no field-context-aware resolution

Phase 3.0's Step 0 (already in scope) removed `resolve_shorthand` from `src/assets/refs.py`. New rules in workflow YAML files reject non-3-seg `type:` values with `WorkflowInvalidRef`.

**But:** existing YAML across `bundles/`, `platform.yaml`, agent YAMLs, permissions rules, and skill manifests was authored under the 1-seg-shorthand-allowed convention. This QoL ships the codemod to upgrade them mechanically.

## Scope

### In scope

- A `kolsch refs upgrade-to-3-seg` CLI command (Click subcommand under a new `refs` group, or under `tools`)
- Walks YAML files in the workspace (recursive scan of `bundles/`, `platform.yaml`, and any other YAML referenced by Phase 1 conventions)
- For each YAML file, identifies fields known to carry asset refs:
  - Agent YAML: `tools: [...]`, `skills: [...]`, prompt refs
  - Bundle.yaml: `permissions: [...]` rules (`what:` field, new `from:` field)
  - Platform.yaml: same as bundle.yaml
  - Workflow YAML: `extends: [...]`, `type:` (node types), agent refs in agent nodes, sub-workflow refs in workflow nodes, `permissions: [...]` rules (note: edges no longer carry a `ref:` field — see Cross-references)
  - Skill metadata: any ref fields
- Upgrades 1-seg refs to full 3-seg using catalog lookup:
  - Catalog is scanned to resolve the asset_type for each 1-seg name
  - If unambiguous (exactly one catalog match), upgrade in place
  - If ambiguous (multiple matches in different bundles or asset types), surface as a conflict the operator resolves manually
- Preserves YAML comments and formatting (uses `ruamel.yaml` round-trip mode — already in Phase 3.0 Step 0)
- Dry-run mode (`--dry-run`): prints planned upgrades without writing
- Diff output (`--diff`): shows the change per file
- `--format json` per the cross-cutting CLI convention (`qol-cli-format-output-rollout.md`)

### Out of scope

- Migrating event-log JSONLs (forever-retention semantics mean old JSONLs may contain 1-seg refs in payloads; codemod does NOT rewrite history)
- Upgrading any docs or comments that mention refs (only YAML field values are rewritten)
- Upgrading source code Python strings (if any tool tests hardcode 1-seg refs, they're updated by hand)

## Why this is a QoL, not a feature

Phase 3.0's spec already declares 3-seg as the only valid form. The validator already rejects non-3-seg refs at the gate. This QoL is the mechanical migration that lets existing workspaces upgrade without manual sed across dozens of files.

## CLI shape

```
kolsch refs upgrade-to-3-seg [PATH] [--dry-run] [--diff] [--format text|json]
```

- `PATH` defaults to the workspace root (resolved via existing kolsch workspace discovery)
- `--dry-run` prints planned upgrades without writing files
- `--diff` shows per-file diffs of planned upgrades
- `--format json` envelope: `{command: "refs upgrade-to-3-seg", status: "ok", data: {files_scanned, files_upgraded, refs_upgraded, conflicts: [...]}}`

## Conflict resolution

When a 1-seg ref resolves to multiple catalog entries (e.g., bundle `foo` has both `foo/agents/bar` and `foo/tools/bar`), the codemod cannot disambiguate. Reports the conflict:

```
CONFLICT in bundles/acme/agents/triage.yaml line 12:
  ref "bar" matches both 'foo/agents/bar' and 'foo/tools/bar'
  Resolve manually by replacing with the explicit 3-seg form.
```

Conflicts exit non-zero; operator hand-resolves each.

## Acceptance Criteria

1. `kolsch refs upgrade-to-3-seg` exists as a subcommand; visible in `kolsch --help`.
2. Runs against the workspace; identifies YAML files with 1-seg refs in known ref-carrying fields.
3. Upgrades each 1-seg ref to its 3-seg equivalent via catalog lookup.
4. Preserves YAML formatting and comments (uses ruamel.yaml round-trip mode).
5. `--dry-run` lists planned upgrades without writing.
6. `--diff` shows per-file diffs.
7. Conflict reporting: ambiguous 1-seg refs are surfaced clearly; codemod does NOT make a guess.
8. `--format json` envelope per the cross-cutting CLI convention.
9. Tests: at least one fixture per known ref-carrying YAML shape (agent YAML, bundle.yaml, platform.yaml, workflow YAML, skill metadata); each fixture asserts upgrade + format preservation.
10. Idempotent: running the codemod twice yields the same result the second time (no diffs).

## Effort estimate

- CLI command + workspace walker + ruamel-round-trip-load/dump pipeline: ~4-6 hours
- Per-shape ref-extractor logic (one extractor per known YAML shape): ~3-4 hours
- Conflict detection + reporting: ~2 hours
- Tests: ~3-4 hours
- Docs (update `docs/workflows.md` for the codemod usage): ~1 hour
- **Total: ~1.5-2 days**

## Risks

- **Workspace YAML can be hand-edited mid-codemod** — codemod should fail-loud if a target file changes during the run (file-modification-time check before write).
- **ruamel.yaml round-trip preserves anchors but may shift quoting style** — golden tests on a few representative YAMLs catch any unintended drift.
- **Catalog scan cost** — for very large workspaces, the codemod re-scans the catalog; acceptable for a once-per-workspace migration.
- **Conflict resolution UX** — if many conflicts surface, operator wades through them manually; consider a `--interactive` mode in a follow-up if real usage hits this.

## Cross-references

- Master workflow spec `process/phase3.0-workflow-spec.md` §13.1 — the locked 3-seg-only rule
- Master workflow spec §4.0.1 — the built-in dispatch via `kolsch/nodes/<name>`
- Master workflow spec §6.4 — edge structure is `{from, to, binds, branch}`. The edge `ref:` field is **REMOVED** per Phase 3.0's multi-input redesign (Round 4 item #11). Earlier drafts of this QoL also included an edge `ref:` rename codemod; that scope is now moot — there is no edge `ref:` field left to migrate. Only the 1-seg → 3-seg upgrade scope remains.
- Parser-validator spec `process/phase3.0-parser-validator-spec.md` Step 1 — `resolve_shorthand` removal
- The cross-cutting CLI convention: `process/qol-cli-format-output-rollout.md`
