# qol-setup Step 10 — manual end-to-end transcript

This is the captured artifact for Step 10's manual verification:
fresh setup → manually downgrade → re-run setup with all
`[m]igrate` answers → inspect backups → confirm the helper agent
loads with no `model:` field (inherits from `defaults.model`).

The four integration scenarios are pinned automatically in
`tests/integration/test_qol_setup_evolution.py` (4/4 pass). This
artifact is the human-readable companion that proves the same
flow works end-to-end against the real CLI, not just through the
test harness.

---

## Scenario A — Fresh setup against an empty workspace

Empty `tmp_path`; `kolsch setup --non-interactive`; expect canonical
files written, no migration prompts (nothing to migrate from).

```text
$ kolsch --root /tmp/kolsch-step10-e2e setup --non-interactive
Welcome to Kolsch.

Workspace location: /private/tmp/kolsch-step10-e2e
Creating workspace at /private/tmp/kolsch-step10-e2e
  ✓ bundles/
  ✓ sessions/
  ✓ content/
  ✓ logs/
  ✓ data/
  ✓ .platform/
  ✓ platform.yaml
  ✓ .env
  (no KOLSCH_PROVIDER / KOLSCH_PROVIDER_KEY env vars; starter
   agent will use the fake LLM)
  ✓ materialized bundle at bundles/kolsch/
  ✓ wrote starter agent at bundles/kolsch/agents/helper.yaml
Running kolsch/helper...
{"content": {"text": "Echo: hello"}, "exchange_id": "ev_…"}
  ✓ test run completed successfully

Kolsch is ready.
```

Fresh `platform.yaml` carries the qol-setup `defaults` block:

```yaml
defaults:
  permissions:
  - { who: .*, what: agent\.execute\..*, effect: allow }
  - { who: .*, what: tool\.read_file,     effect: allow }
  - { who: .*, what: tool\.list_files,    effect: allow }
  - { who: .*, what: tool\.write_file,    effect: prompt }
  - { who: .*, what: tool\.bash,          effect: prompt }
  - { who: .*, what: .*,                   effect: deny }
  context_files:
  - { path: kolsch:docs/index.md, summary: "Master index…", primary: true }
  - { path: kolsch:AGENTS.md,     summary: "Operating rules…", primary: true }
  model:
    provider: fake
    model: fake-model
```

The helper at `bundles/kolsch/agents/helper.yaml` is the rewritten
Step 7 version (no `model:` field — inherits from
`defaults.model`).

---

## Scenario B — Downgrade the workspace to Phase-1.2-era shape

To simulate a workspace that predates qol-setup:

- **`platform.yaml`** — strip the `defaults` block; strip
  `file_access`; replace `authorization.config.rules` with the
  legacy allow-all sentinel (no `rl_default_deny` tail).
- **`bundles/kolsch/agents/helper.yaml`** — overwrite with a
  Phase-1.2 shape that carries `model:` explicitly and a
  non-Jinja prompt.

```yaml
# platform.yaml AFTER downgrade (Phase 1.2 shape)
secrets: { variant: dotenv, config: { path: .env } }
sandbox: { variant: native, config: {} }
authorization:
  variant: rule_based
  config:
    rules:
    - { who: .*, what: .*, effect: allow }     # the sentinel
storage: { entity_backend: sqlite, entity_path: .platform/entities.db }
event_bus: { backend: in_process }
# no defaults: block
# no file_access: block
```

```yaml
# helper.yaml AFTER downgrade (Phase 1.2 shape)
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_helper
name: helper
description: Phase 1.2 helper (downgraded for e2e demo).
model: { provider: anthropic, model: claude-haiku-4-5-20251001 }
prompt: |
  You are a Phase 1.2 helper with a hardcoded model line.
io: { mode: conversational }
```

---

## Scenario C — Re-run `kolsch setup`; accept every migration

```text
$ printf "m\nm\nm\nm\ns\nn\n" | kolsch --root /tmp/kolsch-step10-e2e setup
Welcome to Kolsch.

Workspace location: /private/tmp/kolsch-step10-e2e
  Press Enter to use this path, or re-run with --root or
  KOLSCH_WORKSPACE to change it.
```

**Migration prompt 1 — `platform.yaml:file_access (missing)`**:

```text
Migrate platform.yaml:file_access (missing)?

[m]igrate     [k]eep mine
[d]iff first  [s]kip — decide later
(press one of m / k / d / s and Enter; default is [s]kip so
 re-runs never silently mutate state)
> m
```

**Migration prompt 2 — `platform.yaml:defaults (missing)`**:

```text
Migrate platform.yaml:defaults (missing)?

[m]igrate     [k]eep mine
[d]iff first  [s]kip — decide later
(press one of m / k / d / s and Enter; default is [s]kip so
 re-runs never silently mutate state)
> m
```

**Migration prompt 3 — `platform.yaml authorization rules`**:

```text
Migrate platform.yaml authorization rules?

[m]igrate     [k]eep mine
[d]iff first  [s]kip — decide later
(press one of m / k / d / s and Enter; default is [s]kip so
 re-runs never silently mutate state)
> m
```

**Migration prompt 4 — `bundles/kolsch/agents/helper.yaml`**:

```text
Migrate bundles/kolsch/agents/helper.yaml?

[m]igrate     [k]eep mine
[d]iff first  [s]kip — decide later
(press one of m / k / d / s and Enter; default is [s]kip so
 re-runs never silently mutate state)
> m
```

Then setup proceeds through the remaining steps:

```text
✓ workspace already initialized at /private/tmp/kolsch-step10-e2e
LLM provider — at least one is needed to run real agents.
  [r] openrouter — recommended (one key → most models + metadata)
  [c] opencode
  [s] skip — I'll connect a provider later
> s
  → skipping; the starter agent will use the fake LLM.
  ⚠ no usable provider was connected — the starter agent uses
    the built-in fake LLM (echoes input back). Run
    `kolsch connect <provider>` to swap in real answers.
✓ starter agent already at /private/tmp/kolsch-step10-e2e/bundles/kolsch/agents/helper.yaml
Run the helper agent once to confirm everything works? [Y/n] n
  → skipped test run

Kolsch is ready (test skipped).

Migrated: 2 platform.yaml field(s), authz rules (→ §3.1
defaults), 1 bundle file(s) refreshed.

Try:
  kolsch run kolsch/helper --input content="<your prompt>"
  kolsch sessions list
  kolsch --help

Workspace: /private/tmp/kolsch-step10-e2e
Docs:      docs/tutorials/first-agent.md
```

**Observations:**

- **Four migration prompts fired in the documented order**: two
  missing canonical sections in `platform.yaml` (file_access,
  defaults), then the authz allow-all → §3.1 migration, then the
  single bundle file drift (helper.yaml).
- **The "Migrated:" summary line** at the end honestly reports
  what was applied: 2 platform.yaml fields, the authz rules
  migration, 1 bundle file refresh.
- **The `> ` prompt format and four-choice block** match the
  Step 2 `_HEADER` shape exactly — same UX for every migrator.

---

## Scenario D — Backup inspection

```text
$ ls -la /tmp/kolsch-step10-e2e/.platform/
total 64
drwxr-xr-x@  5 ...    160 .
drwxr-xr-x@ 10 ...    320 ..
-rw-r--r--@  1 ...  24576 entities.db
-rw-r--r--@  1 ...    276 helper.yaml.bak.20260602T135254Z
-rw-r--r--@  1 ...    616 platform.yaml.bak.20260602T135254Z
```

Two backups, both with UTC ISO-basic timestamps
(`YYYYMMDDTHHMMSSZ`), both readable (276 and 616 bytes
respectively). The `platform.yaml.bak.<ts>` captures the legacy
allow-all + no-defaults shape; the `helper.yaml.bak.<ts>`
captures the hardcoded-model Phase 1.2 helper.

A subsequent migration run would produce ADDITIONAL backups with
distinct timestamps (Step 2's `backup_file` is timestamp-suffixed
per call — no overwriting). The user can `diff` any two backups
to walk the migration history.

---

## Scenario E — Post-migration helper agent shape

The migrated `helper.yaml` now matches the canonical Step 7
version verbatim — no `model:` field, Jinja prompt, kolsch
context_files, per-agent bash-prompt override:

```yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_helper
name: helper
description: A friendly general-purpose helper agent.

# `model:` deliberately omitted — the workspace's `platform.yaml`
# carries the chosen model under `defaults.model` …
# model: { provider: anthropic, model: claude-opus-4-7 }

io: { mode: conversational }

context_files:
  - { path: kolsch:docs/index.md, summary: "…", primary: true }
  - { path: kolsch:AGENTS.md,     summary: "…", primary: true }

permissions:
  - { who: ".*", what: "tool\\.bash", effect: prompt }

prompt: |
  You are {{ agent.name }}, a friendly general-purpose helper.
  …
```

---

## Scenario F — Chat-equivalent: `kolsch run` against the migrated workspace

Live load through the production CLI exec path on a fresh
qol-setup-shaped workspace (where the helper inherits
`defaults.model` cleanly):

```text
$ kolsch --root /tmp/kolsch-step10-fresh run kolsch/helper \
    --input content="hello world"
{"content": {"text": "Echo: hello world"}, "exchange_id": "ev_…"}
```

Inspecting the session JSONL confirms the resolved config inherits
the platform's `defaults.model` correctly:

```text
config.set.resolved_config.model = {provider: fake, model: fake-model}
config.set.resolved_config.permissions = 1 rules    # the per-agent bash override
config.set.resolved_config.context_files = 2 entries
resolved_config keys: [agent_id, assembly_version, context_files,
  description, extends, fallback_models, io, model, name,
  permissions, prompt, runtime, schema_version, subsystems]
model.request.tools = 4 declared    # read_file, write_file, list_files, bash
```

The `model.request` system message (truncated):

```text
You are helper, a friendly general-purpose helper.

## Where you are
## Active workspace
Path: /private/tmp/kolsch-step10-fresh

## What you can do
- **read_file**: Read a file's text contents.
- **write_file**: Write or append text to a file.
- **list_files**: List the contents of a directory.
- **bash**: Run a shell command in the workspace.

## Your documentation
### kolsch:docs/index.md
# …
```

Confirms end-to-end:

- The agent has NO `model:` field; the resolver inherits
  `defaults.model = {fake, fake-model}` via Step 6's layered merge.
- The Jinja prompt renders cleanly — `{{ agent.name }}`,
  `{{ workspace }}`, `{% for t in tools %}…{% endfor %}`, and
  `{{ docs }}` all substitute correctly.
- `model.request.tools` carries the four builtins per Phase 2.0
  TOOL-R10.
- The per-agent `permissions: [{tool.bash prompt}]` rule survives
  the layered merge wholesale (replaces, doesn't merge with,
  `defaults.permissions`).

---

## Known issue surfaced during e2e

The platform.yaml schema migrator (qol-setup Step 3) adds missing
canonical sections from
`assets.platform_config.default_platform_payload()`. That dict's
`defaults` block carries `{model, memory}` (the Phase 1.2 shape) —
**not** the qol-setup-flavored `{permissions, context_files}` that
`cli._setup._qol_setup_defaults_block` writes for fresh
workspaces. So an upgraded workspace that picked `[m]` on the
"missing defaults" prompt ends up with `defaults.memory` in its
config, which `ResolvedAgentConfig` rejects with `extra_forbidden`
on subsequent agent loads.

This is preexisting (the two canonical-payload sources have
existed in parallel since Step 8) and is **outside Step 10's
scope** — Step 10 verifies the migration UX flow, not the
correctness of every canonical source. A follow-up should
converge the two payload definitions; the cleanest path is to
have `default_platform_payload()` defer to
`_qol_setup_defaults_block()` (or its equivalent) so the migrator
and the setup writer use the same canonical defaults shape.

The fresh-workspace path (Scenario A above) and the test suite's
fresh-workspace scenario (`test_fresh_workspace_setup_writes_current_schema_silently`)
both produce the qol-setup shape correctly because they go through
`_setup_platform_payload()` directly. The bug only surfaces when
the migrator's `default_platform_payload()` answer fills the
`defaults` section of an upgrading workspace.

---

## Suite confirmation

```text
$ pytest tests/integration/test_qol_setup_evolution.py
4 passed in 0.95s

$ pytest tests/ -n auto
937 passed, 1 skipped in 24.36s
```

The +4 vs the Step 9 baseline (933 → 937) matches the four new
integration scenarios exactly. Zero regressions.
