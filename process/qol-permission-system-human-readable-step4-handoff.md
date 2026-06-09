# qol-permission-system-human-readable.md — Step 4 handoff

**Status:** REVISE-PENDING-USER. Step 4 is functionally complete but
the reviewer verdict stays REVISE-PENDING-USER until the user
confirms the migration + chat behavior against their own workspace
(`~/Documents/kolsch`).

## What ships in Step 4

### Code
- `src/cli/_setup.py` — new `_maybe_migrate_authz_rules_to_qol_schema`
  hook runs unconditionally on `kolsch setup` re-run; converts legacy
  rules + atomically rewrites `platform.yaml` + emits
  `config.deprecated_regex_rule` Recoverable warnings for unconvertible
  legacy entries. Resolves carryover **E-QOL-PERMSYS-002**.
- `src/cli/_setup.py:_workspace_only_default_rules` — new
  workspaces are now born in the new schema (`what/pattern/effect`,
  no `who`/`id`); previously emitted legacy shape. Setup re-runs
  are byte-idempotent.
- `src/cli/permissions.py:_cmd_list` — legacy rules render with
  `[legacy regex — please convert]` annotation; new-shape rules
  show `what=... pattern=...`.
- `src/cli/_slash.py:_governing_rule` — `/tools` now handles both
  legacy and new-shape rules; bridges action↔tool_ref for new-shape.

### Tests
- `tests/integration/test_perm_system_e2e.py` — five PERM-SYS
  cases pinning the cross-step properties.
- `tests/component/test_setup.py` — defaults assertions updated
  for new schema.

### Docs
- `docs/how-to/permissions.md` — full rewrite. Sections: rule
  shape, tool refs (qualified required), patterns (glob), editing
  rules (three surfaces), denial recovery capsule, migration from
  legacy, four-level stack, bundle permissions.
- `docs/reference/schemas.md` §A.11.4 — `PermissionRule` documented
  as `{what, pattern, effect}` with the **Legacy rule form**
  subsection naming the `config.deprecated_regex_rule` event.
  §A.13 BundleManifest updated to mirror.
- `docs/how-to/chatting-effectively.md` — bash-approval section
  switched to glob examples; new `/perm` section documenting the
  five verbs.
- `docs/explanation/error-model.md` §4.16 — adds the four QoL
  codes (`authz.bare_tool_ref`, `authz.pattern_invalid`,
  `authz.tool_ref_unknown`, `config.deprecated_regex_rule`);
  documents the dispatcher's `tool.unauthorized` wrap behavior.
  Resolves carryover **E-QOL-PERMSYS-003**.
- `docs/tutorials/first-agent.md` — platform.yaml example uses
  new schema.
- `process/test-catalog.md` — new **Part 9.8.3 Permission System
  (PERM-SYS)** with five test entries.

### Process writeups
- `process/qol-permission-system-human-readable-step4-prefix-verification.md`
- `process/qol-permission-system-human-readable-step4-manual-e2e.md`
- `process/qol-permission-system-human-readable-step4-handoff.md`
  (this file)

### Carryover status

| ID | Source | Resolved in Step 4 |
|---|---|---|
| E-QOL-PERMSYS-001 | Step 2 reviewer | **PARTIAL**. The transitional `Rule.who` / `Rule.id` fields stay — `_modal.py` still writes them via the legacy fallback when no `tool_ref_lookup` is bound, and Step 3 promised the field-drop in Step 3 but reviewer carried it forward. New-shape construction is the primary path; legacy fields can be dropped in a follow-up PR once non-chat callers stop relying on them. **Not blocking.** |
| E-QOL-PERMSYS-002 | Step 2 reviewer | ✅ Setup-runner wiring shipped. `_cmd_list` marks legacy rules. |
| E-QOL-PERMSYS-003 | Step 3 reviewer | ✅ `error-model.md` reconciles `authz.denied` ↔ `tool.unauthorized`. Unused `AuthzDenied` class retained (documented) — its removal is a low-priority follow-up that would require updating the catalog round-trip tests. |
| E-QOL-PERMSYS-004 | Step 3 reviewer | ✅ `permissions.md` "Why didn't my /perm rule take effect?" subsection documents the broader-shadow case + points at `/perm list`. Code unchanged. |

## Test results

```
$ uv run pytest tests/
1136 passed, 1 skipped, 4 deselected in 68.00s
```

- 1085 passing tests at the Step 1 baseline.
- +25 from Step 2 (`tests/unit/test_authz_qol_schema.py`,
  `tests/integration/test_authz_qol_migration.py`).
- +19 from Step 3 (`tests/component/cli/test_perm_slash.py` + the
  promised modal-with-lookup test).
- +5 from Step 4 (`tests/integration/test_perm_system_e2e.py`).
- 2 existing setup-migration tests adjusted for the new default
  schema; no semantics changed.

## Pre-fix verification

See `process/qol-permission-system-human-readable-step4-prefix-verification.md`.
Each PERM-SYS-01..05 documents which Step-2/3 PR shipped the
piece that fixes the pre-fix failure mode, and what the user's
workspace looked like before the QoL.

## Manual e2e

Executed against a sandbox copy of the user's workspace at
`/tmp/kolsch-e2e-sandbox`. Full output + captured JSONL excerpts
in `process/qol-permission-system-human-readable-step4-manual-e2e.md`.

Migration produced the exact Step 1 expected output: every
canonical rule converted; zero deprecation warnings; backup
written to `.platform/platform.yaml.bak.20260608T180349Z`.

The denied-tool simulation produced the expected:

- Provider decision: `prompt` collapsed to deny without callback,
  matched the migrated `kolsch/bash` rule via the action→tool_ref
  map.
- Engine-derived hint: `/perm allow kolsch/bash "rm *"`.
- JSONL `tool.failed` event carries the hint in
  `recovery_hint`.
- Rendered capsule uses the `permission` source label with the
  `to allow:` line.

## User confirmation needed

Please run the following against your real workspace (and report
back; the reviewer verdict stays REVISE-PENDING-USER until you
confirm):

```bash
cd ~/Documents/kolsch
kolsch setup        # the QoL migration hook runs unconditionally
                    # on re-run; backup lands under .platform/
kolsch permissions list
# Expected: every rule in the new {what, pattern, effect} shape;
# no [legacy regex — please convert] markers.

kolsch chat kolsch/helper
# In chat, ask the agent to run something denied (e.g., "rm a
# file"). Expected: an orange `permission` capsule renders with
# the matched rule + `/perm allow kolsch/bash "rm *"` line.

# Type the /perm command. Expected: the next bash call with that
# pattern runs silently.
```

If any step diverges from the captured output in
`process/qol-permission-system-human-readable-step4-manual-e2e.md`,
restore the original platform.yaml from `.platform/platform.yaml.bak.*`
and file an escalation under `E-QOL-PERMSYS-005`.

## Files changed (final tally)

```
docs/explanation/error-model.md         |  32 +-
docs/how-to/chatting-effectively.md     |  98 +-
docs/how-to/permissions.md              | 520 +++--
docs/reference/schemas.md               |  65 +-
docs/tutorials/first-agent.md           |   7 +-
process/test-catalog.md                 |  85 +
src/assets/asset_manager.py             |  11 +
src/cli/_modal.py                       | 173 ++-
src/cli/_renderer.py                    |  91 +-
src/cli/_setup.py                       | 136 +-
src/cli/_slash.py                       | 434 +++++-
src/cli/chat.py                         |  12 +-
src/cli/permissions.py                  |  57 +-
src/engine/observers.py                 | 117 +-
src/errors/__init__.py                  |   8 +
src/errors/authz.py                     | 142 ++
src/errors/catalog.py                   |   5 +
src/platform_runtime/runtime.py         |   9 +-
src/providers/authz.py                  | 586 ++++--
src/schemas/config.py                   |  31 +-
src/schemas/events.py                   |  18 +-
tests/component/cli/test_chat_modal.py  |  88 +-
tests/component/cli/test_chat_slash.py  |  21 +-
tests/component/test_setup.py           |  18 +-
tests/unit/test_errors.py               |  23 +
+ 7 new files (1 _migrations/, 4 new test files, 3 process writeups)
```

25 modified, 7 new. Net +2348 / -439.
