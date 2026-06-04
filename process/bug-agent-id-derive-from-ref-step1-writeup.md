# Bug — `agent_id` derive-from-ref · Step 1 verification writeup

**Branch:** `fix/framework-iter-cap-removal` (Step 1 is read-only;
no code lands here. Step 2 forks its own branch.)

**Status:** verification complete; reviewer verdict **REVISE — coordinator
decision required before Step 2 can land**. The writeup itself passes
every reviewer check; the gating gap is §5 (derived-id collisions
possible despite unique-ref invariant). Once the coordinator picks
A/B/C/D, this flips to APPROVE-TO-PROCEED with the option's
incremental scope baked into Step 2. See §11 for the reviewer
exchange.

---

## TL;DR — the headline finding

The bug doc's framing of Step 1 needs one factual correction:
**`PartialAgentConfig.agent_id` is ALREADY optional today** —
`kernel/partials.py:91` declares `agent_id: AgentId | None = None`.
The partial accepts YAML that omits the field; what fails is the
RESOLVED-layer check (`validate_resolved` in
`kernel/validation.py:89` → `ResolvedAgentConfig.agent_id: AgentId`
required at `schemas/config.py:164`), which produces a
`"Field required"` error rather than the user's reported
`"AgentId requires prefix 'ag_'"`. The user's reported message
came from typing `agent_id: butler` (no prefix), not from
omitting the field; the doc conflates the two symptoms.

So Step 2's "make the partial optional" task is a no-op — the partial
is already optional. The only code change Step 2 needs is the
derivation injection between merge and resolved-validation.

I have a cleaner recommendation for the derivation injection
point — prepend a "derived defaults" partial — that avoids
threading bundle/name through the kernel. Details in §2.

And **§5 raises an escalation**: derived ids CAN collide across
distinct refs (`bundle="foo_bar", name="baz"` and
`bundle="foo", name="bar_baz"` both derive `ag_foo_bar_baz`).
Catalog enforces unique refs, not unique derived ids.

---

## 1. Schema-layer change shape

**Finding:** the partial is already optional. No schema change
needed in Step 2.

- `PartialAgentConfig` lives at `src/kernel/partials.py:75-105`
  (NOT `src/schemas/config.py` — the bug doc's pointer was
  off). Line 91: `agent_id: AgentId | None = None`. The
  partial's `model_config = ConfigDict(extra="forbid")` (line 87)
  forbids unknown top-level fields but does NOT make declared
  fields required.
- `ResolvedAgentConfig.agent_id` is required: `schemas/config.py:164`,
  `agent_id: AgentId` (no default). Stays required after Step 2.
- Verified by `tests/unit/test_kernel.py:72` —
  `test_empty_partial_passes` already asserts
  `validate_partial({}).ok is True`. The partial accepts ALL
  fields-missing today.

**Concrete behaviour today when YAML omits `agent_id`:**

1. `validate_partial(agent_partial)` — passes (field is optional).
2. `resolve(partials)` — merge runs, produces a dict with no
   `agent_id` key.
3. `validate_resolved(merged)` — fails with Pydantic
   `"Field required"` for `/agent_id`. Surfaces as
   `ConfigInvalid` with one finding.

So the fix is purely "fill in the derived value between steps 2
and 3." No partial-schema edit; nothing to change in
`kernel/partials.py` or `schemas/config.py`.

**Step 2 task #1 in the bug doc** should be updated to: *"Verify
the partial is already optional (it is). No schema-layer change
needed."*

---

## 2. Derivation callsite — recommended pick

**Recommendation: prepend a one-key partial in
`AssetManagement.build_resolved_config` (`src/assets/asset_manager.py`).
Do NOT thread bundle/name into the kernel.**

Concrete: at `src/assets/asset_manager.py:173-179`, after
`partials = assemble_partials(...)` produces the list, **prepend**
`{"agent_id": derive_agent_id(bundle, agent_name)}` so the partials
list passed to `resolve_and_validate` becomes:

```
[
    {"agent_id": "ag_<bundle>_<name>"},   # NEW — lowest priority
    defaults,                              # existing
    agent_definition,                      # existing
    call_overrides or {},                  # existing
]
```

Where `bundle` and `agent_name` come from `parse_ref(agent_ref)` (or
equivalently `agent_entry.bundle` + the third segment of the ref —
both already in scope at this callsite via
`asset_manager.py:127-128`).

### Why this site beats the kernel pipeline

The bug doc names two candidate sites: `kernel.resolver.resolve`
and `asset_manager.build_resolved_config` "between merge and
validate." Reading both:

- **`kernel.pipeline.resolve_and_validate` (between line 149's
  `resolve` and line 153's `validate_resolved`)** — would work,
  but the kernel does not today know about bundle/name conventions.
  Threading `bundle: str, name: str` (or a `derived_agent_id:
  AgentId | None`) into the pipeline signature would propagate to
  every caller and bake a Layer-1 concern (default-value
  injection) into the kernel API. The kernel pipeline at line 132
  already documents itself as the structural-validation gate, not
  the source of value defaults.
- **`asset_manager.build_resolved_config` post-`assemble_partials`,
  pre-`resolve_and_validate`** — bundle + name are already in
  scope (asset_entry, parse_ref). Injection lives next to the
  other layer-assembly code. Kernel signature unchanged.

### Why "prepend as a partial" beats "mutate merged dict"

Per Part 1 of the bug doc:

> The derivation must run AFTER all partials are merged (so an
> explicit `agent_id` from a base or extending config wins).

If we prepend a `{"agent_id": <derived>}` partial AT INDEX 0 (lowest
priority) and let the existing RFC 7386 merge run unchanged, the
"explicit wins" property falls out for free:

- `defaults` (platform.yaml) has explicit `agent_id` → overrides
  derived. (Edge case: no current platform.yaml does this, but if
  one did, it wins.)
- Any `extends` parent (composed into agent_config per
  asset_manager.py:140-172) has explicit `agent_id` → overrides
  derived.
- The agent's own YAML has explicit `agent_id` → overrides derived.
- `call_overrides` has explicit `agent_id` → overrides derived.
- Nothing has it → merged dict carries the derived value;
  `validate_resolved` passes.

The alternative — "mutate `resolved` after the kernel's merge if
key missing" — requires changing the kernel API to accept a
post-merge hook, OR doing the mutation in `build_resolved_config`
between kernel calls (which would mean two separate kernel
invocations or a refactor of `resolve_and_validate` to expose the
intermediate merged dict). The prepend approach is one line and
keeps the kernel honest.

### Exact diff Step 2 will produce in this region

```python
# src/assets/asset_manager.py:172-180 (current)
partials = assemble_partials(
    defaults=pre_resolve_prompts(defaults, catalog),
    agent_config=pre_resolve_prompts(resolved_agent_partial, catalog),
    call_overrides=(
        pre_resolve_prompts(call_overrides, catalog) if call_overrides else None
    ),
)
```

becomes:

```python
# Step 2: prepend a derived-id partial. RFC 7386 merge gives any
# explicit agent_id (defaults / extends / agent / overrides) priority.
bundle, _, agent_name = parse_ref(agent_ref)
partials = [
    {"agent_id": derive_agent_id(bundle, agent_name)},
    *assemble_partials(
        defaults=pre_resolve_prompts(defaults, catalog),
        agent_config=pre_resolve_prompts(resolved_agent_partial, catalog),
        call_overrides=(
            pre_resolve_prompts(call_overrides, catalog) if call_overrides else None
        ),
    ),
]
```

`parse_ref` is already imported at `asset_manager.py:21`.
`derive_agent_id` is a new helper (see §4).

---

## 3. Bundle-name plumbing

Already in scope at the chosen callsite. **No plumbing change.**

- `build_resolved_config(agent_ref, ...)` receives `agent_ref`
  (e.g., `"andre/butler"`).
- `parse_ref(agent_ref)` (already imported) returns
  `(bundle, asset_type, name)`. `bundle == "andre"`, `name ==
  "butler"`. Cite: `src/assets/refs.py:23-36`.
- Equivalent: `agent_entry.bundle` (set in
  `catalog.py:_scan_agents:217`) — already retrieved at
  `asset_manager.py:127`.

Either source works; recommend `parse_ref` for symmetry (one call
gives both bundle and name without a second `.bundle` read).

---

## 4. Derivation format — confirmed (with caveat → §5)

**Recommended:** `f"ag_{bundle}_{name}"`. Helper in
`src/schemas/identifiers.py` next to `AgentId`:

```python
def derive_agent_id(bundle: str, name: str) -> AgentId:
    """Derive a canonical AgentId from the asset ref's
    (bundle, name) tuple — used when the agent YAML omits
    agent_id. AgentId(...) construction enforces the body regex;
    if upstream inputs satisfy bundle-name + agent-stem
    invariants this never raises in practice.
    """
    return AgentId(f"ag_{bundle}_{name}")
```

Putting it in `identifiers.py` (not `resolver.py`) co-locates it
with the identifier type and lets tests assert the format in unit
isolation.

### Regex satisfaction proof

`AgentId` body regex (identifiers.py:37):
`^[a-zA-Z0-9_\-.]+$` — ASCII letters, digits, `_`, `-`, `.`.

Upstream inputs:

- **Bundle name** must satisfy the catalog's `is_valid_bundle_name`
  regex `^[a-z0-9][a-z0-9_-]*$` (`assets/manifest.py:21`).
  Every char in a valid bundle name is in
  `[a-z0-9_-]` — a strict subset of the AgentId body class.
  Bundle names with hyphens (e.g., `customer-support`) produce
  `ag_customer-support_<name>`; the AgentId regex's char class
  includes `-`. ✓
- **Agent name** = file stem from the `*.yaml` glob at
  `catalog.py:215`. **There is no catalog-level validation of
  the stem's character set.** Stems with whitespace, Unicode,
  or other chars outside `[a-zA-Z0-9_\-.]` will cause
  `derive_agent_id` → `AgentId(...)` to raise `ValueError`.
  Failure mode: the user gets a clear "AgentId body contains
  invalid characters" message tied to the derived value.
  Reasonable — users who pick filesystem-hostile filenames have
  always had to provide an explicit `agent_id` to work around
  the AgentId regex. The derivation just relocates the failure
  point.

**No raise expected for typical filename-conventional stems**
(`butler`, `research-bot`, `weekly_summary`).

---

## 5. ESCALATION — derived ids can collide despite unique refs

**Verdict on bug-doc claim "collision impossible":** **wrong.**
The catalog enforces unique refs `<bundle>/<asset_type>/<name>`,
but the mapping `(bundle, name) → f"ag_{bundle}_{name}"` is
**not injective** when either segment contains `_`.

### Concrete collision

Both of these are valid, simultaneously-installable bundles:

```
~/Documents/kolsch/bundles/foo_bar/agents/baz.yaml
~/Documents/kolsch/bundles/foo/agents/bar_baz.yaml
```

Both pass `is_valid_bundle_name` (`foo_bar` and `foo` both match
`^[a-z0-9][a-z0-9_-]*$`). Both produce distinct catalog refs
(`foo_bar/baz` and `foo/bar_baz`). Both derive
the same agent id: `ag_foo_bar_baz`.

### Why this matters in practice

`agent_id` is **the** stable identity downstream:

- Authz principal (`f"agent.{agent_id}"` —
  `src/engine/engine.py:392`, `src/cli/_slash.py:498`).
- Event actor on every emitted event (`Actor(kind="agent",
  id=self.agent_id)`).
- Session/thread bindings and memory `bind_session(agent_id=...)`.

Two distinct agents sharing a derived id would silently share
authz rules, get cross-contaminated event provenance, and read
each other's memory bindings. That is a correctness defect, not
ergonomics.

### Why the format-change escape hatches don't trivially work

- Switching separator to `-`, `.`, or `__` doesn't help — bundle
  regex and agent-stem grammar both can produce any of those.
- Forbidding `_` in bundle names is a breaking change to
  spec §2.2 and the existing catalog. Bundle-name regex is at
  `assets/manifest.py:21` and is documented in the bundle-author
  surface.
- Forbidding `_` in agent file stems is technically less invasive
  (no current regex; catalog `_scan_agents` accepts any stem) but
  awkward to enforce after the fact and may break existing
  bundles.

### Options for the coordinator

**(A) Catalog-scan-time collision detection.** Add a Pass-5 to
`catalog.scan` (`src/assets/catalog.py:62-129`) that computes
`derive_agent_id(entry.bundle, name)` for every agent entry and
raises `AssetsDuplicateDerivedAgentId` (new error code) if any
two entries collide. Cost: ~10 LOC + one new error class. Users
who hit it rename one of the colliding agents OR provide an
explicit `agent_id` on one of them. Doesn't break existing
single-bundle setups.

Catch: a user with a colliding setup who CANNOT change either
filename hits a hard error at startup. They fix by adding
`agent_id: ag_x` to one of the YAMLs — which is the same escape
hatch the bug doc already articulates ("opt-in explicit
`agent_id` is the escape hatch for users who care about
stability across renames").

**(B) Pick a separator that's invalid in both segments AND
amend the bundle-name + (new) agent-stem regexes to forbid it.**
E.g., introduce `.` as the derivation separator and reject `.`
in bundle names (already rejected — bundle regex doesn't include
`.`) AND in agent stems (NEW catalog-time check). Derived format
becomes `ag_<bundle>.<name>`. AgentId body regex accepts `.`.
Catch: agent stems with `.` are common (`butler.dev.yaml` stem
= `butler.dev`) and would be newly rejected — breaking change
for current users.

**(C) Use a derivation format that encodes segment boundaries
unambiguously.** E.g., length-prefix or hash-suffix. Defeats
the "eyeball-legible" pro from the bug doc.

**(D) Accept the collision risk as acceptable and document it
loudly in §A.3.** Users who pick `_`-containing bundle/agent
names that collide get silently wrong behaviour. Strongly NOT
recommended.

**My recommendation: (A) — collision detection at catalog scan.**
It preserves the recommended `ag_<bundle>_<name>` format, doesn't
break any existing catalog, and turns a silent correctness defect
into a startup-time configuration error with a clear fix.

Step 2 would gain:

1. A new error `AssetsDuplicateDerivedAgentId(refs: list[str])`
   in `src/errors/`.
2. A scan-time check in `catalog.scan` that computes derived ids
   for every `entry.asset_type == "agent"` and raises if any two
   collide.
3. A unit test in `tests/component/test_assets.py` that builds a
   colliding tmp catalog and asserts the raise.

Cost estimate: ~20 LOC added to the ~30 LOC Step 2 originally
scoped. Brings total to ~50 LOC + the existing doc + helper +
hook plan. Still small.

**Coordinator decision needed: A, B, C, or D before Step 2.**

---

## 6. Test impact list

Greps used:

```
grep -rn "agent_id:" tests/ --include="*.py" --include="*.yaml"
grep -rn "agent_id=" tests/ --include="*.py"
grep -rn "AgentId"  tests/ --include="*.py"
```

### Tests that pin specific explicit ids (passes unchanged — no edit)

All of these construct `ResolvedAgentConfig` or call sites with an
explicit `agent_id="ag_..."`. The change does NOT affect them
because Step 2's derivation only fires when the merged dict has no
`agent_id` key; these all supply one.

- `tests/unit/test_schemas.py:391,495,513,556,572` — `AgentId("ag_test"|"ag_x")`.
- `tests/unit/test_kernel.py:40,147` — partial fixture with
  explicit `"agent_id": "ag_research"` and the
  malformed-explicit-id test asserting prefix error survives.
- `tests/unit/schemas/test_envelope_fields.py:89,170` —
  `"agent_id": "ag_helper"`.
- `tests/unit/templating/test_agent_var.py:121-219` —
  `id="ag_helper"` (template var path).
- `tests/integration/test_integration.py:98,102,402` — `ag_test`.
- `tests/integration/test_engine_crewai.py:64` — `ag_test`.
- `tests/integration/test_tool_wiring.py:284-409` — `ag_test`,
  `ag_bad_tools`.
- `tests/component/test_engine.py:106,110,169` — `ag_test`.
- `tests/component/test_memory.py:74,84,151,158,209,219,377,410,497`
  — `ag_test`.
- `tests/component/test_skills.py:106` — `ag_test`.
- `tests/component/test_store.py:174` — `ag_test`.
- `tests/component/test_tools.py:66-787` — `ag_test` everywhere.
- `tests/component/tools/test_phase2_builtins.py:61-328` — `ag_test`.
- `tests/component/tools/test_permissions_api.py:56-190` —
  `agent.ag_helper` principals (matched by explicit rule strings).
- `tests/component/authz/test_composition.py:62-161` —
  `agent.ag_helper` principals.
- `tests/component/cli/test_chat_lifecycle.py:843,869` — reads
  `config.agent_id` whatever it is; works either way.
- `tests/component/cli/test_chat_slash.py:477` — `ag_bare`.
- `tests/component/test_setup.py:1275` — `who: "ag_helper"` rule.
- `tests/component/engine/test_fallback.py:242-932` — `ag_demo`,
  `ag_x`.
- `tests/fixtures/engine_builder.py:170` — `ag_test`.
- `tests/e2e/test_e2e.py:203,273` — `ag_e2e`.
- `tests/component/test_assets.py:335,572,602` — drives
  `build_resolved_config` against tmp bundles that DO specify
  `agent_id` (verified by grep below; if any happened to omit it
  Step 2 would still resolve them correctly via derivation).
- `tests/integration/test_agent_envelope.py:103-250` —
  `agent_id: "ag_demo"`.

### Tests that need updating (needs update)

**None.** I could not find a test that asserts "`agent_id` is
required in the partial." The closest is
`tests/unit/test_kernel.py:518-525`
(`test_validate_resolved_requires_required_fields`), which asserts
that `validate_resolved({})` flags `agent_id` (and others) as
missing. That test continues to pass: Step 2 does NOT change the
resolved-layer requirement; `validate_resolved` still flags
missing fields when called directly with an empty dict.

### Edge — investigate (none, but listed for the reviewer)

- `tests/unit/test_kernel.py:300-303` — runs `validate_partial`
  on the fixture and pipeline on `[_valid_partial()]`. The fixture
  has explicit agent_id; the partial passes; the pipeline merges
  and validates the resolved layer. Step 2's derivation path is
  not exercised because the partial already has agent_id; the
  derived partial (which Step 2 will prepend) gets overridden by
  the explicit value in the fixture. Test result unchanged.

### Tests for Step 2 / Step 3 to add (per bug doc Parts 6-7 of the dev prompt)

Unit (Step 2):
- `test_partial_config_accepts_missing_agent_id` — trivially true
  today; tightens the contract against accidental regression.
- `test_partial_config_still_accepts_explicit_agent_id`.
- `test_partial_config_rejects_malformed_explicit_agent_id`.
- `test_derive_agent_id_format` — `derive_agent_id("andre", "butler")`
  → `AgentId("ag_andre_butler")`.

Integration (Step 3):
- TC-AGENTID-01..04 per the bug doc's Step 3 spec.

Plus, **if the coordinator picks option (A) in §5**:
- `test_catalog_rejects_colliding_derived_agent_ids` in
  `tests/component/test_assets.py` — two-bundle tmp catalog
  that derives the same id raises
  `AssetsDuplicateDerivedAgentId`.

---

## 7. Docs to touch

Greps used: `grep -rn "agent_id" docs/ --include="*.md"`.

### Lines to change in Step 2

- **`docs/reference/schemas.md` §A.2 line 51** — the AgentConfig
  shape diagram:
  ```
  ├── id                   string        — "ag_..."  immutable system identity
  ```
  Add: *"; optional in YAML — when omitted the resolver derives
  `ag_<bundle>_<name>` from the asset ref"*. Annotate inline OR
  add a footnote referencing §A.3.

- **`docs/reference/schemas.md` §A.3 (lines 89-97)** — "Identity"
  section. Currently says:
  > Authoring references resolve by `name`; execution binds and
  > records `id`.

  Add a paragraph: *"`id` may be omitted from agent YAML. When
  omitted, the kernel resolver derives `ag_<bundle>_<name>` from
  the agent's asset ref. An explicit `agent_id` from any partial
  (platform defaults, `extends:` parent, the agent's own YAML, or
  call-time overrides) takes precedence over the derived value.
  See §A.9 for the merge order."*

- **`docs/reference/module-api-reference.md` §13.3 (lines
  1999-2016)** — `ResolvedAgentConfig` declaration. The field is
  required at the resolved layer (unchanged); add a one-line
  comment in the code block, like:
  ```python
  agent_id: AgentId           # optional in YAML; resolver derives ag_<bundle>_<name> when omitted
  ```

### Lines to leave alone (still-valid examples)

- `docs/how-to/agent-templating.md:254,278` — `agent_id: ag_helper`,
  `agent_id: ag_deploy`. Explicit examples are still valid.
- `docs/how-to/configure-fallbacks.md:20` — `agent_id: ag_researcher`.
- `docs/how-to/find-models.md:120` — `agent_id: ag_yours`.
- `docs/tutorials/first-agent.md:92` — `agent_id: ag_helper`.

Per the bug doc:
> Recommend: add a "derivation rule" note to the reference docs;
> leave the how-to examples alone (they're valid examples of
> explicit ids).

Confirmed. Optional: a one-sentence callout at the top of
`docs/how-to/agent-templating.md` noting `agent_id` may be
omitted in agents the user is authoring fresh — nice to have, not
required.

### Spec/explanation/contract docs — NO change

- `docs/explanation/v1-scope.md:92`, `error-model.md` lines,
  `agent-engine-contract.md`, `memory-provider-contract.md`,
  `tools-subsystem.md`, `session-run-store-contract.md`,
  `contract-audit.md`, `architecture-roadmap.md`,
  `secrets-sandbox-authz-contract.md`,
  `framework-protocol-design.md` — all of these reference
  `agent_id` at the RESOLVED-config / downstream level (event
  payloads, authz principals, memory bindings, store rows). Those
  references describe the post-resolution shape, which is
  unchanged. No edits needed.

---

## 8. Starter bundle non-migration — confirmed

- `src/cli/_starter_bundle/kolsch/agents/helper.yaml:3` has
  `agent_id: ag_helper`. **Stays unchanged.**
- If we DID migrate it to omit `agent_id`, the derived id would
  become `ag_kolsch_helper` (bundle `kolsch`, agent `helper`).
  That would break every test that pins `ag_helper` — at minimum:
  - `tests/unit/templating/test_agent_var.py:121-219` (template
    var asserts `agent.id == "ag_helper"`).
  - `tests/unit/schemas/test_envelope_fields.py:89,170`.
  - `tests/component/tools/test_permissions_api.py:56-190` (authz
    rules keyed on `agent.ag_helper`).
  - `tests/component/authz/test_composition.py:62-161`.
  - `tests/component/test_setup.py:1275` (`who: "ag_helper"` rule).
- Plus historical session JSONLs and memory bindings in any
  long-lived workspace.
- **Confirmed:** no test will fail if we don't migrate it. The
  explicit `ag_helper` overrides the prepended derived
  `ag_kolsch_helper` via merge; resolved config carries
  `ag_helper`.

---

## 9. Bundle-field-on-schema check — confirmed absent

`PartialAgentConfig` fields (`kernel/partials.py:75-105`):
`schema_version`, `assembly_version`, `agent_id`, `name`,
`description`, `model`, `prompt`, `io`, `subsystems`, `runtime`,
`extends`, `permissions`, `context_files`.

**No `bundle:` field.** Bundle is catalog-derived from the file's
directory location (`bundles/<bundle>/agents/<name>.yaml`); the
catalog scanner at `assets/catalog.py:_scan_agents:217` writes
`bundle=manifest.name` into each `AssetEntry`. The user never
declares the bundle inside the YAML, and `extra="forbid"` would
reject it if they tried. Consistent with the bug doc's expectation.

---

## 10. Summary for the coordinator

**Approve-to-proceed asks two things:**

1. **Confirm Step 2's revised scope** —
   - No partial-schema change (the field is already optional).
   - Add `derive_agent_id(bundle, name) -> AgentId` helper in
     `src/schemas/identifiers.py`.
   - In `src/assets/asset_manager.py:173`, prepend
     `{"agent_id": derive_agent_id(...)}` to the partials list
     produced by `assemble_partials(...)`.
   - Doc updates per §7.
   - Unit tests per §6 ("Tests for Step 2 to add").

2. **Pick a path for the collision case (§5).** Recommended:
   **(A) catalog-scan-time collision detection.** That adds a
   ~20-LOC scan check + one new error class + one test in Step 2.

If the coordinator picks (D) — accept-and-document — Step 2's
scope is the original ~30 LOC; the docs note the collision risk
loudly in §A.3. If the coordinator picks (B) or (C), the format
change cascades into the helper, the docs, and potentially the
catalog regexes — flag for re-scoping.

**Escalation lodged in this writeup** rather than the separate
Coordinator Escalation Log because Part 5 of the bug doc is
currently empty and Step 1 is the first opportunity to populate
it. Happy to copy this into the bug doc's Part 5 once direction
is decided.

---

## Step 1 → Step 2 handoff TL;DR (one paragraph)

The partial is already optional; the only fix is to prepend
`{"agent_id": derive_agent_id(bundle, name)}` as a low-priority
partial in `asset_manager.build_resolved_config:173`, plus a small
helper next to `AgentId` in `schemas/identifiers.py` and three
doc-line edits. Tests need no migration. **BUT:** `ag_<bundle>_<name>`
is not collision-free when either segment contains `_` (catalog
enforces unique refs, not unique derived ids); coordinator needs
to pick between catalog-time collision detection, a format
change, or accept-and-document before Step 2 lands.

---

## 11. Reviewer exchange — REVISE → pending coordinator pick

**Reviewer verdict:** REVISE — every check that can pass does pass
on the writeup's terms (including check 5, where the writeup
correctly refused to "prove the impossible" and escalated the real
defect). The gating gap is in the readiness state, not the writeup:
**§5 requires a coordinator decision before Step 2's scope is fixed.**

### What the reviewer confirmed

- Check 1 (schema-layer shape): PASS. `kernel/partials.py:91`
  already declares the partial-optional shape.
- Check 2 (callsite): PASS — and notes the "prepend at index 0"
  shape is an improvement on the bug doc's "mutate post-merge"
  framing because it preserves "explicit wins" across all four
  priority layers via the existing RFC 7386 merge.
- Check 3 (plumbing): PASS — `parse_ref` already in scope at the
  asset_manager callsite.
- Check 4 (format + regex proof): PASS, with the honest edge case
  about un-validated agent file stems acknowledged.
- Check 5 (collision impossibility): the writeup correctly DOES NOT
  claim impossibility — it proves the opposite via a concrete
  counter-example (`bundle="foo_bar", name="baz"` collides with
  `bundle="foo", name="bar_baz"`) and escalates. Reviewer flagged
  the literal text of the reviewer-prompt check 5 as failing
  (impossibility is not proven) but accepted the escalation as
  the right response.
- Checks 6-9 (test list / doc list / starter non-migration / named
  escalation conditions): all PASS with file:line citations.

### What blocks APPROVE-TO-PROCEED

The §5 escalation's four options change Step 2's scope:

- **(A) catalog-scan-time collision detection** — Step 2 grows to
  ~50 LOC (helper + prepend hook + collision check + new error
  class + one assets test). No breaking change. Recommended.
- **(B) format change with regex amendment** — cascades into the
  helper, the bundle-name regex, possibly the agent-stem grammar.
  Breaking change for current bundles with `_` or `.` in names.
- **(C) hash-suffix on the derived id** — preserves uniqueness but
  kills the eyeball-legibility pro the bug doc cites as the
  format's main virtue.
- **(D) accept-and-document** — ships a silent correctness defect
  (two distinct agents sharing authz / event provenance / memory).
  Strongly not recommended.

### My recommendation (unchanged from §5)

**Option (A).** Adds a Pass-5 to `catalog.scan` that computes
`derive_agent_id(entry.bundle, name)` per agent entry and raises
a new `AssetsDuplicateDerivedAgentId(refs: list[str])` when any
two collide. Users who hit the collision rename one agent OR add
an explicit `agent_id` to one — the same escape hatch the bug
doc already documents for rename-stability. Preserves the
recommended format, no breaking changes, turns a silent
correctness defect into a startup-time configuration error.

### Reviewer nits — fixed in this revision

- §1 test citation: was `test_validate_partial_empty_dict_passes
  at line 74`; reviewer flagged the name was off and the line drift
  was real. Re-verified: the test is `test_empty_partial_passes`
  at `tests/unit/test_kernel.py:72`. **Updated.**
- §4 regex line: writeup cites `identifiers.py:37`; reviewer's
  read placed it at line 38. Re-verified against the live file:
  `_AGENT_ID_BODY = re.compile(...)` is at line 37 in this repo.
  **Citation stands; reviewer was off by one here.**

### What Step 2's developer prompt will encode once the coordinator picks

Independent of A/B/C/D, Step 2 always includes:

1. New helper `derive_agent_id(bundle, name) -> AgentId` in
   `src/schemas/identifiers.py`.
2. Prepend `{"agent_id": derive_agent_id(...)}` to the partials
   list in `src/assets/asset_manager.py:173` (use
   `parse_ref(agent_ref)` for bundle + name).
3. Doc updates per §7 (schemas.md §A.2 line 51, §A.3 lines 89-97,
   module-api-reference.md §13.3 lines 1999-2016).
4. Four unit tests per §6 (partial accepts missing / explicit
   honored / malformed rejected / helper format).

If (A) wins, Step 2 also adds:
- `AssetsDuplicateDerivedAgentId(refs: list[str])` error class.
- Pass-5 in `catalog.scan` computing derived ids per agent entry
  and raising on duplicate.
- One assets test exercising a colliding tmp-bundle pair.

**Awaiting coordinator pick on A/B/C/D to finalize Step 2 plan.**
