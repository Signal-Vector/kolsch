# QoL: Roll out `--format text|json` to all existing CLI commands

**Status:** Proposed (depends on Phase 3.0 landing first)
**Effort:** ~2 days
**Owner:** TBD
**Depends on:** Phase 3.0 (master spec §14.3 and §14.4 — the format contract; `src/cli/_format.py` helper)

---

## Background

Phase 3.0 locked a cross-cutting CLI output convention (master workflow spec §14.3 + §14.4):

- Global `--format text|json` flag accepted by every kolsch CLI command
- `text` is the default and preserves current human-friendly output
- `json` produces a structured envelope: `{command, status, data, errors}`
- Per-command `data` schemas defined per command; consistent envelope across all
- Errors rendered per the §14.4 exception JSON shape

The Phase 3.0 implementation shipped this contract for the `workflows` subcommand group (`list`, `show`, `validate`, `run`) via the shared `src/cli/_format.py` helper. **Existing CLI commands do not yet honor `--format`** and continue to produce only their legacy text output.

This QoL rolls out the contract to those existing commands.

## Scope

### In scope

- Add `--format text|json` to every existing top-level command and subcommand:
  - `kolsch agents` (list / show / etc.)
  - `kolsch tools` (list / show / etc.)
  - `kolsch skills` (list / show / etc.)
  - `kolsch prompts` (list / show / etc.)
  - `kolsch permissions` (show / lint / etc.)
  - `kolsch run` (agent invocation)
  - Anything else under `kolsch --help` that produces author-facing output
- Route every terminal output through `src/cli/_format.py:render()` — no direct `print()` or `click.echo()` of payloads
- Define per-command `data` schemas (mirror the structure used in workflows commands)
- Define error renderings for non-`WorkflowValidationError` exceptions raised by these commands (e.g., `ToolNotFound`, `AgentInvocationFailed`, `PermissionsRuleInvalid`)
- Tests: one `--format json` test per command (success case + at least one error case)
- Documentation updates: each command's `--help` text references the `--format` flag

### Out of scope

- Re-styling the existing text output. Behavior under `--format text` (default) must be byte-identical to today.
- Versioning the JSON envelope. The contract from §14.3 + §14.4 is the v1 shape; we do not add a `format_version` field here.
- Output to stdout vs stderr conventions beyond what each command does today.
- A "yaml" format. Future work; not in this QoL.

## Why this is a QoL, not a feature

The `--format` contract was designed and landed in Phase 3.0 (master spec §14.3 + §14.4). This QoL is purely the mechanical rollout to existing commands — the design work is done. Each command's migration is shape:

```python
# Before
click.echo(format_human(payload))

# After
from cli._format import render
fmt = ctx.obj.get("fmt", "text")
click.echo(render(command="...", data=payload, fmt=fmt))
```

The migration is tedious but uncontroversial.

## Acceptance Criteria

1. Every command listed under "In scope" accepts `--format text|json` at the group level (`kolsch --format json <cmd>...`) AND at the subcommand level (`kolsch <cmd> --format json ...`). Click handles both forms naturally.
2. Default behavior (`--format text` or no flag) is byte-identical to current output for every command.
3. `--format json` produces a well-formed JSON envelope matching master spec §14.3 for every command.
4. Each command's `data` payload follows a documented per-command schema (added to a new "CLI command JSON schemas" appendix or per-command docs).
5. Error rendering uses the master spec §14.4 exception JSON shape for any kolsch-namespace exception (not just `WorkflowValidationError`).
6. No direct `print()` or `click.echo()` of payloads remains anywhere outside `src/cli/_format.py`. Reviewer asserts via grep.
7. Tests: one `--format json` success test + at least one error test per command. ExceptionGroup rendering verified on at least one command that can raise it.

## Effort estimate

- Per-command migration: ~15-30 minutes (mechanical)
- ~10 existing commands × 30 minutes = ~5 hours implementation
- Tests: ~10 commands × 2 tests = 20 tests, ~3 hours
- Schema documentation: ~2 hours
- Cross-cutting review + golden refresh: ~2 hours
- **Total: ~1.5-2 days**

## Risks

- **Edge case: commands that already emit JSON natively** (e.g., something that exports a config blob). These need to be wrapped in the envelope OR explicitly excluded with documented rationale.
- **Exception classes outside `WorkflowValidationError` may lack structured attrs** — the §14.4 JSON shape assumes `file`, `line`, `identifying_names`, etc. exist as attributes. For exceptions that don't carry them, the JSON object omits or nulls those keys.
- **Golden test refresh wave** — text-output tests should not change, but if any test was implicitly asserting on `print()` order vs `click.echo()` order, the routing-through-helper change may shift output minutely.

## Cross-references

- Master workflow spec `process/phase3.0-workflow-spec.md` §14.3, §14.4 — the locked format contract
- Master workflow spec `process/phase3.0-workflow-spec.md` §12.3 — strict single-line error message format (referenced for the `summary` and `fix` fields of the §14.4 JSON shape)
- Parser-validator spec `process/phase3.0-workflow-build.md` Step 6 — the first consumer of the contract (`workflows` commands)
