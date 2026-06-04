# Bug — `{{MODEL_LINE}}` setup-time substitution collides with Jinja syntax in agent YAML

**Draft 0.1** *(three-step build plan. Independent of the inheritance fix and the unified-refs QoL; ships in any order.)*

> The starter-bundle helper.yaml ships with a literal
> placeholder:
>
> ```yaml
> model: {{MODEL_LINE}}
> ```
>
> `kolsch setup` performs a literal-string substitution at
> install time (`src/cli/_setup.py:488, 553`) replacing the
> `{{MODEL_LINE}}` token with the user-selected model spec.
> The placeholder is NOT Jinja — it's a setup-time string
> substitution that happens to use Jinja-style `{{ }}` curlies.
>
> **The collision is real**: Phase 2.2's Jinja prompt
> rendering (`src/templating/render.py:71`) uses
> `StrictUndefined`. If a user reads the starter helper.yaml
> BEFORE running setup, they see `model: {{MODEL_LINE}}` and
> reasonably assume it's a Jinja variable. If they try to
> author a similar pattern in their own YAML expecting Jinja
> evaluation, they get StrictUndefined errors. The two
> templating systems share syntax but have different
> semantics, different execution times, and different scope.

---

## Part 0 — Why this matters

### 0.1 Two templating systems, same syntax

Kolsch has TWO mechanisms that use `{{ … }}` curly-bracket
syntax:

1. **Jinja templating** at agent-load (`src/templating/render.py`):
   - Variables resolved against the engine's context dict.
   - StrictUndefined: typos raise.
   - Runs at engine pre-flight step 5.6.6.
   - Available: `{{ agent.name }}`, `{{ workspace }}`,
     `{{ tools }}`, etc.

2. **Setup-time string substitution** at install
   (`src/cli/_setup.py:553`):
   - Literal Python str.replace.
   - Only one token: `{{MODEL_LINE}}`.
   - Runs ONCE when `kolsch setup` writes the helper into
     the workspace.

A reader seeing `{{MODEL_LINE}}` cannot tell which system
will resolve it. The first-read assumption is "Jinja" —
because the docs at `docs/how-to/agent-templating.md`
describe Jinja templating. Setup-time substitution is
documented nowhere prominent.

### 0.2 The collision can produce real errors

A user who reads the starter helper and then writes their
own agent YAML with `model: {{MY_PROVIDER}}` (assuming
Jinja-style substitution) gets:

```
config.template_render_failed: 'MY_PROVIDER' is undefined
```

The error doesn't explain that they were attempting a
non-Jinja pattern. They reach for the wrong docs.

### 0.3 The fix is small and clean

`kolsch setup` should produce a final YAML through a proper
mechanism, not string substitution:

1. **YAML-parse-and-edit**: read the starter YAML, set
   `model.provider` and `model.model` on the parsed dict,
   write back. No template placeholder needed; the starter
   ships with a default and setup overwrites it.

2. **Default-and-override**: starter ships with a default
   `model` value (e.g., `opencode/qwen3.6-plus`). Setup
   doesn't touch it; the user edits if they want different.
   Minimum machinery.

3. **Separate workspace-level override**: starter has a
   placeholder default; a workspace-level `defaults.yaml`
   (or similar) provides per-install overrides via the
   resolver's existing defaults composition. Heavier but
   most flexible.

Option 2 is the cleanest. The starter ships a working
default; setup doesn't run any text substitution; users
edit `model:` themselves if they want to switch providers.
Aligns with the principle that YAML files are
human-readable AND machine-readable WITHOUT a hidden
pre-processing step.

---

## Part 1 — Decisions locked

From the agent-YAML-design conversation:

1. **Remove the `{{MODEL_LINE}}` placeholder mechanism**
   entirely. The setup-time string substitution at
   `src/cli/_setup.py:488, 553` goes away.

2. **Starter bundle ships with a default model.** Use the
   provider+model that `kolsch setup` would have selected
   for an unconfigured user (`opencode/qwen3.6-plus`
   per the current starter-pickem flow), so the agent
   works immediately after `kolsch init` without manual
   editing.

3. **Setup-time model selection still happens**, but it
   writes to a separate place. Two sub-options for where:
   a. Writes to workspace's `platform.yaml` under a
      `defaults.model:` key (the resolver already supports
      defaults composition per qol-setup-evolution).
   b. Writes directly to the helper.yaml via YAML-parse +
      edit (mutating the file on disk).
   Step 1 picks one. Recommendation: (a) — separation of
   concerns; the starter file stays static; per-workspace
   defaults live with other workspace defaults.

4. **No Jinja-side change.** The Jinja templating system
   stays as-is. The fix is to remove the conflicting
   non-Jinja system, not to merge them.

5. **Migration for existing users.** Existing workspaces
   already have a substituted helper.yaml — no
   `{{MODEL_LINE}}` remains there. The starter bundle's
   change affects only NEW workspaces and any re-run of
   `kolsch setup` against an existing workspace (where the
   drift detector already handles user edits per
   qol-setup-evolution Step 6).

---

## Part 2 — Acceptance criteria

The bug is fixed when **all** hold:

1. **`{{MODEL_LINE}}` appears nowhere in src/.** `grep -rn
   "MODEL_LINE" src/` returns zero hits.

2. **Starter bundle helper.yaml ships with a working
   default model** spelled out as ordinary YAML:
   ```yaml
   model: { provider: opencode, model: qwen3.6-plus }
   ```
   (Or whatever the canonical default is at the time the
   spec lands.)

3. **`kolsch setup` no longer performs string substitution
   on helper.yaml.** The setup flow either:
   a. Writes the user-selected model to a separate
      defaults config (per Step 1's pick option a), OR
   b. YAML-parse-edits the helper.yaml (per option b).

4. **Existing user workspaces are unaffected.** A workspace
   that already went through old-style setup has a
   substituted helper.yaml with no `{{MODEL_LINE}}`; the
   fix doesn't regress those.

5. **Fresh `kolsch init` produces a working agent** without
   any further user edit. Manual smoke: install kolsch in
   a tmp dir, run `kolsch init`, then `kolsch chat`. The
   helper runs with the default model.

6. **User-driven setup of a different model still works.**
   Manual smoke: run `kolsch setup` and pick a non-default
   model. The user-selected model takes effect.

7. **Documentation surfaces the change.** `docs/how-to/`
   workspace-setup docs (or equivalent) explain that the
   starter ships with a default model; users override via
   `kolsch setup` OR direct YAML edit.

8. **Regression tests cover both paths**: fresh init
   (default model), setup-with-different-model.

### Out of scope

- **Refactoring Jinja templating.** The fix removes the
  conflicting non-Jinja system, not the Jinja one.
- **Migrating existing user-authored helper.yaml files.**
  Existing workspaces post-setup don't contain
  `{{MODEL_LINE}}`; no migration needed.
- **Changing the setup flow's interactive UX** (model
  picker, etc.). Setup still picks a model; only the
  WRITE mechanism changes.
- **Removing all setup-time templating.** If other
  setup-time substitutions exist (Step 1 will surface
  any), they're outside this fix's scope.

---

## Part 3 — Step dependency graph

```
Step 1 (verification + setup-write pick) ←── STOP gate
   │
   ▼
Step 2 (starter bundle default + setup rewrite + tests)
   │
   ▼
Step 3 (docs + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming the setup-write target + audit of any other substitutions | ~20 min |
| **2** | Starter YAML + setup code + unit tests | ~30 min |
| **3** | Docs + manual e2e | ~15 min |

---

## Step 1 — Verification + setup-write pick

### Developer prompt

```
[standing instructions]

Step 1 — Verify the substitution surface + pick where
setup writes the user's chosen model.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-model-line-placeholder-collides-with-jinja.md Parts 0-2.
- src/cli/_setup.py around line 488 (_MODEL_PLACEHOLDER
  definition) and 553 (substitution site).
- src/cli/_starter_bundle/kolsch/agents/helper.yaml (the
  source of the {{MODEL_LINE}} token).
- src/templating/render.py (the Jinja side it collides
  with).
- src/cli/_migrations/ (if there are setup-time
  rewriters using similar patterns).

Verification tasks:

1. Audit ALL setup-time substitutions.
     grep -rn "MODEL_LINE\|_PLACEHOLDER\|{{.*}}" src/cli/_setup.py
     grep -rn "{{[A-Z_]\\+}}" src/cli/_starter_bundle/ --include="*.yaml"
   List every substitution token + every file it appears
   in.

2. Confirm {{MODEL_LINE}} is the ONLY collision-with-Jinja
   token. If there are others (e.g., {{WORKSPACE_PATH}},
   {{KOLSCH_VERSION}}), document them. They might belong
   in this fix or a sibling ticket.

3. Pick where setup writes the chosen model — option (a)
   or (b) from Part 1 §3.
   - Option (a): write to platform.yaml's defaults.model.
     - Trace the resolver's defaults composition. Does
       `defaults.model` already flow through to the
       agent's resolved model field? Cite the merge order.
     - If yes (likely per qol-setup-evolution Step 6),
       this option is clean.
   - Option (b): YAML-parse-edit helper.yaml.
     - Heavier; mutates a starter file on disk.
     - Requires a stable YAML parser/writer in the
       codebase.
   - Recommend (a). Confirm.

4. Confirm the starter helper.yaml default.
   - Pick the model that becomes the default when shipped:
     opencode/qwen3.6-plus.
   - Verify this matches what `kolsch setup` would
     default-pick today for an unconfigured user.

5. Trace impact on existing workspaces.
   - Confirm: a workspace that ran `kolsch setup` already
     has helper.yaml with the substituted value (no
     {{MODEL_LINE}} remaining). The fix doesn't regress
     these.
   - Confirm: `kolsch setup` re-run against an existing
     workspace uses drift detection (per
     qol-setup-evolution Step 6). What happens when the
     re-run encounters the new default-shipped form vs.
     the user's substituted form? Drift detection will
     flag it; the user can keep their version. Document.

6. Test surface — list existing tests that pin the
   substitution behavior:
     grep -rn "MODEL_LINE\|_MODEL_PLACEHOLDER" tests/
   For each: confirm what they assert. They'll likely all
   need updating (the substitution behavior is going
   away).

7. STOP and post the writeup.

ESCALATION PATH: if Step 1 finds OTHER setup-time
substitution tokens beyond {{MODEL_LINE}} that collide
with Jinja syntax, escalate. They might belong in this
fix or warrant a separate ticket.

Acceptance: writeup posted with the audit + setup-write
pick + impact analysis; coordinator approves.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. Writeup is the deliverable.

Checks:

1. The audit lists every setup-time substitution token in
   src/cli/_setup.py and the starter bundle. {{MODEL_LINE}}
   is confirmed as the only collision-with-Jinja token (or
   others are explicitly listed for separate-ticket
   triage).

2. The setup-write pick (option a or b) is locked with
   rationale.

3. If option (a): the resolver's defaults.model
   composition is confirmed to flow into the agent's
   resolved model. Cite the merge order.

4. The starter default model is named.

5. Impact on existing workspaces is documented. Drift
   detection's interaction with the new default form is
   explicit.

6. Test surface listed. Any test that breaks under the
   change is flagged for update in Step 2.

7. Escalation condition (other substitution tokens) is
   confirmed not-an-issue or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Starter bundle default + setup rewrite + tests

### Developer prompt

```
[standing instructions]

Step 2 — Implement the fix per Step 1's design.

Read first:
- Step 1's writeup.
- src/cli/_setup.py.
- src/cli/_starter_bundle/kolsch/agents/helper.yaml.
- The resolver's defaults composition (per qol-setup-evolution).

Tasks:

1. Update src/cli/_starter_bundle/kolsch/agents/helper.yaml.
   - Replace `model: {{MODEL_LINE}}` with the canonical
     default per Step 1's pick:
     `model: { provider: opencode, model: qwen3.6-plus }`
     (or whichever spec Step 1 named).

2. Remove the substitution logic from src/cli/_setup.py.
   - Delete `_MODEL_PLACEHOLDER = "{{MODEL_LINE}}"` at
     line 488.
   - Delete the substitution call site at line 553.
   - Remove any helper functions that exist only to
     support this substitution.

3. Wire setup's model-selection output to the chosen
   location (per Step 1's pick).
   - If option (a) — write to platform.yaml:
     - Setup writes the user-selected model to
       platform.yaml's defaults.model.provider and
       defaults.model.model.
     - The resolver's existing defaults composition
       handles propagation.
   - If option (b) — YAML-parse-edit:
     - Read helper.yaml as YAML; set the model fields;
       write back. Use ruamel.yaml or pyyaml (whichever
       the project already depends on) preserving
       comments / formatting.

4. Update src/cli/_setup.py's flow so:
   - A user accepting defaults gets a workspace where the
     starter helper.yaml's shipped default model is in
     effect.
   - A user picking a different model gets that model
     active via the chosen mechanism (a or b).

5. Tests:
   - tests/component/test_setup_no_model_line.py:
     * After setup with default selection, helper.yaml in
       the workspace contains the starter's default model
       value (NO {{MODEL_LINE}}).
     * After setup with a non-default selection, the
       chosen model is active per the wiring (defaults
       composition OR YAML-edited helper).
     * grep for {{MODEL_LINE}} in the workspace after
       setup returns zero hits.

6. Update existing tests that asserted the substitution
   behavior. The old behavior is gone; the tests should
   either be deleted (asserted behavior no longer exists)
   or updated to the new path.

7. NO change to:
   - The Jinja templating system.
   - The agent schema or resolver merge rules.
   - Other setup-time logic outside the helper.yaml model
     substitution.

Scope: ~40 LOC code changes + ~70 LOC tests.

ESCALATION PATH: if option (a)'s defaults composition
doesn't actually flow into the resolved agent model
(contrary to Step 1's verification), fall back to option
(b). Document the fallback.

Acceptance: starter helper.yaml has no placeholder; setup
code has no substitution logic; both fresh init and
non-default selection produce a working chat against the
correct model.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. `grep -rn "MODEL_LINE" src/` returns zero hits.

2. Starter helper.yaml has an explicit default model with
   the value Step 1 named.

3. _setup.py substitution code is GONE. No vestigial
   helpers that only existed to support it.

4. Setup's user-selection flow writes to the location
   Step 1 picked. Verify by reading.

5. New tests cover both paths (default + custom
   selection).

6. Existing substitution-asserting tests are deleted or
   updated; the change is justified in the PR.

7. NO regression on the broader setup test suite.

8. Full test suite green. tc_eng_* goldens still
   byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Docs + manual e2e + handoff

### Developer prompt

```
[standing instructions]

Step 3 — Document the change + verify in real terminal.

Read first:
- Step 2's PR.
- docs/how-to/workspace-and-setup.md (or equivalent
  setup how-to).

Tasks:

1. Update docs/how-to/workspace-and-setup.md.
   - Note that the starter bundle ships with a default
     model (name the model). Users running `kolsch
     setup` can pick a different one OR edit the YAML
     directly.
   - Remove any mention of {{MODEL_LINE}} or "template
     placeholder" if it appears.

2. Update docs/reference/schemas.md if it mentions
   setup-time templating. There should be no language
   that suggests Jinja-style syntax in YAML is anything
   other than Jinja.

3. Update CLAUDE.md or AGENTS.md if either references
   the substitution mechanism.

4. Manual e2e — fresh install:
   a. In a tmp dir, run `kolsch init` to bootstrap a
      fresh workspace (or whatever the current
      bootstrap command is).
   b. Run `kolsch chat` — the helper should start
      without any further config.
   c. Capture session JSONL excerpt showing the
      model.request event with the default model.

5. Manual e2e — setup with non-default selection:
   a. In a tmp dir, run `kolsch setup` and pick a
      non-default model.
   b. Run `kolsch chat` — the helper should use the
      chosen model.
   c. Capture model.request showing the user's
      selection.

6. Manual e2e — existing workspace re-run:
   a. Use ~/Documents/kolsch (existing workspace).
   b. Run `kolsch setup` — the drift detector should
      flag the helper.yaml change (new shipped default vs.
      existing substituted form).
   c. User keeps their version; setup completes.

7. NO new how-to required. The reference + workspace
   how-to updates are enough.

Scope: ~30 LOC docs + manual report.

ESCALATION PATH: if the existing-workspace re-run shows
unexpected drift-handling behavior, escalate. The
interaction with qol-setup-evolution's drift mechanism is
load-bearing.

Acceptance: docs updated; all three e2e scenarios pass;
follow-ups (if any) filed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. docs/how-to/workspace-and-setup.md updated to mention
   the shipped default model. No mention of {{MODEL_LINE}}
   or setup-time templating in any updated doc.

2. Manual e2e: PR description has captured JSONL excerpts
   for all three scenarios (fresh install, custom
   selection, existing-workspace re-run).

3. The existing-workspace re-run scenario explicitly
   names the drift detector's behavior (flagged vs.
   silent migration vs. forced overwrite).

4. Any follow-up tickets named are FILED under process/.

5. Full test suite green.

Verdict: PASS or FAIL.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-BUG-MODELLINE-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-MODELLINE-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds other setup-time substitution tokens that
  collide with Jinja syntax (might belong in this fix or
  spawn a sibling ticket).
- Step 1's chosen option (a) doesn't actually flow through
  the resolver; fallback to option (b) needs coordinator
  sign-off.
- Step 3's existing-workspace re-run shows unexpected
  drift-handling behavior.

When NOT to escalate:
- Doc wording.
- The canonical default model choice if Step 1 picked
  reasonably.
- Whether the YAML-parse-edit uses ruamel.yaml vs.
  pyyaml.

---

## Part 5 — Coordinator Escalation Log

*(empty)*

---

## Part 6 — Out of scope

- Refactoring Jinja templating.
- Migrating existing user-authored helper.yaml files.
- Changing the setup flow's interactive UX.
- Removing all setup-time templating (only the
  Jinja-syntax-colliding one).

---

## Part 7 — Related work

- **Sibling specs in same conversation**:
  - `qol-yaml-version-fields-optional.md` — independent;
    smallest.
  - `qol-prefix-defaults-and-unified-refs.md` —
    independent; blocks the inheritance fix's docs.
- **Drift detection**: qol-setup-evolution-and-defaults
  Step 6 ships the per-file drift handler this fix
  interacts with at existing-workspace re-run time.
- **Resolver defaults composition**: qol-setup-evolution
  Step 6 also ships the `defaults.<field>` resolution
  path that option (a) plugs into.
- **Jinja templating system**: src/templating/render.py
  and src/templating/inheritance.py — the system this
  fix's removal makes unambiguously canonical.
