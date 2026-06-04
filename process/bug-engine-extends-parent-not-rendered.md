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

Step 1 — Verify the design picks before any production code.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-engine-extends-parent-not-rendered.md Parts 0-4.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/templating/inheritance.py (apply_inheritance, resolve_extends_chain, render_with_parent).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/templating/render.py (render_prompt — the no-loader environment).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/engine.py:667-805 (_render_prompt_if_needed).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/asset_manager.py:130-170 (chain resolution + extends stripping).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/assembly.py:48-90 (skills_listing post-append).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/docs/how-to/agent-templating.md:225-320.

Verification tasks:

1. Confirm the wiring gap.
   - Trace from agent.config.prompt (with `{% extends parent %}`)
     to render_prompt (no loader). Cite the exact lines.
   - Confirm apply_inheritance strips extends from every layer
     (asset_manager.py:155). Cite.
   - Confirm render_with_parent is not called anywhere except
     tests. grep for it; document.

2. Pick the dispatch design (Option A vs B from Part 2).
   - Default recommendation: A (preserve leaf's extends;
     engine re-walks the catalog at render time).
   - Confirm engine has access to the asset catalog at render
     time. Trace from `_render_prompt_if_needed` to
     `self.engine._assets._catalog` or equivalent — what's the
     accessor for "give me the parent agent's prompt source"?
   - If catalog access from the engine is awkward, fall back
     to Option B (derive parent_prompt_source on the resolved
     config). Document the call.

3. Pin the asset_manager.py change.
   - Recommended: keep stripping `extends` from non-leaf
     layers (those have already been resolved during chain
     walk); but for the LEAF (index 0 in agent_partials_chain
     per :154-157), preserve the extends value.
   - Confirm by reading whether the resolver's downstream
     merge would otherwise re-apply the stripped extends.
     If the resolver iterates and re-strips, the asset manager
     fix needs to be at a different point.

4. Pin the engine change.
   - At `_render_prompt_if_needed`, after building the
     context dict (line ~787-793):
       if self.config.extends is not None:
           parent_source = await self._load_parent_prompt_source()
           rendered = render_with_parent(
               child_template_string=prompt,
               parent_template_string=parent_source,
               context=context,
               template_name=f"agent:{self.config.agent_id}",
           )
       else:
           rendered = render_prompt(prompt, context, template_name=...)
   - Sketch the `_load_parent_prompt_source` helper. What
     does it call? Likely:
       parent_entry = self.engine._assets.catalog.get(self.config.extends)
       parent_partial = load_agent_definition(parent_entry.path)
       return parent_partial.get("prompt", "")
   - For multi-level chains (E1-E2), the parent itself may
     have its own `extends:`. Does the helper need to walk
     the chain, or does Jinja handle this naturally if we
     also load grandparents into the DictLoader? Decide.

5. Pin C5's error class.
   - Case C5: child uses `{% extends parent %}` but has no
     `extends:` field. Today: TypeError from Jinja.
   - Step 2's dispatch falls into the else-branch (no
     extends → render_prompt → still no loader → still
     TypeError).
   - Recommend: add a pre-render check: if the prompt
     contains `{% extends parent %}` AND `self.config.extends`
     is None → raise a typed PlatformError.
     Pick the code: new (`config.extends_parent_without_extends_field`)
     OR reuse `config.template_render_failed` with a specific
     detail. Recommend new code; clearer for the author.
   - Document the pick.

6. F5 message improvement (optional, per Part 7).
   - Today: `kolsch:agents/helper.yaml` → "parent agent ... not found"
     with the resolved package path.
   - Decide whether Step 2 amends the message to say
     "kolsch: resolves against the installed package which
     ships only docs/ and AGENTS.md; use workspace: for
     agent YAMLs in your bundle." Lock yes/no in the writeup.

7. Multi-level chain handling for render_with_parent.
   - Today: render_with_parent loads ONE parent into DictLoader.
   - If parent has its own extends (Category E1 — A extends B
     extends C, with blocks defined in C), the parent's
     prompt contains `{% extends parent %}` too. Does the
     current render_with_parent support this?
   - Trace by reading. If not, document the multi-level fix:
     load EVERY ancestor into the DictLoader under names
     `parent`, `grandparent`, etc. OR re-bind `parent` per
     level via recursive render. Pick one.
   - Recommend: the simpler approach is the asset manager
     pre-flattens the chain at apply_inheritance time:
     walks the chain, recursively renders each layer against
     its own parent, producing a single "flat parent prompt
     source" that the leaf sees. The leaf calls
     render_with_parent against that flattened source.
     Document the call.

8. Corpus fixture layout.
   - Pin the directory layout: tests/fixtures/extends/
     with subdirs a/, b/, c/, d/, e/, f/, g/, h/, i/, k/.
   - Each fixture is a minimal YAML; for cases that need
     a parent, include both files in the same subdir.
   - Pin the fixture-load helper signature for Step 3 (a
     function that takes a category + case id and returns
     a temp workspace with the fixtures planted, ready for
     build_resolved_config).
   - Document.

9. STOP and post the writeup + fixture skeleton (empty
   subdirs + a README listing case names per Part 4). No
   production code, no fixture content.

ESCALATION PATH: if Step 1 reveals that:
 (a) Engine cannot access the asset catalog at render time
     without a wider refactor (Option A becomes infeasible),
 (b) render_with_parent's DictLoader-of-one approach does
     not support multi-level chains and the recursive
     pre-flatten is non-trivial,
 (c) the resolved config's extends value can't be preserved
     without a Pydantic schema change that touches multiple
     callers,
escalate before proceeding.

Acceptance: writeup posted; fixture skeleton ready;
coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. The writeup + fixture skeleton are the
deliverable.

Checks:

1. The wiring gap is cited with file:line in three places
   (render.py no-loader; engine.py:795 always-render_prompt;
   asset_manager.py:155 extends-stripped).

2. Option A vs B is decided with rationale. If A, the
   engine-side `_load_parent_prompt_source` accessor is
   sketched concretely (which catalog object, which method).
   If B, the schema change is justified.

3. The leaf-preservation strategy for asset_manager is
   concrete: "keep extends on agent_partials_chain[0],
   strip from the rest" or equivalent.

4. C5's error class is decided (new code vs. reused).
   "TBD" is a fail.

5. F5 message decision is locked (yes/no on amending).

6. Multi-level chain handling is decided. The two options
   (load-all-ancestors-into-DictLoader vs.
   asset-manager-pre-flattens) are not both fine; pick one.
   If the writeup says "we'll figure it out in Step 2,"
   fail.

7. Fixture directory layout is concrete (paths, file naming
   convention).

8. All three escalation conditions are confirmed
   not-an-issue with citations or escalated.

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

Tasks (in this order):

1. Asset manager — preserve leaf's extends.
   - At src/assets/asset_manager.py around line 154-157:
     change the loop that strips extends from each layer to
     preserve it on the LEAF (index 0 of agent_partials_chain)
     while stripping from non-leaf layers.
   - The leaf preservation must happen BEFORE the chain is
     reversed for the kernel merge (or coordinate with
     wherever the resolved config gets the field).
   - Confirm the resolved config's `extends` field carries
     the original `extends:` value from the child YAML.

2. Asset manager — multi-level chain pre-flatten (if Step 1
   picked the pre-flatten approach).
   - If the leaf's prompt source contains `{% extends parent %}`
     AND the chain has more than one parent, compute a
     "flattened parent" prompt source by recursively rendering
     each layer against its own parent using render_with_parent
     called with an EMPTY context (block resolution only,
     deferred variable evaluation).
   - This is the hard part. If Jinja's render-with-empty-context
     doesn't actually defer variable evaluation (it errors on
     StrictUndefined for `{{ agent.name }}`), fall back to the
     load-all-ancestors approach (Step 1's alternative).
   - Document the chosen path in the PR description.

3. Engine — dispatch at _render_prompt_if_needed.
   - In src/engine/engine.py around line 793-799:
     - Before the `rendered = render_prompt(...)` line, add:
         if self.config.extends is not None:
             parent_source = await self._load_parent_prompt_source()
             rendered = render_with_parent(
                 child_template_string=prompt,
                 parent_template_string=parent_source,
                 context=context,
                 template_name=f"agent:{self.config.agent_id}",
             )
         else:
             rendered = render_prompt(prompt, context, ...)
   - Add the `_load_parent_prompt_source` async helper as a
     method on the exchange (or inline if simple):
       async def _load_parent_prompt_source(self) -> str:
           parent_ref = self.config.extends  # e.g., "workspace:bundles/X/agents/Y.yaml"
           # Resolve via catalog accessor Step 1 pinned.
           ...
           # Return the parent's `prompt` field source string.

4. Engine — C5 detection (no extends but uses {% extends parent %}).
   - Before falling into the else-branch render_prompt call,
     check: if `prompt` contains the literal string `{% extends parent %}`
     (or any `{% extends `) AND `self.config.extends is None`,
     raise the typed PlatformError Step 1 picked.
   - The check can be a simple substring match; false
     positives from comments are acceptable (a comment containing
     `{% extends parent %}` is weird enough that an error is
     fine).

5. F5 message (if Step 1 picked yes).
   - Amend the ConfigParentAgentNotFound error to include
     a hint about prefix when the original ref was
     `kolsch:agents/...`.
   - File location: src/errors/kernel.py (or wherever the
     class lives).

6. Unit tests in tests/unit/test_engine_extends_dispatch.py:
   - test_extends_none_uses_render_prompt: a config with
     `extends=None` and a plain prompt → render_prompt path.
   - test_extends_set_uses_render_with_parent: a config with
     `extends="workspace:..."` and a `{% extends parent %}`
     prompt → render_with_parent path (mock the catalog to
     return a known parent source; assert the rendered output
     reflects parent + child content).
   - test_extends_parent_without_extends_field_raises:
     prompt has `{% extends parent %}` but extends=None →
     the C5 error.

7. NO change to:
   - The Framework protocol or implementations.
   - The boundary classifier work (separate ticket).
   - The skills_listing post-append at assembly.py:83-87.
   - render_prompt itself.
   - render_with_parent itself (the helper works; we're
     wiring it).

Scope: ~120 LOC across asset_manager + engine + errors;
~80 LOC tests.

ESCALATION PATH: if Jinja's render-with-empty-context for
the pre-flatten path errors instead of deferring variable
evaluation, escalate. The fallback (load-all-ancestors) is a
larger change.

Acceptance: unit tests pass; the trigger case (butler with
{% extends parent %}) renders cleanly when manually probed
via the same Python harness used in Step 1.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. asset_manager.py preserves the leaf's extends. Verify by
   reading the strip loop. The change is a single index-0
   carve-out, not a broader rewrite.

2. The engine's dispatch branch reads
   `self.config.extends is not None` — NOT a substring
   check on the prompt. Heuristic-on-prompt dispatch is
   fragile (comments could trigger). The schema field is
   the source of truth.

3. _load_parent_prompt_source resolves the parent via the
   catalog accessor Step 1 named. Verify by tracing.

4. C5 detection FIRES BEFORE falling into render_prompt.
   Order: extends-set branch → C5 check on else-branch →
   render_prompt. Reverse order would let C5 hit Jinja and
   produce TypeError again.

5. The C5 error class is the one Step 1 picked. New code
   path consistent with errors/kernel.py conventions.

6. F5 message: matches Step 1's decision. If "no," no
   changes to the error class are present.

7. Multi-level chain handling matches Step 1's pick.
   No surprise alternative.

8. NO changes outside the documented scope. Specifically:
   - render_prompt unchanged.
   - render_with_parent unchanged.
   - assembly.py unchanged.
   - Engine boundary classifier unchanged.

9. Unit tests cover the three documented paths (no-extends,
   extends-set, C5).

10. Run the full test suite. tc_eng_* goldens still
    byte-equal (the dispatch is additive; existing paths
    unchanged).

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

1. Create tests/fixtures/extends/ with the 10 category
   subdirs (a/, b/, c/, d/, e/, f/, g/, h/, i/, k/).
   Step 1 already scaffolded the dir skeleton; this step
   populates each fixture.

2. Author each fixture per Part 4's case table. Each fixture
   is a minimal YAML expressing the case's intent. Keep
   fixtures SHORT (the corpus is large; per-fixture verbosity
   makes the suite unreviewable).

   Examples:
     # tests/fixtures/extends/c/c1_parent.yaml
     schema_version: "0.1"
     assembly_version: "1"
     agent_id: ag_c1_parent
     name: c1_parent
     model: { provider: opencode, model: qwen3.6-plus }
     io: { mode: conversational }
     prompt: |
       You are {% block role %}defaultrole{% endblock %} for testing.

     # tests/fixtures/extends/c/c1_child.yaml
     schema_version: "0.1"
     assembly_version: "1"
     agent_id: ag_c1_child
     name: c1_child
     extends: workspace:bundles/test/agents/c1_parent.yaml
     io: { mode: conversational }
     prompt: |
       {% extends parent %}
       {% block role %}filledrole{% endblock %}

3. Build tests/integration/test_extends_corpus.py:

   The test parametrizes over the 24 cases. Each case
   declares:
     - case_id (e.g., "c1")
     - category (e.g., "c")
     - fixture_files: list of YAMLs to install in the temp
       workspace.
     - target_agent_ref: which one to build_resolved_config for.
     - expected_outcome: "loads_with_render" | "loads_with_error" |
       "error_at_load_<code>"
     - expected_render_contains: list[str] (substrings the
       rendered prompt must contain) — only for loads_with_render
     - expected_render_excludes: list[str] (substrings that
       must NOT appear) — only for loads_with_render
     - expected_error_code: str (e.g., "config.required_block_missing")
       — only for error_at_load_*

   Test runner per case:
     - Builds a tmp workspace with the fixture files at the
       declared paths.
     - Bootstraps the runtime.
     - Calls build_resolved_config(target_agent_ref).
     - If expected_outcome starts with "error_at_load":
       - assert that ConfigInvalid (or the named exception
         class) raises with the expected code in
         detail.validation_errors.
     - If expected_outcome is "loads_with_render":
       - Drive the prompt through the engine's render path
         (or invoke a thin helper that does just the render
         step with a mock context).
       - Assert each expected_render_contains substring
         appears.
       - Assert each expected_render_excludes substring does
         NOT appear.
     - If expected_outcome is "loads_with_error" (rare):
       - assert the load succeeds AND a specific event was
         emitted (e.g., agent.template_warning for H1).

   The case table itself is a Python module
   (tests/integration/_extends_corpus_cases.py) so tests
   parametrize over the same list the docs reference. The
   IDs (C1, C2, etc.) MUST match Part 4's IDs verbatim.

4. Walk the case table. For each case:
   - If it's in the ✓ regression-guard column: confirm test
     passes (this is the regression check).
   - If it's in the ✗ column: confirm test passes AFTER
     Step 2's wiring (Step 2's tests covered the engine
     side; this step covers the fixtures-as-source path).
   - If it's in the ⚠ column: confirm the test pins
     whatever current behavior IS, with a comment naming
     that we're documenting reality.

5. Pre-fix verification — sampling.
   - git-stash Step 2's code; run a sampling: TC C1, C2, K1, K2.
     All should FAIL with the documented pre-fix failure mode
     (TypeError no loader, or empty render output).
   - Document in the PR description for each sampled case:
     pre-fix failure message + post-fix success.

6. Add the 24 cases to process/test-catalog.md under a new
   "Engine Extends Corpus (EXT-CORPUS)" section. Use the
   ID convention (TC-EXT-A1, TC-EXT-B1, etc.). One row per
   case with intent + expected.

7. NO production code changes. If any case reveals a Step 2
   gap, escalate — don't patch Step 2 silently from Step 3.

Scope: 24 fixture YAMLs (small, ~10-30 lines each) +
~300 LOC test runner + ~150 LOC case table + catalog entries.

ESCALATION PATH: if any case in the ✗ column STILL fails
after Step 2's wiring landed, Step 2 is incomplete.
Escalate with the failing case ID + the captured failure
output.

Acceptance: all 24 tests pass on post-fix code; pre-fix
sampling documented; catalog updated.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. tests/fixtures/extends/ has all 24 fixtures organized by
   category subdir. Spot-check: open three random fixtures
   from different categories; confirm shape matches Part 4.

2. The case table (tests/integration/_extends_corpus_cases.py)
   lists ALL 24 case IDs from Part 4 with consistent naming.
   Missing any ID is a fail.

3. The test runner walks the corpus and runs each case
   without skipping. Confirm by reading the parametrize
   decorator + the runner body.

4. Pre-fix verification: PR description shows at least
   3-4 cases failing pre-Step-2-code and passing post.
   The pre-fix failure messages match the "Currently"
   column from Part 4 (✗ cases produce loader errors;
   ✓ cases work both pre and post; ⚠ cases pin current
   behavior).

5. The ⚠ cases (C5 and G3 per Part 4) are pinned to
   documented behavior. Comments in the test explain what
   was decided in Step 1.

6. process/test-catalog.md gained the EXT-CORPUS section
   with all 24 entries.

7. Full test suite green. tc_eng_* goldens still
   byte-equal.

8. NO production-code changes outside src/ since Step 2.

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
- Steps 1-3 PRs (especially Step 3 — the fixture corpus
  is your example source).
- qol-prefix-defaults-and-unified-refs.md (the convention
  the docs use).
- docs/how-to/agent-templating.md (current state — the
  misleading file).
- docs/reference/schemas.md around lines 60-100 (where
  agent envelope fields are documented) and 290-340
  (composition order + prefix resolution).
- docs/how-to/ as a directory — get a sense of the
  established how-to voice + length.
- docs/index.md and docs/how-to/README.md (if it exists)
  for how-to listings.

Tasks:

1. Fix docs/how-to/agent-templating.md.
   - Find every occurrence of `kolsch:agents/...` in the
     file (grep). Each is misleading.
   - Replace the canonical example (currently around
     lines 251-262 + 275-284) with the working
     `workspace:bundles/kolsch/agents/helper.yaml` form.
     The YAML body should match Step 3's C1 fixture so
     the doc and the test prove the same thing.
   - Add a "Resolving the parent reference" subsection
     (200-400 words) explaining:
     - The two prefix forms (workspace:, kolsch:).
     - That `kolsch:` resolves against the installed
       kolsch package (which currently ships only
       docs/ + AGENTS.md per src/kolsch/__init__.py),
       so `kolsch:agents/...` is documented but not yet
       realised. Point at the Step 4 follow-up ticket
       for shipping canonical agents as package resources.
     - Recommend `workspace:bundles/<bundle>/agents/<name>.yaml`
       as the only currently-working form for cross-bundle
       agent inheritance.

   - Add a "Common gotchas" subsection:
     - C3 (stray text in child that uses extends gets
       silently discarded): show an example, name the
       behavior, link the C3 fixture.
     - C5 (using `{% extends parent %}` without an
       `extends:` field): show the error code Step 2 picked.
     - F5 (`kolsch:` ref to a non-existent kolsch-package
       resource): show the error message + the fix.

   - Add a "Compose parent content with `{{ super() }}`"
     subsection covering K2. Concrete example:
       parent: {% block role %}helper{% endblock %}
       child:  {% block role %}{{ super() }} + butler{% endblock %}
       → renders as "helper + butler".
     Explain this is the ONLY additive composition kolsch
     supports — at the YAML field level everything is
     RFC 7386 replace-only. Link the K2 fixture.

   - Add a "Multi-level chains" subsection covering E1.
     Use the same three-file structure E1's fixture uses;
     mention that intermediate parents can also have blocks.

2. Author NEW docs/how-to/extending-an-agent.md.
   - Target length: ~150-250 lines.
   - Pattern: focused how-to, butler as the running example.
   - Structure:
     ## Pick a parent
     ## Decide what to override
       - Pattern A: inherit prompt verbatim (drop `prompt:`
         from child) — show B1 fixture
       - Pattern B: write a fresh prompt (no `{% extends parent %}`)
         — show B4 fixture
       - Pattern C: extend the parent's template + fill
         blocks — show C1 fixture (this is the canonical
         pattern; spend the most words here)
     ## Define your child agent
       (full butler.yaml shown verbatim — must match the
       Step 5 restoration)
     ## Verify it loads
       Show the Python harness from this conversation:
       `await runtime.build_resolved_config('andre/agents/butler')`
     ## Run a chat session against it
       Show `uv run kolsch chat andre/agents/butler` + what
       to look for in the rendered system prompt event in
       the session JSONL.
   - Link FROM agent-templating.md TO this new how-to as
     "for a step-by-step walkthrough, see Extending an
     Agent."

3. Update docs/reference/schemas.md.
   - At the `extends` field docs (around line 68-72 + 311):
     - Note that the value is preserved on the resolved
       config (post-Step-2 behavior). Engine uses it at
       render time to look up the parent prompt source.
     - Cross-reference the new how-to.
   - At the prefix resolution table (around line 311):
     - Add a note that `kolsch:` reaches resources packaged
       inside the installed kolsch package — currently
       docs/ and AGENTS.md only, per
       src/kolsch/__init__.py's force-include.
     - Add a sentence to the `workspace:` row reinforcing
       that workspace-bundle agents are the only currently-
       reachable extends target.

4. Update docs/index.md (if it has a how-to listing).
   - Add the new how-to under the agent-authoring section.

5. EVERY example in the new docs MUST correspond to a
   Step 3 fixture. If you find yourself writing a new
   example that has no matching fixture, either:
   - Add a fixture to Step 3's corpus (escalate — Step 3
     is in main; adding a fixture is a small follow-up
     PR), OR
   - Cite an existing fixture and rework the example to
     match it.
   Doc-fixture divergence is the recurring failure mode
   this step eliminates.

6. Run a docs lint pass:
     grep -rn "kolsch:agents/" docs/
   should return ZERO matches after this step. Any
   remaining occurrence is either:
   - In a "this doesn't work yet" call-out (acceptable;
     comment must explicitly mark it), OR
   - An oversight (fix).

7. NO production code changes. NO new fixtures (those
   land in Step 3 or escalate). NO bundle YAML changes
   (those land in Step 5).

Scope: ~200 LOC docs added/changed across three files +
one new how-to file.

ESCALATION PATH: if writing the docs reveals an
unanswered design question (e.g., "what's the actual
intended UX for inheritance debugging?"), escalate.
Don't ship docs that paper over a real uncertainty.

Acceptance: agent-templating.md is honest; the new how-to
exists; schemas.md is consistent; the lint pass is clean.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 (Docs).

Checks:

1. grep -rn "kolsch:agents/" docs/ returns zero hits OR
   only explicitly-marked "not yet implemented" examples.
   Any unmarked kolsch:agents/ reference is a fail.

2. EVERY YAML example in the touched docs corresponds to
   a fixture under tests/fixtures/extends/ from Step 3.
   Spot-check four examples (one from each major doc):
     - agent-templating.md canonical example → C1 fixture
     - agent-templating.md {{ super() }} example → K2 fixture
     - extending-an-agent.md walkthrough → at least C1, B1, B4
     - schemas.md prefix example → matches I1 or B1 fixture
   Doc-fixture mismatch is a fail.

3. docs/how-to/extending-an-agent.md exists, has the
   "## Pick a parent / Decide what to override / Define
   your child agent / Verify it loads / Run a chat session"
   structure, and uses butler as the worked example. The
   butler YAML in the doc MUST match the form Step 5
   will restore in ~/Documents/kolsch/bundles/andre/agents/butler.yaml.

4. docs/reference/schemas.md notes the post-Step-2
   behavior on the `extends` field (preserved on the
   resolved config). If the dev left it saying "stripped
   during composition" — fail; that's the pre-Step-2 lie.

5. "Common gotchas" subsection in agent-templating.md
   covers C3, C5, F5 explicitly with the error codes /
   messages Step 2 produced.

6. "Compose parent content with {{ super() }}" subsection
   has a runnable K2-equivalent example. Verify by reading
   K2's fixture file alongside the doc example.

7. "Multi-level chains" subsection has an E1-equivalent
   example (three files: grandparent, parent, child).

8. docs/index.md (or the how-to listing) links the new
   how-to. Missing link is a fail — undiscoverable docs
   are nearly as bad as missing docs.

9. The how-to is the recommended length (~150-250 lines).
   Substantially shorter usually means content was elided;
   substantially longer usually means scope crept.

10. NO production code changes. git diff src/ should be
    empty.

11. The mkdocs/sphinx build (or whichever doc build the
    project uses) succeeds. Render the touched files
    locally before approving.

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
- docs/how-to/extending-an-agent.md (Step 4's new tutorial
  — you're going to use IT as the script for this step).
- bundles/andre/agents/butler.yaml in ~/Documents/kolsch
  (currently the stop-gap drop-prompt form per the
  same-conversation work).

Tasks:

1. Follow docs/how-to/extending-an-agent.md as written.
   - Restore butler.yaml to the `{% extends parent %}` form
     per the how-to's "Define your child agent" section.
   - The butler.yaml content MUST match what the how-to
     shows verbatim. If the dev finds they need to deviate,
     the how-to is wrong → escalate to Step 4.

2. Verify it loads (the how-to's "Verify it loads" section
   is your script).
   - Run the build_resolved_config harness shown in the
     how-to. Confirm no errors.
   - Confirm: 4 tools inherited, 2 context_files inherited,
     the bash-prompt permission inherited.

3. Run a real chat session per the how-to's "Run a chat
   session against it" section.
   - uv run kolsch chat andre/agents/butler
   - Send a prompt that exercises butler:
     "list files in src/engine and explain what each is for"
   - Capture session JSONL.
   - Verify in the system prompt event:
     a. Parent's prose present (tool selection guide,
        conciseness rule).
     b. Child's role filled (text says "butler" not
        "helper agent").
     c. Child's special_instructions filled.
     d. {{ agent.name }} → "butler".

4. Confirm canonical helper has the blocks.
   - src/cli/_starter_bundle/kolsch/agents/helper.yaml has
     the {% block role %} and {% block special_instructions %}
     placeholders (per same-conversation work).
   - User's workspace ~/Documents/kolsch/bundles/kolsch/agents/helper.yaml
     similarly.

5. Follow-up tickets to file (each as a small process/
   doc):
   - "Ship canonical kolsch agents as package resources"
     — needed for kolsch:agents/... to reach agents
     end-to-end. Reference Step 4's call-out in
     agent-templating.md.
   - "Symmetric {{ skills }} Jinja variable" — addresses
     the assembly.py:83-87 asymmetry surfaced in Category G.
   - "Empty-context Jinja partial render" — only if
     Step 2 hit the escalation around deferred variable
     evaluation.

6. Closure writeup at
   process/bug-engine-extends-parent-not-rendered-step5-closure.md:
   - JSONL excerpt with the rendered system prompt
     showing parent + child composition (post-fix).
   - Corpus pass status: all 24 ✓.
   - The docs walkthrough was followed without modification
     — confirms doc-reality match.
   - Pointers to the follow-up tickets filed in task 5.

7. Hand off to the user. The PR description MUST:
   - Ask the user to follow docs/how-to/extending-an-agent.md
     from a fresh chat session.
   - Ask them to confirm both that the docs read clearly AND
     that butler renders the inherited content.
   - Note that the reviewer's verdict stays REVISE-PENDING-USER
     until the user confirms.

Scope: small bundle file edits (butler.yaml restore) +
manual report + follow-up tickets. NO new production code,
NO new docs (those landed in Step 4).

ESCALATION PATH:
- If the docs need editing to match what the dev
  actually has to do, Step 4 was inaccurate → revise
  Step 4, don't patch in Step 5.
- If the manual chat shows the rendered prompt is missing
  content, Step 2's wiring missed a case → revise Step 2.
- If the user, after confirming, reports a remaining
  papercut not in any current ticket, file a fresh one.

Acceptance: butler renders correctly via the steps in
docs/how-to/extending-an-agent.md; user is asked to
confirm; follow-up tickets filed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5.

Checks:

1. butler.yaml in ~/Documents/kolsch matches the form shown
   in docs/how-to/extending-an-agent.md verbatim. If they
   diverge, either the doc is wrong (back to Step 4) or the
   dev silently rewrote the YAML mid-step (fail).

2. Manual chat: PR description has a JSONL excerpt showing
   the resolved system prompt event with:
   - Parent prose AND child block content visible.
   - {{ agent.name }} rendering to "butler".
   No JSONL excerpt → fail.

3. The dev FOLLOWED Step 4's how-to as written. The PR
   description names which sections of the how-to were
   used at each task. Vague "I read the docs" is a fail —
   the discipline is doc-as-script.

4. Both copies of helper.yaml (starter bundle AND
   workspace) have the {% block %} placeholders. Verify
   by reading.

5. Follow-up tickets are FILED as actual files under
   process/ (not just listed in the PR description).

6. The user-confirmation request is in the PR description.
   The reviewer's verdict is REVISE-PENDING-USER until
   the user confirms via a real chat session AND
   confirms the docs read cleanly.

7. Full test suite green. Goldens byte-equal.

Verdict: PASS (only after user confirms), REVISE-PENDING-USER,
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
