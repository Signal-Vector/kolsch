# Step 1 writeup — qol-skills-jinja-variable-symmetric-with-tools

**Author:** developer
**Status:** PROPOSE-TO-PROCEED.

---

## 1. Plumbing trace — where each piece lives

| Concern | File | Lines | Note |
|---|---|---|---|
| Post-append site (REMOVE) | `src/engine/assembly.py` | 48–87 | `skills_listing` kwarg + the bolt-on at 83-87 |
| Sole `assemble_context` caller passing `skills_listing` | `src/engine/engine.py` | 1115, 1126–1130 | After this QoL the kwarg goes away entirely |
| `format_skills_listing` (DELETE candidate) | `src/skills/listing.py` | 29–44 | Plain-markdown emitter; only caller is engine.py:1115 |
| Engine pre-flight prompt render | `src/engine/engine.py` | 676–860 | `_render_prompt_if_needed` — where SkillsVar gets built |
| ToolsVar construction (model to mirror) | `src/engine/engine.py` | 725–745 | Build `ToolItem` list from registered tools |
| Jinja render context dict | `src/engine/engine.py` | 801–807 | Add `"skills": skills_var` here |
| `agent.template_warning` walker (symmetry target) | `src/engine/engine.py` | 861–896 | Already checks `{{ tools }}`/`{{ docs }}`; add `{{ skills }}` |
| `EnabledSkill` dataclass | `src/skills/models.py` | 24–46 | Has `name`, `description`, `ref`, …; lacks `bundle`/`path` |
| `AssetEntry` (catalog) | `src/assets/catalog.py` | 43–52 | Has `bundle`, `path` |
| Starter helper.yaml prompt | `src/cli/_starter_bundle/kolsch/agents/helper.yaml` | 48–79 | `{{ tools }}` block lives here; mirror for `{{ skills }}` |
| Tests touching `format_skills_listing` | 4 files | per §6 | Migration candidates |

---

## 2. Resolutions to the two open questions (spec §"Open questions")

### Q1 — filter by enabled, or expose the whole catalog?

**Decision: filter by enabled, mirroring tools.** `ToolsVar` ships only the agent's registered tools (the same set the model is told about), not the catalog at large. Same discipline for skills: `SkillsVar` lists only the agent's `subsystems.skills.config.enabled` entries (also already filtered by `disable_model_invocation` per Sk-I2).

Rationale: the prompt is the agent's view of its own capabilities. Listing skills the agent can't actually invoke would mislead the model and surface a confusing read for the user. Authors who want catalog-wide listings can do it through a different surface (e.g. a future `kolsch skills list` CLI); the prompt-side variable stays scoped to "what this agent can do."

This also makes the `enabled_in_agent` field the spec proposed unnecessary — every item in `SkillsVar` IS enabled by construction.

### Q2 — emit `agent.template_warning` when `{{ skills }}` is referenced but the agent declares no skills?

**Decision: yes, symmetric with tools/docs.** Add a fourth branch to the existing walker at `engine.py:861–896` so a prompt with `{{ skills }}` against a skill-less agent produces a Recoverable warning:

```python
if "skills" in prompt and skills_var.count == 0:
    warnings.append((
        "skills",
        "prompt references {{ skills }} but agent declares no skills",
        "add a skills entry under subsystems.skills.config.enabled or remove the reference",
    ))
```

Matches the `tools`/`docs` branches verbatim (same shape, same event class, same `AgentTemplateWarningData` emission at line 889).

---

## 3. SkillItem field surface — narrower than the spec proposed

Spec proposes `SkillItem(name, description, bundle, path, enabled_in_agent)`. The audit shows:

- `name`, `description` — already on `EnabledSkill` (`src/skills/models.py:40-41`). ✓
- `ref` — already on `EnabledSkill` (line 32, the 3-seg catalog ref `<bundle>/skills/<name>`). Useful for prompts that want to surface the canonical reference.
- `bundle`, `path` — NOT on `EnabledSkill`. Would need catalog lookup.
- `enabled_in_agent` — REMOVED per Q1 decision (every item is enabled).

**Pick: `SkillItem(name, description, ref)`** — three fields. Drop `bundle`/`path`/`enabled_in_agent`:

- `bundle` is recoverable from `ref.split("/")[0]` in templates that need it; carrying a redundant field invites drift.
- `path` is an implementation detail the prompt rarely needs — the only existing prompt surface that mentions paths is `format_skills_listing`'s `\`{skill.ref}/SKILL.md\`` line (`src/skills/listing.py:43`), and `ref` covers that.
- `enabled_in_agent` is always `True` per Q1.

Three fields is the minimum that supports `{{ skills }}` (the default `.list()` rendering), `{{ skills.summary() }}`, `{{ s.name }}` / `{{ s.description }}` / `{{ s.ref }}` in `{% for s in skills %}` loops. Matches the `ToolItem` minimalism principle.

**Open future extension**: if a future use case needs more fields, adding to a frozen dataclass is mechanical and additive.

---

## 4. The default `.list()` rendering

`ToolsVar.list()` produces verbose blocks (`### <name>(<signature>)\n<description>`). For skills, parallel rendering matches `format_skills_listing`'s current shape:

```
- **<name>** — <description> Path: `<ref>/SKILL.md`
```

Or more idiomatic to the `ToolsVar` style:

```
### <name>
<description>

Path: `<ref>/SKILL.md`
```

**Pick the bullet-list shape** — preserves the current rendering byte-identically (modulo the `## Available Skills` header which the auto-append produced and the template now controls). The migration story is "your prompt now owns the header text; the per-skill bullets render identically when you write `{{ skills }}` or `{{ skills.list() }}`."

The `## Available Skills` header from `format_skills_listing` (line 22-26) is what users typed for free; the migrated helper.yaml needs an explicit header in its prompt. See §5.

---

## 5. Starter helper.yaml migration

Current template (`src/cli/_starter_bundle/kolsch/agents/helper.yaml:54-58`):

```jinja
## What you can do

{% for t in tools %}
- **{{ t.name }}**: {{ t.summary }}
{% endfor %}
```

Add a symmetric `## Available skills` block immediately after, so the rendered prompt has both sections in one consistent place:

```jinja
## What you can do

{% for t in tools %}
- **{{ t.name }}**: {{ t.summary }}
{% endfor %}
{% if skills.count %}

## Available skills

{% for s in skills %}
- **{{ s.name }}** — {{ s.description }}
{% endfor %}
{% endif %}
```

The `{% if skills.count %}` guard keeps the section header from rendering for skill-less agents (the auto-append used to do the same via "empty string when no skills").

---

## 6. Tests touched

### A. New tests (Step 2)

- `tests/component/test_skills.py` — new tests for `SkillsVar`/`SkillItem` mirroring the `ToolsVar` test patterns (name lookup, iteration order, `summary()` / `names()` rendering, default `__str__`).
- `tests/integration/test_agent_envelope.py` — new test for `agent.template_warning` on `{{ skills }}` referenced + no skills (TC-ENV-13 or similar, parallel to the existing TC-ENV-11 for tools).
- `tests/component/test_engine.py` — new test confirming `skills_var` is bound into the render context.

### B. Tests to migrate / delete

| File | Current state | Migration |
|---|---|---|
| `tests/component/test_skills.py:160,190` | Calls `format_skills_listing(...)` | Replace with `SkillsVar(items).list()` assertion |
| `tests/component/test_skills.py:296-313` | Empty-listing tests | Replace with `SkillsVar([])` shape tests |
| `tests/integration/test_skills_authz.py:34,267` | Imports `format_skills_listing` for post-append assertion | Migrate to verify the rendered prompt contains skill bullets via the Jinja path |
| `tests/component/test_engine.py:1006` | `test_tc_eng_11_skills_listing_in_system_prompt` | Migrate to assert the rendered template (not the post-append) contains the skill block |
| `tests/integration/test_engine_crewai.py:357` | Same | Migrate same way |

### C. `format_skills_listing` itself

After the migration, the function has zero in-tree callers. **Delete** `src/skills/listing.py` and its export from `src/skills/__init__.py`.

---

## 7. Deprecation warning for legacy agents

Spec §Migration says "Any workspace agent that was relying on the post-append gets a deprecation warning on first agent-load and instructions to add the variable."

**Decision: keep this narrow.** The auto-append was unconditional — every agent got the listing whether it referenced it or not. There's no way to know which agents *intentionally* relied on the post-append vs. which were just receiving noise. A blanket warning would fire for every existing agent including the starter (until the starter is migrated in the same step).

Instead: emit ONE warning at agent-load only when:

1. The agent has `subsystems.skills.config.enabled` populated, AND
2. The rendered prompt does NOT contain `{{ skills }}` (or the rendered output doesn't contain skill names — see below)

Detection mechanism: check the raw `prompt:` template (pre-render) for the substring `"skills"` per the same heuristic the existing walker uses at lines 868-877. If the agent declares skills but the template doesn't mention them, fire `agent.template_warning` with reference="skills", reason="agent declares skills but prompt does not reference {{ skills }} — the auto-appended listing was removed in qol-skills-jinja-variable-symmetric-with-tools.md", hint="add a `{{ skills }}` block to the prompt template".

This is a single new branch added at `engine.py:861–896` next to Q2's branch.

---

## 8. Acceptance criteria

1. `SkillsVar` + `SkillItem` exist in `src/templating/vars/skills.py`, exported from `src/templating/vars/__init__.py`.
2. Engine pre-flight builds `skills_var` from `enabled_skills` (already computed at `engine.py:1104-1114`); render context dict gains `"skills": skills_var`.
3. `assemble_context`'s `skills_listing` kwarg removed; post-append (lines 83-87) deleted.
4. `format_skills_listing` + `src/skills/listing.py` deleted; import removed from engine.py:73.
5. Starter helper.yaml gets `{{ skills }}` block.
6. `{{ skills }}` referenced + no enabled skills → `agent.template_warning`.
7. Agent declares skills + prompt omits `{{ skills }}` → `agent.template_warning` (the migration warning per §7).
8. Tests covering post-append migrated to assert the Jinja path; new `SkillsVar` unit tests added.

---

## 9. Risk

- **R1**: Agent prompts in the wild that depend on the post-append. The blanket auto-append meant every agent author had skills surfaced for free; users who never wrote `{{ skills }}` would see the section disappear. The migration warning per §7 covers the discovery; users add `{{ skills }}` to their template once. Acceptable — same shape as the unified-refs deprecation event.
- **R2**: `format_skills_listing` is a public-ish symbol exported from `skills/__init__.py`. Deleting it is a breaking change for anyone who imported it outside the engine. Mitigation: the spec explicitly removes the post-append site, so the function has no remaining call site; keeping it as dead code is worse than deleting.

---

**Verdict from developer**: ready for Step 2.
