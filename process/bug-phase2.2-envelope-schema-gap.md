# Bug — Phase 2.2 envelope features rejected by Layer-1 schema

**Status:** Closed (2026-06-02) · All 15 steps landed · Starter bundle now ships Phase 2.2 envelope

> The workspace ships agents that declare Phase 2.2 envelope fields
> (`context_files`, `permissions`, Jinja-templated prompts referencing
> `{{ agent }}` / `{{ tools }}` / `{{ docs }}` / `{{ workspace }}`),
> but the platform schema is still Phase 1.3 and `extra="forbid"` rejects
> them at Layer 1 validation. `kolsch chat` against any such agent fails
> at agent-load with `config.invalid` — the user never gets a prompt.

---

## 1 — Symptom

```
$ uv run kolsch chat
Traceback (most recent call last):
  ...
  File "src/kernel/pipeline.py", line 146, in resolve_and_validate
    _raise(partial_errors)
  File "src/kernel/pipeline.py", line 43, in _raise
    raise ConfigInvalid(validation_errors=payload, where=_WHERE_PIPELINE)
errors.kernel.ConfigInvalid: Configuration validation failed with 2 error(s)
```

The `validation_errors` payload (not surfaced in the traceback but visible
via `exc.to_dict()`):

```
[
  {layer:1, source:"partial[1]", path:"/context_files", code:"extra_forbidden"},
  {layer:1, source:"partial[1]", path:"/permissions",   code:"extra_forbidden"},
]
```

Additional latent failures the schema fix alone won't unblock — the
agent's prompt is a Jinja2 template (`{{ agent.name }}`, `{% for t in
tools %}…{% endfor %}`, `{{ docs }}`, `{{ workspace }}`) and the engine
has no templating pre-flight. With strict undefined variables it would
raise `config.template_render_failed` at agent-load even after the
schema accepts the fields.

---

## 2 — Reproduction

1. Have a workspace at `~/Documents/kolsch` (or `$KOLSCH_WORKSPACE`).
2. Edit `<workspace>/bundles/kolsch/agents/helper.yaml` to declare
   any of: `context_files`, `permissions`, `extends`, OR a Jinja
   prompt referencing one of the five Phase 2.2 variables.
3. `uv run kolsch chat` → traceback above.

The starter bundle shipped in `src/cli/_starter_bundle/kolsch/agents/
helper.yaml` does NOT use these fields, so a fresh `kolsch setup`
into an empty directory does not hit this bug — only workspaces
that have been hand-migrated or that ship the Phase 2.2-style helper
do.

---

## 3 — Root cause

Phase 2.2 (Agent Envelope) was specified and a build plan was written
but no code landed. The platform stopped at Phase 2.1 (`5500ff7`); the
most recent feature commit (`ba4673f`) is a Phase 2.0 setup migration.
Specifically missing:

- `ResolvedAgentConfig` doesn't carry `context_files` or
  `permissions`. `extra="forbid"` means any agent that declares
  them fails Layer 1.
- No Jinja2 pre-flight: the engine assembles the prompt as plain
  text. Template syntax in a prompt either renders as literal
  `{{ … }}` to the LLM or, once Jinja is wired with
  `StrictUndefined`, raises on the first undefined variable.
- No per-agent `permissions` composition into authz. The
  workspace's `platform.yaml:authorization` is the only rule
  source today.
- No `context_files` resolver: the `kolsch:` and `workspace:`
  path prefixes have no handler.
- No `@tool(summary=...)` extension, so the `tools` variable
  has nothing to project.

The design is documented in `process/phase2.2-agent-envelope-specs.md`
(14-part patch doc) and the implementation plan in
`process/phase2.2-agent-envelope-build.md` (15 dependency-ordered
steps with per-step dev + reviewer prompts).

---

## 4 — Why this matters

The user-facing surface — `kolsch chat` — is completely broken for any
workspace that uses the Phase 2.2-style helper. Quick remediation
(rewrite the workspace helper.yaml back to Phase 1.3 shape) is
possible but the workspace template the qol-setup work landed
presumes Phase 2.2 features. Continuing to ship that template
without the platform changes means every new user hits this on
their first `kolsch chat` invocation.

---

## 5 — Acceptance criteria

Done when:

1. **The user's `helper.yaml` loads and runs unchanged.** No edits to
   the workspace YAML required. The exact file with `context_files`,
   `permissions`, and the Jinja prompt referencing all five Phase 2.2
   variables produces a working chat against the configured LLM.
2. **`ResolvedAgentConfig` accepts the new envelope fields** with
   strict typing (no `dict[str, Any]` escape hatches for the new
   fields). `extra="forbid"` stays — typos in the new fields still
   fail Layer 1.
3. **Jinja templating is wired through engine pre-flight** with
   `StrictUndefined`. Plain-text prompts (no `{{ }}` / `{% %}`)
   pass through unchanged.
4. **Per-agent permissions compose over platform defaults** in the
   documented order: agent > extends > defaults > schema default.
   Composition is wholesale field override (no per-rule merging).
5. **`context_files` resolution** handles both `kolsch:` (via
   `importlib.resources.files("kolsch")`) and `workspace:` prefixes,
   and inlines primary entries into the rendered prompt.
6. **All existing tests still green.** No regressions in
   `tests/unit`, `tests/component`, or `tests/integration`.
7. **New regression tests cover the envelope features**
   (TC-ENV-* per the build plan's Step 15).
8. **Manual end-to-end with the user's workspace helper.yaml** —
   `kolsch chat` against it produces a real LLM reply that
   demonstrates each variable resolved.

---

## 6 — Implementation reference

The work is broken into 15 dependency-ordered steps in
`process/phase2.2-agent-envelope-build.md`. Each step has its own
focused dev + reviewer prompts and is sized to land as one PR.

Step graph (from the build plan):

```
1  ENV-R1..R13 revisions  →  2  Jinja render harness
2  → 3 ToolsVar / 4 DocsVar / 5 WorkspaceVar / 6 AgentVar / 7 PlatformVar
3-7 → 8  Schema additions
8  → 9 Permission composition → 10 permissions.check API
8  → 11 @tool(summary=...) + builtin updates
8  → 12 context_files resolution
8  → 13 Agent inheritance (extends)
9..13 → 14 Engine pre-flight integration   ← user-visible pivot
14 → 15 Regression tests + manual e2e
```

Steps 1-13 are scaffold-and-wire — no user-visible behaviour change
yet. Step 14 is the pivot where everything composes into pre-flight
and chat starts accepting Phase 2.2 agents. Step 15 pins the
regression coverage and confirms the user's helper.yaml works.

**Per-step PRs.** Each step lands as its own PR. The build plan's
reviewer prompts gate progression; the coordinator runs `pytest -q`
after each merge and refuses to advance if anything regresses.

---

## 7 — Coordinator notes

- **Workspace-template alignment.** The starter bundle
  (`src/cli/_starter_bundle/kolsch/agents/helper.yaml`) currently
  ships the Phase 1.3-compatible helper. The qol-setup work also
  references a Phase 2.2-style helper in user workspaces. After
  Phase 2.2 lands, the starter bundle should be updated to ship
  the Phase 2.2 helper (and the user's workspace becomes the
  canonical example). Add this to Step 15's manual e2e.
- **`extra="forbid"` stays.** A Phase 2.3 author who types
  `contexts_files:` (with an extra `s`) gets a Layer 1 reject
  naming the typo, not silent ignore. Resist the urge to relax
  the model config to accommodate the new fields.
- **Composition order is non-negotiable.** Agent fields override
  the parent's fields wholesale (no merge); the parent's fields
  override defaults; defaults override the schema default. The
  `permissions:` list specifically is NOT merged — replacing the
  list wins. The build plan's Step 9 reviewer prompt has the
  test cases.
- **The four-choice modal continues to consume the prompt
  callback's tuple shape.** Phase 2.2's per-agent permissions
  feed authz; authz's prompt callback is unchanged. The chat
  renderer needs no changes.

---

## 8 — Step 0 (investigation writeup) — STOP gate before code lands

The investigation in this doc establishes the symptom and root
cause. Before any Phase 2.2 code is written, a developer must
produce the Step 0 writeup confirming the gap, listing exact
fields/lines that need to change, and surfacing any design
ambiguity that needs coordinator resolution.

### Developer prompt — Step 0

> You are the developer agent working on
> `process/bug-phase2.2-envelope-schema-gap.md`. **Do not write
> production code in this step.** Your output is a writeup the
> coordinator will review before code lands.
>
> Read `process/phase2.2-agent-envelope-specs.md` and
> `process/phase2.2-agent-envelope-build.md` in full.
>
> Read these source files:
>
> - `src/schemas/config.py` (full)
> - `src/kernel/pipeline.py`, `src/kernel/resolver.py`,
>   `src/kernel/validation.py`
> - `src/engine/engine.py` — focus on pre-flight (the model
>   spec resolution + prompt assembly path)
> - `src/assets/asset_manager.py` — `build_resolved_config`
> - `src/providers/authz.py` (full, including rule composition)
> - `src/tools/decorator.py` and `src/tools/builtins.py`
> - `~/Documents/kolsch/bundles/kolsch/agents/helper.yaml`
>
> Reproduce the bug:
> ```
> cd ~/Documents/kolsch && uv --directory <repo> run kolsch chat
> ```
> Capture the `ConfigInvalid.to_dict()` output via the snippet
> in §1 of this doc.
>
> Produce a writeup with these sections (write directly into
> §10 of this doc, replacing the empty placeholder):
>
> 1. **Confirmed symptom.** Exact `validation_errors` payload
>    from your reproduction.
> 2. **Affected fields.** Every Phase 2.2 envelope field
>    present in the user's helper.yaml, with the schema
>    location each one needs to land. Cross-reference the
>    spec's ENV-R sections.
> 3. **Latent failures.** Beyond the two surfaced errors,
>    what else would fail once those two are accepted?
>    (Jinja render with no engine pre-flight, etc.)
> 4. **Design ambiguities surfaced.** Anything in the spec
>    that's underspecified for your implementation question.
>    Flag for coordinator resolution; do NOT decide
>    unilaterally.
> 5. **Acceptance test list.** The TC-ENV-* you'd write to
>    pin each envelope feature, with one-line descriptions.
>    Cross-check against the build plan's Step 15.
> 6. **Step 1 plan.** A concrete list of edits for
>    `process/phase2.2-agent-envelope-build.md` Step 1
>    (ENV-R1..R13 revisions) — what files you'll touch, what
>    line-level changes, what tests you'll update.
>
> Hand back to the coordinator before writing any production
> code. The Step 0 writeup is the STOP gate.

### Reviewer prompt — Step 0

> You are the reviewer agent. The developer has produced a
> Step 0 writeup in §10 of
> `process/bug-phase2.2-envelope-schema-gap.md`. Your job:
> decide whether they have ENOUGH understanding to start
> Step 1, or whether the writeup has gaps that would let bad
> code in.
>
> Check each section against the source:
>
> 1. Confirmed symptom — does the developer's reproduction
>    output match the symptom in §1? If the
>    `validation_errors` differ, the workspace state has
>    drifted and Step 1 may have different scope.
> 2. Affected fields — pull `src/schemas/config.py` and
>    cross-check that every field listed actually doesn't
>    exist today. If the developer claims a field that's
>    already there, push back. Pull
>    `process/phase2.2-agent-envelope-specs.md` and
>    cross-check the ENV-R section references.
> 3. Latent failures — does the developer correctly
>    identify Jinja templating, permissions composition,
>    context_files resolution, `@tool(summary=...)`, and
>    `extends` as the OTHER missing pieces? If they only
>    flagged the schema gap, the writeup is incomplete.
> 4. Design ambiguities — are these REAL ambiguities the
>    coordinator needs to decide, or things the developer
>    can resolve by reading the spec? Push back on the
>    latter.
> 5. Acceptance test list — does the TC-ENV-* list cover
>    every envelope feature, or are there gaps?
> 6. Step 1 plan — is the file list complete? Does it match
>    what the build plan Step 1 actually requires?
>
> **PASS** = developer can proceed to Step 1.
> **REVISE** = list the specific gaps; developer iterates
> on the writeup, then re-submits.
> **ESCALATE** = real design ambiguity that needs
> coordinator resolution; log it in §9 and pause Step 1
> until the coordinator resolves it.

---

## 9 — Coordinator escalation log

### E-2.2-001 (Step 0 ambiguities A1–A5) — 2026-06-02

The Step 0 writeup surfaced five ambiguities. Resolutions below are
binding for Step 1; the developer applies them as part of ENV-R1..R13
edits.

**A1 — `permissions[].who` / `permissions[].what` orientation.**

**Resolution: (a) amend the spec to match the implementation.** `who`
matches the principal; `what` matches the action-or-target. The
existing Phase 1 / Phase 2.0 authz rules, the user's helper.yaml, the
qol-setup-evolution Change 1 migration, and the chat-modal callback
signature all already follow this. Swapping the impl would break every
deployed rule.

Step 1 scope: edit `process/phase2.2-agent-envelope-specs.md` §1.1
example, §2.1, §2.2 (`permissions.require(who, what)` docstring), and
any other ENV-R section that asserts the opposite. Add a one-line note
in the spec's Part 0 calling out that this resolution closed an
inconsistency.

**A2 — `defaults.permissions` / `defaults.context_files` composition
when the agent and its parent are both silent.**

**Resolution: (a) silence falls through; explicit `[]` blocks defaults.**

`null` deletes (RFC 7386); explicit `[]` is a present-but-empty value
that wholesale-replaces; absence falls through to the next layer.
Composition order (binding):

```
schema default  ←  defaults.<field>  ←  extends chain  ←  agent
        (lowest)                                          (highest)
```

A child layer present (even as `[]`) wins over deeper layers. A child
silent on a field is transparent — the next layer is consulted.
Document this in spec §8.2 explicitly; the prose today says "wholesale
override" but doesn't define "child silent" semantics.

Step 1 scope: amend spec §8.2 + add a worked example (parent
silent + agent silent + defaults set → defaults win; parent `[]` +
agent silent + defaults set → parent's `[]` wins).

**A3 — `kolsch:` prefix resolution.**

**Resolution: (a) Add `src/kolsch/` umbrella package; ship `docs/` and
`AGENTS.md` as package data via `[tool.hatch.build.targets.wheel.force-include]`.**

The `kolsch:` prefix is one resolution mechanism: `importlib.resources
.files("kolsch") / <relative_path>`. Source-tree dev and installed
contexts both go through the same lookup. The umbrella package's
`__init__.py` is empty; the package exists so the package-data
mechanism works.

Step 12 (context_files resolution) lands the loader. Step 1 just
pre-stages the package directory + pyproject.toml edits so subsequent
steps don't churn the build config. Specifically:

- Create `src/kolsch/__init__.py` (empty).
- Add to `pyproject.toml` `[tool.hatch.build.targets.wheel]`:
  `packages = ["src/kernel", ..., "src/kolsch"]` (add to the
  existing list).
- Under the same wheel target, add `force-include` entries mapping
  `docs/` → `src/kolsch/docs/` and `AGENTS.md` →
  `src/kolsch/AGENTS.md` (or whatever hatch's syntax requires for
  shipping repo-root files inside a package's resource tree).

Verify via `uv build` + `unzip dist/kolsch-*.whl | grep kolsch/docs/`.

**A4 — Spec references stale `process/` paths after the
cleanup.**

**Resolution: amend the spec's internal cross-refs to `process/`
during Step 1.** Small, contained edit. The spec sits in `process/`
now; cross-refs within `process/` are local-only and stable. Don't
carry the drift.

Step 1 scope: search `process/phase2.2-agent-envelope-specs.md` for
`process/` and rewrite each match to `process/`. Sanity-check
no other references break.

**A5 — `{% extends parent %}` Jinja loader.**

**Resolution: defer to Step 13. Do NOT pin a path here.**

The developer's Step 13 prompt must surface their proposed loader
strategy (DictLoader-with-preprocessing vs custom-Environment vs
something else) for reviewer + coordinator review before Step 13
code lands. Step 13 is the right place to make this call with full
implementation context; deciding now risks under- or over-engineering.

Step 1 scope: NO action on A5. Spec §8.3 stays as-is (with its open
question flagged).

### Secondary findings from §10.5

**Naming: `docs.primary()` vs `docs.primaries()`.**

Spec uses `docs.primaries()` (plural method on a collection — returns
a list). Test catalog uses `docs.primary()` (singular). The spec
wins: a doc collection's "give me the primary docs" reads as
`primaries()`, consistent with `docs.list()` returning a list and
`docs.secondaries()` returning the complement.

Step 1 scope: update `process/test-catalog.md` lines 1618, 1648 (and
any other occurrences) from `.primary()` to `.primaries()`. Spec
unchanged.

**TC-ENV-13 / TC-ENV-14 additions.**

Approved. Add to `process/test-catalog.md` after TC-ENV-12:

- **TC-ENV-13.** `defaults.<field>` fall-through: parent silent +
  agent silent + `defaults.permissions: [rule_x]` resolves to
  `[rule_x]`. Parent `[]` + agent silent + `defaults.permissions:
  [rule_x]` resolves to `[]`. Agent `[rule_y]` + anything below
  resolves to `[rule_y]`. Pins A2's resolution.
- **TC-ENV-14.** `kolsch:` prefix resolution: install the wheel
  into a clean venv, render a prompt with
  `{% include "kolsch:docs/index.md" %}` (or whatever the loader
  surface is), assert the contents match the source-tree
  `docs/index.md`. Pins A3's resolution.

These are part of Step 15's regression suite; the developer adds the
catalog entries during Step 1 (so the build plan reflects the full
target set) and the tests during Step 15.

| ID | Date | Issue | Resolution |
|---|---|---|---|
| E-2.2-001 | 2026-06-02 | A1–A5 + naming + TC-ENV-13/14 | See above |

---

## 10 — Step 0 writeup (developer fills in)

### 10.1 Confirmed symptom

Reproduction harness loaded `~/Documents/kolsch/bundles/kolsch/agents/helper.yaml`
through the same code path `kolsch chat` exercises
(`AssetManagement.build_resolved_config("kolsch/helper")`).
The raised `ConfigInvalid.to_dict()`:

```json
{
  "type": "config.invalid",
  "message": "Configuration validation failed with 2 error(s)",
  "where": "kernel.resolver.validate",
  "detail": {
    "validation_errors": [
      {
        "layer": 1,
        "source": "partial[1]",
        "path": "/context_files",
        "code": "extra_forbidden",
        "message": "Extra inputs are not permitted"
      },
      {
        "layer": 1,
        "source": "partial[1]",
        "path": "/permissions",
        "code": "extra_forbidden",
        "message": "Extra inputs are not permitted"
      }
    ]
  },
  "cause": null
}
```

Exact match to §1's described payload. `partial[1]` is the agent
layer (the assembler orders `[defaults, agent_definition,
call_overrides]` per `AssetManagement.build_resolved_config` step 3,
`src/assets/asset_manager.py:138`). `extra_forbidden` is Pydantic v2's
code for `PartialAgentConfig.model_config = ConfigDict(extra="forbid")`
(`src/kernel/partials.py:82`). One quirk worth noting for the
coordinator: `exc.detail.get("where")` returns `None` in the dump —
`where` lives at the top level of the envelope, not inside `detail`.
Doesn't change scope; just clarifies the error-envelope shape if
someone copies the §1 snippet verbatim.

The traceback in §1 shows `pipeline.py:146` and `:43`; current
`src/kernel/pipeline.py` raises from those exact lines
(`_raise(partial_errors)` and `raise ConfigInvalid(...)`). No drift.

### 10.2 Affected fields

Every field actually present in the user's `helper.yaml` plus the
load-bearing dependencies the build plan requires alongside them.
Cross-referenced to the spec's ENV-R numbering AND to the concrete
schema files that need editing once Step 1 (docs) lands and Steps 8–13
(code) execute.

| `helper.yaml` field | Spec section | Schema landing site | Build-plan step |
|---|---|---|---|
| `context_files` (list of `{path, summary, primary}`) | §6, ENV-R1 | `ResolvedAgentConfig` + `PartialAgentConfig` — new `list[ContextFileSpec]` field + `ContextFileSpec` Pydantic model with `kolsch:` / `workspace:` prefix validation | Step 8 |
| `permissions` (list of `{who, what, effect}`) | §1, ENV-R1 / ENV-R3 | `ResolvedAgentConfig` + `PartialAgentConfig` — `list[Rule]` reusing the existing `providers.authz.Rule` (NOT a duplicate model — build plan Step 8 reviewer check #5) | Step 8 |
| `prompt:` (Jinja2 template — uses `{{ agent.name }}`, `{{ workspace }}`, `{% for t in tools %}`, `{{ docs }}`) | §3, ENV-R4 | Schema unchanged — `PromptSpec = str | list[str]` already accepts the raw string. Behavioural change is in the engine's `_compose_system_prompt` / pre-flight rendering path (`src/engine/assembly.py:120` + new pre-flight step 5.6.6). | Steps 2 + 14 |

Phase 2.2 envelope fields NOT in the user's `helper.yaml` but the
build plan still ships in the same scope:

| Field | Spec section | Schema landing site | Build-plan step |
|---|---|---|---|
| `extends:` (parent agent prefixed path) | §8, ENV-R1 | `PartialAgentConfig` — `extends: str | None = None` with prefix-grammar validation | Steps 8 + 13 |
| `platform.yaml:workspace_summary` (multi-line text) | §7, ENV-R2 | Platform-config schema (loaded in `src/assets/platform_config.py`); not a `ResolvedAgentConfig` field | Step 5 |
| `@tool(summary=...)` | §5, ENV-R5 | `tools.decorator.tool(...)` signature + `ToolDefinition.summary` field (`src/tools/decorator.py:118` / `:92`) | Step 11 |

The `permissions:` entry in the user's helper carries an in-file
comment (`bundles/kolsch/agents/helper.yaml:35-39`) flagging that the
implementation's `who`/`what` orientation differs from the spec's §1.1
example — the helper follows the implementation (`who=".*"` matches
the principal; `what="tool\\.bash"` matches the action). That
divergence is the ambiguity called out in §10.4 below.

### 10.3 Latent failures

Once the schema accepts `context_files` and `permissions`, the
following blow up next — in dependency order, so the writeup pins
where the **next** failure lands rather than only the top of the
stack:

1. **Jinja render with no template engine.** `src/engine/assembly.py:120
   `_compose_system_prompt` is plain-text concatenation. With the
   helper's `{{ agent.name }} / {% for t in tools %}…{% endfor %} /
   {{ workspace }} / {{ docs }}` prompt and no rendering step, the
   model would receive the **literal** template string. Degraded
   behaviour, not a hard failure — until Step 2 (render harness) wires
   `StrictUndefined`, at which point the missing variable context
   surfaces as `config.template_render_failed` at agent-load
   (spec §3.4 / §10's `config.template_render_failed` error code).
   So the order matters: lighting up Step 2 alone (render + strict)
   without Steps 3–7 (the five variable objects) turns the agent
   un-loadable. Steps 2–7 ship as one PR group from the coordinator's
   POV.

2. **No `{{ agent }}` / `{{ tools }}` / `{{ docs }}` / `{{ workspace }}` /
   `{{ platform }}` objects.** Spec Part 4 defines the standardized
   variable API; none of the five `*Var` classes exist in
   `src/templating/vars/` today (the directory exists but contains
   only `__pycache__`; build plan Steps 3–7).

3. **`permissions` field stored but ignored.** Engine pre-flight
   step 3 (`engine.py:397`) calls `self.engine._authz.allow(principal,
   action)` with no `agent_permissions=` parameter. Build plan Step 9
   extends `AuthorizationProvider.allow` (or a parallel `check`) to
   compose `agent → platform → default-deny`. Without that, the
   helper's `tool.bash → prompt` rule never fires — platform rules
   alone gate dispatch.

4. **`@tool(summary=…)` rejected.** `src/tools/decorator.py:118`'s
   `tool(...)` signature has no `summary` kwarg; the four builtins
   under `src/tools/builtins.py` would need to pass it (`Step 11`).
   `{{ t.summary }}` in the helper prompt resolves via
   `ToolItem.summary`, which would currently be `None` → template
   renders empty strings (and `agent.template_warning` fires per
   spec §9 once the warning system is wired).

5. **`kolsch:` / `workspace:` prefix resolver missing.** No code
   today maps `kolsch:docs/index.md` → an absolute path. Spec §6.2
   says `importlib.resources.files("kolsch")`; `pyproject.toml`'s
   wheel package list (`[tool.hatch.build.targets.wheel].packages`)
   has no `kolsch` package or `docs/` package data, so the prefix
   would fail even after the resolver lands. ENV-R13 closes the
   pyproject side; Step 12 builds the resolver.

6. **64 KiB context-file size cap missing.** Spec §6.4 +
   `context.file_too_large` code (§10). Build plan Step 12.

7. **Agent inheritance + cycle detection + required-block
   enforcement.** Spec §8. The user's helper doesn't use `extends:`,
   so it doesn't trigger this — but the build plan ships it in the
   same envelope (Step 13).

8. **`agent.template_warning` event emission.** Spec §9. The
   chat renderer already consumes generic warning capsules per
   Phase 2.1, but the event TYPE itself doesn't exist in
   `src/schemas/events.py` yet (would be a new `EventType` member).
   Step 14 wires it.

9. **Default-fallback composition for the new fields.** The
   `qol-setup-evolution-and-defaults.md` amendment (referenced from
   spec §14) puts `defaults.permissions` / `defaults.context_files`
   into `platform.yaml`. The kernel's RFC 7386 merge
   (`src/kernel/resolver.py`) handles per-field wholesale override
   automatically once the fields are declared on `PartialAgentConfig`
   — so this is "free" once Step 8 lands, but worth confirming in
   a TC-ENV (see §10.5 below).

Items 1–4 are blocking for ANY useful end-to-end behaviour against
the user's helper. Items 5–9 round out the envelope.

### 10.4 Design ambiguities surfaced

Flagged for coordinator resolution — not unilateral developer calls.

**A1. `permissions[].who` / `permissions[].what` orientation
contradicts the implementation.**

Spec §1.1 example:

```yaml
permissions:
  - who: tool.bash      # spec: action goes in `who`
    what: ".*"          # spec: target / wildcard goes in `what`
    effect: prompt
```

`src/providers/authz.py:367-372`:

```python
if not compiled.who_re.fullmatch(principal):   # who → principal
    continue
target_match = ...what_re.fullmatch(target)    # what → target
action_match = compiled.what_re.fullmatch(action)  # OR action
```

The Phase 1 / Phase 2.0 implementation's `who` matches the principal
(`user.local`, `agent.ag_helper`); `what` matches the
action-OR-target. The user's `helper.yaml:40-41` already follows the
implementation:

```yaml
permissions:
  - { who: ".*", what: "tool\\.bash", effect: prompt }
```

…and its in-file comment (`helper.yaml:35-39`) acknowledges the
mismatch and asks for the spec to be amended. Two paths:

- **(a)** Amend §1.1 (and §2.1, §2.2's `permissions.require(who, what)`)
  to match the implementation. The user's helper, the existing authz
  tests, and the migration documented in
  `qol-setup-evolution-and-defaults.md` Change 1 all stay valid;
  the spec docstring becomes consistent with the code.
- **(b)** Swap the implementation. Would invalidate every Phase 2.0
  rule (`platform.yaml` already in user workspaces) and the
  Phase 2.1 chat-modal callback signature.

The user's helper.yaml comment leans (a). Confirm in §9 before the
developer lands ENV-R1/R3/R5 docs.

**A2. `defaults.permissions` / `defaults.context_files` composition
semantics.**

`qol-setup-evolution-and-defaults.md` §Change-2 says `platform.yaml`
gains `defaults.permissions` (a list) and `defaults.context_files`
(a list). Phase 2.2 §8.2 says agent fields override parent fields
**wholesale per field** — no merging within lists. The kernel's
RFC 7386 merge respects this (a child key replaces the parent's
value wholesale).

But the qol-setup composition order is `agent → extends chain →
defaults → schema default`. When the agent is silent and `extends:`
is set, does the parent's `permissions:` block fall through to
`defaults.permissions`, or does the parent's empty value win? Spec
§8.2 says "any field the child defines completely replaces the
parent's value (no merging within lists or dicts)" — but doesn't
address "child + parent both silent + defaults set." Most likely
intent: silence means "fall through"; only an explicit `[]` (empty
list) suppresses defaults.

Coordinator should pin one of:

- **(a)** Silence falls through. Explicit `[]` blocks defaults.
- **(b)** Any layer's presence (even `[]`) suppresses defaults.

The kernel's `merge_patch` already implements (a) for free —
`null` deletes a key, anything else replaces, missing falls
through. So (a) is the path of least resistance AND matches
the Phase 2.2 spec's "agents are minimal — most agents only need
to specify what differs" prose. Flagging because the spec text
itself doesn't say so explicitly.

**A3. `kolsch:` prefix in source-tree vs installed environments.**

Spec §6.2 specifies `importlib.resources.files("kolsch") / <path>`.
But the project has no `src/kolsch/` package; the wheel packages list
(`pyproject.toml:55-72`) names the individual subsystems
(`src/kernel`, `src/engine`, …). `importlib.resources.files("kolsch")`
would currently fail at the package-name lookup.

Two paths:

- **(a)** Add `src/kolsch/` as an umbrella package with `docs/` as
  package data (via `[tool.hatch.build.targets.wheel.force-include]`
  or similar). All `kolsch:<rel>` references resolve via this.
- **(b)** Resolve `kolsch:` to the repo root in source-tree dev
  and to the `kolsch_data` resource in installed contexts, with
  a dispatch helper. Spec §6.2's "works whether kolsch was
  installed via pip, uv tool install, or run via uv run from a
  source checkout" implies (a) is the intended path.

This is a build-system question the coordinator should pin before
ENV-R13 lands (it changes the pyproject edit footprint).

**A4. Repository layout drift — `process/` no longer exists.**

The spec consistently references `process/...` paths
(`process/phase2.2-agent-envelope-specs.md`,
`process/test-catalog.md`, etc.) in its ENV-R revisions. The
actual layout has `process/` at the repo root (top-level), with
`docs/` containing only `explanation/`, `how-to/`, `reference/`,
`tutorials/`, `index.md`. ENV-R7, ENV-R8, and several cross-refs
in spec sections all point at the old paths.

Not a true design ambiguity — just an artifact of when the spec
was authored. Flagging because the Step 1 plan (§10.6) needs to
target the actual paths, not the spec's stale ones, and the spec
itself wants a sweep to update the cross-references. Coordinator
decision: amend the spec's paths now (during Step 1), OR carry
the drift as a known-issue and fix when the docs/ layout
stabilises.

**A5. `{% extends parent %}` resolution (already flagged in spec
Part 13 Item 1).**

Spec §8.3 calls for a variable-based `{% extends parent %}` form
but Jinja's native `{% extends %}` expects a quoted template name.
The spec explicitly marks this as an open implementation question.
Surfacing here because Step 13's reviewer prompt expects an
escalation if the BaseLoader can't bind by variable cleanly; the
developer should NOT pick a path unilaterally.

### 10.5 Acceptance test list

Cross-checked against the spec's ENV-R8 / build plan Step 15. The
12 TC-ENV-* entries ALREADY EXIST in `process/test-catalog.md`
Part 9.10 (lines 1592–1719). Each is listed below with the
envelope feature it pins and any gap I noticed against the spec.

| ID | Pins | Build-plan step | Notes / gaps |
|---|---|---|---|
| TC-ENV-01 | Agent permissions override platform (composition order) | 9 + 14 | Uses spec §1.1 orientation (`who: tool.bash`). Once A1 resolves, update either the test setup or the spec example so they agree. |
| TC-ENV-02 | `permissions.require` raises `authz.denied` from a tool body | 10 | Maps to spec §2.2 — ContextVar plumbing covered. |
| TC-ENV-03 | Jinja prompt renders all five top-level variables | 2 + 14 | Setup names `{{ tools.list() }}` / `{{ docs.primary() }}` etc.; spec §4.2 calls the methods `.list()` / `.primaries()` — the catalog entry uses `.primary()` (singular). Either spec or catalog should change. |
| TC-ENV-04 | Plain-text prompt renders unchanged (backward compat) | 14 | Pins the byte-identical contract — load-bearing for Phase 1.x agents. |
| TC-ENV-05 | `tools.summary()` reads `@tool(summary=...)` + first-sentence fallback | 11 | Covers decorator + ToolItem. |
| TC-ENV-06 | `context_files` two-tier rendering (primary inlined, secondary listed) | 12 + 14 | Maps cleanly to spec §6.3. |
| TC-ENV-07 | `kolsch:` resolves to install location; `workspace:` to workspace root | 12 | A3 needs to resolve before this test can be written cleanly. |
| TC-ENV-08 | `extends:` chain — child override wins wholesale | 13 | Verifies §8.2 (wholesale per-field). |
| TC-ENV-09 | Cycle detection raises `config.agent_inheritance_cycle` | 13 | Maps to §8.6. |
| TC-ENV-10 | Required block missing raises `config.required_block_missing` | 13 | Jinja 3.1 required-block dependency. |
| TC-ENV-11 | `agent.template_warning` emits at agent-load for empty-data references | 14 | Maps to §9 — needs the new `EventType.AGENT_TEMPLATE_WARNING` member. |
| TC-ENV-12 | StrictUndefined raises on typo'd variable names | 2 | The load-bearing render-harness contract. |

**Gaps I'd add — flagging but not unilateral:**

- **TC-ENV-13 (suggested)** — `agent.permissions=[]` AND
  `defaults.permissions: [...]` set: the empty list suppresses
  defaults vs. silence falls through. Pins ambiguity **A2**.
  Belongs in the qol-setup test sweep too.
- **TC-ENV-14 (suggested)** — `kolsch:` resolves under
  `uv run` from a source checkout AND under a built wheel.
  Pins ambiguity **A3**.

Neither is in the current 12. Recommend the coordinator decide
whether to expand the suite at Step 1 (catalog edit during the
revisions pass) or defer to Step 15 (the integration sweep).

### 10.6 Step 1 plan

Build-plan Step 1 is the mechanical revisions pass ENV-R1..R13 —
no production code. Below is the concrete edit map. Each row is
sized for one commit; two logical commit groups proposed at the
end (matching the build plan's commit-discipline suggestion).

**Survey results** (what's already in the tree):

| Flag | Target | State today |
|---|---|---|
| ENV-R1 | `docs/reference/schemas.md` | Phase 2.2 fields absent. |
| ENV-R2 | `docs/explanation/asset-management-contract.md` | `workspace_summary` absent; `importlib.resources` not mentioned. |
| ENV-R3 | `docs/explanation/secrets-sandbox-authz-contract.md` | Per-agent section absent. |
| ENV-R4 | `docs/explanation/agent-engine-contract.md` | Steps 5.6.5 / 5.6.6 absent; `template_warning` absent. |
| ENV-R5 | `docs/explanation/tools-subsystem.md` | Permission API absent; `@tool(summary=...)` absent. |
| ENV-R6 | `docs/explanation/error-model.md` | §4.18 absent. |
| ENV-R7 | `process/development-testing-handbook.md` | **§10.17 PRESENT** (verified at line 1027). |
| ENV-R8 | `process/test-catalog.md` | **Part 9.10 PRESENT** with TC-ENV-01..12 (lines 1592–1719). Minor edits per §10.5 (`.primary()` → `.primaries()`, optionally add TC-ENV-13/14). |
| ENV-R9 | `docs/explanation/v1-scope.md` | Phase 2.2 fields not noted. |
| ENV-R10 | `AGENTS.md` | Neither hard rule present. |
| ENV-R11 | `docs/how-to/agent-templating.md` | **File does not exist.** |
| ENV-R12 | `docs/reference/module-api-reference.md` | Part 11.10 absent. |
| ENV-R13 | `pyproject.toml` | No `jinja2`; no `docs/` package data; no `src/kolsch` umbrella package. |

So 11 of 13 ENV-R flags need real edits; ENV-R7 is done; ENV-R8
needs a small touch-up.

**Edit map — what I'd do for each flag.**

| Flag | File touched | Change |
|---|---|---|
| ENV-R1 | `docs/reference/schemas.md` | Add `extends: str | None`, `permissions: list[Rule]`, `context_files: list[ContextFileSpec]` to the agent schema section. Add `ContextFileSpec` table (`path` required + prefixed; `summary` optional; `primary` bool default `false`). Note that `prompt:` is a Jinja2 template (back-compat: plain text unchanged). |
| ENV-R2 | `docs/explanation/asset-management-contract.md` | Add `workspace_summary: str | None` to the platform.yaml schema. New subsection: install-relative path resolution via `importlib.resources.files("kolsch")` — cross-reference ambiguity **A3** for which package the resolver lands on. |
| ENV-R3 | `docs/explanation/secrets-sandbox-authz-contract.md` | New section "Per-agent permissions": composition order (`agent → platform → default-deny`); the new keyword param on `AuthorizationProvider.allow` / `.check`. Cross-reference ambiguity **A1** for `who`/`what` orientation. |
| ENV-R4 | `docs/explanation/agent-engine-contract.md` | Insert pre-flight steps 5.6.5 (extends chain resolution) and 5.6.6 (Jinja render with the five variable objects). Document `agent.template_warning` emission. |
| ENV-R5 | `docs/explanation/tools-subsystem.md` | Add "Permission API for tool authors" section: `permissions.check(who, what) -> AuthzEffect`, `permissions.require(who, what) -> None`, ContextVar-bound. Update `@tool` decorator table to include `summary: str | None`. |
| ENV-R6 | `docs/explanation/error-model.md` | New §4.18 "Agent Envelope" listing the 11 codes from spec Part 10 with the correct Fatal / Recoverable / Warning levels. |
| ENV-R7 | _(no edit — already at `process/development-testing-handbook.md:1027`)_ | — |
| ENV-R8 | `process/test-catalog.md` | Touch-up: TC-ENV-03 setup uses `{{ docs.primary() }}` — change to `.primaries()` to match spec §4.2 (or vice versa once A1/A2/A3 are resolved). Consider adding TC-ENV-13 + TC-ENV-14 per §10.5. |
| ENV-R9 | `docs/explanation/v1-scope.md` | Note Jinja templates + `extends:` + per-agent permissions. Update §2.3's agent-fields list to include `permissions`, `context_files`, `extends`. |
| ENV-R10 | `AGENTS.md` | Add two Hard Rules per spec §11 ENV-R10 verbatim (Jinja prompts + variable resolution; secrets never in `platform.yaml`). |
| ENV-R11 | `docs/how-to/agent-templating.md` | **Create file.** Recipes per spec §11 ENV-R11: first templated prompt; using the five variables; mixing compact + explicit styles; extending a parent agent; declaring required blocks. |
| ENV-R12 | `docs/reference/module-api-reference.md` | Add Part 11.10 "Agent Envelope": `kolsch.tools.permissions` surface; Jinja render context; `extends:` chain semantics. Update dependency-matrix entry for engine pre-flight. |
| ENV-R13 | `pyproject.toml` | Add `jinja2>=3.1.0` to runtime `dependencies`. ALSO add the `kolsch` umbrella package + `docs/` as package data — this depends on ambiguity **A3**'s resolution (whether to add `src/kolsch/` as an umbrella or use `force-include`). Add `src/templating` to the wheel `packages` list (currently absent). |

**Path drift note.** Every "docs/explanation/…" / "docs/reference/…"
path above is the actual current layout. Every spec reference to
`process/…` needs amending to `process/…` as a side-effect of
the ENV-R7 / ENV-R8 pass — coordinator decides whether to do
this as part of Step 1 or defer (ambiguity **A4**).

**Tests touched.** ENV-R8 is the only test-adjacent change in this
pass; the catalog touch-ups are doc-only. No production tests are
written in Step 1; the 12 TC-ENV-* tests land at Step 15.

**Suggested commit groups** (per the build plan's "two logical
commits" guidance):

- **Commit A — existing-doc revisions:** ENV-R1, R2, R3, R4, R5,
  R6, R8 (touch-up), R9, R10, R12. Touches ten existing docs.
- **Commit B — new artifacts + deps:** ENV-R11 (new how-to file),
  ENV-R13 (pyproject + package data). Two new artifacts + the
  dependency edit kept in one commit for atomicity.

**Total scope estimate.** ~600–800 lines across the 12 doc files
(rough; most are 1–3 new sections per file). No production code.

**Pre-Step-1 STOP requests** (coordinator must answer before edits
start):
- **A1** (who/what orientation) — does the spec amend toward
  the implementation, or vice versa?
- **A3** (`kolsch:` package layout) — `src/kolsch/` umbrella or
  `force-include`?
- **A4** (`process/` → `process/`) — amend cross-refs during
  Step 1 or carry as known issue?

A2 and A5 don't gate Step 1; they gate Steps 9/14 and Step 13
respectively.

---

## 11 — Steps 1-15 (execute the build plan)

Once Step 0 is PASS, the developer works through
`process/phase2.2-agent-envelope-build.md` Steps 1-15. That
doc carries the per-step dev + reviewer prompts; this bug
doc does not duplicate them.

### Developer prompt — Steps 1-15

> You have a PASS from the reviewer on Step 0 of
> `process/bug-phase2.2-envelope-schema-gap.md`.
>
> Now execute
> `process/phase2.2-agent-envelope-build.md` Steps 1-15
> in order. Each step lands as its own PR with its own
> reviewer cycle — do NOT batch steps. The build plan's
> per-step developer prompt is your source of truth; this
> doc adds no instructions beyond it.
>
> After each step's reviewer PASS and merge, run:
> ```
> uv run pytest -q tests/unit tests/component tests/integration
> ```
> If anything regresses, STOP and surface to the
> coordinator before starting the next step.
>
> Step 14 is the user-visible pivot — that's the first
> step where `kolsch chat` starts behaving differently
> against a Phase 2.2 agent. Step 15 pins regression
> coverage and the manual end-to-end.
>
> When Step 15 lands, follow the §12 manual verification
> below before marking the bug closed.

### Reviewer prompt — Steps 1-15

> You will review each of the 15 PRs that this bug
> produces. The build plan's per-step reviewer prompt is
> your source of truth.
>
> After each PR you PASS, cross-check that:
> - The full suite still passes (`tests/unit`,
>   `tests/component`, `tests/integration`).
> - The PR honours the build plan's acceptance criteria
>   for that step (not just the prompt — the criteria too).
> - No production code outside the step's scope was
>   touched.
>
> If a PR drifts beyond its step's scope, REVISE — the
> per-step PR discipline is what keeps this 15-step bug
> reviewable.

---

## 12 — Manual end-to-end (close-out)

Run by the developer after Step 15 lands. Witnessed by the
coordinator before the bug is marked Closed.

1. **Clean repro.** Reset your workspace's `helper.yaml` to
   match the in-repo
   `src/cli/_starter_bundle/kolsch/agents/helper.yaml`
   AFTER Step 15 updates it to the Phase 2.2 shape. (If
   you used the original Phase 1.3 helper for testing,
   diff against the new starter and resolve.)
2. **Chat smoke.** `uv run kolsch chat kolsch/helper`
   against your configured LLM. Verify:
   - The system prompt the model receives (visible via
     `model.request` event) has `agent.name`, `tools`
     listing, `docs` table, and `workspace` summary all
     resolved — no literal `{{ … }}` artifacts.
   - A `bash` tool call triggers the four-choice approval
     modal (the per-agent `tool.bash → prompt` rule fires
     before the platform default).
   - Reading and writing files inside the workspace works
     via the inlined `context_files` references the prompt
     was rendered with.
3. **Replay.** `uv run kolsch sessions list`, pick the
   session, `kolsch sessions show <id>` (or replay path)
   and confirm the rendered prompt + envelope variables
   were captured in the JSONL.
4. **Close.** Update §1 status to Closed; note the closing
   PR sha in §9's log.

---

## 13 — Status updates

_Append-only. The coordinator stamps each step's land._

| Date | Step | Sha | Notes |
|---|---|---|---|
| 2026-06-02 | 0 | _uncommitted_ | §10 writeup + reviewer PASS |
| 2026-06-02 | 1 | _uncommitted_ | ENV-R1..R13 + E-2.2-001 resolutions applied (spec amendments §1.1/§2.1/§2.2/§8.2, repo-wide docs/process/→process/ sweep, `src/kolsch` umbrella + wheel force-include, jinja2 dep, new how-to) |
| 2026-06-02 | 2 | _uncommitted_ | `templating.render.render_prompt` + `ConfigTemplateRenderFailed`; 11 unit tests; `\| default` escalation gate cleared (jinja2 3.1.6) |
| 2026-06-02 | 3 | _uncommitted_ | `ToolsVar` + `ToolItem` (Option A — separate dataclass; ToolInfo not widened); 17 tests |
| 2026-06-02 | 4 | _uncommitted_ | `DocsVar` + `DocItem` two-tier rendering; 28 tests; rendering format documented per §6.3 |
| 2026-06-02 | 5 | _uncommitted_ | `WorkspaceVar` (single-object slot discipline); 9 tests |
| 2026-06-02 | 6 | _uncommitted_ | `AgentVar` + `ModelView` projection (ModelSpec preserved; wrapper adds `ref`/`context_length`/`supports_tools`); 11 tests |
| 2026-06-02 | 7 | _uncommitted_ | `PlatformVar` + `ProvidersVar` / `AuthorizationVar` / `FileAccessVar` / `AliasesVar`; 16 tests |
| 2026-06-02 | 8 | _uncommitted_ | `extends` / `permissions` / `context_files` on `ResolvedAgentConfig` + `PartialAgentConfig`; `ContextFileSpec` + `PermissionRule` schemas; prefix grammar at Layer 1; `config.context_file_unprefixed`; 12 tests; golden files regenerated |
| 2026-06-02 | 9 | _uncommitted_ | `AuthorizationProvider.allow(agent_permissions=)` composition (agent → platform → default-deny); `who_chain` detail; 6 tests; existing authz tests unchanged |
| 2026-06-02 | 10 | _uncommitted_ | `kolsch.tools.permissions` async module + ContextVar binding + `ToolPermissionsNoContext`; 9 tests |
| 2026-06-02 | 11 | _uncommitted_ | `@tool(summary=...)` + first-sentence derivation + 4 builtin updates (re-applied after worktree revert); 4 tests |
| 2026-06-02 | 12 | _uncommitted_ | `resolve_path` / `load_context_file` with wheel-vs-source-tree dispatch; 64 KiB cap; `ContextFileNotFound` / `ContextFileTooLarge`; 10 tests |
| 2026-06-02 | 13 | _uncommitted_ | `resolve_extends_chain` + `apply_inheritance` + `render_with_parent` (DictLoader-with-preprocessing — A5 chosen); 4 new error classes; 11 tests |
| 2026-06-02 | 14 | _uncommitted_ | engine pre-flight 5.6.5 (extends in `build_resolved_config`) + 5.6.6 (`_render_prompt_if_needed`); `AGENT_TEMPLATE_WARNING` event; minimal warning emission; ResolvedAgentConfig replaced via `model_copy` |
| 2026-06-02 | 15 | _uncommitted_ | TC-ENV-04 (plain-text round-trip) + bug-closure end-to-end + TC-ENV-11 (template warning); starter `helper.yaml` updated to ship Phase 2.2 envelope (Jinja prompt + `permissions:` + `context_files:` referencing `kolsch:docs/index.md` + `kolsch:AGENTS.md`); full regression **878 passed, 1 skipped** |
