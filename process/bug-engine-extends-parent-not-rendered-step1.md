# bug-engine-extends-parent-not-rendered.md — Step 1 writeup

**Status:** APPROVE-TO-PROCEED (pending reviewer).
**Scope:** verification + design picks; NO production code.
**Reviewable in:** ~30 min.

This writeup discharges the six tasks the Step 1 developer prompt
named, in order. Tasks 1–5 are concrete enough that Step 2 can
implement directly from the file:line citations and chosen
designs; Task 6 sketches the fixture layout Step 3 will populate.

---

## Task 1 — Cite the wiring gap (file:line ×3)

The bug is a three-point wiring gap. All three sit in
production code paths today; the helper module that knows how
to handle inheritance at render time is real and tested but
never invoked by the engine.

### Gap 1 — `render.py` builds a loader-less Jinja environment

`src/templating/render.py:71`:

```python
env = Environment(undefined=StrictUndefined, autoescape=False)
```

No `loader=` argument. `{% extends parent %}` requires Jinja
to resolve "parent" through a loader; with no loader the parse
itself succeeds, but at render time Jinja raises
`TypeError: no loader for this environment specified`. This is
the literal exception observed in the bug-report trigger case
(Part 0 of the spec).

### Gap 2 — Engine's prompt-render path always routes through `render_prompt`

`src/engine/engine.py:795`:

```python
rendered = render_prompt(
    prompt,
    context,
    template_name=f"agent:{self.config.agent_id}",
)
```

This is the only render call from `_render_prompt_if_needed`
(line 667 onward). There is no branch for the inheritance
case — every agent, with or without `extends:`, goes through
`render_prompt` and therefore through the no-loader env.
`render_with_parent` (the helper at
`src/templating/inheritance.py:208`) is never reached.

### Gap 3 — Asset manager strips `extends:` from every layer of the chain

`src/assets/asset_manager.py:262-265`:

```python
agent_partials_chain = [
    {k: v for k, v in layer.items() if k != "extends"}
    for layer in extends_chain
]
```

The comprehension applies to every element of `extends_chain`,
including index 0 (the leaf). By the time the partial reaches
`resolve_and_validate`, the leaf's `extends:` is gone. The
resolved `ResolvedAgentConfig.extends` is `None` even when the
agent declared one. The engine therefore has no signal it can
test at line 795 to decide which render path to take.

(The spec's developer prompt cites `asset_manager.py:155`; that
was a stale line number — current code lives at 262-265 after
the qol-prefix-defaults-and-unified-refs.md refactor. Same
behaviour, different address.)

#### Adjacent observation — `apply_inheritance` is dead production code

`src/templating/inheritance.py:179` also strips `extends:` from
every layer, but `apply_inheritance` is only called from
`tests/component/templating/test_inheritance.py:117+`. The
asset manager inlined the merge at line 275-280 rather than
calling `apply_inheritance`. So Gap 3 is fully inside
`asset_manager.py`; we do not need to touch `apply_inheritance`
to fix this bug. (Step 2 may want to either delete
`apply_inheritance` or update its docstring to reflect that it
is no longer the production composer; out of scope for Step 1.)

---

## Task 2 — Dispatch design pick: **Option A**

**Pick:** Option A — preserve the leaf's `extends:` on the
resolved config; engine re-walks the catalog at render time to
load the parent prompt source.

**Why not B (derive `parent_prompt_source` on the resolved
config):** Pollutes `ResolvedAgentConfig` with an
implementation-detail field that no consumer outside the engine
needs. The schema is the agent surface, not a place to stash
engine-internal state. Option A keeps schema clean.

**Why not C (asset-manager pre-render):** Needs partial Jinja
render (envelope vars deferred, blocks resolved). Jinja doesn't
support that natively, and the rejection rationale in Part 2 of
the spec stands.

### Catalog accessor for Option A

The engine already holds `self.engine._assets` (the
`AssetManager`). Two public read-only accessors are available:

- `AssetManager.catalog()` — public **method** (NOT a property;
  no `@property` decorator) at `src/assets/asset_manager.py:96`.
  Returns the `AssetCatalog` instance. Call site must use
  parentheses: `self.engine._assets.catalog()`.
- `AssetManager.get_asset(ref, field_asset_type="agent")`
  (`src/assets/asset_manager.py:161+`) → resolves a ref to an
  `AssetEntry` whose `.path` is the on-disk YAML path.

Reach via the `_assets` handle is already proven —
`_render_prompt_if_needed` reads `self.engine._assets._root`
at `engine.py:738` to compute `workspace_root`. That accesses
a private attribute, not `catalog()`; Step 2 will be the first
production call site for `catalog()` from inside the engine.
But the handle is in scope, the method is public, and other
modules (e.g. authz provider construction) call it today.
Catalog reach is confirmed; no engine-side refactor required.

Step 2's render-time helper will be a thin wrapper:

```python
# src/engine/engine.py, helper inside _render_prompt_if_needed
async def _load_parent_prompt_source(self) -> str:
    extends_ref = self.config.extends
    catalog = self.engine._assets.catalog()   # method call, not attribute
    workspace_root = Path(self.engine._assets._root)
    # Reuse the resolver helper from templating.inheritance.
    parent_path = _resolve_extends_to_path(
        extends_ref, catalog, workspace_root,
    )
    payload = yaml.safe_load(parent_path.read_text(encoding="utf-8"))
    return payload.get("prompt", "")
```

`_resolve_extends_to_path` already exists at
`src/templating/inheritance.py:70`; Step 2 just imports it.
Multi-level handling is Task 5 below.

---

## Task 3 — Pin the asset_manager change

**Concrete edit:** `src/assets/asset_manager.py:262-265`.

Change the comprehension to preserve `extends:` on the leaf
(index 0 of `extends_chain` — `resolve_extends_chain` returns
leaf-first per
`src/templating/inheritance.py:119-127`):

```python
agent_partials_chain = [
    layer if i == 0
    else {k: v for k, v in layer.items() if k != "extends"}
    for i, layer in enumerate(extends_chain)
]
```

The leaf's `extends:` value flows through the chain composition
at lines 275-280 (which iterates `reversed(agent_partials_chain)`
and `composed.update(layer)`; the leaf is the LAST update so
its `extends:` wins regardless of what intermediate layers had).

### Downstream-merge re-strip check

Walked every site that consumes `extends:`:

- `src/schemas/config.py:206` — `ResolvedAgentConfig.extends:
  str | None = None`. Field already exists; preserving it just
  means it carries the leaf's value instead of always-`None`.
- `src/kernel/partials.py:103` — `PartialAgentConfig.extends:
  str | None = None`. Permissive validator (line 107-118)
  accepts any non-empty string; no re-strip.
- `src/assets/assembler.py:84` — `pre_resolve_prompts` only
  touches the `prompt` key (line 101); does NOT iterate or
  strip `extends:`.
- `assemble_partials` (kernel) does last-wins per-field merge;
  `extends:` is a permissive field with no merger override.
- `grep -rn "config\.extends" src/ tests/` returns ZERO hits
  outside the schema/validator/catalog files. No downstream
  consumer asserts `extends is None` post-resolution; we are
  safe to populate it.

### Behavioural test for Step 2 to add

```python
# tests/unit/test_asset_manager_extends_preservation.py
def test_resolved_config_preserves_leaf_extends(...):
    # given an agent with extends: foo/bar
    cfg = await mgr.build_resolved_config("baz/quux")
    assert cfg.extends == "foo/bar"  # was None pre-fix
```

---

## Task 4 — Pin the C5 error class

**The class already exists. NO new code required.**

`src/errors/kernel.py:158`:

```python
class ConfigExtendsTemplateWithoutInheritance(PlatformError):
    code = "config.extends_template_without_inheritance"
    level = Level.FATAL

    def __init__(self, agent_path: str, *,
                 where: str = "templating.inheritance.render_with_parent") -> None:
        super().__init__(
            message=(
                f"agent {agent_path!r} uses {{% extends parent %}} "
                f"in its prompt but has no extends: field"
            ),
            where=where,
            detail={"agent_path": agent_path},
        )
```

Also wired into:

- `src/errors/__init__.py:77, 175` (import + `__all__`).
- `src/errors/catalog.py:41` (Level mapping).
- `tests/unit/test_errors.py:43, 153` (construction test exists).
- `docs/explanation/error-model.md:791` (documented as engine
  pre-flight 5.6.6).
- `docs/reference/module-api-reference.md:1925`.

**The class was written but never raised in production.** The
spec's developer prompt said "New code recommended" — that is a
factual error in the spec; correction goes here, not in Step 2.

**Step 2's job:** raise it. Before the dispatch branch in
`_render_prompt_if_needed`, check:

```python
if "{% extends parent %}" in prompt and self.config.extends is None:
    raise ConfigExtendsTemplateWithoutInheritance(
        agent_path=f"agent:{self.config.agent_id}",
    )
```

This must fire BEFORE the `render_prompt` else-branch so the
loader-less Environment never sees the orphan directive
(otherwise the user gets the unhelpful `TypeError: no loader
for this environment specified` instead of the typed error).

Order, locked: orphan-check → dispatch (`extends is not None` →
`render_with_parent` ELSE `render_prompt`).

### Why a substring match (not an AST walk)?

- Jinja's `{% extends ... %}` directive can ONLY appear once
  per template and must be the first significant directive
  (Jinja docs §Template Inheritance). A substring match is
  sufficient because there is no valid context for the literal
  string `{% extends parent %}` to appear anywhere except as
  the directive. (Comments are stripped; literal blocks
  enclosing the string would still parse as the directive
  outside the literal block.)
- Cost: O(len(prompt)) one-shot scan; negligible.

The reviewer-prompt concern that "a prompt substring check
would let comments trigger" is addressed by the same Jinja
constraint — a `{# ... #}` comment containing the substring
would not be a syntactic extends directive, but the substring
match would false-positive on it. **Mitigation:** strip Jinja
comments before the substring check, or use Jinja's `env.parse`
+ AST walk for `ExtendsNode`.

**Decision:** use the AST-walk form. It's ~5 lines and removes
the false-positive class entirely:

```python
from jinja2 import Environment
from jinja2.nodes import Extends

def _prompt_uses_extends_directive(prompt: str) -> bool:
    env = Environment()  # bare env: parse-only, no loader needed
    ast = env.parse(prompt)
    return any(isinstance(n, Extends) for n in ast.find_all(Extends))
```

The bare `Environment()` has no loader; that is fine for
**parse only** — Jinja's loader lookup only fires at template
**load** time. AST inspection of an already-parsed source
doesn't trigger the lookup. (Verified by reading
`jinja2/environment.py` Environment.parse signature; no
loader access.)

---

## Task 5 — Multi-level chain handling

The two options Step 1 had to pick between:

- **(a) load-all-ancestors-into-DictLoader** — engine loads
  every ancestor's prompt source into a single `DictLoader`
  with synthetic names per layer; rewrites each non-leaf
  layer's `{% extends parent %}` to a literal
  `{% extends "<next>" %}` at load time.
- **(b) asset-manager-pre-flatten** — chain-render at the
  asset manager so a single fully-composed parent string
  reaches the engine.

**Pick:** **(a) load-all-ancestors-into-DictLoader.**

### Why (a)

- (b) needs deferred variable evaluation: the leaf's `{{
  agent.name }}` cannot be rendered at asset-manager time
  because the engine populates the `agent.*` context. Jinja
  has no native partial-render mode; the workaround (mock-
  context, multiple passes, regex masking) is fragile and
  escalates exactly the way Part 2 of the spec already
  rejected for Option C.
- (a) keeps the render-time machinery additive: extend
  `render_with_parent` from "one parent" to "an ordered chain
  of parents," all loaded together. The render context is
  unchanged; the leaf still sees one parent name.
- The string rewrite in (a) is bounded and safe: we are
  rewriting a directive that we own (`{% extends parent %}`)
  with a string we generate (`{% extends "__kolsch_parent_N" %}`).
  No author-provided string can collide because `__kolsch_*`
  names are reserved by convention and the literal-string
  form bypasses the `parent` variable entirely.

### Concrete API shape for Step 2

Extend the helper at `src/templating/inheritance.py:208`:

```python
def render_with_parents(
    child_template_string: str,
    parent_chain: list[str],         # immediate parent → root, in order
    context: dict[str, Any],
    *,
    template_name: str = "<inline>",
) -> str:
    """Render a child that may use ``{% extends parent %}``,
    where each ancestor in ``parent_chain`` may itself use
    ``{% extends parent %}`` to point at its OWN parent.

    Single-parent case: pass ``parent_chain=[parent_source]``.
    Equivalent to the existing ``render_with_parent``."""
    loader_map: dict[str, str] = {}
    for i, src in enumerate(parent_chain):
        name = f"__kolsch_parent_{i}"
        if i + 1 < len(parent_chain):
            # Rewrite this layer's `{% extends parent %}` to
            # `{% extends "__kolsch_parent_<i+1>" %}`. The next
            # layer up is the grandparent of THIS layer.
            next_name = f"__kolsch_parent_{i+1}"
            src = re.sub(
                r"\{%\s*extends\s+parent\s*%\}",
                f'{{% extends "{next_name}" %}}',
                src,
                count=1,
            )
        loader_map[name] = src
    env = Environment(
        undefined=StrictUndefined, autoescape=False,
        loader=DictLoader(loader_map),
    )
    enriched_context = {**context, "parent": "__kolsch_parent_0"}
    # ... existing error trapping ...
```

The existing `render_with_parent` becomes a thin shim:

```python
def render_with_parent(child, parent, context, *, template_name="<inline>"):
    return render_with_parents(child, [parent], context, template_name=template_name)
```

That keeps `test_inheritance.py:151+` passing byte-for-byte and
makes the multi-level extension an additive change to the
public surface.

### Engine-side: walking the chain

Inside `_load_parent_prompt_source` (the helper proposed in
Task 2), call `resolve_extends_chain(leaf_path, workspace_root,
catalog)` — same function the asset manager already uses at
`asset_manager.py:253`. Returns a leaf-first list of raw
YAML dicts. Strip the leaf, extract `prompt:` from each
ancestor in order, return the list.

```python
# inside engine.py
def _load_parent_chain_prompts(self) -> list[str]:
    leaf_entry = ... # AssetEntry of self.config.agent_id
    chain = resolve_extends_chain(
        Path(leaf_entry.path),
        Path(self.engine._assets._root),
        self.engine._assets.catalog(),   # method call
    )
    # chain[0] is the leaf — skip it.
    return [layer.get("prompt", "") for layer in chain[1:]]
```

Single-level (the butler trigger case) → `len(parent_chain) ==
1` → identical to existing `render_with_parent`. Multi-level
(E1) → `len(parent_chain) > 1` → rewrite kicks in.

### Compliance with the spec's escalation conditions

Part 5 ESCALATION line 506-509: "Jinja's empty-context render-
for-pre-flatten errors on StrictUndefined for `{{ agent.name }}`
instead of deferring variable evaluation; fallback is the larger
load-all-ancestors approach."

We are skipping straight to the fallback. Pre-flatten was the
fragile path; load-all-ancestors is the robust one. NOT an
escalation — the spec named load-all-ancestors as the safer
option and Step 1 picked it on the merits without hitting the
pre-flatten failure mode.

---

## Task 6 — Fixture layout pin + loader sketch

### Directory layout

```
tests/fixtures/extends/
├── _loader.py               # the helper Step 3 imports
├── a/
│   ├── a1_plain.yaml
│   └── a2_jinja.yaml
├── b/
│   ├── b1_parent.yaml
│   ├── b1_child.yaml
│   ├── b2_parent.yaml
│   ├── b2_child.yaml
│   ├── b3_parent.yaml
│   ├── b3_child.yaml
│   ├── b4_parent.yaml
│   └── b4_child.yaml
├── c/
│   ├── c1_parent.yaml
│   ├── c1_child.yaml
│   ├── c2_parent.yaml
│   ├── c2_child.yaml
│   ├── c3_parent.yaml
│   ├── c3_child.yaml
│   ├── c4_parent.yaml
│   ├── c4_child.yaml
│   └── c5_orphan.yaml
├── d/   # d1_*, d2_*, d3_* parent/child pairs
├── e/   # e1_a / e1_b / e1_c, e2_*
├── f/   # f1-f5 (single-file or paired as listed in spec)
├── g/   # g1, g3 + the fixture skill
├── h/   # h1, h2
├── i/   # i1 + a workspace doc
└── k/   # k1, k2
```

Naming convention: lowercase category subdir (matches Part 4's
ID prefix); per-case prefix `<id_lower>_` followed by the
role (`parent` / `child` / `a` / `b` / `c` for multi-level /
descriptive suffix). Each YAML is a complete agent partial; no
includes, no fragments.

(The reviewer's "a/ b/ c/ subdirs vs. flat" question in Part 6
line 866 is resolved here — subdirs win because the corpus is
big enough that a flat directory of 30+ YAMLs is unscannable.)

### Loader helper sketch — Step 3 will write this verbatim

```python
# tests/fixtures/extends/_loader.py
"""Loader helper for the EXT-CORPUS fixture suite.

Each Part-4 case lives under ``tests/fixtures/extends/<cat>/``.
This helper:
  - Materialises the fixture YAML(s) into a tmp workspace as a
    minimal bundle (bundles/x/agents/<name>.yaml).
  - Returns the agent-ref the test calls into
    build_resolved_config with.

Skill/doc support: G-class and I-class cases declare extra
fixture files via the case table; the loader copies them too.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import shutil

FIXTURES_ROOT = Path(__file__).parent


@dataclass(frozen=True)
class FixtureCase:
    case_id: str                       # "C1", "E1", etc.
    category: str                      # "c", "e", etc.
    # Pairs of (src filename, dst path under workspace).
    files: tuple[tuple[str, str], ...]
    target_agent_ref: str              # what the test passes to build_resolved_config
    expected_outcome: str              # "loads_with_render" | "error_at_load"
    expected_error_code: str | None = None
    expected_render_contains: tuple[str, ...] = ()
    expected_render_excludes: tuple[str, ...] = ()


def install_fixture(case: FixtureCase, workspace_root: Path) -> None:
    """Copy ``case.files`` into ``workspace_root`` so the asset
    catalog scan picks them up. Caller is responsible for the
    workspace structure (bundle dirs, platform.yaml)."""
    src_dir = FIXTURES_ROOT / case.category
    for src_name, dst_rel in case.files:
        src = src_dir / src_name
        dst = workspace_root / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
```

Step 3's test runner parametrizes over the case table and
calls `install_fixture(case, ws) → build_resolved_config(case.target_agent_ref)
→ run engine → assert outcome`.

### Pre-existing test fixture practices observed

- `tests/fixtures/` already exists (factories.py, fake_llm.py,
  engine_builder.py, assertions.py). No YAML fixtures live
  there today; EXT-CORPUS is the first.
- `tests/conftest.py` and `tests/integration/conftest.py` will
  not need modification — fixture install is per-test via the
  `install_fixture` helper.

---

## ESCALATION conditions — none raised

The three escalation triggers in the Step 1 developer prompt:

1. **Engine can't reach the catalog at render time.** — Cleared.
   `self.engine._assets.catalog` is a read-only property
   already in use elsewhere in `_render_prompt_if_needed`.
2. **`render_with_parent` doesn't support multi-level +
   pre-flatten needs deferred-variable Jinja.** — Sidestepped.
   Picked load-all-ancestors directly per Task 5; pre-flatten
   not attempted, deferred-variable problem not encountered.
3. **Preserving leaf.extends requires schema changes that
   touch unrelated callers.** — Cleared. `ResolvedAgentConfig.extends`
   and `PartialAgentConfig.extends` already accept `str | None`;
   `grep -rn "config\.extends" src/ tests/` finds zero
   consumers downstream of the schema. No knock-on effects.

---

## Summary — what Step 2 takes from here

1. **One asset-manager edit:** `src/assets/asset_manager.py:262-265`
   — preserve leaf's `extends:` (Task 3 patch).
2. **One engine helper + branch in `_render_prompt_if_needed`:**
   - Add `_prompt_uses_extends_directive(prompt)` AST check.
   - Orphan-check raises `ConfigExtendsTemplateWithoutInheritance`
     when extends directive present + `config.extends is None`.
   - Dispatch: `if self.config.extends is not None` → call
     `render_with_parents` with chain loaded via
     `_load_parent_chain_prompts`; else → call `render_prompt`
     as today.
3. **One templating extension:** `render_with_parents` (chain
   form) alongside the existing `render_with_parent` shim
   (Task 5 API).
4. **Three unit tests** at `tests/unit/test_engine_extends_dispatch.py`:
   no-extends → `render_prompt`, single-level extends →
   `render_with_parents([one])`, orphan extends → typed error.

No schema changes. No `apply_inheritance` changes. No
`render_prompt` changes. No `assembly.py` changes. No
boundary-classifier changes. No documentation changes (those
land in Step 4).

**Reviewer verdict requested: APPROVE-TO-PROCEED.**
