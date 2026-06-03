# Bug / QoL — Make `agent_id` optional in agent YAML; derive from bundle/name when omitted

**Draft 0.1** *(three dependency-ordered steps with focused dev + reviewer prompts. Smaller in scope than the framework / chat specs — Step 2's implementation is ~30 LOC.)*

> Reported symptom: a user authored a new agent in a new
> bundle (`~/Documents/kolsch/bundles/andre/agents/butler.yaml`)
> with `agent_id: butler`. The CLI failed at config validation
> with:
>
> ```
> AgentId requires prefix 'ag_'; got 'butler'
> ```
>
> Fixing the field to `agent_id: ag_butler` resolved it. The
> user pushed back on the design: the `<bundle>/<name>` tuple
> (here `andre/butler`) already uniquely identifies the agent,
> so the schema requiring a third user-typed-and-prefixed
> field is unnecessary friction.
>
> Per the conversation: **the right shape is `agent_id`
> becomes optional in the YAML; when omitted, the resolver
> derives it from the asset ref.** The field stays in the
> resolved config (and in events, authz principals, memory
> bindings, etc.) — just stops being a thing the user has to
> type or know about.

---

## Part 0 — Why this matters

Three problems compound:

1. **The `ag_` prefix is a structural detail the user shouldn't
   own.** It exists for good reasons — see
   `src/schemas/identifiers.py:1-21` — as a type tag in event
   logs (`ses_`, `th_`, `ev_`, `ag_`) and a way to keep agent
   identity decoupled from file location. None of those reasons
   require the user to type the prefix. Today, missing or
   misformatted prefix raises a Layer-1 validation error with a
   message most users decode through trial-and-error.

2. **There are now three names for one agent.** A new bundle
   author types the bundle name (`andre`), the agent name
   (`butler` in the filename + `name: butler` in YAML), AND an
   `agent_id` they have to invent and prefix
   (`ag_butler`). Two of those are redundant — the ref
   `andre/agents/butler` is already canonical. The `agent_id`
   adds one more identifier to keep in sync.

3. **The papercut compounds with the bad error surface.** The
   underlying validation error (`AgentId requires prefix
   'ag_'`) bubbles up through `_cmd_run` as
   `errors.kernel.ConfigInvalid: Configuration validation
   failed with 1 error(s)` — the actual per-field message
   sits in `detail["validation_errors"]` which the CLI does
   not print. So the user sees a generic failure and has to
   either read the traceback or guess. (That CLI-error
   surfacing gap is **out of scope here** — its own ticket —
   but it's the reason this papercut is more painful than it
   would be otherwise.)

**Design decision (made before this doc):** make `agent_id`
optional in the YAML. On omission, the resolver derives
`ag_<bundle>_<name>` and surfaces it in the resolved config.
When provided, honor the user's value (must still be a
canonical `ag_*` form — we are not also building a
"prepend-prefix" leniency layer). The starter bundle's
`kolsch/agents/helper.yaml` keeps its explicit `agent_id:
ag_helper` (no migration; no breakage of existing event logs
or authz rules).

---

## Part 1 — What we already know

This is not a hypothesis-search bug — the cause is confirmed
and the fix is shaped. Step 1 is a small verification pass
before changing the schema.

### Known facts (with citations)

- `AgentId` lives at `src/schemas/identifiers.py:132-149`.
  Prefix `ag_`; body must match
  `^[a-zA-Z0-9_\-.]+$`. The validator runs on construction
  and via Pydantic's `__get_pydantic_core_schema__`.
- The resolved-config schema requires it:
  `src/schemas/config.py:164` declares
  `agent_id: AgentId` on `ResolvedAgentConfig` (no default).
- `agent_id` is used as the **stable identity** across the
  system:
  - Authz principal: `f"agent.{config.agent_id}"`
    (`src/cli/_slash.py:498`) and
    `f"agent.execute.{self.agent_id}"`
    (`src/engine/engine.py:392`).
  - Event actor: `Actor(kind="agent", id=self.agent_id)`
    everywhere events are emitted (multiple sites in
    `src/engine/engine.py`).
  - Session/thread bindings: `create_session(initial_agent_id)`,
    memory `bind_session(agent_id=...)`.
  - Template var: `AgentVar.id` projects `config.agent_id`
    (`src/templating/vars/agent.py:6`).
  - Tool/skill registration: `agent_id` is included in
    error payloads (`tool.not_found` etc.).
- The catalog already knows the bundle and the asset name at
  resolution time — `src/assets/refs.py:23-36` parses
  `<bundle>/<asset-type>/<name>` and the catalog walker
  (`src/assets/catalog.py:62-129`) tracks both. **The
  derivation input is already in scope at the resolver
  layer.**
- The starter bundle uses `agent_id: ag_helper` explicitly
  (`src/cli/_starter_bundle/kolsch/agents/helper.yaml`). The
  fallback agent ref `_DEFAULT_AGENT_REF = "kolsch/agents/helper"`
  (`src/cli/chat.py:96`) does NOT depend on the agent_id
  shape; it's a ref string. So a derived id for helper would
  be `ag_kolsch_helper` — DIFFERENT from `ag_helper`. **We
  must not migrate helper.yaml to omission**, because that
  would change the id and break every historical session log,
  authz rule, and memory binding that pins `ag_helper`.

### What Step 1 confirms before code lands

- **Where exactly the derivation hooks into the resolver.**
  The kernel pipeline is at
  `src/kernel/pipeline.py`; the resolver is at
  `src/kernel/resolver.py`. The derivation must happen AFTER
  partials are merged but BEFORE Layer-2 `ResolvedAgentConfig`
  validation runs (otherwise the missing `agent_id` fails
  validation). Step 1 names the exact callsite + the function
  that owns it.

- **Where the bundle name is available at that callsite.**
  Resolution starts from an asset ref
  (`andre/agents/butler`); the resolver must thread the
  bundle name through to the derivation hook. Step 1 confirms
  the bundle name is already in scope OR identifies the
  minimal plumbing to thread it through.

- **The derivation format.** Recommendation:
  `ag_<bundle>_<name>` (e.g. `ag_andre_butler`). Pros:
  deterministic, collision-free across bundles, eyeball-
  legible. Cons: changing the bundle name OR the agent name
  changes the id and breaks references — same surface as
  any "id derived from location" tradeoff. The opt-in
  explicit `agent_id` is the escape hatch for users who care
  about stability across renames.

- **Validation: derived ids must satisfy `AgentId`'s body
  regex.** The body regex
  (`^[a-zA-Z0-9_\-.]+$`) accepts letters, digits, `_`, `-`,
  `.`. Bundle names are validated lowercase-alphanumeric +
  `-`/`_` (per `src/assets/catalog.py:89-95`; see
  `is_valid_bundle_name`). Asset file stems land in the agent
  `name` field. Step 1 confirms that
  `f"ag_{bundle}_{name}"` always satisfies the regex given the
  upstream validations on bundle and asset-name shape.

- **No collision between explicit and derived ids.** Two
  agents in two bundles could both end up with the same
  derived id only if they share both bundle name and asset
  name — which is impossible because the catalog enforces
  unique refs (`src/assets/catalog.py:106-120`). Step 1 just
  confirms this by reading.

- **Does any test pin `agent_id: ag_<value>` literally?**
  Per Step 1's grep, identify tests that construct or assert
  on specific agent_id values. They keep working IF they
  explicitly set agent_id; only tests that asserted "agent_id
  is required" need updating.

- **Does the YAML editor / templating reference `agent_id` as
  a template variable in any starter bundle file?** The
  templating var `AgentVar.id` works whether the id was user-
  typed or derived. Step 1 just confirms by tracing.

- **Should we ALSO accept a no-prefix `agent_id` (e.g. `agent_id:
  butler`) and prepend `ag_` for the user?** That's a
  separate "be more lenient with explicit values" change.
  **OUT OF SCOPE per the design decision.** If `agent_id` is
  provided, it must be in canonical `ag_*` form. The
  papercut is solved by letting users omit the field
  entirely.

---

## Part 2 — Acceptance criteria (applies across all steps)

The bug is fixed when **all** hold:

1. **`agent_id` is optional in the agent YAML schema.**
   Layer-1 validation accepts an agent YAML that does not
   include `agent_id`.

2. **The resolver derives a canonical `agent_id` when
   omitted.** Format: `ag_<bundle>_<name>` (matching Step 1's
   pick if different). The derived value satisfies the
   `AgentId` body regex.

3. **Explicit `agent_id` still works and is honored.** If the
   YAML specifies `agent_id: ag_butler`, the resolved config
   uses that exact value. The derivation does NOT
   override.

4. **Malformed explicit `agent_id` still fails.** If the YAML
   specifies `agent_id: butler` (no prefix) or any other
   non-canonical form, validation fails with the existing
   `AgentId` error message. The fix lets users omit the field
   — it does NOT make malformed values valid.

5. **`ResolvedAgentConfig` still has a required, non-None
   `agent_id: AgentId`.** The change is the user-facing input
   shape (optional), not the post-resolution shape (always
   present, always valid).

6. **No migration for `kolsch/agents/helper.yaml`.** The
   starter bundle keeps `agent_id: ag_helper` explicitly.
   This protects existing event logs, authz rules, and memory
   bindings that pin `ag_helper`. Changing it would derive
   `ag_kolsch_helper` and break historical state.

7. **Authoring docs surface the new shape.** At minimum:
   - `docs/how-to/` gains an "authoring an agent" page (or
     extends an existing one) that shows a minimal YAML
     without `agent_id`.
   - `docs/reference/schemas.md` notes that `agent_id` is
     optional with the derivation rule.
   - `docs/reference/module-api-reference.md` notes the
     derivation if it documents the agent schema.

8. **Regression tests cover all paths:**
   - YAML without `agent_id` resolves; resolved
     config has the derived id.
   - YAML with explicit `agent_id: ag_x` resolves; resolved
     config has `ag_x` (NOT the derived form).
   - YAML with explicit `agent_id: x` (malformed) fails with
     the existing AgentId error.
   - Manual: `kolsch chat andre/agents/butler` with no
     `agent_id` in butler.yaml succeeds.

9. **No regressions in:**
   - `tests/golden/tc_eng_*` (all explicit agent_ids in
     test fixtures are untouched; goldens stay byte-equal).
   - Existing component / unit tests against the agent
     schema.

### Explicitly out of scope for this bug

- **A "prepend `ag_` to malformed explicit values" leniency
  layer.** If the user provides `agent_id: butler`, it still
  fails. The fix is "omit the field"; explicit values stay
  canonical.
- **CLI surfacing of `ConfigInvalid.detail.validation_errors`.**
  That's a different papercut (the actual per-field message
  gets swallowed today). Tracked as its own follow-up; this
  fix narrows the surface where users hit it but doesn't
  improve the surface itself.
- **A how-to document for authoring agents end-to-end.**
  Part 2 #7 requires AT LEAST a mention of the new shape in
  existing docs. A full "authoring an agent" how-to is a
  bigger doc effort and lives in its own ticket.
- **Migrating `kolsch/agents/helper.yaml`** to omit
  `agent_id`. Explicitly required NOT to migrate (see
  #6 above).
- **Renaming `agent_id` to something else** (e.g. just
  `id`). The field name stays; only the shape becomes
  optional.
- **Adding the `bundle` field to the schema.** The bundle is
  catalog-derived from the asset's location; the user
  doesn't put bundle inside the YAML. (Verify in Step 1.)

---

## Part 3 — Step dependency graph

```
Step 1 (verification + derivation-callsite pick) ←── STOP gate
    │
    ▼
Step 2 (schema change + resolver derivation + doc updates)
    │
    ▼
Step 3 (regression tests + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming the derivation callsite, the derived format, and the test-update list | ~15 min |
| **2** | Optional field + resolver derivation + doc lines | ~25 min |
| **3** | Regression tests + manual e2e + follow-up handoff | ~15 min |

Each step ships as its own PR. Step 1 is the gate; Steps 2-3
are mechanical once Step 1 pins the callsite.

---

## Step 1 — Verification + derivation-callsite pick

- **Builds:** a writeup that names (a) the schema-layer change
  shape (optional field on the partial config), (b) the exact
  callsite in the resolver where derivation runs, (c) the
  derivation format, (d) the docs to touch, and (e) the test
  files to update.
- **No code changes.** Pure read-only verification.
- **Acceptance:** the writeup is posted to the coordinator with
  file:line citations for every claim.

### Developer prompt

```
[standing instructions]

Step 1 — Verify the derivation hook before any code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-agent-id-derive-from-ref.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/schemas/identifiers.py (AgentId).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/schemas/config.py (the schema layers — partial vs resolved).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/kernel/pipeline.py and src/kernel/resolver.py.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/asset_manager.py (build_resolved_config entry).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/catalog.py (bundle / asset-name validation rules).

Verification tasks:

1. Schema layer change shape.
   - Find the Layer-1 partial-config Pydantic model that
     parses raw agent YAML (likely PartialAgentConfig or
     similar in schemas/config.py).
   - Confirm where agent_id is declared in BOTH the partial
     and the resolved shapes.
   - Recommendation: make agent_id Optional[AgentId] = None
     on the PARTIAL. Resolved stays as agent_id: AgentId
     (required). The resolver fills in the derived value
     between layers.
   - Document the exact line(s) to change.

2. Derivation callsite — pick it.
   - The derivation must run:
     * AFTER all partials are merged (so an explicit
       agent_id from a base or extending config wins).
     * BEFORE ResolvedAgentConfig.model_validate runs (so
       the required field is populated by the time
       validation runs).
   - Likely site: inside kernel.resolver.resolve, or in
     asset_manager.build_resolved_config between the merge
     and the validate.
   - Document the EXACT function + line.

3. Bundle name plumbing.
   - The derivation needs bundle + agent name. The asset
     ref (e.g. "andre/agents/butler") is the canonical source
     and is available at build_resolved_config's entry. Trace
     whether it's already in scope at the chosen callsite or
     whether one more arg needs threading through.
   - Document the plumbing path.

4. Derivation format — confirm.
   - Recommendation: f"ag_{bundle}_{name}".
   - Confirm the output ALWAYS satisfies AgentId's body
     regex (^[a-zA-Z0-9_\\-.]+$) given the upstream bundle-
     name validation and asset-name validation. Cite the
     constraints.
   - Edge cases: bundle names with hyphens (e.g.
     "customer-support") produce "ag_customer-support_<name>"
     — verify the regex accepts hyphens (it does; -
     is in the character class).

5. Collision impossibility.
   - Confirm that two agents in two different bundles cannot
     produce the same derived id. The catalog enforces
     unique refs <bundle>/agents/<name>; map this to
     uniqueness of f"ag_{bundle}_{name}". Document.

6. Test impact.
     grep -rn "agent_id:" tests/ --include="*.py" --include="*.yaml"
     grep -rn "ag_" tests/ --include="*.py"
     grep -rn "AgentId" tests/ --include="*.py"
   - List EVERY test file that references agent_id values.
   - For each: does it pin a specific id (continues to
     work unchanged) or does it assert that agent_id is
     REQUIRED in the partial (needs update)?
   - List the files needing updates.

7. Docs to touch.
     grep -rn "agent_id" docs/ --include="*.md"
   - Identify the lines in docs/reference/schemas.md and
     docs/reference/module-api-reference.md that document
     agent_id.
   - Identify the how-to files that use agent_id in
     examples (docs/how-to/agent-templating.md,
     docs/how-to/configure-fallbacks.md per Part 1).
   - Recommend: add a "derivation rule" note to the
     reference docs; leave the how-to examples alone (they're
     valid examples of explicit ids).

8. Starter bundle non-migration.
   - Confirm src/cli/_starter_bundle/kolsch/agents/helper.yaml
     still uses agent_id: ag_helper after this fix lands.
   - Confirm no test will fail if we DON'T migrate it.

9. Bundle-field-on-schema check (sanity).
   - Confirm the raw YAML does NOT take a bundle field
     today. The bundle is catalog-derived from the file's
     location. (If somehow it IS in the schema, that's a
     surprise — document and flag.)

10. STOP and post the writeup before any code lands.

ESCALATION PATH: if Step 1 reveals that:
 (a) the bundle name is NOT in scope at any callsite between
     partial-merge and resolved-validate without a deep
     refactor, or
 (b) some downstream code path REQUIRES the user-typed
     agent_id to match a specific shape that derivation
     wouldn't produce (e.g. a hardcoded "ag_helper" check
     somewhere), or
 (c) the catalog allows duplicate refs in a way that would
     produce derived-id collisions,
escalate via the Coordinator Escalation Log before proceeding.

Acceptance: writeup names every shape with rationale +
file:line citations; coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — verification writeup.

NO code to review. The writeup is the deliverable.

Checks:

1. The schema-layer change shape is named with file:line.
   "Make it optional somewhere" is a fail.

2. The derivation callsite is a specific function at a
   specific line. The writeup explains why that callsite is
   AFTER partial merge and BEFORE resolved validation. If
   the writeup picks a callsite that runs before merge
   (an explicit agent_id from an extending YAML would lose)
   or after validation (which can't run with a required
   missing field), fail.

3. The bundle-name plumbing is concrete — either "already in
   scope at X line" or "thread through Y arg." Vague is a
   fail.

4. The derivation format is named (recommendation:
   ag_<bundle>_<name>) AND the writeup proves the output
   always satisfies the AgentId body regex. Reading the
   bundle-name and asset-name validation rules upstream is
   the proof.

5. Collision impossibility is proven with a citation to
   the catalog's uniqueness invariant (Am-I2 or equivalent).

6. The test impact list is concrete: each test file with
   one of three labels:
   - "passes unchanged" (pins explicit ids only)
   - "needs update" (asserts agent_id-required)
   - "edge — investigate" (anything ambiguous)

7. Doc lines to touch are listed with file:line.

8. Starter bundle non-migration is confirmed (the writeup
   states the starter stays unchanged AND no test breaks
   from that decision).

9. Any of the three escalation conditions are either
   confirmed not-an-issue with citations or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step 2 — Schema + derivation + doc updates

- **Builds:** the partial-config field becomes optional; the
  resolver derives the id; doc lines are updated.
- **Depends on:** Step 1 approved.
- **Acceptance:** unit tests pass; resolved configs without
  YAML `agent_id` carry the derived value; explicit values
  still win.

### Developer prompt

```
[standing instructions]

Step 2 — Make agent_id optional + derive when omitted.

Read first:
- Step 1's writeup in the PR description / coordinator log.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-agent-id-derive-from-ref.md Parts 1-2.
- src/schemas/config.py, src/schemas/identifiers.py,
  src/kernel/resolver.py, src/kernel/pipeline.py,
  src/assets/asset_manager.py.

Tasks (in this order):

1. Schema — make agent_id optional on the partial.
   - In src/schemas/config.py, change the partial-config's
     agent_id field declaration to Optional / | None with
     default None. (The exact field name + class per Step 1.)
   - DO NOT change ResolvedAgentConfig. agent_id stays
     required + typed AgentId on the resolved.

2. Derivation helper.
   - In a sensible module — likely src/schemas/identifiers.py
     next to AgentId, or src/kernel/resolver.py if Step 1
     locates the derivation there — add:

       def derive_agent_id(bundle: str, name: str) -> AgentId:
           return AgentId(f"ag_{bundle}_{name}")

     Use AgentId(...) so the body-regex validation runs at
     construction. If the inputs are valid (per Step 1's
     proof), this always succeeds; if they're somehow
     invalid, the AgentId raise surfaces the bad input — a
     reasonable failure mode.

3. Resolver hook.
   - At the callsite Step 1 picked, after partial merge and
     BEFORE ResolvedAgentConfig validation:

       merged = ...  # the merged-partials dict
       if merged.get("agent_id") is None:
           merged["agent_id"] = derive_agent_id(bundle, name)

   - If the bundle name isn't in scope, thread it through
     per Step 1's plumbing plan. Keep the threading minimal
     — one additional kwarg through the relevant call.

4. NO migration of src/cli/_starter_bundle/kolsch/agents/helper.yaml.
   It keeps `agent_id: ag_helper` explicitly.

5. Doc updates.
   - docs/reference/schemas.md: at the agent_id line (Step 1
     pinned the line), add a note that the field is OPTIONAL
     in the partial YAML; when omitted the resolver derives
     `ag_<bundle>_<name>`. Keep the existing description for
     the resolved-config case.
   - docs/reference/module-api-reference.md: same — at the
     agent_id documentation, add the derivation note.
   - docs/how-to/agent-templating.md and
     docs/how-to/configure-fallbacks.md: leave the existing
     examples alone (they show explicit ids — still valid).
     Optionally add ONE sentence at the top of agent-templating
     noting that agent_id is optional in newer agents.

6. NO other doc / code changes:
   - Don't add a new how-to "authoring an agent" page —
     that's a separate doc ticket per Part 2 out-of-scope.
   - Don't add prepend-leniency for malformed explicit
     values — out of scope.
   - Don't touch event payloads, authz rules, or memory
     bindings — agent_id is still the same shape downstream.

7. Tests — small unit-test additions:
   - In tests/unit/test_config.py (or the closest existing
     schema-tests file Step 1 named), add:
     * test_partial_config_accepts_missing_agent_id —
       constructs the partial without agent_id; asserts no
       raise.
     * test_partial_config_still_accepts_explicit_agent_id —
       constructs with agent_id="ag_x"; asserts the value is
       preserved.
     * test_partial_config_rejects_malformed_explicit_agent_id —
       constructs with agent_id="butler"; asserts the existing
       AgentId raise.
     * test_derive_agent_id_format — calls the helper
       directly with ("andre", "butler"); asserts result ==
       "ag_andre_butler" and is an AgentId instance.

   - Step 3 owns the end-to-end resolver tests; this step's
     tests are unit-level on schema + helper.

Scope: ~30 LOC code (schema change + helper + resolver hook)
+ ~5 LOC doc + ~50 LOC unit tests.

ESCALATION PATH: if the schema change breaks an unexpected
test path (e.g. somewhere a partial config is constructed
positionally and the new optional field changes argument
order), escalate before adding compatibility shims. We don't
want a `# preserve old signature` workaround papering over a
real wiring issue.

Acceptance: full test suite green except the resolver e2e
tests Step 3 will add; the new unit tests pass; resolver
output for a partial-without-agent_id carries the derived
value.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — schema + derivation + doc updates.

Checks:

1. The partial-config field for agent_id is OPTIONAL with
   default None. Not required-with-Field(...). Not Optional
   with a placeholder default like "" — must be None so the
   resolver can branch on omission.

2. ResolvedAgentConfig.agent_id is UNCHANGED — still
   required, still typed AgentId. If the reviewer sees a
   change here, fail; the post-resolution shape must not
   weaken.

3. The derivation helper is a named function (not an
   inline f-string at the callsite). Tests can pin its
   behavior independently. The helper calls AgentId(...) so
   the body-regex validation runs at construction.

4. The resolver callsite matches Step 1's pick:
   - Runs AFTER partial merge.
   - Runs BEFORE ResolvedAgentConfig validation.
   - Reads merged.get("agent_id") (or equivalent) and only
     derives when None. An explicit ag_x passes through
     untouched.

5. src/cli/_starter_bundle/kolsch/agents/helper.yaml is
   UNCHANGED. Cross-check: git diff -- src/cli/_starter_bundle/.
   Any diff is a fail (the design explicitly forbids
   migration).

6. Doc updates:
   - docs/reference/schemas.md notes the field is optional
     with the derivation rule.
   - docs/reference/module-api-reference.md likewise.
   - how-to files: existing examples unchanged. Optional
     one-sentence note in agent-templating is fine.
   - NO new how-to file added.

7. Tests:
   - All four new unit tests exist and pass.
   - The malformed-explicit test confirms we still reject
     agent_id="butler" — proves the fix didn't accidentally
     turn into a leniency layer.

8. Run the FULL test suite. Specifically verify:
   - tc_eng_* goldens still byte-equal (test fixtures use
     explicit ids; no resolved-config shape change).
   - No existing test asserts "agent_id is required in YAML"
     in a way that now fails — if it does, Step 1 missed it
     and the test list needs updating.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 3 — Regression tests + manual e2e + handoff

- **Builds:** end-to-end resolver tests + manual verification
  with a real bundle/agent.
- **Depends on:** Step 2 in main.
- **Acceptance:** new tests pass; manual run of the butler
  case with no `agent_id` in YAML succeeds.

### Developer prompt

```
[standing instructions]

Step 3 — Regression tests + manual end-to-end.

Read first:
- Step 2's PR.
- The closest existing resolver-level test file — Step 1's
  writeup pinned it (likely tests/integration/test_resolver.py
  or tests/component/test_asset_manager.py).
- process/test-catalog.md.

Tasks:

1. Add tests/integration/test_agent_id_derivation.py (or
   extend the closest existing file):

   TC-AGENTID-01 — YAML without agent_id resolves with derived id.
     def test_partial_without_agent_id_derives():
         """A new agent in a new bundle without agent_id:
         field resolves to ag_<bundle>_<name>."""
         # Setup: tmp workspace with bundle "test"; agent
         # "demo.yaml" missing agent_id; other required
         # fields present.
         # Action: runtime.build_resolved_config("test/agents/demo").
         # Expected:
         #   - returns ResolvedAgentConfig with
         #     agent_id == "ag_test_demo".
         #   - other fields parse normally.

   TC-AGENTID-02 — Explicit agent_id is honored over derivation.
     def test_explicit_agent_id_wins():
         """When the YAML provides agent_id: ag_custom, the
         resolved config uses ag_custom — NOT the derived
         ag_<bundle>_<name>."""
         # Setup: tmp workspace; agent YAML with
         # `agent_id: ag_custom`.
         # Expected:
         #   - resolved config.agent_id == "ag_custom".

   TC-AGENTID-03 — Malformed explicit agent_id still fails.
     def test_malformed_explicit_agent_id_rejected():
         """agent_id: butler (no prefix) still raises
         ConfigInvalid with the AgentId prefix message."""
         # Setup: agent YAML with `agent_id: butler`.
         # Action: build_resolved_config(...).
         # Expected:
         #   - raises ConfigInvalid with detail.validation_errors
         #     containing the "AgentId requires prefix 'ag_'"
         #     message.

   TC-AGENTID-04 — Starter helper still resolves to ag_helper.
     def test_starter_helper_keeps_explicit_id():
         """kolsch/agents/helper resolves to ag_helper, NOT
         ag_kolsch_helper — proving non-migration of the
         starter."""
         # Setup: workspace with the starter bundle installed.
         # Action: build_resolved_config("kolsch/agents/helper").
         # Expected:
         #   - resolved config.agent_id == "ag_helper".

2. Add the four TCs to process/test-catalog.md under a new
   "Agent ID Derivation (AGENTID)" section.

3. Pre-fix verification:
   - On a worktree at pre-Step-2 code, run TC-AGENTID-01.
     Confirm it FAILS with the existing ConfigInvalid
     ("AgentId requires prefix" — actually for missing field
     the message will be "Field required" — capture whatever
     the pre-fix failure is and document).
   - Document the failure output in the PR description.

4. Manual end-to-end:
   a. Use a fresh or existing workspace at ~/Documents/kolsch.
   b. Edit ~/Documents/kolsch/bundles/andre/agents/butler.yaml
      to REMOVE the agent_id line. Save.
   c. Run:
        uv run --project /Users/andrecohen/Desktop/SV/Projects/kolsch \\
          kolsch run andre/agents/butler --input content="hello"
      (or `kolsch chat andre/agents/butler` if preferred).
   d. Verify:
      * No ConfigInvalid raise.
      * Resolved config (via a brief Python probe — see
        Step 2's reviewer notes for the pattern) shows
        agent_id == "ag_andre_butler".
      * The agent runs at least one turn against the
        configured provider.

5. Manual e2e for the explicit case:
   a. Re-add `agent_id: ag_butler` to butler.yaml.
   b. Run the same command.
   c. Verify:
      * Resolved config shows agent_id == "ag_butler"
        (NOT ag_andre_butler).
      * Events emitted carry actor.id == "ag_butler".
      * Session JSONL shows the explicit id.

6. Manual e2e for the malformed case:
   a. Set `agent_id: butler` (no prefix) in butler.yaml.
   b. Run the same command.
   c. Verify:
      * Run fails.
      * (Acknowledge the CLI surface gap: the user sees
        "Configuration validation failed with 1 error(s)" —
        no per-field message. This is the separate ticket;
        document in the PR that the underlying validation_errors
        still contain the right AgentId message even though the
        CLI doesn't print it.)

7. Document any follow-up tickets Step 1 noted (CLI
   surfacing of ConfigInvalid.detail, full authoring how-to,
   prepend-leniency for malformed explicit values if anyone
   eventually wants that) — file as separate process/ docs
   or list as deferred in the PR description.

Scope: ~80 LOC tests + catalog entries + manual report.

ESCALATION PATH: if the manual run with omitted agent_id
STILL fails, Step 2 didn't fully land. Don't declare the bug
fixed; escalate with the captured failure output.

Acceptance: TC-AGENTID-01..04 pass on post-fix code;
pre-fix verification documented; all three manual scenarios
behave as expected.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — regression tests + manual e2e.

Checks:

1. All four TCs exist and pass:
   - TC-AGENTID-01 (derivation when omitted)
   - TC-AGENTID-02 (explicit wins)
   - TC-AGENTID-03 (malformed explicit fails)
   - TC-AGENTID-04 (starter helper non-migration)

2. Pre-fix verification: the PR description shows
   TC-AGENTID-01 FAILING on pre-fix code with a sensible
   failure mode (most likely "Field required" or
   "AgentId requires prefix 'ag_'" — either is fine; the
   point is the test would have caught the pre-fix gap).

3. Manual e2e: the PR description contains evidence for all
   three scenarios:
   - Omitted agent_id → derived ag_andre_butler in the
     resolved config (probe output or JSONL excerpt).
   - Explicit ag_butler → exactly ag_butler in the resolved
     config and in event actors.
   - Malformed agent_id → ConfigInvalid raised; the
     underlying validation_errors carry the AgentId message
     (even if CLI surfacing is bad — that's the separate
     ticket).

4. process/test-catalog.md gained the AGENTID entries in
   full Setup/Action/Expected/Verifies format.

5. Full test suite green. Specifically:
   - tc_eng_* goldens still byte-equal.
   - No new test asserts a behavior the fix doesn't actually
     provide.

6. Follow-up tickets named (CLI ConfigInvalid surfacing,
   authoring how-to) are either filed in process/ or
   explicitly listed in the PR description as deferred.

Verdict: PASS or FAIL. On PASS, this bug is closed.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-BUG-AGENTID-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-AGENTID-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds the bundle name is not in scope at any
  derivation callsite between partial-merge and
  resolved-validate without a deep refactor.
- Step 1 finds a code path that pins the user-typed
  agent_id format in a way derivation wouldn't satisfy.
- Step 2 reveals a positional-argument signature change that
  breaks unrelated callers (don't add a compat shim — fix the
  call sites or pick a different field-default approach).
- Step 3's manual run with omitted agent_id still fails.

When NOT to escalate:
- The exact derivation format. `ag_<bundle>_<name>` is the
  default; if Step 1 has a strong reason to pick another
  format (e.g. `ag_<bundle>.<name>`) document and proceed.
- Doc wording. Minor variations don't need coordinator input.
- Whether the helper lives in identifiers.py or resolver.py.
  Pick based on existing code layout.

---

## Part 5 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 6 — Notes on running these three steps

- **Strict order: 1 → 2 → 3.** Step 1 is the gate. Step 2's
  resolver hook depends on Step 1's callsite pick.
- **Step 2 must ship as one PR.** Splitting schema + helper +
  doc into multiple PRs creates a window where the partial
  accepts None but the resolver doesn't fill it — every
  test breaks. One PR.
- **Don't migrate the starter bundle.** Part 2 #6 + Step 1
  task 8 + Step 2 reviewer check 5 all guard this. The
  helper's `ag_helper` id is load-bearing for every
  historical event log; deriving `ag_kolsch_helper` would
  silently break those.
- **Don't bundle the CLI error surfacing fix.** It's the
  same conversation but a separate concern. This fix
  narrows the surface where users hit the bad error; it
  doesn't improve the error itself.
- **Don't add prepend-leniency.** The user can OMIT the
  field. If they provide a value, it must be canonical
  (`ag_*`). Lenient prefix-prepending makes the validator
  weaker for no real win — once you're typing the field, you
  can type it correctly.

---

## Part 7 — Out of scope

- **CLI surfacing of `ConfigInvalid.detail.validation_errors`.**
  Separate ticket. The CLI today swallows the per-field
  messages, which is why this papercut hurt more than it
  should.
- **A full "authoring an agent" how-to page.** Part 2 #7
  requires reference-doc notes only; a step-by-step how-to
  is a bigger doc effort.
- **Prepend-`ag_` leniency for malformed explicit values.**
  Out of scope by design.
- **Migrating `kolsch/agents/helper.yaml`** or any other
  existing agent YAML to omit `agent_id`. The fix is
  opt-in for new agents; existing explicit values stay.
- **Renaming `agent_id` to `id`** or any other field-rename
  ergonomics. The name stays.
- **Adding a `bundle:` field to the agent YAML.** The
  bundle is catalog-derived; no user-facing field needed.
```
