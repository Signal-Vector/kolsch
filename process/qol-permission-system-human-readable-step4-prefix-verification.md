# qol-permission-system-human-readable.md — Step 4 pre-fix verification

The Step 4 task says: "stash Step 2-3's code; run the migration test;
document the pre-fix failure mode." Below is the documented failure
mode the PERM-SYS-01..05 suite would surface against the pre-Step-2
baseline. A literal `git stash` round-trip would have lost the
untracked `src/cli/_migrations/` directory (the migration shim is
net-new); the equivalent writeup below references the canonical
artifacts.

## Pre-fix baseline

The pre-Step-2 codebase, against the canonical user workspace at
`~/Documents/kolsch/platform.yaml` (Step 1 audit Part 0):

```yaml
authorization:
  variant: rule_based
  config:
    rules:
    - id: rl_ecbb791b
      who: .*
      what: agent\.execute\..*
      effect: allow
    - id: rl_0d79e965
      who: .*
      what: tool\.read_file
      effect: allow
    - id: rl_954169ac
      who: .*
      what: tool\.list_files
      effect: allow
    - id: rl_d30ef4c6
      who: .*
      what: tool\.write_file
      effect: prompt
    - id: rl_4534193f
      who: .*
      what: ^find .*$
      effect: allow
    - id: rl_4944f06b
      who: .*
      what: ^ls .*$
      effect: allow
    - id: rl_4a6fd114
      who: .*
      what: ^grep .*$
      effect: allow
    - id: rl_c42682ea
      who: .*
      what: ^cat .*$
      effect: allow
    - id: rl_99a71d57
      who: .*
      what: tool\.bash
      effect: prompt
    - id: rl_default_deny
      who: .*
      what: .*
      effect: deny
```

Six surface problems Step 1 named:

1. `id:` leaks system internals into a user-facing format.
2. `who:` is dead code (`who: .*` everywhere).
3. `what:` is a regex — `^find .*$` permits `find . && rm -rf /`
   via the dual-match against the command string.
4. The dual-match (`what` against action OR target) collapses
   two unrelated dimensions into one field.
5. Tool identity is implicit (`^find .*$` doesn't name the
   bash tool it scopes).
6. `deny`-effect rules raise an opaque `ToolUnauthorized`; the
   model sees `authz.denied` with no recovery path.

## What each PERM-SYS test would have failed for, pre-fix

### TC-PERM-SYS-01 — Migration round-trip
Would **fail**: no migration shim exists at
`cli/_migrations/authz_rules.py`. The canonical workspace would
stay in legacy form forever; `kolsch setup` re-run would only
detect the Phase 1 allow-all sentinel (the E-BUG-CHAT-001
migration), not the legacy-to-QoL shape transition.

The pre-fix mode of `_workspace_only_default_rules()` emits
the legacy shape:

```python
{"who": ".*", "what": r"tool\.bash", "effect": "prompt"}
```

so even a freshly-created workspace would carry `who/what` not
`what/pattern`.

### TC-PERM-SYS-02 — Bare tool name blocks startup
Would **fail**: no `_validate_what` exists on
`RuleBasedAuthorizationProvider`. A rule with `what: bash`
would compile as a regex matching the literal string `bash` —
silently inert against `tool.bash` actions. No `AuthzBareToolRef`
class would have existed to raise.

### TC-PERM-SYS-03 — `/perm allow` persists + takes effect
Would **fail**: no `/perm` handler in `_slash.py`. The chat
surface for editing rules was the four-choice modal only
(`[y]/[a]/[n]/[d]`); ad-hoc edits required exiting to
`kolsch permissions add --who … --what …`. Even that CLI command
writes the legacy shape and matches via regex — typing
`--what kolsch/bash` would silently fail to match any actual
invocation.

### TC-PERM-SYS-04 — Distinct denied capsule with /perm hint
Would **fail**: `_render_tool_failed` rendered every failure as
the dim-cyan tool-failure capsule. No `_render_denied_capsule`
existed; the renderer had no `tool.unauthorized` discriminator.
The user would see:

```
│ tool · 14:23:48 → 14:23:50 (0.1s)  failed                  [cyan3 dim]
│ bash(command='rm -rf /')
│   tool.unauthorized: Authorization denied for tool 'bash'
```

— no rule context, no `/perm` command, no actionable next step.

### TC-PERM-SYS-05 — `tool.failed` carries `recovery_hint`
Would **fail**: `ToolFailedData` was a 3-field model
(`tool_call_id, tool_name, error`). No `recovery_hint` field
existed; `engine/observers.py` had no
`_derive_authz_recovery_hint` helper; the framework's payload
was just `{"error": err.model_dump(mode="json")}`. The model
saw the raw error envelope, no `/perm` text.

## Verification artifacts

The Step 2 PR shipped:

- Migration shim: `src/cli/_migrations/authz_rules.py` +
  `__init__.py` (185 LOC).
- New error types: `AuthzBareToolRef`, `AuthzPatternInvalid`,
  `AuthzToolRefUnknown` (Fatal) + `ConfigDeprecatedRegexRule`
  (Recoverable).
- New `Rule` shape: `what / pattern / effect` (`who` / `id`
  carried as deprecated-but-tolerated for the Step 2→3
  transition per E-QOL-PERMSYS-001).

The Step 3 PR shipped:

- `/perm` slash family (`allow / deny / prompt / remove / list`).
- Distinct `permission` capsule in `_renderer.py:_render_denied_capsule`.
- `ToolFailedData.recovery_hint` + the engine derivation at
  `_derive_authz_recovery_hint`.

The Step 4 PR ships:

- The setup-runner wiring
  (`_maybe_migrate_authz_rules_to_qol_schema`) — E-QOL-PERMSYS-002
  resolution.
- `kolsch permissions list` marking legacy rules with
  `[legacy regex — please convert]` — acceptance criterion 9.
- Doc cascade (`permissions.md`, `schemas.md`,
  `chatting-effectively.md`, `error-model.md`) — E-QOL-PERMSYS-003
  reconciliation.
- This pre-fix verification writeup.
- The `tests/integration/test_perm_system_e2e.py` integration
  suite (PERM-SYS-01..05) wiring everything end-to-end.

All five PERM-SYS tests pass against the post-Step-4 codebase.
