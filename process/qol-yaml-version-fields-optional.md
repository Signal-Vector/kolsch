# QoL — Make `schema_version` and `assembly_version` optional with defaults

**Draft 0.2** *(bumps 0.1 after Step 1 verification surfaced E-QOL-VERS-001: Pydantic defaults on the partial schema don't propagate through the dict-based kernel pipeline. Mechanism redirected to `ResolvedAgentConfig`; user-visible contract unchanged. Resolution + spec corrections in Part 5.)*

> Every agent YAML in kolsch starts with two lines of pure
> boilerplate:
>
> ```yaml
> schema_version: "0.1"
> assembly_version: "1"
> ```
>
> Both are required by the partial-config schema today, but
> they teach beginners that YAML has rituals before they can
> express anything useful. `schema_version` has no active
> code use; `assembly_version` is checked at engine
> capability-mismatch time but always has the value `"1"`.
>
> This QoL makes both fields optional at the partial-config
> layer with sensible defaults. The capability check stays
> intact; the resolved config still carries both values; the
> author just doesn't have to type them.

---

## Part 0 — Why this matters

### 0.1 `schema_version` has one narrow active use

`grep -rn "schema_version" src/` (audited in Step 1) shows
the field has one active use: `src/kernel/pipeline.py:77-105`
`_check_schema_versions` enforces a same-version invariant
across partials per Schemas §I8 — if `agent.yaml` declares
`schema_version: "0.1"` and a default-layer YAML declares
`"0.2"`, the pipeline rejects the merge.

The check filters partials that don't carry an explicit
version (line 84: `"schema_version" in p and p["schema_version"]
is not None`). Partials that OMIT the field skip the check
entirely. So making the field implicit (this QoL's change)
doesn't weaken the §I8 invariant — it just means partials
without an explicit version don't participate in the
cross-partial comparison.

Beyond the §I8 check, there is no migration framework, no
version-conditional parsing path, no `if schema_version >=
"0.2"` branch. The value `"0.1"` is the only one anyone has
ever written. The field exists as a future hook with one
narrow same-version invariant.

### 0.2 `assembly_version` is load-bearing but defaults to one value

`src/engine/engine.py` does:

```python
caps = self.engine.capabilities()
required = [self.config.assembly_version]
missing = sorted(set(required) - set(caps.assembly_versions))
if missing:
    raise CapabilityMismatch(...)
```

So the field genuinely matters — it tells the engine "I was
authored against assembly contract version X." If a future
engine v2 changes the assembly contract, declaring
`assembly_version: "1"` against a v2-only engine fails fast
with a clear error.

But today there is exactly one valid value (`"1"`). Every
agent declares it; nobody ever picks a different one. Pure
copy-paste.

### 0.3 Boilerplate kills new-user trust

The user's first agent YAML is short. Adding two version
fields with values they don't understand is a stumble. The
fix is trivial: defaults at the partial layer; required at
the resolved layer; capability check unchanged.

---

## Part 1 — Decisions locked

From the agent-YAML-design conversation:

1. **`schema_version` becomes implicit at the partial layer with
   default `"0.1"` applied at the resolved layer.** The partial
   schema already accepts the field as `str | None = None` (per
   Step 1 verification) — no partial-schema change needed. The
   default lives on `ResolvedAgentConfig` so the dict-flow
   pipeline picks it up at validate time. **Per E-QOL-VERS-001
   coordinator resolution.**

2. **`assembly_version` becomes implicit the same way.** Default
   `"1"` at the resolved layer. The capability check at engine
   pre-flight runs unchanged against the resolved value.

3. **Resolved config still carries both fields.** Both fields
   stay on `ResolvedAgentConfig`; the capability check and
   §I8 cross-partial check still see the values. The change
   is the schema-declaration mechanism (default vs. required)
   — not the user-visible contract that "every resolved agent
   has both versions populated."

4. **Migration story documented.** When a future engine v2
   arrives:
   - Old YAMLs without an explicit version pick up the
     default (`"1"`).
   - New YAMLs that need v2 features write `assembly_version:
     "2"` to opt in.
   - The engine's capability check is the gate. No automatic
     coercion.

5. **No starter-bundle change in this spec.** The starter
   helper.yaml continues to declare both versions explicitly
   (it's a reference example; explicit is fine there). The
   migration story for existing user YAMLs is "you can leave
   them as-is, or remove them on next edit."

6. **No CLI surface change.** No new `kolsch agents migrate`
   command. Defaults at the partial schema layer is the
   entire mechanism.

---

## Part 2 — Acceptance criteria

The QoL is shipped when **all** hold:

1. **Both fields are implicit on the partial config schema.**
   `PartialAgentConfig` already declares them as `str | None
   = None` (verified in Step 1). No partial-schema change is
   needed; the field is already optional at the YAML-input
   layer.

2. **Both fields have schema-level defaults on
   `ResolvedAgentConfig`.** `schema_version: str = Field(default="0.1")`
   and `assembly_version: str = Field(default="1")`. The
   resolved config's user-visible contract is unchanged
   (every resolved agent carries both values populated); the
   declaration mechanism becomes "Pydantic default" instead
   of "required field with no default."

3. **Existing YAMLs that DECLARE the fields still load
   identically.** Byte-equal resolved config; no regression.

4. **YAMLs that OMIT the fields load with the defaults.**
   Resolved config has `schema_version == "0.1"` and
   `assembly_version == "1"`.

5. **The engine's capability check still fires** on a
   resolved config whose `assembly_version` doesn't match the
   engine's supported set. Test this by constructing a
   resolved config with `assembly_version: "999"`.

6. **Regression tests pin both behaviors**: declared-then-load
   AND omitted-then-load.

7. **Docs updated**: `docs/reference/schemas.md` notes the
   fields as optional at the partial layer with defaults.

### Out of scope

- **Removing the fields entirely.** They stay; just become
  implicit.
- **A new version-validation framework.** Future versions
  declare themselves explicitly; old YAMLs default to v1.
- **Starter-bundle YAML simplification.** Reference examples
  keep the fields explicit.
- **Migrating existing user-authored YAMLs.** No `kolsch
  agents migrate` command; users can leave their YAMLs
  as-is or drop the fields on next edit.

---

## Part 3 — Step dependency graph

```
Step 1 (verification + schema-layer pick) ←── STOP gate
   │
   ▼
Step 2 (schema change + tests)
   │
   ▼
Step 3 (docs + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming the partial-vs-resolved layer split + the test surface | ~15 min |
| **2** | Schema defaults + unit tests | ~20 min |
| **3** | Doc update + manual e2e | ~15 min |

---

## Step 1 — Verification + schema-layer pick

### Developer prompt

```
[standing instructions]

Step 1 — Verify the schema-layer split before code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-yaml-version-fields-optional.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/schemas/config.py (PartialAgentConfig + ResolvedAgentConfig).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/engine.py (the capability check around step 381-388 area).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/kernel/pipeline.py (where layer-1 + layer-2 validation runs).

Verification tasks:

1. Identify both schema classes.
   - The PARTIAL accepts user input + sets default values.
   - The RESOLVED is post-merge, post-validation.
   - Confirm both are defined and named.

2. Pin the default values:
   - schema_version: "0.1"
   - assembly_version: "1"

3. Confirm the capability check site.
   - Cite engine.py:_preflight where assembly_version is
     compared against capabilities.assembly_versions.
   - Confirm this check operates on RESOLVED config (not
     partial), so making the partial optional doesn't affect
     it.

4. Confirm no other code path branches on schema_version or
   assembly_version values:
     grep -rn "schema_version\|assembly_version" src/ --include="*.py"
   List every hit. For each, document whether it's:
   - Field declaration (expected)
   - Capability check (expected)
   - Anything else (flag)

5. Test surface — list which existing tests pin the
   "version field required" behavior:
     grep -rn "schema_version\|assembly_version" tests/ --include="*.py"
   For each: confirm whether it asserts the field is
   required (will break under the change) or just uses the
   field in a fixture (will continue working).

6. STOP and post the writeup.

ESCALATION PATH: if grep reveals an unexpected code path
that branches on either version value (suggesting active use
beyond capability check), escalate. The fix may need to
preserve a non-default behavior we don't currently
understand.

Acceptance: writeup posted; coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. Writeup is the deliverable.

Checks:

1. Both schema classes named with file:line citations.

2. Capability check site cited with file:line. Confirms it
   operates on resolved (not partial).

3. The grep on src/ is exhaustive AND each hit categorized.
   "I found a few hits" without a list is a fail.

4. Test surface listed. Any test that breaks under the
   change is flagged for update in Step 2.

5. Escalation condition (unexpected branching code path) is
   either confirmed not-an-issue or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Schema defaults + unit tests

### Developer prompt

```
[standing instructions]

Step 2 — Make both fields optional on the partial.

Read first:
- Step 1's writeup.
- src/schemas/config.py.

Tasks:

1. Update ResolvedAgentConfig in src/schemas/config.py.
   - Change the two field declarations from required to
     defaulted:
       schema_version: str = Field(default="0.1")
       assembly_version: str = Field(default="1")
   - Preserve any existing field-validator or description.
   - **DO NOT** touch PartialAgentConfig in
     src/kernel/partials.py — Step 1 confirmed the partial
     already accepts the fields as `str | None = None`, and
     the dict-flow pipeline means partial defaults wouldn't
     propagate even if added. Per E-QOL-VERS-001.

2. Confirm PartialAgentConfig is UNCHANGED.
   - The partial layer continues to accept the field as
     `str | None = None`. No edits needed there.
   - The kernel pipeline's _check_schema_versions at
     pipeline.py:77-105 continues to filter partials with
     missing/None version values — no change needed there
     either.

3. Unit tests in tests/unit/test_schemas.py (or the file
   Step 1 named):
   - test_resolved_default_schema_version_when_missing:
     constructs ResolvedAgentConfig with the version field
     omitted (other required fields present); asserts the
     instance is created with schema_version == "0.1".
   - test_resolved_default_assembly_version_when_missing:
     same for assembly_version → "1".
   - test_resolved_default_both_when_missing:
     omit both; assert both defaults applied.
   - test_resolved_preserves_explicit_versions:
     construct with schema_version="0.2",
     assembly_version="2"; assert those exact values
     preserved (defaults don't override explicit).

4. Integration test in tests/integration/ (whichever file
   exercises build_resolved_config):
   - Author a tmp workspace with a minimal agent YAML that
     OMITS both version fields.
   - Call build_resolved_config(ref).
   - Assert: succeeds; resolved.schema_version == "0.1";
     resolved.assembly_version == "1".
   - Run a fixture with assembly_version: "999" through to
     the engine preflight; assert CapabilityMismatch raises.

5. Update the existing comment at
   tests/unit/test_kernel.py:554-561 in
   `test_validate_resolved_requires_required_fields`.
   - The assertion (≥1 error of code "missing" /
     "missing_required") continues to pass because other
     required fields (agent_id, name, description, model,
     prompt, io) still raise.
   - But the comment at lines 555-556 lists schema_version
     and assembly_version among the expected missing
     fields. Remove them from that list — they no longer
     raise.

6. NO change to:
   - The starter bundle's helper.yaml (Step 3 docs may
     touch it; this step does not).
   - Engine.py's capability check.
   - The kernel pipeline (including _check_schema_versions —
     its filter at line 84 already handles None/missing
     cleanly).

Scope: ~15 LOC schema + ~60 LOC tests.

ESCALATION PATH: if a test reveals an existing fixture
relied on either field being required (e.g., a negative
test "yaml without schema_version is rejected"), update
that test under Step 2 — flag the change in the PR
description.

Acceptance: full test suite green; new tests pass; existing
tests either pass unchanged or are updated with
justification.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. Partial schema has both fields with defaults. Resolved
   schema has both fields still required. Verify by reading.

2. The four required unit tests exist + pass.

3. The integration test that drives a missing-versions
   YAML through build_resolved_config passes.

4. The CapabilityMismatch test (assembly_version="999")
   still fires. This is the load-bearing behavior the QoL
   must preserve.

5. Any existing test that asserted "version field required"
   is updated; the update is justified in the PR.

6. NO change to engine.py, starter bundle, or kernel
   pipeline.

7. Full test suite green. tc_eng_* goldens still byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Docs + manual e2e + handoff

### Developer prompt

```
[standing instructions]

Step 3 — Document the change + verify in a real workspace.

Read first:
- Step 2's PR.
- docs/reference/schemas.md (the section on agent envelope
  fields).

Tasks:

1. Update docs/reference/schemas.md.
   - At the schema_version row: add "Optional at the partial
     layer; defaults to '0.1' when omitted."
   - At the assembly_version row: add "Optional at the
     partial layer; defaults to '1' when omitted. The
     engine's capability check runs against the resolved
     value; declaring a value the engine doesn't support
     raises CapabilityMismatch."

2. Manual e2e:
   a. In a fresh workspace (or a tmp dir), author a minimal
      butler-like agent YAML omitting BOTH version fields.
      Something like:
        agent_id: ag_minimal
        name: minimal
        description: minimal test agent
        model: { provider: opencode, model: qwen3.6-plus }
        prompt: "You are minimal. Be terse."
        io: { mode: conversational }
   b. Run: kolsch chat <bundle>/agents/minimal
   c. Verify: loads cleanly; resolved config has
      schema_version="0.1" and assembly_version="1".
   d. Capture session JSONL excerpt showing the config.set
      event with the default values.

3. Follow-up tickets (file as new process/ docs ONLY if
   surfaced during this work; don't fish):
   - If Step 2 surfaced a need for explicit version
     documentation in tutorials, file as a docs follow-up.

4. NO new how-to. The reference doc update + a brief
   migration sentence is enough.

Scope: ~20 LOC docs + manual report.

ESCALATION PATH: if the manual e2e shows the resolved
config does NOT have the defaults (a hidden code path
overrides them), Step 2 missed something. Escalate.

Acceptance: docs updated; manual e2e shows defaults apply;
no remaining ✗ from the spec's acceptance criteria.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. docs/reference/schemas.md updated for both fields with
   the documented defaults + the capability-check note.

2. Manual e2e: PR description has the JSONL excerpt showing
   the config.set event with default values applied.

3. No new how-to or tutorial added. The reference update is
   enough for this QoL.

4. Full test suite green.

5. Any follow-up tickets named are FILED (not just listed)
   under process/.

Verdict: PASS or FAIL.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-QOL-VERS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-VERS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds an unexpected code path branching on either
  version value.
- Step 2 finds the change breaks unrelated tests in ways the
  surface inspection didn't predict.
- Step 3's manual e2e shows defaults aren't applied.

When NOT to escalate:
- Doc wording in schemas.md.
- Whether the default values are literal strings vs.
  constants imported from a module.

---

## Part 5 — Coordinator Escalation Log

### E-QOL-VERS-001 — Partial-layer Pydantic defaults don't propagate through the dict-based pipeline

- **Raised by:** developer (Step 1 verification writeup)
- **Step:** 1
- **Trigger:** `src/kernel/validation.py:69-86` calls
  `PartialAgentConfig.model_validate(partial)` and discards
  the result (returns only `ValidationResult`). The raw
  partial dict flows unchanged into `resolve(partials)` at
  `src/kernel/pipeline.py:149`. Pydantic defaults set on
  `PartialAgentConfig` do not appear in the merged dict
  that reaches `ResolvedAgentConfig.model_validate(resolved)`
  at `pipeline.py:214`.
- **Why this is not a guess:** the pipeline at line 148
  takes `list[dict]` as input; Pydantic models are
  constructed only by validators that discard their
  results. The data flow path is dict → merge → dict →
  ResolvedAgentConfig.model_validate.
- **Consequence:** spec Part 2 acceptance criterion #1
  ("Both fields are optional on the partial config
  schema" with defaults `"0.1"`/`"1"`) is a no-op for the
  actual data flow. Acceptance criterion #4 ("YAMLs that
  OMIT the fields load with the defaults — resolved
  carries `"0.1"`/`"1"`") cannot be achieved by editing
  the partial schema alone.
- **Options:**
  - **(A)** Put defaults on `ResolvedAgentConfig` only.
    Smallest change. Contradicts spec Part 1 decision #2's
    "required" wording but achieves user-visible
    acceptance.
  - **(B)** Change `validate_partial` (or the pipeline) to
    pass `model_validate(...).model_dump(exclude_unset=False)`
    forward. Bigger change; affects partial round-trip
    semantics; risk of surprise test failures.
  - **(C)** Set defaults on both layers. Belt-and-
    suspenders. Honors the spec's partial-layer framing.
- **Estimated cost:** (A) ~5 LOC + tests. (C) ~5 LOC +
  tests. (B) larger and not recommended.

---

### Coordinator resolution — E-QOL-VERS-001

- **Decided by:** coordinator
- **Decision:** **Option A.** Defaults live on
  `ResolvedAgentConfig` only. `PartialAgentConfig` stays
  as `str | None = None` (already optional per Step 1).
- **Rationale:** Option C adds dead code on the partial
  layer — defaults that never fire because the pipeline
  discards the validated model. A future reader would
  reasonably assume both defaults were load-bearing.
  Cleaner to put the default exactly where it works.
  Part 1 decision #2's "required" wording was the only
  thing relaxed — the user-visible contract ("resolved
  config always carries both versions populated") is
  preserved.
- **Resume instruction:** Spec corrections applied in
  Parts 0/1/2 and Step 2 task list. Developer proceeds
  with Step 2 against `src/schemas/config.py`
  (ResolvedAgentConfig) per the revised task 1.

---

## Part 6 — Out of scope

- Removing the fields entirely.
- A new version-validation framework.
- Starter-bundle YAML simplification (reference examples
  keep explicit values).
- Migrating existing user-authored YAMLs (no `kolsch agents
  migrate` command).
- CLI surface for version inspection.

---

## Part 7 — Related work

- **Sibling specs in the same conversation**:
  - `qol-prefix-defaults-and-unified-refs.md` — bigger
    schema-shape change; ships second.
  - `bug-model-line-placeholder-collides-with-jinja.md` —
    independent.
- **Engine capability mismatch**:
  `src/errors/engine.py` CapabilityMismatch is the
  load-bearing downstream consumer. This QoL must not
  weaken its behavior.
- **Schema reference**:
  `docs/reference/schemas.md` is the doc surface this QoL
  updates.
