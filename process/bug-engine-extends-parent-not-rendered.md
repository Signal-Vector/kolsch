# Bug — Engine doesn't render `{% extends parent %}`; the Phase 2.2 inheritance feature is wired at the asset layer but not at the render layer

**Draft 0.1** *(four-step build plan, dependency-ordered, each step shipping its own PR with focused dev + reviewer prompts. Includes a 24-case fixture corpus enumerated in Part 4 — used as both the test surface AND the acceptance gate for Steps 3-4.)*

> Reported symptom: a user agent extending the kolsch helper with
> `{% extends parent %}` and Jinja block overrides crashes at
> agent-load with:
>
> ```
> TypeError: no loader for this environment specified
> ```
>
> Verified by running `kolsch chat andre/agents/butler` against
> a butler.yaml using the canonical Phase 2.2 prompt-inheritance
> pattern from `docs/how-to/agent-templating.md:231-292`. The
> docs describe the feature as supported; the engine doesn't
> implement the render-time half of it.
>
> Two related Phase 2.2 quality issues surface alongside:
> - The `kolsch:agents/_base.yaml` example in the how-to is
>   misleading — `kolsch:` resolves against the installed
>   package (which ships only `docs/` + `AGENTS.md`), not the
>   workspace's bundle directory. No agent YAML reachable via
>   `kolsch:` exists today.
> - Skills listing is post-appended to the rendered prompt
>   (`assembly.py:83-87`) — asymmetric with tools, which expose
>   `{{ tools }}` for inline templating. The asymmetry is not
>   the bug here but it interacts with the test corpus.

---

## Part 0 — Concrete trigger case

Workspace `~/Documents/kolsch`. Butler defined as:

```yaml
# bundles/andre/agents/butler.yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_butler
name: butler
description: A personal butler agent inheriting from kolsch helper.
extends: workspace:bundles/kolsch/agents/helper.yaml
prompt: |
  {% extends parent %}
  {% block role %}a personal butler — attentive, precise, and proactive{% endblock %}
  {% block special_instructions %}
  Manage files, run commands, keep the workspace organized.
  {% endblock %}
```

Helper has the required Jinja blocks:

```yaml
# bundles/kolsch/agents/helper.yaml (after the same-conversation edit)
prompt: |
  You are {{ agent.name }}, {% block role %}a friendly general-purpose helper agent{% endblock %}
  running on {{ agent.model.ref }}.
  ...
  {% block special_instructions %}{% endblock %}
  Be concise. ...
```

Running `uv run kolsch chat andre/agents/butler`:

```
TypeError: no loader for this environment specified
```

The asset-manager-layer composition works fine (we confirmed
butler inherits 2 context_files, 4 tools, model, permissions
via RFC 7386). The crash is at the render step.

---

## Part 1 — The wiring gap

Phase 2.2's `extends:` feature has **two distinct halves**:

### 1.1 Field composition (works)

`apply_inheritance` at `src/templating/inheritance.py:126` and
`resolve_extends_chain` at `:81` walk the parent chain, merge
fields per RFC 7386, strip the `extends:` key from each layer,
and return a composed dict. Called from
`src/assets/asset_manager.py:139-157`. Result: child gets
parent's model, tools, context_files, permissions wholesale
when the child doesn't redefine them. **This works correctly.**

### 1.2 Prompt template inheritance (broken)

`render_with_parent` at `src/templating/inheritance.py:155-205`
exists for exactly this purpose. Constructs a Jinja
`Environment` with a `DictLoader({"parent": parent_template_string})`
so `{% extends parent %}` resolves naturally. **Never called from
production code** — only by `tests/component/test_template_inheritance.py`.

The engine's prompt-render step
(`src/engine/engine.py:_render_prompt_if_needed` at line 667+,
specifically the call at line 795) only knows about
`render_prompt` (`src/templating/render.py:40`), which
constructs:

```python
env = Environment(undefined=StrictUndefined, autoescape=False)
```

No loader. Jinja's `{% extends parent %}` requires a loader to
resolve "parent" → `TypeError: no loader for this environment
specified` at render time.

Worse: `apply_inheritance` strips `extends:` from every layer
during composition (line 142-143). By the time the engine sees
the resolved config, **`self.config.extends is None`** even
when the agent had an `extends:` field — the engine has no way
to know there's a parent prompt to inherit from.

So the engine literally cannot do the right thing without
changes to BOTH the asset manager (preserve the leaf's extends)
AND the engine (dispatch to `render_with_parent` when extends
is set).

### 1.3 Why nobody noticed

Pre-this-conversation, no canonical agent used
`{% extends parent %}`. The pattern is documented at
`docs/how-to/agent-templating.md:231-292` with a single example
(`extends: kolsch:agents/_base.yaml`) that doesn't resolve
anyway (`kolsch:` → installed package, which doesn't ship
agents). So the only path that would have exercised the gap was
inaccessible in normal use.

---

## Part 2 — Theory of the solution

The principle:

> **The asset manager owns chain composition; the engine owns
> render-time variable evaluation. The two need to hand off
> the parent prompt source at the boundary, and neither does
> today.**

Three implementation options:

| Where | How | Tradeoff |
|---|---|---|
| **A — Preserve leaf's `extends:` on the resolved config; engine re-walks catalog at render time** | `apply_inheritance` strips extends from non-leaf layers but keeps it on the leaf. Engine at render time looks up the parent in `self.engine._assets._catalog`, reads its prompt source, calls `render_with_parent`. | No schema change. Engine has the catalog handle already. Slight render-time cost (one extra catalog lookup); negligible. |
| **B — Asset manager derives `parent_prompt_source` field on resolved config** | New field on `ResolvedAgentConfig`: `parent_prompt_source: str \| None = None`. Asset manager populates it from the parent's YAML during chain resolution. Engine reads it. | Schema gains an engine-internal-looking field. Faster (no render-time catalog lookup). Pollutes the public schema with implementation detail. |
| **C — Asset manager pre-renders the inheritance** | At `apply_inheritance`, detect `{% extends parent %}` in the leaf's prompt, render against the parent's source using a partial-context Jinja pass, store the result as the resolved prompt. Engine then renders normally. | Requires "partial Jinja render" (envelope variables still as placeholders, blocks resolved). Jinja doesn't natively support this; would require a hack (regex substitution or AST manipulation). |

**Locked recommendation: A.** Smallest blast radius, no schema
change, clear separation of concerns (asset manager composes
field-level; engine renders). The render-time catalog lookup is
free (catalog is in-memory).

C is rejected because "partial Jinja render" is fragile and
introduces a non-standard render mode that the spec hasn't
characterized. B is acceptable as a fallback if Step 1 reveals
catalog access from the engine is awkward, but A is the default.

---

## Part 3 — Acceptance criteria

The bug is fixed when **all** hold:

1. **The trigger case (butler.yaml with `{% extends parent %}`)
   loads and renders.** Re-running the original `kolsch chat
   andre/agents/butler` succeeds; the rendered system prompt
   contains both parent prose (tool-use rules, conciseness
   guidance) and child block content (butler identity, butler
   instructions).

2. **`apply_inheritance` preserves the leaf's `extends:`.** Non-leaf
   layers still have `extends` stripped during chain
   composition (their extends already resolved). The leaf's
   `extends` value survives to the resolved config.

3. **The engine dispatches correctly.**
   `_render_prompt_if_needed` at `engine.py:795` calls
   `render_with_parent` when `self.config.extends is not None`,
   and `render_prompt` otherwise. The dispatch is a single
   branch — no behavioral change for agents without extends.

4. **All 24 cases in the Part 4 corpus produce the expected
   outcome.** Pass for valid cases; the documented error for
   invalid cases. The corpus IS the acceptance gate — see Part 4
   for details.

5. **No regression on Category A or B** (no-inheritance and
   field-only-inheritance cases). These already worked; the
   wiring change must not affect them.

6. **`config.required_block_missing` fires for Category D2.** The
   error class already exists at
   `src/errors/kernel.py:147`; the wiring change must surface
   it through the new dispatch path.

7. **Documentation reflects the working feature** (Step 4
   ships this). Three deliverables:

   a. `docs/how-to/agent-templating.md` — misleading
      `kolsch:agents/_base.yaml` examples replaced with
      `workspace:bundles/kolsch/agents/helper.yaml` equivalents;
      a "Common gotchas" subsection added covering C3 (silent
      stray-text discard), C5 (orphan `{% extends parent %}`),
      and the F5 `kolsch:` vs. `workspace:` prefix confusion;
      a `{{ super() }}` subsection added covering K2 (additive
      block composition); a multi-level chain example using
      the E1-style fixture.

   b. `docs/how-to/extending-an-agent.md` — NEW focused
      tutorial-recipe using butler as the worked example.
      Covers picking a parent, the three patterns from
      agent-templating.md as concrete steps, and testing via
      `build_resolved_config`. Length: ~150-250 lines.

   c. `docs/reference/schemas.md` — `extends` field
      description updated to reflect that the leaf's
      `extends` value is preserved on the resolved config
      (post-Step-2 behavior). Note the engine's render-time
      use of this field.

   **Every example in the new docs MUST correspond to a
   fixture from Step 3's corpus.** Doc-fixture divergence
   is the recurring failure mode that left the original
   how-to misleading. Step 4 reviewer enforces this.

### Out of scope

- **Shipping canonical agents as kolsch-package resources** so
  `kolsch:agents/helper.yaml` becomes a valid extends target.
  Future work; not blocking this fix.
- **Adding `{{ skills }}` as a Jinja variable** for inline-rendered
  skills listing (symmetric with `{{ tools }}`). The
  post-append pattern at `assembly.py:83-87` stays. Separate
  ticket.
- **Additive composition syntax** (`extend:` / `+declared:`) at
  the YAML field level. RFC 7386 replace-only stays — locked
  in `bug-platform-internal-error-boundary-swallow.md`
  conversation history.
- **Diamond inheritance** (single-parent only stays).
- **Conditional inheritance** (`{% if %}{% extends %}{% endif %}`).
  Jinja allows; nobody asks; out of scope.

---

## Part 4 — The 24-case fixture corpus

This corpus is both the test surface AND the acceptance gate
for Steps 3-4. Fixtures live under `tests/fixtures/extends/`
organized by category subdirectory (`a/`, `b/`, …, `l/`). Each
case is a small YAML or YAML-pair with a single expected
outcome.

The **Currently** column indicates pre-fix behavior:
- ✓ = works correctly (regression guard only)
- ✗ = fails today; the fix must make it pass
- ⚠ = unclear / never exercised

### Category A — No inheritance (baseline)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| A1 | Plain prompt, no Jinja | `a/a1_plain.yaml` | Renders byte-identical to itself. | ✓ |
| A2 | Jinja vars resolve | `a/a2_jinja.yaml` (uses `{{ agent.name }}`, `{% for t in tools %}`) | Rendered prompt has agent name + tool names inlined. | ✓ |

### Category B — Field inheritance (RFC 7386; no Jinja blocks)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| B1 | Child omits everything (no prompt either) → renamed parent | `b/b1_parent.yaml`, `b/b1_child.yaml` | Loads; child's `{{ agent.name }}` renders to child's name. | ✓ |
| B2 | Child overrides model only, inherits rest | `b/b2_*.yaml` | Resolved model = child's; tools/prompt/etc = parent's. | ✓ |
| B3 | Child replaces tools list entirely | `b/b3_*.yaml` | Child has ONLY the tools it declared (NOT parent's). | ✓ |
| B4 | Child writes fresh prompt without `{% extends parent %}` | `b/b4_*.yaml` | Child's prompt wins; parent prompt ignored; other fields inherited. | ✓ |

### Category C — Prompt block inheritance (the bug)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| C1 | Parent has block; child fills via `{% extends parent %}` | `c/c1_*.yaml` | Rendered prompt has parent prose + child block content. | ✗ |
| C2 | Parent has block; child extends but doesn't override | `c/c2_*.yaml` | Rendered prompt has parent's default block content. | ✗ |
| C3 | Parent has NO blocks; child uses `{% extends parent %}` | `c/c3_*.yaml` | Rendered prompt == parent's prompt verbatim (Jinja silently discards child's non-block content). | ✗ |
| C4 | Multiple blocks; child fills some | `c/c4_*.yaml` | Filled blocks = child's; unfilled = parent defaults. | ✗ |
| C5 | `{% extends parent %}` without `extends:` field | `c/c5_orphan.yaml` | Clean error at agent-load (NOT `TypeError`). Pick the error code in Step 1. | ⚠ |

### Category D — Required blocks

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| D1 | Required block filled | `d/d1_*.yaml` | Loads cleanly. | ✗ |
| D2 | Required block NOT filled | `d/d2_*.yaml` | `config.required_block_missing` Fatal at agent-load. | ✗ |
| D3 | Required block declared but child writes fresh prompt | `d/d3_*.yaml` | Loads; required check does NOT fire (per `agent-templating.md:315-319`). | ✗ |

### Category E — Multi-level chains

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| E1 | A extends B extends C; A fills C's block | `e/e1_a.yaml`, `e/e1_b.yaml`, `e/e1_c.yaml` | Block traverses chain; A's content wins. | ✗ |
| E2 | A extends B extends C; each layer adds different fields | `e/e2_*.yaml` | Each non-redefined field walks up the chain to find its definition. | ✓ (field side works; render side needs C-class) |

### Category F — Cycles and missing references (must fail cleanly)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| F1 | Self-cycle: A extends A | `f/f1_self.yaml` | Fatal at load with `config.agent_inheritance_cycle`. | ✓ |
| F2 | Two-node cycle: A → B → A | `f/f2_a.yaml`, `f/f2_b.yaml` | Same Fatal. | ✓ |
| F3 | Missing parent | `f/f3_orphan.yaml` | Fatal with `config.parent_agent_not_found`. | ✓ |
| F4 | Malformed extends (no prefix) | `f/f4_no_prefix.yaml` | Layer-1 validation error. | ✓ |
| F5 | `kolsch:` to non-existent kolsch-package resource | `f/f5_kolsch_no_agent.yaml` | Today: `parent agent ... not found`. Step 2 may improve the message; see Part 7. | ✓ (functions; message could be better) |

### Category G — Skills + prompts (asymmetric handling)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| G1 | Agent with enabled skills → listing appended | `g/g1_*.yaml` + a fixture skill | Rendered prompt + `\n\n## Available Skills\n…` appended. | ✓ |
| G3 | Child inherits enabled-skills list | `g/g3_*.yaml` | Child rendered prompt has parent's skills listing appended. | ✓ (field side) / ⚠ (untested for child agents) |

### Category H — Render-time guarantees

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| H1 | `{{ tools }}` referenced + no tools declared | `h/h1_empty_tools.yaml` | `agent.template_warning` Recoverable; render succeeds. | ✓ |
| H2 | Typo'd variable name (StrictUndefined) | `h/h2_typo.yaml` (uses `{{ agent.naem }}`) | `config.template_render_failed` Fatal at agent-load. | ✓ |

### Category I — context_files prefix resolution

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| I1 | `workspace:` context_files entry resolves | `i/i1_*.yaml` + a workspace doc | Doc body inlined into `{{ docs }}`. | ✓ |

### Category K — Inheritance + Jinja vars (load-bearing for the fix)

| ID | Intent | Files | Expected | Currently |
|---|---|---|---|---|
| K1 | Block content references envelope vars | `k/k1_*.yaml` | Vars inside the child's block render against the CHILD's context (child's `agent.name`, not parent's). | ✗ |
| K2 | Child uses `{{ super() }}` to extend parent's block | `k/k2_*.yaml` | Rendered block = parent's content + child's content concatenated. The only additive-composition path kolsch has. | ✗ |

### Totals

- 24 cases (A1, A2, B1-B4, C1-C5, D1-D3, E1, E2, F1-F5, G1, G3, H1, H2, I1, K1, K2).
- 12 currently work (✓). Regression guard — must keep working.
- 10 currently broken (✗). Step 2's wiring fix must turn them ✓.
- 2 undertested (⚠) — Step 1 confirms current behavior; Step 2 may improve.

---

## Part 5 — Step dependency graph

```
Step 1 (verification + design picks + corpus fixture layout)  ←── STOP gate
   │
   ▼
Step 2 (asset manager preserves extends + engine dispatches)
   │
   ▼
Step 3 (corpus fixtures + per-case tests run)
   │
   ▼
Step 4 (documentation: agent-templating.md fix + extending-an-agent.md NEW + schemas.md)
   │
   ▼
Step 5 (manual chat e2e + user-confirmation handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Verification writeup + design picks + corpus fixture skeleton | ~30 min |
| **2** | Asset manager + engine wiring changes; unit-level tests | ~40 min |
| **3** | Full 24-case fixture corpus + test runner walking the corpus | ~60 min |
| **4** | Three doc deliverables, each example pinned to a Step 3 fixture | ~40 min |
| **5** | Manual chat e2e using the docs from Step 4 + user-confirmation handoff | ~25 min |

Each step ships its own PR. Step 1 is the gate. Step 4
(docs) explicitly comes BEFORE Step 5 (e2e + handoff) so
the manual e2e can follow the new doc walkthrough and prove
the docs are accurate.

---

## Step 1 — Verification + design picks + corpus fixture layout

### Developer prompt

```
[standing instructions]

Step 1 — Verify the design and lock the open questions.
No production code.

Read first:
- This spec, Parts 0-4.
- src/templating/inheritance.py (apply_inheritance,
  render_with_parent).
- src/templating/render.py (render_prompt — no-loader env).
- src/engine/engine.py:667-805 (_render_prompt_if_needed).
- src/assets/asset_manager.py:130-170 (chain + extends
  stripping).

Tasks:

1. Cite the wiring gap with file:line in three places:
   render.py no-loader env; engine.py:795 always calls
   render_prompt; asset_manager.py:155 strips extends.

2. Pick dispatch design (A vs B from Part 2). Confirm
   engine's catalog access for Option A; name the accessor.

3. Pin the asset_manager change: preserve `extends` on the
   leaf (chain index 0); confirm downstream merge doesn't
   re-strip.

4. Pin the C5 error class (orphan `{% extends parent %}` —
   prompt uses the directive but config.extends is None).
   New code recommended.

5. Pin multi-level chain handling: load-all-ancestors-into-
   DictLoader vs. asset-manager-pre-flatten. Pick one.

6. Pin fixture layout: tests/fixtures/extends/ with subdirs
   per category (a..k); sketch the fixture-load helper Step 3
   uses.

ESCALATION:
- Engine can't reach the catalog at render time.
- render_with_parent doesn't support multi-level + pre-flatten
  needs deferred-variable Jinja that doesn't natively exist.
- Preserving leaf.extends requires schema changes that touch
  unrelated callers.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1. No code; writeup is the deliverable.

Checks:
1. Three wiring-gap citations with file:line.
2. Dispatch design (A or B) picked with rationale; catalog
   accessor named if A.
3. asset_manager leaf-preservation is concrete.
4. C5 error class decided (no TBD).
5. Multi-level chain handling picked (single approach, not
   "figure out in Step 2").
6. Fixture layout pinned.
7. Escalation conditions confirmed or raised.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Asset manager preserves `extends`; engine dispatches

### Developer prompt

```
[standing instructions]

Step 2 — Wire the asset manager + engine per Step 1's design.

Read first:
- Step 1's writeup.
- src/assets/asset_manager.py:130-170.
- src/engine/engine.py:667-805.
- src/templating/inheritance.py.

Tasks:

1. asset_manager.py:154-157 — preserve `extends` on the leaf
   (chain index 0); keep stripping from non-leaf layers.
   Confirm the resolved config carries the original value.

2. Multi-level chain handling per Step 1's pick (pre-flatten
   in asset_manager OR load-all-ancestors at render time).
   Document the chosen path in the PR.

3. engine.py:_render_prompt_if_needed — add the dispatch
   branch before the existing `render_prompt(...)` call:
   if `self.config.extends is not None` → call
   `render_with_parent` with the parent source; else →
   render_prompt as today. Add a `_load_parent_prompt_source`
   helper using the catalog accessor Step 1 pinned.

4. C5 detection — BEFORE the else-branch render_prompt call,
   check: prompt contains `{% extends parent %}` AND
   self.config.extends is None → raise the typed PlatformError
   Step 1 picked. Order matters (must fire before render_prompt).

5. F5 message — if Step 1 picked "yes," amend
   ConfigParentAgentNotFound with the `kolsch:`-vs-`workspace:`
   hint. Skip otherwise.

6. Unit tests in tests/unit/test_engine_extends_dispatch.py:
   extends=None → render_prompt path; extends set →
   render_with_parent; orphan `{% extends parent %}` → C5
   error.

7. NO change to render_prompt, render_with_parent,
   Framework protocol, assembly.py, or the boundary
   classifier.

ESCALATION: Jinja's empty-context render-for-pre-flatten
errors on StrictUndefined for `{{ agent.name }}` instead of
deferring variable evaluation; fallback is the larger
load-all-ancestors approach.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:
1. asset_manager preserves leaf.extends as a single
   index-0 carve-out (not a broader rewrite).
2. Engine dispatch reads `self.config.extends is not None`
   — NOT a prompt substring check (would let comments
   trigger).
3. _load_parent_prompt_source uses the Step 1 catalog
   accessor.
4. C5 detection fires BEFORE the render_prompt call (order
   verified by reading).
5. C5 error class matches Step 1's pick.
6. Multi-level chain handling matches Step 1 (no surprise
   alternative).
7. NO change to render_prompt, render_with_parent,
   assembly.py, boundary classifier.
8. Unit tests cover the three paths.
9. Full suite green; tc_eng_* goldens byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Build the 24-case fixture corpus + test runner

### Developer prompt

```
[standing instructions]

Step 3 — Materialise the Part 4 corpus as fixtures + tests.

Read first:
- Step 2's PR.
- Part 4 of the spec (the 24-case table).
- Step 1's pinned fixture layout convention.

Tasks:

1. Populate tests/fixtures/extends/ with all 24 fixtures
   per Part 4. Step 1 scaffolded the subdirs (a..k); this
   step writes the YAMLs. Keep each fixture minimal.

2. Build tests/integration/_extends_corpus_cases.py — the
   case table. One entry per Part 4 ID with:
   case_id, category, fixture_files, target_agent_ref,
   expected_outcome
   (loads_with_render | loads_with_error | error_at_load_<code>),
   expected_render_contains / excludes, expected_error_code.

3. Build tests/integration/test_extends_corpus.py that
   parametrizes over the case table: install fixtures in a
   tmp workspace, call build_resolved_config, drive the
   render, assert outcome matches.

4. Walk Part 4's table: ✓ cases must pass (regression
   guard); ✗ cases must pass post-Step-2; ⚠ cases pin
   current behavior with an explanatory comment.

5. Pre-fix sampling — stash Step 2; run C1, C2, K1, K2;
   document each pre-fix failure mode in the PR.

6. Add EXT-CORPUS section to process/test-catalog.md (24
   entries, IDs match Part 4 verbatim).

7. NO production code changes. If any case reveals a
   Step 2 gap, escalate.

ESCALATION: a ✗ case still fails after Step 2 landed.
Escalate with the failing case ID + the captured output.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:
1. tests/fixtures/extends/ has all 24 fixtures organized
   by category. Spot-check three from different categories.
2. _extends_corpus_cases.py lists all 24 IDs from Part 4
   verbatim.
3. test_extends_corpus.py parametrizes over the table; no
   skips.
4. Pre-fix sampling shows 3-4 ✗ cases failing pre-Step-2
   with the expected failure modes.
5. ⚠ cases (C5 + G3) pinned to Step 1's documented
   behavior.
6. EXT-CORPUS section added to test-catalog.md.
7. Full suite green; tc_eng_* goldens byte-equal.
8. NO production code changes outside src/ since Step 2.

Verdict: PASS or FAIL.
```

---

## Step 4 — Documentation: fix misleading examples + new tutorial + reference update

> **PRE-CONDITION**: `qol-prefix-defaults-and-unified-refs.md`
> MUST be in main before this step starts. That QoL changes
> the canonical ref convention (drops the asset-type middle
> segment; makes `workspace:` prefix optional). All the
> examples this step writes — in both the fixed
> `agent-templating.md` and the new `extending-an-agent.md`
> — must use the new convention from day one. If the QoL
> hasn't shipped, BLOCK this step and escalate.

### Developer prompt

```
[standing instructions]

Step 4 — Make the docs honest about how `extends:` works,
using the same fixtures the test corpus uses so docs and
tests can't drift.

VERIFY THE PRE-CONDITION FIRST:
- Confirm qol-prefix-defaults-and-unified-refs.md has
  shipped (parse_ref accepts the new 2-segment form;
  context_files default to workspace:).
- If NOT shipped, BLOCK this step. The docs you write
  here would immediately need rewriting.

Read first:
- Steps 1-3 PRs (Step 3's corpus is your example source).
- docs/how-to/agent-templating.md (the misleading file).
- docs/reference/schemas.md (envelope fields + prefix table).
- docs/index.md (how-to listing).

Tasks:

1. Fix docs/how-to/agent-templating.md. Replace every
   `kolsch:agents/...` example with the working
   `workspace:bundles/kolsch/agents/helper.yaml` form. The
   canonical example must match Step 3's C1 fixture.

2. Add four subsections to agent-templating.md:
   - "Resolving the parent reference" — explain workspace:
     vs. kolsch: (the latter currently ships only docs/ +
     AGENTS.md per src/kolsch/__init__.py).
   - "Common gotchas" — C3 stray-text discard, C5 orphan
     {% extends parent %}, F5 kolsch:-not-found.
   - "Compose with {{ super() }}" — K2 example; note this
     is the only additive composition kolsch has.
   - "Multi-level chains" — E1 three-file example.

3. Author docs/how-to/extending-an-agent.md (~150-250
   lines) using butler as the worked example. Sections:
   Pick a parent → Decide what to override (Patterns
   A/B/C tied to B1/B4/C1 fixtures) → Define your child
   agent → Verify it loads → Run a chat session against it.
   Butler YAML must match what Step 5 will restore.

4. Update docs/reference/schemas.md: note `extends:` is
   preserved on the resolved config (post-Step-2); update
   the prefix table to reflect the kolsch:-only-ships-docs
   reality.

5. Update docs/index.md to list the new how-to.

6. Lint pass: `grep -rn "kolsch:agents/" docs/` must
   return zero unmarked hits.

7. NO production code changes. NO new fixtures. NO bundle
   YAML changes.

ESCALATION: docs work surfaces a design question Step 1
didn't pin (e.g., inheritance debugging UX). Escalate;
don't paper over.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 (Docs).

Checks:
1. `grep -rn "kolsch:agents/" docs/` returns zero unmarked
   hits.
2. Every YAML example in touched docs corresponds to a
   Step 3 fixture. Spot-check four: agent-templating
   canonical (C1), super() (K2), extending-an-agent
   walkthrough (B1/B4/C1), schemas prefix (I1 or B1).
3. extending-an-agent.md has all five sections; butler
   YAML matches what Step 5 will restore.
4. schemas.md says `extends:` is preserved on resolved
   config (NOT "stripped during composition").
5. Common gotchas covers C3, C5, F5 with the actual error
   codes Step 2 produced.
6. {{ super() }} subsection has a runnable K2-equivalent
   example.
7. Multi-level chains has an E1-equivalent three-file
   example.
8. docs/index.md lists the new how-to.
9. How-to length 150-250 lines.
10. NO production code changes (git diff src/ empty).

Verdict: PASS or FAIL.
```

---

## Step 5 — Manual chat e2e + user-confirmation handoff

### Developer prompt

```
[standing instructions]

Step 5 — Final closure: prove the docs from Step 4 actually
work by following them, then hand off to the user.

Read first:
- Steps 1-4 PRs.
- docs/how-to/extending-an-agent.md (use as the script).
- ~/Documents/kolsch/bundles/andre/agents/butler.yaml
  (currently the stop-gap drop-prompt form).

Tasks:

1. Restore butler.yaml to the `{% extends parent %}` form
   per the how-to's "Define your child agent" section.
   Content must match the how-to verbatim. If you need to
   deviate, the how-to is wrong → escalate to Step 4.

2. Run the build_resolved_config harness from the how-to.
   Confirm load + that butler inherits 4 tools, 2
   context_files, the bash-prompt permission.

3. Run a real chat session: `uv run kolsch chat
   andre/agents/butler`; send a tool-exercising prompt;
   capture JSONL. Verify the rendered system prompt shows
   parent prose + child role + child special_instructions
   + {{ agent.name }} → "butler".

4. Verify both helper.yaml copies (starter bundle +
   workspace) have the {% block %} placeholders.

5. File follow-up tickets as actual files under process/:
   - "Ship canonical kolsch agents as package resources."
   - "Symmetric {{ skills }} Jinja variable."
   - "Empty-context Jinja partial render" (only if Step 2
     hit the deferred-variable-evaluation escalation).

6. Closure writeup at
   process/bug-engine-extends-parent-not-rendered-step5-closure.md
   with JSONL excerpt + corpus pass status (all 24 ✓) +
   confirmation that the docs walkthrough worked
   unmodified.

7. PR description asks the user to follow
   extending-an-agent.md from their own session and
   confirm both that docs read clearly AND butler renders
   correctly. Reviewer verdict stays REVISE-PENDING-USER
   until confirmed.

ESCALATION:
- Docs need editing to match what the dev actually does →
  Step 4 inaccurate; revise Step 4.
- Rendered prompt missing content → Step 2 incomplete;
  revise Step 2.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5.

Checks:
1. butler.yaml matches the how-to verbatim. Divergence is
   either a doc bug (back to Step 4) or a silent rewrite
   (fail).
2. PR description has the JSONL excerpt showing parent +
   child composition in the system prompt event;
   {{ agent.name }} renders to "butler".
3. The dev followed the how-to as the script. PR names
   which sections were used at each task. Vague "I read
   the docs" is a fail.
4. Both helper.yaml copies have the {% block %}
   placeholders.
5. Follow-up tickets filed as files in process/.
6. User-confirmation request in PR; verdict
   REVISE-PENDING-USER until the user confirms.
7. Full suite green; goldens byte-equal.

Verdict: PASS (after user confirms), REVISE-PENDING-USER,
or FAIL.
```

---

## Part 6 — How to escalate

```markdown
### Escalation E-BUG-EXTENDS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–4
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-EXTENDS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds engine cannot reach the asset catalog at render
  time without a wider refactor.
- Step 1 finds render_with_parent's multi-level chain handling
  needs a recursive Jinja render with deferred variable
  evaluation that Jinja doesn't natively support.
- Step 2 finds preserving extends on the resolved config has
  schema knock-on effects (other code paths assumed extends
  was always None post-resolution).
- Step 3 finds any ✗ case still failing after Step 2 lands.
- Step 4 finds writing docs surfaces an unanswered design
  question (e.g., debugging-inheritance UX).
- Step 4 finds the lint pass (`grep -rn "kolsch:agents/" docs/`)
  has hits that aren't easily resolvable — there may be a
  doc location we haven't audited.
- Step 5's manual chat shows the rendered prompt is missing
  content (Step 2 incomplete).
- Step 5 finds the dev had to deviate from the doc-as-written
  to make butler work (Step 4 inaccurate — revise Step 4,
  don't patch in Step 5).

When NOT to escalate:
- Whether C5's error code is `config.extends_parent_without_extends_field`
  vs. some other name (pick consistent with errors/kernel.py
  conventions).
- Whether the fixture directory layout uses a/ b/ c/ subdirs
  or one flat dir with prefixed names.
- F5's message wording (any clearer version is fine; review
  for tone).

---

## Part 7 — Coordinator Escalation Log

### 2026-06-04 — Step 4 pre-condition cleared

**Source:** `qol-prefix-defaults-and-unified-refs.md` Step 4
closure.

**Status:** The Step 4 PRE-CONDITION at line 914 of this doc
("qol-prefix-defaults-and-unified-refs.md MUST be in main
before this step starts") is **satisfied as of Steps 1–4 of
that QoL**. Specifically:

- Step 2 shipped the new ref grammar — `parse_ref` accepts
  1 / 2 / 3-segment input with `field_asset_type` for
  disambiguation; `resolve_shorthand` resolves all four input
  forms against the catalog; `templating.context_files.resolve_path`
  defaults plain paths to `workspace:` and emits
  `config.deprecated_ref_form` Recoverable when the explicit
  `workspace:` prefix is used; the resolved schema validators
  for `extends:` and `context_files[].path` are permissive.
- Step 3 migrated the canonical examples — starter bundle
  `helper.yaml`, the user workspace copy, all `docs/` examples,
  the test catalog, and the CLI hardcoded refs (`src/cli/chat.py`
  `_DEFAULT_AGENT_REF`, help-text examples) all use the new
  2-segment form. `docs/reference/schemas.md` §A.8.1 carries
  the migration table.
- Step 4 (the closure step) ran the two manual e2e scenarios
  and confirmed both new-form and legacy-form agents load and
  execute end-to-end; the legacy form emits the expected
  `config.deprecated_ref_form` warning at agent-load. Closure
  writeup at
  `process/qol-prefix-defaults-and-unified-refs-step4-closure.md`.

**Resume instruction:** Step 4 of this bug is unblocked.
`docs/how-to/extending-an-agent.md` and the updates to
`docs/how-to/agent-templating.md` MUST use the new 2-segment
canonical ref form throughout (e.g. `extends: kolsch/helper`,
NOT `extends: workspace:bundles/kolsch/agents/helper.yaml`).
Reference `docs/reference/schemas.md` §A.8.1 from the new
how-to so readers learn the canonical form alongside the
inheritance feature.

---

## Part 8 — Out of scope (consolidated)

- Shipping canonical kolsch agents as package resources
  (`kolsch:agents/helper.yaml` becoming a real reference).
  Filed as Step 5 follow-up.
- Symmetric `{{ skills }}` Jinja variable for inline skills
  listing (replacing assembly.py:83-87 post-append). Filed
  as Step 5 follow-up.
- Additive composition (`extend:` / `+declared:`) at YAML
  field level. `{{ super() }}` inside Jinja blocks (Category
  K2) is the only additive path; RFC 7386 replace-only
  stays at the field level.
- Diamond / conditional inheritance.
- **Full** rewrite of `docs/how-to/agent-templating.md`. Step 4
  fixes the misleading examples and adds the missing
  subsections; a comprehensive tutorial overhaul is its
  own doc ticket.
- New how-to or tutorial coverage of inheritance debugging
  (rendering inspection, diff visualization) — if Step 4
  escalates around this, file a separate ticket.
- Phase 3 workflow-level inheritance.
- Translating the new `extending-an-agent.md` how-to into
  other formats (video, interactive REPL walkthrough).

---

## Part 9 — Related work

- **Trigger conversation**: butler.yaml debugging session in
  the same-conversation Phase 2.2 audit.

- **HARD DEPENDENCY (blocks Step 4)**:
  `qol-prefix-defaults-and-unified-refs.md` — that QoL
  changes the canonical ref convention (no `/agents/`,
  `/tools/`, `/skills/` middle segment; `workspace:` prefix
  optional). Step 4's docs MUST use the new convention.
  Sequence: ship that QoL, then start this Step 4. The
  inheritance fix's Steps 1-3 can proceed in parallel with
  the QoL; only Step 4 is blocked.

- **Soft sibling specs from same conversation** (no hard
  dependency, but coordinate):
  - `qol-yaml-version-fields-optional.md` — once shipped,
    fixtures in Step 3 can omit `schema_version` and
    `assembly_version`. Cleaner; not required.
  - `bug-model-line-placeholder-collides-with-jinja.md` —
    independent; mention in Step 4 docs if the doc covers
    `kolsch setup` model selection at all.

- **Sibling Phase 2.2 audit findings**:
  - Skills listing post-append asymmetry — file as separate
    qol-* ticket per Step 5 follow-ups.
  - `kolsch:` resolution doesn't reach agents — same.

- **Boundary classifier work**:
  `bug-platform-internal-error-boundary-swallow.md` Draft 0.3
  is the parent pattern for "engine-side wiring missing
  despite working helper module." This bug follows the same
  shape.

- **QoL pressure**:
  `qol-tool-output-truncation-and-persistence.md` shares the
  prompt-rendering surface (read_file description tweaks).
  No fix dependency, but the docs work in Step 4 may overlap.

- **Fixture corpus reuse**: the 24-case Part 4 corpus could
  serve future Phase 2.2 envelope work (e.g., when
  context_files chaining needs validation). Keep the
  parametrized runner generic.
