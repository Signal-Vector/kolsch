# QoL — Setup evolution + platform defaults + showcase helper

**Draft 0.1**

> Three connected pain points in `kolsch setup` today:
>
> 1. **platform.yaml doesn't evolve.** Setup creates-or-skips. When
>    a later phase adds a field to the canonical schema, existing
>    user workspaces never gain it.
> 2. **The kolsch bundle doesn't update.** Same issue at the bundle
>    layer — setup skips the bundle if it exists; users stay on
>    whatever helper.yaml was current when they first ran setup.
> 3. **The helper agent's `model:` is hardcoded at write-time.**
>    If the user reconnects to a different provider, the helper
>    doesn't follow. The helper also doesn't demonstrate any of
>    Phase 2.2's envelope features.
>
> This QoL fixes all three with a shared mechanism (drift detection
> + four-choice migration prompt + backup) plus a small Phase 2.2
> amendment (agents inherit from `platform.yaml`'s `defaults:` block
> when fields are omitted) plus a helper agent rewrite that showcases
> Phase 2.2's envelope.
>
> Supersedes Phase 2.3's drift-detection plan (the mechanism here
> covers it) and folds in the allow-all → §3.1 rules migration from
> `bug-chat-permissions-and-console-leak.md` E-BUG-CHAT-001 option
> (a). After this lands, Phase 2.3 shrinks to "add OpenClaw + Pi to
> the canonical bundle" — the heavy machinery is here.

---

## Context

`kolsch setup` was specced in Phase 1.2 before later phases existed.
It assumed a single-pass setup; "idempotent" meant "skip if
initialized" rather than "evolve to current canonical." That
interpretation was correct for Phase 1.2 in isolation, but Phase
2.0, 2.2, and beyond add new fields to `platform.yaml` and new
files to the kolsch bundle. Users who ran setup before those
phases stay frozen at the original schema; setup's create-or-skip
behavior is the lock-in.

We need setup to be the migration point — the canonical place a
user gets caught up to the current kolsch version's schema and
bundle. Non-destructively: existing user values are preserved;
only missing pieces are added; format violations are surfaced to
the user for explicit resolution.

---

## Three changes in one patch

### Change 1 — Setup evolution (drift detection + four-choice prompt)

A shared mechanism applied to:
- `platform.yaml` (schema-level: add missing fields, prompt on
  format violations).
- `bundles/kolsch/` (file-level: per-file drift vs canonical
  bundle, prompt to migrate).
- The legacy allow-all authz rules (folds in
  `bug-chat-permissions-and-console-leak.md` E-BUG-CHAT-001 option
  (a)).

Same four-choice prompt across all three:

```
[m]igrate to canonical    [k]eep mine
[d]iff first              [s]kip — decide later
```

Backups always written before changes:
- `.platform/platform.yaml.bak.<ts>` for platform.yaml.
- `.platform/bundle.bak.<ts>/` for bundle files.

### Change 2 — Platform defaults block

`platform.yaml` gains a `defaults:` block that any agent field can
fall back into:

```yaml
# platform.yaml
defaults:
  model: opus
  permissions:
    - { who: tool.bash, what: ".*", effect: prompt }
    - { who: tool.write_file, what: "<workspace>/.*", effect: prompt }
  context_files:
    - path: kolsch:docs/INDEX.md
      summary: "Master index of all Kolsch documentation."
      primary: true
    - path: kolsch:AGENTS.md
      summary: "Operating rules for AI agents."
      primary: true
```

Resolution order for any agent field becomes:

1. Agent's own value (if specified).
2. `extends:` chain (Phase 2.2; walks parent files).
3. `platform.yaml`'s `defaults.<field>` (this QoL adds).
4. Schema default or validation error if required.

This makes agent YAMLs minimal — most agents only need to specify
what differs from the workspace baseline.

### Change 3 — Helper agent showcase + auto-default model

The helper agent is rewritten to:
- Omit `model:` entirely — inherits from `defaults.model`.
- Use Phase 2.2's Jinja templating in the prompt.
- Use `context_files:` referencing `kolsch:` paths.
- Declare per-agent `permissions:` to demonstrate the override-
  platform pattern.

Result: a single self-contained agent file that demonstrates every
Phase 2.2 envelope feature, and that automatically follows the
user's connected provider via `defaults.model`.

---

## Critical files

| File | Touched by |
|---|---|
| `src/cli/_setup.py` | Steps 2, 3, 4, 5, 8 — migration mechanism + orchestrator |
| `src/cli/_migrations/` (new) | Steps 2–5 — shared migration utilities |
| `src/schemas/platform.py` | Step 6 — `defaults` block |
| `src/schemas/agent.py` | Step 6 — resolver applies platform defaults |
| `bundles/kolsch/agents/helper.yaml` | Step 7 — full rewrite |
| `bundles/kolsch/_canonical/` (new) | Steps 4, 7 — canonical bundle source for drift comparison |
| `docs/tutorials/first-agent.md` | Step 9 — getting-started update |
| `docs/how-to/permissions.md`, `agent-templating.md` | Step 9 — accuracy updates |
| `README.md` | Step 9 — quickstart update |
| `process/phase1.2-usability-specs.md` | Step 1 — amendment notes |
| `process/phase2.2-agent-envelope-specs.md` | Step 1 — amendment for defaults block |
| `process/roadmap.md` | Step 1 — Phase 2.3 absorption note |

---

## Step dependency graph

```
1. Spec amendments (forward references to this QoL)
   ↓
2. Migration foundation (shared utility: detect, prompt, backup)
   ↓
3. platform.yaml schema-evolution (uses Step 2)
4. Bundle drift detection (uses Step 2)
5. Allow-all → §3.1 migration (uses Step 2)
   ↓
6. defaults: block on platform.yaml + resolver inheritance
   ↓
7. Helper agent rewrite (showcase using 2.2 envelope + Step 6 defaults)
   ↓
8. Setup orchestrator integration (wires Steps 3, 4, 5, 6 into the flow)
   ↓
9. Documentation review + getting-started update
   ↓
10. Regression tests + manual e2e + coordinator handoff
```

| Step | Ships | Lines (est.) |
|---|---|---|
| **1** | Spec amendments + roadmap update | docs only |
| **2** | Shared migration utility (detect/prompt/backup) | ~120 |
| **3** | platform.yaml schema-evolution migrator | ~80 |
| **4** | Bundle file-drift migrator | ~80 |
| **5** | Allow-all → §3.1 migrator (uses Step 2) | ~70 |
| **6** | `defaults:` block + resolver fallback | ~100 |
| **7** | Helper agent YAML rewrite + canonical capture | ~60 |
| **8** | Setup orchestrator wiring | ~80 |
| **9** | Docs review + getting-started + tutorial updates | docs only |
| **10** | Regression tests + manual verification | ~200 |

Each step's PR independently mergeable. Step 8 is the pivot —
behavior changes visibly there for existing-workspace users.

---

## Step 1 — Spec amendments (no runtime code)

- **Builds:** forward-reference notes in existing specs so future
  readers find this QoL.
- **Acceptance:** the three named specs gain pointers; roadmap
  reflects Phase 2.3 absorption.

### Developer prompt

```
[standing instructions]

Step 1 — Spec amendments to reflect the decisions in
qol-setup-evolution-and-defaults.md.

Tasks:

1. process/phase1.2-usability-specs.md §2.5 "Idempotency
   rules" — add a final paragraph:

   > **Migration-aware idempotency (this QoL):** "idempotent" in
   > setup is interpreted as "detect drift between local and
   > canonical state; offer migration; non-destructively apply."
   > See qol-setup-evolution-and-defaults.md for the mechanism.

2. process/phase1.2-usability-specs.md §2.4 "The starter
   agent file" — add a note:

   > After qol-setup-evolution-and-defaults.md, the starter helper
   > agent omits `model:` and inherits from `platform.yaml`'s
   > `defaults.model`. The `<MODEL>` placeholder in this section's
   > template becomes obsolete.

3. process/phase2.2-agent-envelope-specs.md — new Part 14
   subsection after the existing Part 14:

   > **Post-2.2 amendment (qol-setup-evolution-and-defaults.md):**
   > `platform.yaml` gains a `defaults:` block; agent field
   > resolution falls through to `defaults.<field>` when the agent
   > and its `extends:` chain don't specify it. Composition order:
   > agent → extends chain → defaults → schema default.

4. process/roadmap.md — Phase 2.3 section updated:

   > **Status amended:** the drift-detection mechanism is absorbed
   > into qol-setup-evolution-and-defaults.md. Phase 2.3 shrinks
   > to "ship OpenClaw + Pi as additional canonical agents in
   > bundles/kolsch/."

5. process/roadmap.md — Phase 1.2 row in the status table
   gains a note pointing at this QoL for the migration mechanism.

No runtime code; doc-only edits. Commit per amendment OR as one
"docs: spec amendments for qol-setup-evolution" commit.

Acceptance: each named spec carries the forward reference.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — spec amendments.

Checks:

1. Each of the four spec files gained the noted addition. Use
   git diff to verify.

2. The forward references include the exact filename of this QoL
   doc so future readers can navigate.

3. No structural rewrites of the original specs — only additive
   notes.

4. No runtime code changed.

Verdict: PASS or FAIL.
```

---

## Step 2 — Shared migration utility

- **Builds:** `src/cli/_migrations/` with detect/prompt/backup
  primitives. Used by Steps 3–5.
- **Acceptance:** unit tests verify the four-choice prompt parses
  correctly; backup files land at predictable paths; the diff
  rendering works.

### Developer prompt

```
[standing instructions]

Step 2 — Build the shared migration utility.

Read first:
- bug-chat-permissions-and-console-leak.md E-BUG-CHAT-001 option (a)
  — the established four-choice prompt convention.
- qol-setup-evolution-and-defaults.md (this doc).

Deliver:

src/cli/_migrations/
├── __init__.py
├── prompt.py       # the four-choice [m/k/d/s] prompt + footer hint
├── backup.py       # .platform/<artifact>.bak.<ts>/ writing
└── diff.py         # unified diff rendering

The prompt module:
- prompt_migration(label, current, canonical) → MigrationChoice enum
  (MIGRATE, KEEP, DIFF, SKIP)
- DIFF re-prompts after showing the diff.
- Footer hint updates while the prompt is active.

The backup module:
- backup_file(path) → backup path written; returns the backup path
- backup_directory(path) → similar for whole directories

The diff module:
- render_diff(label, current_text, canonical_text) → diff string
  (uses difflib.unified_diff under the hood)
- Truncates long diffs; suggests `kolsch sessions` for full output
  (or similar).

Tests in tests/unit/cli/test_migrations.py:
- prompt_migration with mocked input returns the right choice.
- DIFF then 'm' returns MIGRATE.
- backup_file creates timestamped file; verify it's readable.
- render_diff produces a usable diff for two YAML strings.

Scope: ~120 LOC code + ~80 LOC tests.

ESCALATION PATH: if prompt_toolkit isn't usable inside _setup.py's
non-interactive flow (which uses input() per Phase 1.2), escalate
— may need a parallel "structured input" mechanism for setup vs
chat.

Acceptance: tests pass; the utility is callable from outside.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — migration utility.

Checks:

1. The four-choice prompt convention matches the existing
   E-BUG-CHAT-001 wording exactly: `[m]igrate / [k]eep / [d]iff / [s]kip`.

2. Backup files land at `.platform/<artifact>.bak.<ts>/` with a
   timestamp in the name (NOT a fixed `.bak` filename — multiple
   migrations must each leave their own backup).

3. The diff rendering handles binary files gracefully (raises a
   typed error, doesn't crash).

4. No coupling to a specific artifact type (the utility is
   generic; Steps 3-5 apply it to specific cases).

Verdict: PASS or FAIL.
```

---

## Step 3 — platform.yaml schema-evolution migrator

- **Builds:** the migrator that compares user's platform.yaml against
  the canonical schema. Adds missing fields. Prompts on format
  violations.
- **Acceptance:** an old-schema platform.yaml ends up with all
  current fields after migration; user's existing values
  preserved.

### Developer prompt

```
[standing instructions]

Step 3 — Implement the platform.yaml schema-evolution migrator.

Read first:
- src/schemas/platform.py (the current Pydantic model).
- Step 2's migration utility.

Tasks:

1. Add migrate_platform_yaml(workspace_root) function:
   - Load user's platform.yaml.
   - Load the canonical default (from the Pydantic schema's
     default values).
   - For each key in canonical not present in user's: prompt to
     add (uses Step 2's prompt).
   - For each key in user's: validate against schema. If
     malformed (wrong type, invalid enum value), prompt user with
     diff [m/k/d/s].
   - For each key in user's NOT in canonical: keep as-is (don't
     remove unknown keys; the user may have added them
     intentionally).

2. On MIGRATE: apply the change to an in-memory dict; backup the
   current file via Step 2; write the updated YAML atomically.

3. On KEEP / DIFF / SKIP: leave the file alone (or re-prompt
   after diff).

Tests in tests/component/cli/test_migrate_platform_yaml.py:
- platform.yaml missing a key from canonical → migration prompt
  fires; MIGRATE adds the key with canonical default; KEEP
  leaves the file alone.
- platform.yaml with a malformed value → migration prompt fires
  with diff context; user's choice applied.
- platform.yaml with extra unknown keys → preserved through
  migration (user-added customs survive).
- Atomicity: interrupting between backup and write leaves the
  original file intact.

Scope: ~80 LOC code + ~80 LOC tests.

ESCALATION PATH: if the schema model's "default values" aren't
introspectable (Pydantic v2 changes), escalate.

Acceptance: tests pass; running the migrator against a real
old-schema platform.yaml produces a current-schema file with
user's values preserved.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — platform.yaml migrator.

Checks:

1. User's existing values are NEVER overwritten without prompt.
   The "non-destructive" property is the load-bearing one. Run a
   test that deliberately puts unexpected values in user's
   platform.yaml and confirm they survive a no-op migration run.

2. Unknown keys (user-added customs not in the schema) are
   preserved through migration.

3. Backups land before any change (Step 2's discipline).

4. Atomic write — interrupt the write and verify the original
   file is intact.

5. The migrator can be called as a standalone function (not
   wired into setup yet — that's Step 8).

Verdict: PASS or FAIL.
```

---

## Step 4 — Bundle drift detection

- **Builds:** the canonical bundle source (`bundles/kolsch/_canonical/`
  or equivalent), the per-file drift comparator, and the migration
  applier.
- **Acceptance:** files in `bundles/kolsch/` that differ from
  canonical are detected; user is prompted; on migrate, the file
  is replaced (with backup).

### Developer prompt

```
[standing instructions]

Step 4 — Implement the bundle drift detector and migrator.

Read first:
- qol-setup-evolution-and-defaults.md §"Change 1".
- Step 2's migration utility.

Tasks:

1. Decide where the canonical bundle lives. Two reasonable
   options:
   - In the kolsch package: `src/kolsch_canonical_bundle/`
     accessible via importlib.resources.
   - As part of the install's package-data: a separate dir.
   The first is simpler; go with that unless there's a reason
   not to.

2. Implement migrate_kolsch_bundle(workspace_root):
   - For each file under the canonical bundle:
     - If it doesn't exist in the user's bundle → prompt to add
       (silent ALLOW is fine for missing-file additions; no diff
       needed).
     - If it exists but differs → prompt with diff [m/k/d/s].
     - If it matches byte-for-byte → silent skip.
   - Files in the user's bundle that AREN'T in the canonical are
     preserved (user-added agents/tools).

3. On MIGRATE: backup the user's file via Step 2; replace with
   canonical.

4. The canonical contents are SNAPSHOTTED at install time.
   Different kolsch versions ship different canonical bundles;
   the migrator always compares against the currently-installed
   version's canonical.

Tests in tests/component/cli/test_migrate_bundle.py:
- User's helper.yaml differs from canonical → prompt fires;
  MIGRATE replaces.
- User's bundle has an additional file not in canonical → file
  preserved.
- User's bundle missing a canonical file → added on MIGRATE.
- Backup is timestamped and contains the pre-migration file.

Scope: ~80 LOC code + ~60 LOC tests.

ESCALATION PATH: if the bundle's binary files (any non-text
content) need special handling, escalate — the canonical-snapshot
approach assumes text.

Acceptance: tests pass; running against a real workspace with a
modified helper.yaml correctly detects and offers migration.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 — bundle drift.

Checks:

1. User-added files in bundles/kolsch/ are PRESERVED — not deleted
   when the migrator runs.

2. Files matching byte-for-byte get silent skip (no prompt). The
   user only sees prompts when there's actual drift.

3. Backups timestamped per file, not a single overwriting backup.

4. The canonical-bundle source is reachable via
   importlib.resources or similar — works in both source-tree and
   installed contexts.

5. Standalone callable; not yet wired into setup (Step 8).

Verdict: PASS or FAIL.
```

---

## Step 5 — Allow-all → §3.1 migration

- **Builds:** the migrator that detects the Phase-1-era allow-all
  authz rule and migrates to Phase 2.0's §3.1 default rule set.
  Folds in `bug-chat-permissions-and-console-leak.md` E-BUG-CHAT-001
  option (a).
- **Acceptance:** existing workspaces with the legacy allow-all
  rule get the Phase 2.0 §3.1 rule set, with user-added custom
  rules preserved in their relative position.

### Developer prompt

```
[standing instructions]

Step 5 — Implement the allow-all → §3.1 authz migration.

Read first:
- bug-chat-permissions-and-console-leak.md E-BUG-CHAT-001 option
  (a) (the previously-spec'd version of this migration).
- process/phase2.0-tools-specs.md §3.1 (the canonical rule set).
- Step 2's migration utility.

Tasks:

1. Implement migrate_authz_rules(workspace_root):
   - Load platform.yaml.
   - Check authorization.config.rules for the Phase 1 allow-all
     sentinel: exactly one rule with who=".*", what=".*",
     effect="allow", and no rl_default_deny tail.
   - If detected, prompt user with diff [m/k/d/s].
   - On MIGRATE: replace the rules list with §3.1's canonical
     set + any user-added rules from the original list
     preserved in their relative position. Custom rules land
     BEFORE rl_default_deny tail.

2. The "user-added custom rules" definition: any rule whose
   (who, what, effect) triple is NOT in the Phase 1 allow-all
   sentinel — i.e., anything the user added beyond the original.

3. Backup before write (Step 2).

Tests in tests/component/cli/test_migrate_authz.py:
- Workspace with allow-all sentinel + zero custom rules → after
  migration, exactly §3.1's five rules.
- Workspace with allow-all + 2 custom rules → after migration,
  §3.1's five rules with the 2 custom rules preserved before
  rl_default_deny.
- Workspace already at §3.1 → no migration prompt (silent skip).
- Idempotent: re-running against a migrated workspace is a no-op.

Scope: ~70 LOC code + ~80 LOC tests.

Acceptance: the migration produces a workspace whose subsequent
`kolsch chat` triggers the bash-prompt modal as Phase 2.0 §3.1
intended.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5 — authz migration.

Checks:

1. Custom rules added by the user (rules NOT in the Phase 1
   allow-all sentinel) are PRESERVED through migration. Verify
   with a test that has a custom rule like `(tool.bash, ^rg .*$,
   allow)`; confirm it survives.

2. rl_default_deny lands LAST after migration. Order matters per
   Phase 2.0 §3.2 first-match-wins.

3. The sentinel detection is precise: exactly the allow-all
   pattern. A workspace with a slightly different rule structure
   (e.g., a deny rule mixed in) doesn't trigger the prompt.

4. Idempotent — re-running against a migrated workspace is a
   silent skip.

5. Backup written before any change.

Verdict: PASS or FAIL.
```

---

## Step 6 — `defaults:` block on platform.yaml + resolver fallback

- **Builds:** the `defaults:` field on platform.yaml; the agent
  config resolver falls through to `defaults.<field>` when the
  agent and its `extends:` chain don't specify a field.
- **Acceptance:** an agent without `model:` resolves to
  `defaults.model`; omitting any other agent field falls through
  the same way.

### Developer prompt

```
[standing instructions]

Step 6 — Implement the defaults block + resolver fallback.

Read first:
- qol-setup-evolution-and-defaults.md §"Change 2".
- src/schemas/agent.py and src/schemas/platform.py.

Tasks:

1. platform.yaml schema gains:
   defaults: dict (optional). Keys map to agent field names; values
   are the agent-field-shaped values (e.g. defaults.model is a
   ModelSpec; defaults.permissions is list[Rule]; etc.).

2. Agent config resolver (likely in src/platform_runtime/runtime.py
   or wherever build_resolved_config lives):
   - For each agent field NOT specified by the agent AND NOT
     specified by any parent in the extends: chain (Phase 2.2):
     - Look up the field in platform.yaml's defaults.
     - If found, apply.
     - Otherwise, schema default (or validation error if required).

3. Composition order is locked: agent > extends chain > defaults >
   schema default.

4. Existing fields that have schema-level defaults (e.g.,
   `permissions: list[Rule] = []`) continue to use those defaults
   when neither agent nor extends nor platform-defaults specify.
   The change is additive: defaults is a NEW fallback layer
   between the extends chain and the schema's hardcoded default.

Tests in tests/unit/schemas/test_defaults_fallback.py:
- Agent without `model:` + defaults.model set → resolved config
  has defaults.model.
- Agent with `model: haiku` + defaults.model: opus → resolved
  config has haiku (agent wins).
- Agent extends parent that sets `model: claude` + defaults.model:
  opus → resolved config has claude (extends wins over defaults).
- Agent with `permissions: [<custom>]` + defaults.permissions
  set → resolved has the custom list (agent wins; no merging
  per wholesale-override discipline).
- Required field missing from agent, extends, AND defaults →
  validation error.

Scope: ~100 LOC code + ~80 LOC tests.

ESCALATION PATH: if Phase 2.2's extends chain resolver doesn't
expose a clean hook for "after extends, before schema default,"
escalate.

Acceptance: tests pass; an agent file with just `agent_id` + `name`
+ a Jinja-templated prompt successfully resolves against
defaults.
```

### Reviewer prompt

```
[standing instructions]

Review Step 6 — defaults block.

Checks:

1. Composition order is exactly agent > extends > defaults >
   schema. Verify with a test where all four levels set the same
   field; resolved should be agent's value.

2. defaults.permissions is wholesale — agent's permissions field
   REPLACES, doesn't merge with defaults.permissions. (Same
   wholesale discipline as Phase 2.2 inheritance.)

3. An agent file with just agent_id + name + prompt resolves
   successfully when defaults.model is set. This is the load-
   bearing UX property — minimal agent files work.

4. Required fields that aren't satisfied anywhere → validation
   error, not silent.

5. No regression in existing Phase 1.x / 2.0 agent tests.

Verdict: PASS or FAIL.
```

---

## Step 7 — Helper agent rewrite + canonical capture

- **Builds:** `bundles/kolsch/agents/helper.yaml` rewritten to
  demonstrate Phase 2.2's envelope. The canonical version is
  captured (per Step 4's mechanism) for future drift comparison.
- **Acceptance:** the helper agent is a self-contained showcase
  using Jinja, context_files, per-agent permissions, and omits
  `model:` (inherits from defaults).

### Developer prompt

```
[standing instructions]

Step 7 — Rewrite the helper agent + capture the canonical version.

Read first:
- qol-setup-evolution-and-defaults.md §"Change 3".
- process/phase2.2-agent-envelope-specs.md (the envelope
  features the helper should showcase).

Tasks:

1. Rewrite bundles/kolsch/agents/helper.yaml:

   agent_id: ag_helper
   name: helper
   description: A friendly general-purpose helper agent.

   # No `model:` — inherits from platform's defaults.model
   # No `extends:` — single self-contained file per coordinator's call

   tools: [read_file, write_file, list_files, bash]

   context_files:
     - path: kolsch:docs/INDEX.md
       summary: "Master index of all Kolsch documentation."
       primary: true
     - path: kolsch:AGENTS.md
       summary: "Operating rules for AI agents in this codebase."
       primary: true

   permissions:
     - { who: tool.bash, what: ".*", effect: prompt }
     # Demonstrates that this agent's permissions OVERRIDE the
     # platform's defaults — even if platform has a more permissive
     # rule, this agent always prompts on bash.

   prompt: |
     You are {{ agent.name }}, a friendly general-purpose helper.

     ## Where you are

     {{ workspace }}

     ## What you can do

     {% for t in tools %}
     - **{{ t.name }}**: {{ t.summary }}
     {% endfor %}

     ## Your documentation

     {{ docs }}

     When the user asks to see file content, return it verbatim in
     a fenced code block. Do not summarize unless explicitly asked.

   io: { mode: conversational }

2. Capture this exact file content as the canonical version for
   Step 4's drift detector. The canonical lives where Step 4
   decided (src/kolsch_canonical_bundle/agents/helper.yaml or
   equivalent).

3. Manual verification: load the helper via the engine; confirm
   the rendered system prompt includes the tool descriptions, docs
   index, workspace summary.

Scope: ~60 LOC (mostly the YAML).

Acceptance: helper agent loads and renders correctly; the rendered
prompt is what we'd want a user to see as a "this is what an agent
looks like" example.
```

### Reviewer prompt

```
[standing instructions]

Review Step 7 — helper rewrite.

Checks:

1. The helper agent omits `model:`. Verify by reading the file.

2. The prompt uses Phase 2.2's variable API (at least three of
   tools/docs/workspace/agent).

3. The context_files use the kolsch: prefix (not workspace:).

4. The permissions demonstrate the override pattern.

5. The canonical version is captured for Step 4's drift detector
   — verify by checking the canonical-bundle path.

6. Manual: load the agent through the engine; the rendered prompt
   includes the expected content.

Verdict: PASS or FAIL.
```

---

## Step 8 — Setup orchestrator integration (PIVOT)

- **Builds:** `kolsch setup` calls the three migrators (Steps 3-5)
  in the right order; honors the `defaults.model` field (Step 6);
  uses the updated helper (Step 7).
- **Acceptance:** running `kolsch setup` against an old-schema
  workspace produces a current-schema workspace; running against
  a current-schema workspace is a silent skip.

### Developer prompt

```
[standing instructions]

Step 8 — Integrate migrations into the setup orchestrator. This is
the user-visible pivot — existing workspaces start migrating here.

Read first:
- src/cli/_setup.py (current orchestrator).
- Steps 3, 4, 5, 6, 7's deliverables.

Tasks:

1. After the existing Step A (workspace location), insert a new
   migration phase before Step B (workspace skeleton):

   - If platform.yaml exists, run migrate_platform_yaml (Step 3).
   - If platform.yaml exists AND has authz rules, run
     migrate_authz_rules (Step 5).
   - If bundles/kolsch/ exists, run migrate_kolsch_bundle (Step 4).

   Each migrator handles its own prompts; the orchestrator just
   invokes them in order.

2. After migrations, the existing Step B (skeleton creation) is
   adapted: if platform.yaml doesn't exist (new workspace), write
   the canonical default INCLUDING the defaults block (Step 6).
   The defaults are populated by:
   - defaults.model: from the user's first connected provider's
     first alias (if any provider connected; otherwise null).
   - defaults.permissions: §3.1's rule set.
   - defaults.context_files: kolsch:docs/INDEX.md + kolsch:AGENTS.md.

3. Step D (starter agent creation) uses the rewritten helper from
   Step 7 instead of substituting <MODEL> into a template.

4. The "ready" banner mentions whether any migrations were
   applied: "Migrated 3 platform.yaml fields and 1 bundle file"
   if non-trivial.

Tests in tests/component/cli/test_setup_integration.py:
- Fresh workspace (no platform.yaml) → setup writes everything
  canonical; no migration prompts.
- Old-schema workspace (legacy allow-all + missing defaults block)
  → setup prompts for each migration; on accept-all, workspace
  ends up current.
- Current-schema workspace → setup is silent skip; no prompts.
- Mid-migration interrupt (Ctrl-C) → no partial state (backups
  intact; user's files unchanged).

Scope: ~80 LOC code + ~100 LOC tests.

ESCALATION PATH: if the existing setup flow is too tightly coupled
to "skip if initialized" semantics, refactoring it cleanly may be
larger than this step. Escalate before mass rewrites.

Acceptance: setup works correctly for fresh, old-schema, and
current-schema workspaces.
```

### Reviewer prompt

```
[standing instructions]

Review Step 8 — orchestrator integration. PIVOT step; be thorough.

Architectural checks:

1. Migration phase is INSERTED before existing setup steps, not
   intermixed with them. Read the orchestrator top-to-bottom.

2. Each migrator (Steps 3, 4, 5) is invoked exactly once; no
   double-running.

3. Mid-migration interrupt safety: kill the process mid-flight;
   confirm backups exist and the original files are intact.

Functional checks:

4. Fresh workspace: setup writes everything canonical; no migration
   prompts fire.

5. Old-schema workspace: migration prompts fire for the relevant
   migrators. User's existing values preserved after accept-all.

6. Current-schema workspace: silent skip; no prompts.

7. The "ready" banner accurately reports migrations applied.

8. The setup-produced platform.yaml includes the defaults block
   with sensible defaults populated from the user's connected
   provider.

Verdict: PASS or FAIL.
```

---

## Step 9 — Documentation review + getting-started update

- **Builds:** updates to user-facing docs reflecting the new
  behavior. Includes a getting-started walkthrough that shows what
  setup does now.
- **Acceptance:** docs accurately reflect the migration mechanism,
  the defaults block, and the helper agent's new shape.

### Developer prompt

```
[standing instructions]

Step 9 — Documentation review and update.

NO new functionality. Doc accuracy pass.

Read first:
- All the changes shipped in Steps 1-8.
- docs/tutorials/first-agent.md (the canonical getting-started
  tutorial).
- README.md (the project's front door).
- docs/how-to/permissions.md, docs/how-to/agent-templating.md
  (touched-but-not-yet-rewritten how-tos).

Tasks:

1. Update docs/tutorials/first-agent.md:
   - The "what kolsch setup produced" section gets a NEW subsection
     describing migrations on re-run: "If you've used kolsch
     before, setup detects what's changed and offers to update —
     non-destructively."
   - The "what's in your platform.yaml" callout includes the new
     defaults block, with comments explaining how it works.
   - The mention of "model" in the helper agent example is updated
     — show that the helper has no model field and inherits.

2. Update README.md quickstart:
   - The "First run" section gets a sentence about the upgrade
     story: "Re-running `kolsch setup` after a kolsch upgrade
     detects schema changes and offers to apply them."

3. Update docs/how-to/permissions.md:
   - Cross-reference the migration mechanism for users upgrading
     from old workspaces.

4. Update docs/how-to/agent-templating.md (added in Phase 2.2):
   - Add a recipe section: "Minimum agent file" showing the
     helper-style file with just agent_id + name + prompt (no
     model, no tools — all inherited from defaults).

5. New how-to (if it doesn't exist): docs/how-to/migrating-an-old-workspace.md
   - One-page walkthrough of what migrations look like, what to
     pick, where backups land.

6. Update process/roadmap.md status table for Phase 1.2 and
   Phase 2.3 to reflect this QoL's effect.

Each update should be small and accurate, NOT a wholesale rewrite.
Use the actual current behavior post-Step-8 as the source of
truth.

Scope: docs only; ~200 lines of edits + one new how-to.

Acceptance: a new user can read first-agent.md and the new how-to
and understand what setup does on first-run and on subsequent
upgrades.
```

### Reviewer prompt

```
[standing instructions]

Review Step 9 — documentation accuracy.

Checks:

1. docs/tutorials/first-agent.md accurately reflects:
   - What setup does on FIRST run (creates everything).
   - What setup does on SUBSEQUENT runs (detects drift, prompts).
   - The defaults block's purpose.
   - The helper agent's new minimum-fields shape.

2. README quickstart mentions the upgrade story (a single
   sentence is enough).

3. The new docs/how-to/migrating-an-old-workspace.md is recipe-
   style, not architecture re-explanation.

4. The "Minimum agent file" recipe in agent-templating.md
   actually shows a minimum file (just agent_id + name + prompt).
   Verify by loading it through the engine in a test fixture.

5. Roadmap status table updated for Phase 1.2 and Phase 2.3.

6. No accidental introduction of out-of-date claims (e.g.,
   "setup skips if initialized" — that's the OLD behavior).

Verdict: PASS or FAIL.
```

---

## Step 10 — Regression tests + manual e2e

- **Builds:** integration-level tests that exercise the full
  migration flow; manual end-to-end against a real old-schema
  workspace.
- **Acceptance:** all migration scenarios pass automated; manual
  verification confirms the UX is acceptable.

### Developer prompt

```
[standing instructions]

Step 10 — Regression tests + manual end-to-end.

Read first:
- Steps 2-8's individual tests.

Tasks:

1. Integration tests in tests/integration/test_qol_setup_evolution.py:
   - Full upgrade scenario: construct a Phase-1.2-era workspace
     fixture; run setup against it; confirm everything migrates
     correctly with user-supplied [m] choices throughout.
   - User chooses [k] on platform.yaml migration; confirm
     platform.yaml stays unchanged but other migrations run.
   - User chooses [s] on bundle migration; subsequent setup run
     re-prompts (skip is one-time).
   - Fresh-workspace scenario: setup produces a current-schema
     workspace; no migration prompts.

2. Manual end-to-end:
   - Set up a fresh kolsch workspace via setup.
   - Manually downgrade the workspace: edit platform.yaml to
     remove the defaults block; revert the bundle's helper.yaml
     to a hardcoded-model version; swap auth rules to allow-all.
   - Re-run setup; verify the three migration prompts fire in
     order; accept each; verify the workspace is current-schema
     after.
   - Inspect backups: confirm `.platform/platform.yaml.bak.<ts>`
     and bundle backups exist and are readable.

3. Run a chat session against the migrated workspace; verify
   helper works without any model field (inherits from defaults).

4. PR description includes the manual e2e transcript.

Scope: ~200 LOC tests + manual verification.

Acceptance: integration tests pass; manual transcript demonstrates
the upgrade flow works end-to-end.
```

### Reviewer prompt

```
[standing instructions]

Review Step 10 — final regression + manual.

Checks:

1. Integration tests cover the four major scenarios (full upgrade,
   partial [k] choice, [s] choice, fresh workspace).

2. Manual transcript in PR shows each migration prompt and the
   user's choice.

3. After migration, a chat session works against the upgraded
   workspace.

4. Backups exist and are readable.

5. Full test suite green — Phase 1.x, 2.0, 2.1, 2.2 all pass.

Verdict: PASS or FAIL. On PASS, this QoL is closed.
```

---

## How to escalate

```markdown
### Escalation E-QOL-SETUP-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–10
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:**
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-SETUP-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- The existing setup orchestrator is too tightly coupled to "skip
  if initialized" semantics — refactoring is bigger than Step 8.
- Pydantic's "default values" aren't cleanly introspectable for
  Step 3's canonical comparison.
- Binary files in the bundle need special migration handling
  (Step 4).
- Phase 2.2's extends-chain resolver lacks a hook for the
  defaults fallback layer (Step 6).

When NOT to escalate:
- Exact prompt wording (use established conventions).
- Backup file naming format.
- Whether to surface migration counts in the ready banner.

---

## Coordinator Escalation Log

*(no entries yet)*

---

## Notes on running these 10 steps

- **Strict order: 1 → 10.** Step 8 is the user-visible pivot;
  Step 9 is mandatory (docs accuracy) and follows the pivot.
- **The merge-gap discipline applies HARD here.** Each step's PR
  must land individually before the next begins. Otherwise the
  "fix setup" effort joins the uncommitted-bundle problem the
  CrewAI-stdout investigation surfaced (E-BUG-CREWAI-002 D3).
- **Step 9 is non-negotiable.** Docs that promise outdated
  behavior are a real source of user confusion. The previous
  Phase 2.X bugs all surfaced because docs and code drifted; this
  QoL's promise is to keep them aligned. The Step 9 reviewer
  PASS-bar should be high.
- **Manual verification (Step 10) is the final gate.** Spec-vs-code
  drift caused this QoL to exist in the first place. The manual
  e2e is what proves we've closed it for THIS round; the
  discipline that prevents the next round is the per-step PR
  commits.

---

## What this QoL does NOT include

- **Phase 2.3's reference agents (OpenClaw + Pi)** — after this
  QoL absorbs the drift-detection mechanism, Phase 2.3 shrinks to
  "add those two YAML files to the canonical bundle." Separate
  small PR.
- **`.env` schema evolution** — out of scope per coordinator's
  decision.
- **Multi-workspace migrations** — if the user has multiple
  workspaces, they migrate each via separate `kolsch setup` runs.
  No bulk-migrate.
- **Automated migration in CI / `--non-interactive`** — for now,
  migrations require interactive prompts. A
  `--migrate-non-interactive` flag that accepts all migrations
  silently is a future polish, not in scope.
- **Format-violation auto-fix** — when platform.yaml has a
  malformed value, we prompt; we don't try to repair
  automatically. User's choice.
