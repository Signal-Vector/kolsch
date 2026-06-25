# QoL: Rename code-side "authz" to "policy" — module + class consistency with user-facing terminology

**Status:** Proposed (Phase 3.x; after Phase 3.0 lands)
**Effort:** ~1 day
**Owner:** TBD
**Depends on:** Phase 3.0 (Round 5 item H1 ships the user-facing rename)

---

## Background

Phase 3.0 Round 5 item H1 renamed the user-facing authz terminology to **"policy"**:

- YAML field: `permissions:` → `policy:`
- Event kind: `authz.decision` → `policy.decision.made`
- Master spec §13.2 renamed to "Policy"

Phase 3.0 LEFT the code-side names unchanged for scope discipline:

- `src/providers/authz.py` (module path)
- `RuleBasedAuthorizationProvider` (class)
- `PermissionLayer` (value object introduced in Round 4 item #7)

This QoL completes the rename for consistency between the user-facing terminology and the codebase identifiers. Without it, every developer reading the code has to translate "authz" mentally back to "policy" — minor papercut, easy fix, but explicitly deferred from Phase 3.0 to keep that release's scope tight.

## Scope

### In scope

- Rename `src/providers/authz.py` → `src/providers/policy.py`
- Rename class `RuleBasedAuthorizationProvider` → `RuleBasedPolicyProvider`
- Rename `PermissionLayer` → `PolicyLayer` value object
- Update any related test files (`tests/providers/test_authz*.py` → `tests/providers/test_policy*.py`)
- Update all imports across the codebase
- Update docstrings referring to "authz" or "authorization" where the natural replacement is "policy"
- Update any in-code constant names matching the same pattern (e.g., module-level `AUTHZ_*` constants → `POLICY_*`)

### Out of scope

- **Behavior changes** — purely a name refactor. No logic touched.
- **Renaming the `Rule` class or rule-shape** — rules ARE rules; they're contained within policies, not equivalent to them. The "rule" terminology is correct as-is.
- **User-facing CLI command renames** — none needed; the rename is internal. The user-facing YAML field rename ships at Phase 3.0.
- **The `PolicyLayer.name` string values** stay as `"workflow" | "agent" | "bundle" | "platform"` — those are layer names, not the policy term itself.
- **Event-log JSONL migration** — historical events with `authz.decision` kind stay as-is (forever-retention semantics). The Phase 3.0 rename already establishes the new kind for new events.

## Why this is a QoL, not a Phase 3.0 task

Phase 3.0's scope is **parser + validator + CLI for workflows**. Touching the authz provider module is outside that perimeter, even though the user-facing rename ships at Phase 3.0. The code-side rename is mechanical (no behavior change, no spec ambiguity to resolve) and can ship anytime after Phase 3.0 — pulling it into Phase 3.0 only adds review surface area without de-risking the release.

## Implementation approach

1. Use `git grep` and `rg` to enumerate every `authz`, `Authorization`, `Permission` reference in `src/` and `tests/`.
2. Use `git mv src/providers/authz.py src/providers/policy.py` for the module rename — preserves history.
3. Class and value-object renames: editor-level replace across the codebase; verified by a full import audit (`rg "from kolsch.providers.authz" src/ tests/` returns empty post-rename).
4. Run the full test suite (`pytest`) to verify no breakage.
5. Update cross-references in `docs/`, `process/`, and `CLAUDE.md` if present.

## Acceptance Criteria

1. `src/providers/policy.py` exists; `src/providers/authz.py` does not.
2. Class `RuleBasedPolicyProvider` exists; `RuleBasedAuthorizationProvider` does not.
3. Value object `PolicyLayer` exists; `PermissionLayer` does not.
4. All Python files: no `authz` or `Authorization` in identifiers (substring grep clean) except where required for stdlib HTTP `Authorization` header handling (rare; flag any remaining hits in the PR description).
5. Test files renamed under `tests/providers/test_policy*.py`.
6. Full test suite passes.
7. Documentation under `process/`, `docs/`, and `CLAUDE.md` updated for the rename.
8. Import audit clean: `rg "authz" src/ tests/` returns no hits (modulo the HTTP-header carve-out above).
9. Git history for the renamed module is preserved (`git log --follow src/providers/policy.py` shows pre-rename history).

## Effort estimate

- File rename + class rename + import updates: ~3-4 hours
- Tests + verification: ~2-3 hours
- Docs updates: ~1-2 hours
- **Total: ~1 day**

## Risks

- **Concurrent PRs touching the authz module at rename time** — coordinate with active branches; ideally land this when no in-flight work touches `src/providers/authz.py`.
- **External callers** — if any kolsch SDK consumers exist that import `kolsch.providers.authz`, they need to update their imports. Call out in release notes for the QoL.
- **Stdlib `Authorization` carve-out** — easy to over-rewrite; the codemod / find-replace must skip HTTP-header literal strings and any `Authorization:` header constants. Verified by post-rename grep.

## Cross-references

- Master workflow spec `process/phase3.0-workflow-spec.md` — Round 5 item H1 (user-facing rename in Phase 3.0)
- Round 4 #7 — `PermissionLayer` value object introduction (the to-be-renamed type)
- `src/providers/authz.py` — the module to rename
