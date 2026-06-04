# QoL — Unified asset-ref convention + implicit `workspace:` prefix

**Draft 0.1** *(four-step build plan. Load-bearing schema-shape change that touches every fixture, doc example, and ref in the codebase. **Blocks Step 4 of `bug-engine-extends-parent-not-rendered.md`** — the inheritance fix's docs must use the new convention.)*

> Today kolsch has three different ref conventions for the
> same conceptual operation:
>
> | Asset | Today's ref |
> |---|---|
> | Agent | `andre/agents/butler` (3-segment full-namespace) |
> | Tool | `kolsch/tools/bash` (3-segment) OR `bash` (shorthand) |
> | Skill | `kolsch/skills/X` (3-segment) OR shorthand |
> | `extends:` | `workspace:bundles/kolsch/agents/helper.yaml` (prefix + path + .yaml) |
> | `context_files[].path` | Same as `extends:` |
>
> Three formats for one concept. This QoL collapses them to
> one. AND drops the required `workspace:` prefix for the
> common case (it's still available when needed; just no
> longer required for workspace-relative refs).

---

## Part 0 — Why this matters

### 0.1 Three formats, same idea

Every author types refs in three different shapes depending
on which field they're filling. Agent refs use 3-segment
form (`bundle/agents/name`); `extends:` uses prefix+path+extension
(`workspace:bundles/X/agents/Y.yaml`); tools and skills use
3-segment shortened to plain name in some contexts. The
convention is inconsistent, the docs reflect the
inconsistency, and authors get it wrong (see this
conversation's butler.yaml debugging session).

### 0.2 The middle segment carries no information

`kolsch/tools/bash` — `tools/` tells you what asset type
this is. But the *field where the ref appears* already tells
you. `subsystems.tools.config.declared` only accepts tool
refs; the asset type is in the path. The middle segment is
visual redundancy.

### 0.3 The required `workspace:` prefix is friction for 99% of refs

`workspace:bundles/X/agents/Y.yaml` is the standard form for
in-workspace agents. `kolsch:agents/Y.yaml` is for kolsch-
package resources (and currently doesn't reach agents
anyway). The author has to type `workspace:` every time they
extend or reference a workspace file, even though that's the
overwhelmingly common case.

### 0.4 Coordination with the inheritance bug fix

`bug-engine-extends-parent-not-rendered.md` Step 4 ships
`docs/how-to/extending-an-agent.md` — a new tutorial
covering inheritance. If THIS QoL ships first, that tutorial
uses the new ref convention from day one. If it ships after,
the tutorial gets immediately updated. The cheaper sequence
is QoL-first.

---

## Part 1 — Decisions locked

From the agent-YAML-design conversation:

### Naming rules
1. **Asset name**: `^[a-z0-9][a-z0-9_-]*$` — lowercase
   alphanumeric + `_-`, must start with letter/digit.
   Consistent with current bundle-name rules. No spaces, no
   uppercase, no `.`, no `/`.

### Ref convention
2. **Within-bundle ref**: `<name>` (just the asset name).
3. **Cross-bundle ref**: `<bundle>/<name>` (two segments).
4. **Asset type is inferred from where the ref appears**:
   - `extends:` value → agent.
   - `subsystems.tools.config.declared[*].ref` → tool.
   - `subsystems.skills.config.enabled[*].ref` → skill.
5. **No file extension in refs** (no `.yaml`, no `.py`).
   Refs are identifiers, not paths.

### Prefix convention
6. **`workspace:` is the default; can be omitted** for any
   ref that points at a workspace-bundle asset. Plain
   `<bundle>/<name>` or `<name>` is workspace-rooted by
   convention.
7. **`kolsch:` remains REQUIRED** when (and only when)
   pointing at a kolsch-package resource. The parser
   disambiguates by looking for `:` — `kolsch:foo` is the
   prefixed form; `kolsch/foo` is the bundle form.
8. **`context_files[].path` keeps the prefix system** because
   it points at files (potentially outside the catalog), not
   catalog-registered assets. But plain paths default to
   `workspace:` here too.

### Catalog discipline
9. **The catalog enforces name uniqueness PER BUNDLE across
   asset types.** A bundle cannot contain a tool named
   `butler` and an agent named `butler`. Today the asset
   type segment would have prevented collision; under the
   new convention, the catalog enforces it directly.

### Migration
10. **Old refs still parse during a migration window.**
    `kolsch/tools/bash` (3-segment) still resolves to the
    same tool as `kolsch/bash` (2-segment). The parser
    detects the legacy form and emits a deprecation warning
    at agent-load (Recoverable; doesn't block).
11. **Old prefix refs still parse.** `workspace:bundles/X/agents/Y.yaml`
    still resolves to the same agent as `X/Y`. Same
    deprecation-warning treatment.
12. **No automatic migration of existing user YAMLs.** Users
    can update their YAMLs at their own pace. The deprecation
    warning surfaces the issue; no forced rewrite.
13. **Starter bundle gets migrated** to the new convention in
    Step 3 so new workspaces immediately use the canonical
    form.

---

## Part 2 — Acceptance criteria

The QoL is shipped when **all** hold:

1. **`<name>` (within bundle) resolves correctly** for tools,
   skills, and agents in every field where refs appear
   (`extends:`, `subsystems.tools.config.declared`,
   `subsystems.skills.config.enabled`).

2. **`<bundle>/<name>` (cross-bundle) resolves correctly**
   for the same fields.

3. **Plain paths in `extends:` and `context_files[].path`
   default to `workspace:`**. `extends: kolsch/helper`
   resolves to the kolsch bundle's helper agent in the
   workspace.

4. **`kolsch:<path>` still resolves to the installed kolsch
   package** for `context_files[].path` (its primary use
   case today). For `extends:`, the prefix is accepted but
   currently has no valid targets (until canonical agents
   ship as package resources — separate ticket).

5. **The legacy 3-segment form still parses with a
   deprecation warning** during the migration window. e.g.,
   `kolsch/tools/bash` still resolves; the agent-load emits
   `agent.template_warning` (or similar Recoverable) naming
   the deprecated form.

6. **The legacy `workspace:` prefix on `extends:` and
   `context_files[].path` still parses with a similar
   warning.**

7. **The catalog rejects name-collision within a bundle**
   across asset types. A bundle with both `agents/butler.yaml`
   and `tools/butler.py` raises at catalog scan time with a
   clear error.

8. **Starter bundle migrated to new convention.**
   `src/cli/_starter_bundle/kolsch/agents/helper.yaml` uses
   2-segment refs in `subsystems.tools.config.declared` and
   no `workspace:` prefix in `context_files[].path` (still
   uses `kolsch:` for package resources).

9. **Documentation surfaces the new convention.**
   `docs/reference/schemas.md` (prefix table + ref examples),
   `docs/how-to/agent-templating.md` (every example), and
   any new how-to (e.g., `extending-an-agent.md` from the
   inheritance fix) use the new form.

10. **Regression tests cover all four major paths**:
    new-form-within-bundle, new-form-cross-bundle,
    legacy-form-still-parses, name-collision-rejected.

### Out of scope

- **Shipping canonical kolsch agents as package resources**
  so `kolsch:helper` becomes a valid `extends:` target.
  Separate ticket (the same one referenced by the
  inheritance fix).
- **Refactoring the ref grammar to support glob patterns**
  (`kolsch/tools/*`). Future scope.
- **Removing the legacy form entirely.** Stays as warning-
  only for at least one minor version.
- **Refactoring the asset catalog's internal data model.**
  Refs change shape; the catalog's internal storage can stay
  as-is.
- **Multi-prefix vocabularies** (`http://`, `git://`, etc.).
  Future scope.

---

## Part 3 — Step dependency graph

```
Step 1 (verification + parser design + migration plan) ←── STOP gate
   │
   ▼
Step 2 (parser changes: new form + legacy fallback)
   │
   ▼
Step 3 (starter-bundle + doc + fixture migration)
   │
   ▼
Step 4 (manual e2e + handoff + dependency-handoff to inheritance fix)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Verification writeup + parser-grammar pin + migration plan | ~40 min |
| **2** | refs.py + context_files.py parser updates + tests | ~60 min |
| **3** | Starter bundle, docs, test fixtures migrated | ~45 min |
| **4** | Manual e2e + handoff + sequencing note to inheritance fix | ~25 min |

---

## Step 1 — Verification + parser design + migration plan

### Developer prompt

```
[standing instructions]

Step 1 — Pin the parser grammar + migration plan before
code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-prefix-defaults-and-unified-refs.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/refs.py (the current parser).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/catalog.py (where uniqueness is enforced today).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/templating/context_files.py (prefix resolution).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/schemas/config.py:178-194 (the extends prefix validator).
- bundles/kolsch/agents/helper.yaml + every fixture under tests/fixtures/extends/ (Step 3 of bug-engine-extends-parent-not-rendered.md if landed).

Verification tasks:

1. Enumerate every code path that parses refs.
     grep -rn "parse_ref\|resolve_shorthand\|_validate_extends_prefix" src/ --include="*.py"
   For each: document the inputs accepted today vs. inputs
   the new convention demands.

2. Parser grammar — pin precisely.
   - The new parser accepts the following forms (in
     precedence order — most specific first):
     1. `<prefix>:<path>` where prefix ∈ {kolsch, workspace}
        AND `<path>` does NOT match a bundle-asset shape.
        → resolved via the prefix-rooted file lookup
        (existing context_files mechanism).
     2. `<bundle>/<name>` — two segments, both matching
        the name regex. → catalog ref; asset type inferred
        by field.
     3. `<bundle>/<asset_type>/<name>` — three segments,
        legacy form. → catalog ref; asset type matches the
        middle segment OR raises if not. Emit deprecation
        warning.
     4. `<name>` — one segment. → catalog shorthand
        (existing behavior); asset type inferred by field.
   - Decide: when a ref looks like `workspace:foo`, is
     `foo` resolved as a file path under workspace_root,
     or as a catalog ref `foo`?
   - Recommendation: prefixed forms are ALWAYS file-path
     style (existing behavior). Plain paths default to
     workspace-prefix-style for `context_files[].path` and
     `extends:`. The non-prefixed catalog forms
     (`<bundle>/<name>` and `<name>`) are catalog refs.
   - This means `extends: workspace:bundles/kolsch/agents/helper.yaml`
     is a FILE path; `extends: kolsch/helper` is a CATALOG
     ref. Both resolve to the same target via different
     mechanisms. Acceptable per spec acceptance criterion 5.

3. Migration deprecation surface.
   - Pick the event type for the deprecation warning.
     Options:
     a. Reuse `agent.template_warning` Recoverable (existing).
     b. New event `config.deprecated_ref_form` Recoverable.
   - Recommend (b) — more specific, easier to find in JSONL
     for users diagnosing their own warnings.

4. Catalog uniqueness rule.
   - The new convention requires per-bundle name uniqueness
     across asset types. Confirm src/assets/catalog.py's
     scan loop CAN enforce this:
     - During Pass 4 (build asset entries), check if a
       newly-added entry's `<bundle>/<name>` (without
       asset_type) collides with an existing entry.
     - Raise a clear error at catalog scan time, NOT at
       agent-load time.

5. Fixture migration impact.
   - Count fixtures that need updating:
     - tests/fixtures/extends/ (24 fixtures from
       bug-engine-extends-parent-not-rendered.md Step 3 —
       if landed; if not, factor in the prep).
     - tests/component/, tests/integration/ — search for
       hardcoded refs in test setup.
     - Test catalog entries with old-form refs.
   - Confirm Step 3's scope is bounded.

6. Doc migration impact.
   - Count docs files using old form:
     grep -rn "workspace:\|kolsch:\|tools/\|agents/\|skills/" docs/ --include="*.md"
   - Distinguish prefixed (`workspace:bundles/...`) and
     bare 3-segment (`kolsch/tools/bash`).
   - Confirm Step 3's scope.

7. Coordination with bug-engine-extends-parent-not-rendered.md.
   - That spec's Step 4 (docs) writes a NEW how-to
     extending-an-agent.md. If THIS QoL ships first, that
     how-to uses the new convention from day one.
   - Decide sequencing: this QoL must ship BEFORE that
     bug's Step 4. The reverse order causes immediate
     rewrites. Confirm coordination in the writeup.

8. STOP and post the writeup.

ESCALATION PATH: if:
 (a) The parser can't disambiguate `kolsch:foo`
     (file-prefix) from `kolsch/foo` (catalog ref) without
     ambiguity in some edge case the writeup didn't cover,
 (b) The catalog uniqueness check has a hidden interaction
     with the existing asset_type segment-based dedup,
 (c) Step 6 reveals docs with old-form refs we don't own
     (e.g., user-written tutorials in their workspace),
escalate before proceeding.

Acceptance: writeup posted with the full parser grammar
table, the migration plan, and the coordination note for
the inheritance fix; coordinator approves.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. Writeup is the deliverable.

Checks:

1. The grammar table lists all four input forms with
   precedence order + resolution mechanism for each.

2. The kolsch:foo (file-prefix) vs. kolsch/foo (catalog)
   disambiguation rule is named and unambiguous.

3. The new deprecation event type is decided (recommended
   new code config.deprecated_ref_form).

4. Catalog uniqueness enforcement plan is concrete — names
   the exact pass + check site.

5. Fixture migration count is numeric and bounded.

6. Doc migration count is numeric and bounded.

7. Sequencing coordination with
   bug-engine-extends-parent-not-rendered.md is explicit:
   this QoL ships first; that bug's Step 4 follows.

8. All three escalation conditions are confirmed
   not-an-issue or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Parser changes + tests

### Developer prompt

```
[standing instructions]

Step 2 — Implement the parser per Step 1's grammar pin.

Read first:
- Step 1's writeup.
- src/assets/refs.py and src/templating/context_files.py.

Tasks (in this order):

1. Update src/assets/refs.py.
   - Refactor parse_ref to handle:
     a. 1-segment: shorthand. Reuse existing resolve_shorthand
        logic with asset_type from the field context.
     b. 2-segment: <bundle>/<name>. New form. Resolve to
        catalog entry; asset type from field context.
     c. 3-segment: <bundle>/<asset_type>/<name>. Legacy
        form. Emit deprecation event; resolve as today.
   - Add a `field_asset_type` parameter to parse_ref so
     callers can disambiguate. Default to None (current
     behavior: 3-segment required).
   - All existing callers updated to pass the right
     asset_type from their field context.

2. Update src/templating/context_files.py.
   - Plain (non-prefixed) paths default to workspace:
     interpretation. Current behavior raises
     ConfigContextFileUnprefixed; new behavior treats
     "bundles/X/docs/notes.md" as
     "workspace:bundles/X/docs/notes.md".
   - kolsch: still resolves as today.
   - Emit the deprecation event when an explicit
     workspace: prefix is used (the new default is
     implicit).

3. Update src/schemas/config.py:_validate_extends_prefix.
   - Today: requires a known prefix (kolsch: or workspace:).
   - New: accepts the four input forms from Step 1's
     grammar. The validator becomes lenient at the schema
     layer; the resolver does the heavy lifting.

4. Update src/assets/catalog.py.
   - Add the per-bundle name-uniqueness check at the
     pass-4 site Step 1 picked.
   - Raise a clear error (use an existing or new
     AssetsDuplicateName class) if two assets in the same
     bundle have the same name.

5. Add the deprecation event class.
   - src/errors/kernel.py or schemas/events.py per Step 1's
     pick.
   - Recoverable.

6. Unit tests:
   - tests/unit/test_refs_grammar.py:
     * test_parses_1_segment: "bash" with field_asset_type="tool" → catalog ref.
     * test_parses_2_segment: "kolsch/bash" with field_asset_type="tool" → catalog ref.
     * test_parses_3_segment_legacy: "kolsch/tools/bash" → catalog ref + deprecation event.
     * test_rejects_4_segment: too many slashes → InvalidRef.
     * test_rejects_invalid_name_chars: uppercase / spaces / dots → InvalidRef.
   - tests/unit/test_context_files_implicit_workspace.py:
     * Plain "bundles/X/notes.md" resolves to workspace
       path.
     * Explicit "workspace:bundles/X/notes.md" resolves
       to same target + deprecation event.
     * Explicit "kolsch:docs/index.md" resolves to package
       path (unchanged).
   - tests/unit/test_catalog_uniqueness.py:
     * Bundle with agents/X.yaml and tools/X.py raises
       AssetsDuplicateName at scan time.

7. Integration test:
   - tests/integration/test_extends_new_form.py:
     * Author a tmp workspace; child agent uses
       extends: kolsch/helper. Asserts resolves correctly.
     * Same with extends: workspace:bundles/kolsch/agents/helper.yaml.
       Asserts resolves + emits deprecation event.

8. NO change to:
   - The engine's render path (inheritance fix's
     responsibility).
   - The starter bundle (Step 3).
   - The fixture corpus from inheritance fix (Step 3).

Scope: ~150 LOC refs.py + ~50 LOC catalog.py +
~30 LOC context_files.py + ~30 LOC schema validator +
~250 LOC tests.

ESCALATION PATH: if implementing the
field_asset_type parameter requires touching every caller
of parse_ref (suggesting a wider refactor), document and
proceed — that's expected scope.

Acceptance: full test suite green; new tests pass; existing
tests pinning the old form either continue passing (via
legacy fallback) or are updated with deprecation
documentation.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. parse_ref handles all four input forms per Step 1's
   grammar. Spot-check by reading the implementation.

2. field_asset_type parameter is threaded through all
   callers. grep for parse_ref usage; every call site has
   the parameter.

3. context_files.py defaults plain paths to workspace:
   interpretation. Verify by reading.

4. The schema validator for extends: is lenient (accepts
   all four forms). Verify.

5. Catalog uniqueness check runs at pass 4. Verify by
   reading.

6. The deprecation event class exists; Recoverable.

7. The seven unit-test groups exist and pass.

8. The integration test for new + legacy forms passes.

9. NO change to engine render path, starter bundle, or
   fixture corpus.

10. Full test suite green. tc_eng_* goldens still
    byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Migrate starter bundle + docs + fixtures

### Developer prompt

```
[standing instructions]

Step 3 — Migrate the canonical examples to the new
convention.

Read first:
- Step 2's PR.
- src/cli/_starter_bundle/kolsch/ (every file).
- docs/ (every file using old-form refs).
- tests/fixtures/ (every fixture using old-form refs).

Tasks:

1. Migrate src/cli/_starter_bundle/kolsch/agents/helper.yaml:
   - subsystems.tools.config.declared entries: change
     `kolsch/tools/bash` to `kolsch/bash` (2-segment form).
   - context_files[].path entries: kolsch: stays for
     package resources (kolsch:docs/index.md,
     kolsch:AGENTS.md); plain workspace: paths drop the
     prefix.

2. Migrate user's workspace copy.
   - ~/Documents/kolsch/bundles/kolsch/agents/helper.yaml
     same way (this is the SAME file content as the source
     after the inheritance-fix conversation work).

3. Migrate test fixtures.
   - tests/fixtures/extends/ (if from bug-engine-extends-parent-not-rendered.md Step 3 landed):
     Walk each fixture; replace 3-segment refs with
     2-segment + drop workspace: prefix where applicable.
     The fixtures' YAMLs should look like the eventual
     canonical examples in the new docs.
   - Other fixture directories: same pass.

4. Migrate docs.
   - docs/how-to/agent-templating.md: every example.
   - docs/how-to/permissions.md, configure-fallbacks.md,
     etc. — anywhere refs appear.
   - docs/reference/schemas.md prefix table: update to
     show plain paths default to workspace.
   - docs/reference/module-api-reference.md: any examples.

5. Add a "Migrating from the legacy ref form" subsection
   to docs/reference/schemas.md:
   - Show the before/after table.
   - Name the deprecation event so users can find their
     own warnings.
   - Note no forced migration; warnings only.

6. Run a grep audit:
     grep -rn "kolsch/tools\|kolsch/skills\|kolsch/agents\|workspace:bundles" src/ tests/ docs/
   Should return ZERO unmarked hits after this step. Any
   remaining hit is either:
   - An explicit "legacy form (deprecated)" example in
     migration docs (acceptable; comment must explicitly
     mark it).
   - An oversight (fix).

7. Test catalog entries — update any TC that lists
   old-form refs.

8. NO production code changes (Step 2 covered those).

Scope: many small edits across ~30-50 files. The total
LOC churn is high but each individual change is mechanical.

ESCALATION PATH: if the grep audit finds old-form refs in
src/ outside the starter bundle (suggesting hardcoded
strings in non-test code paths), escalate. Those may have
behavioral implications.

Acceptance: grep audit clean; full test suite green; docs
read with the new convention consistently.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. Run `grep -rn "kolsch/tools\|kolsch/skills\|kolsch/agents\|workspace:bundles" src/ tests/ docs/`
   Returns either zero hits OR only hits in an explicitly
   marked "legacy form" migration section.

2. Starter bundle helper.yaml uses 2-segment tool refs.

3. User's workspace helper.yaml matches the starter (same
   content).

4. Fixture migration complete. Spot-check three fixtures
   from different categories; confirm new convention.

5. Doc migration complete. Spot-check three doc files;
   confirm new convention.

6. A "Migrating from the legacy ref form" subsection
   exists in schemas.md with the before/after table.

7. Test catalog updated.

8. NO production code changes.

9. Full test suite green.

Verdict: PASS or FAIL.
```

---

## Step 4 — Manual e2e + handoff to inheritance fix

### Developer prompt

```
[standing instructions]

Step 4 — Final closure + signal to the inheritance fix
that its Step 4 can proceed.

Read first:
- Steps 1-3 PRs.

Tasks:

1. Manual e2e — new form end-to-end.
   - Author a fresh agent in the workspace using:
     - 2-segment tool refs.
     - Plain (no workspace:) extends ref.
   - Run kolsch chat against it.
   - Capture JSONL excerpt showing the resolved config
     with all refs resolved correctly.

2. Manual e2e — legacy form still works.
   - Author a parallel fresh agent using:
     - 3-segment tool refs.
     - workspace: prefixed extends.
   - Run kolsch chat against it.
   - Verify session JSONL shows the deprecation events
     (config.deprecated_ref_form Recoverable) AND the
     agent still works.

3. Signal to bug-engine-extends-parent-not-rendered.md.
   - The inheritance fix's Step 4 (docs) is blocked on
     THIS QoL. Open a PR comment / coordinator log entry
     stating that this QoL's Step 4 closure unblocks the
     inheritance fix's Step 4.
   - The inheritance fix's docs MUST be written using the
     new convention.

4. Closure writeup at
   process/qol-prefix-defaults-and-unified-refs-step4-closure.md:
   - Both e2e excerpts.
   - Migration impact stats (number of files migrated,
     number of refs updated).
   - Confirmation that the inheritance fix's Step 4 is
     unblocked.

5. Hand off to the user. The PR description must ask the
   user to confirm:
   - A fresh chat session against a workspace agent using
     new-form refs works cleanly.
   - The deprecation warning surfaces when using legacy
     form.

Scope: manual report + closure writeup. No new production
code.

ESCALATION PATH: if either e2e shows behavior that doesn't
match Step 1's grammar, Step 2 missed a case. Escalate.

Acceptance: both e2e scenarios pass; inheritance fix Step 4
unblocked; user is asked to confirm.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4.

Checks:

1. Both manual e2e scenarios are captured in the PR
   description with JSONL excerpts.

2. The new-form scenario shows the resolved config has all
   refs resolved correctly (no deprecation events emitted).

3. The legacy-form scenario shows BOTH the deprecation
   events AND the agent working. If the agent failed under
   legacy form, the migration discipline is broken — fail.

4. The closure writeup names the inheritance fix's Step 4
   as unblocked.

5. The user-confirmation request is in the PR description.
   Verdict stays REVISE-PENDING-USER until user confirms.

6. Full test suite green.

Verdict: PASS (only after user confirms), REVISE-PENDING-USER,
or FAIL.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-QOL-REFS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–4
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-REFS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds a parser ambiguity Step 1's grammar table
  didn't address.
- Step 1 finds the catalog uniqueness check has hidden
  interactions with existing dedup logic.
- Step 2 finds parse_ref callers outside the field-context
  pattern (e.g., a parser used in an unrelated module).
- Step 3 finds old-form refs in production code paths
  outside the starter bundle.
- Step 4 e2e shows either form failing.

When NOT to escalate:
- Doc wording.
- The exact format of the deprecation event's message.
- Whether the migration subsection uses a table or a list.

---

## Part 5 — Coordinator Escalation Log

*(empty)*

---

## Part 6 — Out of scope

- Shipping canonical kolsch agents as package resources.
- Glob patterns in refs.
- Removing the legacy form entirely.
- Refactoring the catalog's internal data model.
- Multi-prefix vocabularies (http://, git://, etc.).
- Auto-migrating user YAMLs.

---

## Part 7 — Related work

- **Blocks**: `bug-engine-extends-parent-not-rendered.md`
  Step 4 (docs). That step's `extending-an-agent.md`
  tutorial must use the new convention from day one.
- **Sibling specs in same conversation**:
  - `qol-yaml-version-fields-optional.md` — ships first;
    independent.
  - `bug-model-line-placeholder-collides-with-jinja.md` —
    independent.
- **Catalog implementation**: `src/assets/catalog.py` is
  the central enforcement point for both the existing
  bundle-name uniqueness rules and the new
  per-bundle-name-across-asset-types rule.
- **Future**: a `kolsch agents/tools/skills migrate` CLI
  that auto-rewrites legacy refs in user YAMLs. Not in
  this scope; could ship later as a convenience.
