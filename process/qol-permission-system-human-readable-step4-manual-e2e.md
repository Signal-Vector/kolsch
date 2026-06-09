# qol-permission-system-human-readable.md — Step 4 manual e2e + handoff

End-to-end verification against a sandbox copy of the user's
canonical workspace at `~/Documents/kolsch`. The sandbox is at
`/tmp/kolsch-e2e-sandbox` so the real workspace stays untouched
until the user runs the same steps themselves.

## Setup

```bash
cp -R /Users/andrecohen/Documents/kolsch /tmp/kolsch-e2e-sandbox
ls /tmp/kolsch-e2e-sandbox
# bundles  content  data  logs  platform.yaml  sessions
```

Pre-migration `platform.yaml` rule list (legacy schema):

```yaml
authorization:
  variant: rule_based
  config:
    rules:
    - { id: rl_ecbb791b, who: .*, what: agent\.execute\..*, effect: allow }
    - { id: rl_0d79e965, who: .*, what: tool\.read_file,    effect: allow }
    - { id: rl_954169ac, who: .*, what: tool\.list_files,   effect: allow }
    - { id: rl_d30ef4c6, who: .*, what: tool\.write_file,   effect: prompt }
    - { id: rl_4534193f, who: .*, what: ^find .*$,          effect: allow }
    - { id: rl_4944f06b, who: .*, what: ^ls .*$,            effect: allow }
    - { id: rl_4a6fd114, who: .*, what: ^grep .*$,          effect: allow }
    - { id: rl_c42682ea, who: .*, what: ^cat .*$,           effect: allow }
    - { id: rl_99a71d57, who: .*, what: tool\.bash,         effect: prompt }
    - { id: rl_default_deny, who: .*, what: .*,             effect: deny }
```

## Run 1 — migration

Invoke the schema-migration hook the setup runner calls
unconditionally on re-run:

```python
from cli._setup import (
    _SetupIO, NonInteractivePrompter,
    _maybe_migrate_authz_rules_to_qol_schema,
)
io_ = _SetupIO(root=Path("/tmp/kolsch-e2e-sandbox"), ...)
await _maybe_migrate_authz_rules_to_qol_schema(io_)
```

Output:

```
  ✓ migrated 10 authz rule(s) → new schema (what / pattern / effect)
  ✓ backup written to .platform/platform.yaml.bak.20260608T180349Z
```

Post-migration `platform.yaml`:

```yaml
authorization:
  variant: rule_based
  config:
    rules:
    - { what: agent.execute.*,    pattern: '*',     effect: allow }
    - { what: kolsch/read_file,   pattern: '*',     effect: allow }
    - { what: kolsch/list_files,  pattern: '*',     effect: allow }
    - { what: kolsch/write_file,  pattern: '*',     effect: prompt }
    - { what: kolsch/bash,        pattern: find *,  effect: allow }
    - { what: kolsch/bash,        pattern: ls *,    effect: allow }
    - { what: kolsch/bash,        pattern: grep *,  effect: allow }
    - { what: kolsch/bash,        pattern: cat *,   effect: allow }
    - { what: kolsch/bash,        pattern: '*',     effect: prompt }
    - { what: '*',                pattern: '*',     effect: deny  }
```

Every rule matches Step 1's migration table. Zero
`config.deprecated_regex_rule` warnings. Backup is in place.

## Run 2 — denied tool simulation

Simulate `rm -rf /tmp/x` invoked through `tool.bash` (the migrated
`kolsch/bash` prompt rule catches it; with no chat callback the
prompt collapses to deny). Inputs flow through the actual
`RuleBasedAuthorizationProvider`, `engine.observers._derive_authz_recovery_hint`,
`ToolFailedData`, and `Renderer._render_tool_failed`.

### Provider decision

```
allowed=False
effect=prompt
matched_rule={'what': 'kolsch/bash', 'pattern': '*', 'effect': 'prompt',
              'id': 'rl_dc5d327f', 'who': None}
reason=prompt rule rl_dc5d327f matched without a callback;
       collapsed to deny (who_chain=platform)
```

The provider correctly:

- Matches the new-shape `what: kolsch/bash` rule against the
  `tool.bash` action via the action→tool_ref map.
- Reports `who is None` on the matched rule (confirms new shape).
- Reports `who_chain=platform` (platform.yaml rules).

### Engine-derived `/perm` hint

```
'/perm allow kolsch/bash "rm *"'
```

The `bash` heuristic plucks `rm` as the first token; the user can
edit before submitting.

### `tool.failed` event payload (JSONL excerpt)

```json
{
  "tool_call_id": "tc_e2e",
  "tool_name": "bash",
  "error": {
    "type": "tool.unauthorized",
    "message": "Authorization denied for tool 'bash'",
    "where": "tools.dispatch.invoke",
    "detail": {
      "tool_name": "bash",
      "action": "tool.bash",
      "principal": "agent.ag_helper",
      "matched_rule": {
        "what": "kolsch/bash",
        "pattern": "*",
        "effect": "prompt",
        "id": "rl_dc5d327f",
        "who": null
      }
    },
    "cause": null
  },
  "recovery_hint": "/perm allow kolsch/bash \"rm *\""
}
```

Both the JSONL (replay) and the framework payload (model's view)
carry the same `recovery_hint` string.

### Chat-rendered denied capsule

```
│ permission · 14:04:17
│ denied: bash()
│   matched rule:  what='kolsch/bash'  pattern='*'  effect='prompt'
│
│ to allow:  /perm allow kolsch/bash "rm *"
```

Distinct `permission` source label (orange palette), matched rule
in the new schema, exact `/perm` command. The signature shows
`bash()` rather than `bash(command='rm -rf /tmp/x')` because the
simulation didn't go through the dispatcher (the args don't
arrive on the renderer via a separate `tool.called` event in this
narrow demo); a real chat exchange would carry them via the
`_tool_call_args` map.

## Run 3 — `/perm allow` round-trip

Tested in the integration suite as PERM-SYS-03; the same code
path executed end-to-end:

1. Dispatch `/perm allow kolsch/bash "find *"` against a workspace
   with only the catch-all.
2. Provider's in-memory rule list grows by one above the catch-all.
3. `_write_back` atomically rewrites `platform.yaml`.
4. Next `allow(..., "tool.bash", target="find /")` returns
   `allowed=True` with `matched_rule.what == "kolsch/bash"`.
5. Re-loading the YAML into a fresh provider preserves the new rule.

Pass: see `tests/integration/test_perm_system_e2e.py::test_perm_sys_03_perm_allow_persists_and_takes_effect`.

## Handoff to user

The sandbox migration above shows the **expected** post-migration
state of the user's workspace. To apply the migration to the real
workspace:

```bash
cd /Users/andrecohen/Documents/kolsch
kolsch setup        # the new schema-migration hook runs unconditionally
                    # on re-run; backup lands under .platform/
```

**Verification steps the user should run:**

1. `kolsch permissions list` — every rule should print in the new
   `what=... pattern=... effect=...` shape, no `[legacy regex]`
   markers.
2. `kolsch chat kolsch/helper` — start a chat; ask the agent to
   run a `bash` command outside the allow-list (e.g., "rm a file");
   confirm the **permission capsule** renders (orange, not
   dim-cyan) with a `/perm allow` command line.
3. Type the `/perm allow` line — confirm the next `bash` call with
   that same pattern runs silently.
4. `cat ~/Documents/kolsch/platform.yaml | grep -c who:` should
   return `0` (every rule is new-shape).
5. `ls ~/Documents/kolsch/.platform/platform.yaml.bak.*` should
   list at least one timestamped backup.

If anything diverges, the original `platform.yaml` is preserved
verbatim under `.platform/platform.yaml.bak.<timestamp>` and can
be restored with a single `cp` command.
