# Phase 2.2 Build Plan — Developer, Reviewer & Coordinator Prompts

**Draft 0.2** *(restructured from 0.1: 10 large steps split into 15
smaller ones, each focused on a single concern. Same coordinator-
escalation pattern.)*

> Fifteen-step build plan for Phase 2.2 (Agent Envelope). Each step
> ships one focused piece. Steps 1–11 build the foundation in
> isolation; Step 12 is the user-visible pivot (engine wires it
> all together); Steps 13–15 verify and harden. Treat these as
> Steps 25–39 of the overall build sequence — they run after
> Phase 2.1 closes (chat REPL + capsule QoL + chat-permissions
> bug fixes) and the patch document
> (`process/phase2.2-agent-envelope-specs.md`) has been read.

---

## Step dependency graph

```
1. Revisions (ENV-R1..R13)
   ↓
2. Jinja render harness (StrictUndefined; no vars yet)
   ↓
3. ToolsVar + ToolItem  ─┐
4. DocsVar + DocItem      ├─→  variable API complete
5. WorkspaceVar           │
6. AgentVar               │
7. PlatformVar           ─┘
   ↓
8. Schema additions (permissions, context_files, extends, workspace_summary)
   ↓
9. Permission composition (agent overrides platform)
   ↓
10. permissions.check / permissions.require API
    ↓
11. @tool `summary` field
    ↓
12. Context file resolution + loading
    ↓
13. Agent inheritance (chain resolver + Jinja parent binding)
    ↓
14. Engine pre-flight integration (rendering + warnings)  ← USER-VISIBLE PIVOT
    ↓
15. TC-ENV-01..12 + manual end-to-end + coordinator handoff
```

| Step | Ships | Lines (est.) |
|---|---|---|
| **1** | ENV-R1..R13 revisions; new how-to; pyproject deps | docs only |
| **2** | `kolsch.templating.render` + StrictUndefined config | ~40 |
| **3** | `ToolsVar` + `ToolItem` dataclass | ~80 |
| **4** | `DocsVar` + `DocItem` dataclass (primary/secondary) | ~80 |
| **5** | `WorkspaceVar` + `workspace_summary` wiring | ~50 |
| **6** | `AgentVar` (attribute-only) | ~40 |
| **7** | `PlatformVar` + `platform.providers` list-shape | ~70 |
| **8** | Schema additions for the four new fields | ~80 |
| **9** | `AuthzProvider` composition (agent → platform) | ~70 |
| **10** | `kolsch.tools.permissions` module + ContextVar plumbing | ~70 |
| **11** | `@tool(summary=...)` + builtin tool updates | ~60 |
| **12** | `kolsch:` / `workspace:` resolver + file loading | ~100 |
| **13** | `extends:` chain resolver + Jinja parent binding | ~120 |
| **14** | Engine pre-flight 5.6.5 + 5.6.6 + warnings | ~120 |
| **15** | Regression tests + manual e2e | ~250 |

Each PR is independently mergeable; intermediate states are never
broken (just incomplete — engine doesn't render templates until
Step 14).

---

## Step 1 — Apply ENV-R1..R13 revisions

- **Builds:** spec files updated; one new how-to; pyproject deps.
- **Depends on:** Phase 2.1 complete and merged.
- **Source of truth:** `process/phase2.2-agent-envelope-specs.md` Part 11.

### Developer prompt

```
[standing instructions]

Step 1 — Apply ENV-R1..R13. Mechanical revision pass.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 11.

Apply each flag per Part 11 of the patch. Each flag names a target
file + the exact addition (new section, new field, new test cases,
new error codes, etc.). No runtime code is changed.

Critical specifics:
- ENV-R6: §4.18 must have all 11 codes from patch Part 10 with
  correct levels.
- ENV-R8: TC-ENV-01..12 in full Setup/Action/Expected/Verifies form.
- ENV-R10: BOTH hard rules in AGENTS.md's Hard Rules section.
- ENV-R13: jinja2>=3.1.0 in runtime deps; docs/ added as package-data
  in pyproject.toml; verify the wheel build includes docs/.

Commit per flag OR in two logical groups (existing-doc revisions
in one commit; new doc + deps in another).

Acceptance: every flag applied or escalated.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — revisions.

For each ENV-R1..R13:
- Target file changed (or escalated/resolved).
- Change matches Part 11 wording.
- No other file touched outside the flag's scope.

Specific checks:
- §4.18 lists all 11 codes with right levels (Fatal vs Recoverable
  vs Warning).
- TC-ENV-01..12 carry the full test-case format.
- AGENTS.md has BOTH hard rules.
- jinja2 in runtime deps; package-data verified by building the
  wheel and inspecting contents.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 2 — Jinja render harness

- **Builds:** `src/templating/render.py` with `render_prompt(template, context) → str`. Uses StrictUndefined. No variable objects yet.
- **Depends on:** Step 1 PASS.
- **Acceptance:** function renders plain strings + simple variable substitution; typos raise.

### Developer prompt

```
[standing instructions]

Step 2 — Implement the Jinja render harness alone. Variable
objects come in later steps.

Read first:
- process/phase2.2-agent-envelope-specs.md §3 (templating
  semantics).

Deliver:

src/templating/
├── __init__.py
└── render.py    # render_prompt(template_string, context_dict) → str

Implementation:
- Use jinja2.Environment with StrictUndefined, autoescape=False.
- The function is pure — no I/O, no engine deps.
- Surface render errors as PlatformError("config.template_render_failed",
  {template, reason}).

Tests in tests/unit/templating/test_render.py:
- Empty template → empty output.
- Plain text template (no Jinja syntax) → output identical to input.
- Template with simple variable substitution renders correctly.
- StrictUndefined: typo'd variable name raises
  config.template_render_failed at render time.
- `{{ var | default("x") }}` returns "x" when var is undefined
  (verifies the default filter works with StrictUndefined).

Scope: ~40 LOC code + ~50 LOC tests.

ESCALATION PATH: if `{{ var | default("x") }}` raises (because
StrictUndefined intercepts before the filter), escalate —
coordinator may need a custom Undefined class.

Acceptance: tests pass; the harness is callable from outside.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — render harness.

Checks:

1. render_prompt is a single pure function. No globals; no I/O.

2. StrictUndefined is configured (verify by reading the Environment
   construction).

3. The default filter test passes. This is the load-bearing
   compatibility check — if `{{ x | default("y") }}` raises, the
   wrong Undefined class is in use.

4. Render errors surface as config.template_render_failed, NOT
   bare jinja2.UndefinedError.

5. No coupling to the engine, no variable-object imports.

Verdict: PASS or FAIL.
```

---

## Step 3 — `ToolsVar` + `ToolItem`

- **Builds:** the `tools` variable's class + ToolItem dataclass. Implements `.list()` / `.summary()` / `.names()` / `.get()` / `.count` + Pythonic protocols.
- **Depends on:** Step 2 PASS.
- **Acceptance:** unit tests verify every method and protocol against mock items.

### Developer prompt

```
[standing instructions]

Step 3 — Implement the ToolsVar object and its item.

Read first:
- process/phase2.2-agent-envelope-specs.md §4.4 (the tools
  object's surface).

Deliver:

src/templating/vars/tools.py:
- @dataclass ToolItem with fields: name, description, summary,
  signature, schema.
- ToolsVar class wrapping list[ToolItem]:
  - __init__(items: list[ToolItem])
  - .list() → markdown string with each tool's full description
  - .summary() → one-line-per-tool string
  - .names() → comma-separated names
  - .get(name) → ToolItem or None
  - .count → int
  - __str__ = .list()
  - __iter__ / __len__ / __getitem__ / __contains__

Tests in tests/unit/templating/test_tools_var.py:
- Construct ToolsVar with 3 mock items.
- Verify each method's output format.
- Verify iteration produces all items in declaration order.
- Verify len() returns count.
- Verify tools["bash"] works (subscript = get).
- Verify "bash" in tools works (contains).
- Verify tools["nonexistent"] returns None (no raise).

Scope: ~80 LOC code + ~50 LOC tests.

ESCALATION PATH: if there's already a ToolDefinition / ToolInfo
class in src/tools/ that overlaps with ToolItem, escalate to
decide on reuse vs new class. (Likely we reuse and ToolItem is
just an alias.)

Acceptance: all unit tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — ToolsVar.

Checks:

1. All five methods (.list / .summary / .names / .get / .count)
   present.

2. All four Pythonic protocols (iter / len / getitem / contains)
   present.

3. __str__ defaults to .list() per the spec.

4. .get("nonexistent") returns None; does NOT raise.

5. ToolItem fields match the spec's §4.4 documentation.

6. No engine or runtime coupling — ToolsVar can be constructed
   from arbitrary item lists in tests.

Verdict: PASS or FAIL.
```

---

## Step 4 — `DocsVar` + `DocItem`

- **Builds:** the `docs` variable with primary/secondary distinction. Full method set.
- **Depends on:** Step 3 PASS.
- **Acceptance:** primaries inlined; secondaries listed by summary; .primaries() and .secondaries() partition correctly.

### Developer prompt

```
[standing instructions]

Step 4 — Implement DocsVar + DocItem.

Read first:
- process/phase2.2-agent-envelope-specs.md §4.4, §6.3.

Deliver:

src/templating/vars/docs.py:
- @dataclass DocItem with fields: path, summary, primary, content,
  size_bytes.
- DocsVar class wrapping list[DocItem]:
  - .list() → primaries inlined with content + secondaries listed
    by path + summary (per patch §6.3 example output)
  - .primaries() → only primary items, full content inline
  - .secondaries() → only secondary items, path + summary
  - .summary() → all items (both tiers), path + summary, no content
  - .names() → comma-separated paths
  - .get(path) → DocItem or None
  - .count → total count
  - __str__ = .list()
  - __iter__ / __len__ / __getitem__ / __contains__

Tests in tests/unit/templating/test_docs_var.py:
- Construct with 2 primary + 2 secondary mock docs.
- Verify .list() includes all 4, primaries with content,
  secondaries with summary only.
- Verify .primaries() returns 2; .secondaries() returns 2.
- Verify .summary() returns all 4 as one-line entries.
- Verify iteration covers both tiers.

Scope: ~80 LOC code + ~50 LOC tests.

ESCALATION PATH: if the "primaries inlined + secondaries listed"
default-rendering format is ambiguous in the spec, escalate.

Acceptance: all unit tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 — DocsVar.

Checks:

1. All seven methods (.list / .primaries / .secondaries / .summary
   / .names / .get / .count) present.

2. Primary/secondary distinction is correct in .list() output.
   Verify by inspecting the rendered string from a mixed-tier test.

3. Pythonic protocols present (iter / len / getitem / contains).

4. .get("nonexistent") returns None.

5. DocItem fields match the spec's §4.4 list.

Verdict: PASS or FAIL.
```

---

## Step 5 — `WorkspaceVar` + `workspace_summary` wiring

- **Builds:** the `workspace` variable (single-object). Attribute access for path, summary, allowed_paths, denied_paths. Default rendering format.
- **Depends on:** Step 4 PASS.
- **Acceptance:** WorkspaceVar produced from a `platform.yaml` payload; summary is `None` when unset.

### Developer prompt

```
[standing instructions]

Step 5 — Implement WorkspaceVar.

Read first:
- process/phase2.2-agent-envelope-specs.md §4.4 (workspace),
  Part 7 (workspace_summary).

Deliver:

src/templating/vars/workspace.py:
- WorkspaceVar class with attributes:
  - .path (str)
  - .summary (str | None)
  - .allowed_paths (list[str])
  - .denied_paths (list[str])
  - __str__ = default rendering per patch §4.4 (summary block if
    set, then path, then allow/deny lists)
  - __getattr__ raises AttributeError on unknown attrs (typos)

Tests in tests/unit/templating/test_workspace_var.py:
- Construct WorkspaceVar with summary set → .summary returns text.
- Construct with summary unset → .summary returns None.
- {{ workspace }} default rendering includes summary block when
  set, omits it when None.
- Typo'd attribute (workspace.allowd_paths) raises AttributeError.
- workspace.allowed_paths returns the list correctly.

Scope: ~50 LOC code + ~40 LOC tests.

Acceptance: all unit tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5 — WorkspaceVar.

Checks:

1. Four attributes present (.path / .summary / .allowed_paths /
   .denied_paths).

2. .summary returns None when workspace_summary is unset in the
   source platform.yaml (not empty string).

3. Default rendering matches patch §4.4 — summary block omitted
   when None.

4. Typo'd attribute raises AttributeError.

5. NO list-shaped methods on WorkspaceVar (it's single-object).
   No .list(), .summary() method (collides with .summary attribute
   — be careful), .get(), etc.

Verdict: PASS or FAIL.
```

---

## Step 6 — `AgentVar`

- **Builds:** the `agent` variable (single-object). Self-introspection. Attribute access for name, id, model, tools, permissions, context_files.
- **Depends on:** Step 5 PASS.
- **Acceptance:** AgentVar constructible from a resolved agent config; attributes return typed objects (not raw dicts).

### Developer prompt

```
[standing instructions]

Step 6 — Implement AgentVar.

Read first:
- process/phase2.2-agent-envelope-specs.md §4.4 (agent
  attributes).

Deliver:

src/templating/vars/agent.py:
- AgentVar class with attributes:
  - .name (str)
  - .id (str)
  - .model (ModelSpec object with .ref, .provider, .model,
    .context_length, .supports_tools, .fallback)
  - .tools (alias for top-level ToolsVar — same instance)
  - .permissions (list[Rule] objects)
  - .context_files (list[ContextFile] objects)
  - __str__ = "<name> (<id>) — <provider>/<model>"
  - __getattr__ raises AttributeError on typos

Reuse existing ModelSpec / Rule / ContextFile models from src/schemas/
or wherever Phase 2.0 / 2.1 defined them. Don't duplicate.

Tests in tests/unit/templating/test_agent_var.py:
- Construct from a resolved agent config.
- agent.model returns a ModelSpec object (not a string).
- agent.model.context_length returns int or None.
- agent.tools is the same instance as the top-level tools variable.
- agent.permissions returns Rule objects.
- Typo'd attribute raises.

Scope: ~40 LOC code + ~30 LOC tests.

ESCALATION PATH: if ModelSpec lacks the .context_length /
.supports_tools attributes (Phase 1.1 might have just stored the
raw model ref), escalate — those attributes need to be sourced
from the provider's catalog entry.

Acceptance: all unit tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 6 — AgentVar.

Checks:

1. agent.model returns the ModelSpec OBJECT, not a string. Verify
   .ref / .provider / .model / .context_length attributes exist.

2. agent.tools is the same instance as the top-level tools
   variable (object identity, not just equal lists).

3. Typo'd attribute raises AttributeError.

4. ModelSpec is reused from existing Phase 1.1 schemas, not
   redefined.

Verdict: PASS or FAIL.
```

---

## Step 7 — `PlatformVar` + `platform.providers` list-shape

- **Builds:** the `platform` variable. Attribute access for `providers` (list-shaped), `authorization`, `workspace_summary`, `file_access`, `aliases`.
- **Depends on:** Step 6 PASS.
- **Acceptance:** PlatformVar built from a resolved platform.yaml; providers exposes the standardized method family.

### Developer prompt

```
[standing instructions]

Step 7 — Implement PlatformVar (and its nested list-shaped objects).

Read first:
- process/phase2.2-agent-envelope-specs.md §4.4 (platform).

Deliver:

src/templating/vars/_platform.py  # underscore prefix to avoid
                                    # clashing with stdlib `platform`;
                                    # exported as `platform` in the
                                    # template context.

- PlatformVar with attributes:
  - .providers (list-shaped: ProvidersVar wrapping ProviderConfig items)
  - .authorization (AuthorizationVar: .variant, .rules)
  - .workspace_summary (same value as WorkspaceVar.summary)
  - .file_access (FileAccessVar: .allowed_paths, .denied_paths)
  - .aliases (AliasesVar: .get(name), dict-shaped)
  - __str__ = default rendering per patch §4.4
  - __getattr__ raises on typos

- ProvidersVar:
  - .list() / .names() / .get(name) / .count + Pythonic protocols
  - .get(name) returns ProviderConfig or None

Tests in tests/unit/templating/test_platform_var.py:
- Construct PlatformVar from a sample platform.yaml dict.
- platform.providers.names() returns "openrouter, opencode" or similar.
- platform.providers.get("openrouter") returns a ProviderConfig
  object.
- platform.providers.get("nonexistent") returns None.
- platform.authorization.variant returns "rule_based" or whatever
  the config has.
- platform.aliases.get("opus") returns the canonical ref.

Scope: ~70 LOC code + ~50 LOC tests.

ESCALATION PATH: if reusing existing Phase 1.1 / 2.0 platform-config
models is awkward (because they're internal-only or have non-
templating-friendly shapes), escalate.

Acceptance: all unit tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 7 — PlatformVar.

Checks:

1. All five top-level attributes present.

2. platform.providers exposes the full method family (.list /
   .names / .get / .count) PLUS the Pythonic protocols.

3. .get("nonexistent") on any sub-object returns None.

4. The module is named with an underscore prefix to avoid clashing
   with Python's stdlib platform module — confirm.

5. ProviderConfig objects expose their own attributes (.type,
   .api_key_secret, .models, etc.) per Phase 1.1 spec.

Verdict: PASS or FAIL.
```

---

## Step 8 — Schema additions

- **Builds:** the four new fields on agent / platform schemas. Pure schema layer; no runtime behavior.
- **Depends on:** Step 7 PASS.
- **Acceptance:** YAML files with the new fields parse without error; fields appear on ResolvedAgentConfig / ResolvedPlatformConfig.

### Developer prompt

```
[standing instructions]

Step 8 — Add the four new schema fields.

Read first:
- process/phase2.2-agent-envelope-specs.md Parts 1, 6, 7, 8.
- src/schemas/ (existing Pydantic models).

Updates:

1. Agent schema gains:
   - extends: str | None = None
   - permissions: list[Rule] = []  # Rule is the same shape as
     platform's authorization.config.rules entries
   - context_files: list[ContextFileSpec] = []  # new model with
     path/summary/primary

2. Platform schema gains:
   - workspace_summary: str | None = None

3. Validation:
   - context_files[].path must start with "kolsch:" or "workspace:";
     plain paths raise config.context_file_unprefixed.
   - permissions[].effect must be one of allow/prompt/deny.
   - extends, if set, must match the prefix grammar.

4. NO runtime behavior. The fields are stored on the resolved
   config but ignored by everything downstream until later steps
   wire them in.

Tests:
- Load a YAML with all four new fields → parses successfully.
- Load a YAML with context_files[].path = "plain.md" →
  config.context_file_unprefixed.
- Load a YAML without the new fields → parses successfully (back-
  compat).
- Load a YAML with permissions[].effect = "wrong" → validation
  error.

Scope: ~80 LOC code + ~50 LOC tests.

Acceptance: schema tests pass; existing Phase 1/2 schema tests
still pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 8 — schema additions.

Checks:

1. All four new fields present on the right models.

2. context_files path-prefix validation rejects plain paths at
   schema-load (not later).

3. NO runtime behavior — grep src/ for usage outside the schema
   files; should be empty (or test-only).

4. Backward compatibility verified: existing agent YAML files
   without the new fields parse and function as before.

5. The Rule type used by permissions: is the SAME type as
   platform's authorization.config.rules — not a duplicate model.

Verdict: PASS or FAIL.
```

---

## Step 9 — Permission composition (agent overrides platform)

- **Builds:** `AuthzProvider.check` accepts an optional `agent_permissions: list[Rule]` parameter. Composition walks agent rules → platform rules → default-deny.
- **Depends on:** Step 8 PASS.
- **Acceptance:** test cases verify the three composition paths.

### Developer prompt

```
[standing instructions]

Step 9 — Add per-agent permission composition.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 1.

Deliver:

src/providers/authz/rule_based.py extended:
- check method accepts agent_permissions: list[Rule] | None = None
- If agent_permissions is non-empty, evaluate them first (first-
  match-wins). Then platform rules. Then default-deny.

NO engine integration yet (Step 14 wires it). The check method's
signature is extended; downstream callers use the existing
default (None) and continue to work.

Tests in tests/component/authz/test_composition.py:
- Agent rule allow + platform rule deny → check returns ALLOW
  (agent wins).
- Agent rule absent + platform rule deny → check returns DENY
  (platform fallback).
- Both absent → DENY (default-deny tail).
- Empty agent_permissions list (default) → behaves identically
  to the pre-Phase-2.2 baseline.

Scope: ~70 LOC code + ~50 LOC tests.

ESCALATION PATH: if AuthzProvider's check method has a strict
positional signature that resists adding a keyword param,
escalate.

Acceptance: composition tests pass; existing Phase 1.x / 2.0
authz tests still pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 9 — permission composition.

Checks:

1. The check signature gains the new param as KEYWORD-ONLY with a
   None/empty default — no positional breakage.

2. Composition order: agent → platform → default-deny. Verify with
   the three documented tests.

3. agent_permissions=[] OR None produces identical behavior to
   pre-Phase-2.2. Run existing authz tests; all still pass.

4. The error code raised on deny includes a who_chain detail
   identifying which layer denied (agent vs platform).

Verdict: PASS or FAIL.
```

---

## Step 10 — `permissions.check` / `permissions.require` API

- **Builds:** `kolsch.tools.permissions` module exposing the two functions; ContextVar plumbing so the engine's tool dispatcher binds the AuthzProvider for the tool's lifetime.
- **Depends on:** Step 9 PASS.
- **Acceptance:** a tool calling permissions.check from its body gets the composed answer; out-of-context calls raise.

### Developer prompt

```
[standing instructions]

Step 10 — Add the tool-author permissions API.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 2.

Deliver:

src/tools/permissions.py (new):
- _authz_context: ContextVar with default None.
- check(who, what) → AuthzEffect:
  - Reads the context; if None, raises tool.permissions_no_context.
  - Calls the provider's check method.
- require(who, what):
  - Calls check; raises PlatformError("authz.denied", ...) on
    DENY or PROMPT.

src/engine/ (tool dispatcher):
- Before calling a tool function, set _authz_context to a wrapper
  that has the agent's permissions bound.
- After the tool function returns (success OR exception),
  CLEAR the ContextVar.

Tests in tests/component/tools/test_permissions_api.py:
- Tool calls permissions.check; correct effect returned.
- Tool calls permissions.require with denied target → raises
  PlatformError("authz.denied").
- Tool calls permissions.check OUTSIDE the engine dispatch
  context (in a unit test) → raises tool.permissions_no_context.
- After tool exits cleanly, ContextVar is cleared.
- After tool exits via exception, ContextVar is cleared.

Scope: ~70 LOC code + ~60 LOC tests.

Acceptance: API works from inside a real tool dispatched through
the engine; context isolation is verified.
```

### Reviewer prompt

```
[standing instructions]

Review Step 10 — permissions API.

Checks:

1. The module exposes ONLY check and require (and the AuthzEffect
   enum re-export). No surprise additions.

2. ContextVar is set AND cleared in BOTH success and exception
   paths.

3. tool.permissions_no_context fires when the API is called from
   outside the dispatcher.

4. require raises with structured detail including who_chain
   (which layer denied).

5. The error class is the existing PlatformError, not a new
   exception type.

Verdict: PASS or FAIL.
```

---

## Step 11 — `@tool(summary=...)` + builtin updates

- **Builds:** `@tool` decorator accepts `summary` kwarg; default derivation from first sentence of description; the four builtin tools get explicit summaries.
- **Depends on:** Step 10 PASS.
- **Acceptance:** ToolItem (Step 3) carries non-None summary; ToolsVar.summary() renders the right strings.

### Developer prompt

```
[standing instructions]

Step 11 — Add the summary field to @tool.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 5.

Tasks:

1. Decorator in src/tools/decorator.py accepts summary kwarg.

2. If summary is None, derive from description: the substring from
   start through the first period. Trim whitespace.

3. The stored tool metadata (whatever Phase 1's ToolInfo /
   ToolDefinition uses) carries the summary field.

4. Step 3's ToolItem (from src/templating/vars/tools.py) populates
   .summary from this metadata.

5. Update bundles/kolsch/tools/{read_file, write_file, list_files,
   bash}.py with explicit summary= per patch §5.1 (read_file's
   example) — extend to all four tools with consistent voice.

Tests in tests/unit/tools/test_decorator_summary.py:
- Explicit summary stored verbatim.
- Default derivation from description works.
- Description without a period → summary = full description.
- ToolItem.summary populated correctly for real builtin tools.

Scope: ~60 LOC code + ~40 LOC tests.

Acceptance: tool tests pass; rendering tests in Step 3's suite still
pass with real summaries.
```

### Reviewer prompt

```
[standing instructions]

Review Step 11 — @tool summary.

Checks:

1. Decorator accepts the kwarg; default derivation works.

2. All four builtin tools have EXPLICIT summary fields.

3. ToolItem.summary is populated (not None) for the builtin tools.

4. No regression in existing tool dispatch.

Verdict: PASS or FAIL.
```

---

## Step 12 — Context file resolution + loading

- **Builds:** the prefix resolver (`kolsch:` / `workspace:`); file loading with size cap; DocsVar can be constructed from a list of (path, summary, primary) entries that get resolved + loaded.
- **Depends on:** Step 11 PASS.
- **Acceptance:** test fixtures use both prefixes; size cap enforced; errors typed.

### Developer prompt

```
[standing instructions]

Step 12 — Implement context file resolution and loading.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 6.

Deliver:

src/templating/context_files.py (new):
- resolve_path(prefix_path, workspace_root) → Path
  - "kolsch:foo.md" → importlib.resources.files("kolsch") / "foo.md"
  - "workspace:foo.md" → workspace_root / "foo.md"
  - other → raise config.context_file_unprefixed

- load_context_file(spec: ContextFileSpec, workspace_root,
                    max_bytes=65536) → DocItem
  - Reads the file at the resolved path.
  - Raises context.file_not_found if missing.
  - Raises context.file_too_large if over cap.
  - Returns a fully-populated DocItem (Step 4's dataclass).

Tests in tests/component/templating/test_context_files.py:
- "kolsch:test_fixtures/sample.md" resolves correctly (use a
  synthetic test fixture inside src/kolsch_test_fixtures/ —
  not real kolsch docs).
- "workspace:CONVENTIONS.md" resolves to workspace_root / "CONVENTIONS.md".
- "plain.md" → config.context_file_unprefixed.
- Missing file → context.file_not_found.
- File at 64 KiB + 1 byte → context.file_too_large.
- File at 64 KiB exactly → loads.

Scope: ~100 LOC code + ~70 LOC tests.

ESCALATION PATH: if importlib.resources.files("kolsch") returns
something unusable in the source-tree dev environment (because
Step 1's ENV-R13 package-data setup didn't land correctly),
escalate to Step 1's reviewer.

Acceptance: tests pass; both prefixes work end-to-end.
```

### Reviewer prompt

```
[standing instructions]

Review Step 12 — context file resolution.

Checks:

1. Both prefixes resolve correctly. Verify in tests.

2. Plain paths raise config.context_file_unprefixed at the
   resolution layer (not just at schema-load, where Step 8 also
   rejects them).

3. Size cap is 64 KiB exactly. Boundary tested.

4. Missing file → context.file_not_found, not a generic OSError.

5. importlib.resources works in both source-tree and installed
   contexts. Test both if practical.

Verdict: PASS or FAIL.
```

---

## Step 13 — Agent inheritance (chain resolver + Jinja parent binding)

- **Builds:** the `extends:` chain resolver (with cycle detection, parent-not-found, multi-level); the Jinja template loader that binds `parent` to the parent's prompt; required-block enforcement.
- **Depends on:** Step 12 PASS.
- **Acceptance:** all 8 inheritance scenarios from the spec test cases pass.

### Developer prompt

```
[standing instructions]

Step 13 — Implement agent inheritance.

Read first:
- process/phase2.2-agent-envelope-specs.md Part 8.

Deliver:

src/templating/inheritance.py (new):

1. resolve_extends_chain(agent_path, workspace_root) →
   list[AgentConfig]:
   - Walks the extends chain from leaf (child) to root.
   - Uses Step 12's resolve_path for parent paths.
   - Detects cycles via visited set; raises
     config.agent_inheritance_cycle.
   - Raises config.parent_agent_not_found for missing parents.

2. apply_inheritance(chain) → ResolvedAgentConfig:
   - Starts from the root (least-derived).
   - For each child in the chain, applies wholesale field
     override: any field set in child REPLACES the corresponding
     parent value (no merging within lists or dicts).
   - Returns the final resolved config.

3. Jinja loader for prompt inheritance:
   - Custom jinja2 BaseLoader that, when rendering a child's
     prompt, makes `parent` available as a loaded template name.
   - The parent template's source is the parent agent's prompt
     string from the resolved chain.
   - Jinja 3.1's {% block X required %}{% endblock %} natively
     raises jinja2.exceptions.TemplateAssertionError when a child
     extends but doesn't override a required block. Catch this
     and re-raise as config.required_block_missing.

Tests in tests/component/templating/test_inheritance.py:
- Single-level: A extends B; A overrides model, inherits tools.
- Multi-level: A extends B extends C; A's overrides win.
- Cycle: A extends B extends A → config.agent_inheritance_cycle.
- Parent not found → config.parent_agent_not_found.
- Required block missing → config.required_block_missing.
- Required block present → renders.
- Child uses {% extends parent %} without extends: in YAML →
  config.extends_template_without_inheritance.
- Child writes fresh prompt without {% extends parent %} →
  parent's required blocks NOT enforced.

Scope: ~120 LOC code + ~100 LOC tests.

ESCALATION PATH: if Jinja's BaseLoader can't cleanly bind a named
template from a string, escalate (we may need to write a custom
loader or use a DictLoader pre-populated with the parent).

Acceptance: all 8 scenarios pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 13 — agent inheritance.

Checks:

1. Chain resolver handles multi-level correctly. Verify A→B→C with
   each level having different fields.

2. Cycle detection works — verify by constructing one and
   confirming the error.

3. Field overrides are WHOLESALE — verify with a test where parent
   and child both have `tools:`; resolved should be child's list
   exclusively, not a merge.

4. Required block enforcement: verify both directions (block
   missing → fail; block present → render).

5. Child without `{% extends parent %}` is unaffected by parent's
   required blocks. This is the "fresh prompt" path.

Verdict: PASS or FAIL.
```

---

## Step 14 — Engine pre-flight integration + warnings ← USER-VISIBLE PIVOT

- **Builds:** engine pre-flight gains steps 5.6.5 (inheritance) and 5.6.6 (render); template-vs-config consistency walker emits `agent.template_warning` events.
- **Depends on:** Step 13 PASS.
- **Acceptance:** real agents render their Jinja prompts; warnings emit; backward compatibility holds.

### Developer prompt

```
[standing instructions]

Step 14 — Wire everything into the engine pre-flight.

This is the PIVOT step — user-visible behavior changes here.

Read first:
- process/phase2.2-agent-envelope-specs.md Parts 3, 9.
- The current engine pre-flight code (per ENV-R4 in Step 1, steps
  5.6.5 and 5.6.6 are documented insertion points).

Engine changes:

1. After step 5.6 (config resolved), insert step 5.6.5:
   - If config.extends is set, run resolve_extends_chain and
     apply_inheritance (Step 13).
   - The resolved AgentConfig is what subsequent steps see.

2. Insert step 5.6.6 (render prompt template):
   - Build the variable context: ToolsVar/DocsVar/WorkspaceVar/
     AgentVar/PlatformVar populated from the resolved config +
     runtime state.
   - Read context_files paths and load via Step 12.
   - Call render_prompt (Step 2) with the populated context.
   - The rendered string replaces the raw prompt in the resolved
     config; downstream code (the LLM-call construction) reads
     this rendered string.

3. Template-config consistency warning system:
   - Parse the prompt's Jinja AST.
   - Walk variable accesses (tools, docs, workspace, etc.).
   - For each accessed variable that has no data in the resolved
     config, emit an agent.template_warning event with reference,
     reason, hint per patch Part 9.

4. Backward compatibility:
   - A prompt with no Jinja syntax (no {{ }} or {% %}) renders to
     itself. Phase 1.x agent files MUST continue to work
     unchanged.

Tests in tests/integration/test_engine_envelope.py:
- An agent with Jinja prompt + context_files + extends gets a
  resolved + rendered prompt at exchange start.
- A Phase 1.x agent with plain-text prompt renders unchanged.
- An agent whose prompt references docs but has no context_files
  declared emits agent.template_warning.
- The model.request event carries the rendered prompt string
  (not the raw template).

Scope: ~120 LOC code + ~100 LOC tests.

ESCALATION PATH: if the engine's existing pre-flight is a single
long function rather than a numbered list of discrete steps,
escalate — refactoring the pre-flight is bigger than this step.

Acceptance: real `kolsch run` against a Jinja-templated agent
produces the expected rendered prompt in the JSONL; existing tests
still pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 14 — engine pivot. Be thorough; user-visible behavior
changes here.

Architectural checks:

1. The two new steps (5.6.5, 5.6.6) are at the correct positions
   per ENV-R4 / the engine contract. Read the code top-to-bottom.

2. The rendered prompt REPLACES the raw prompt in the resolved
   config. Inspect a captured model.request event and verify.

3. Backward compatibility: a Phase 1.x agent (plain-text prompt)
   renders unchanged. Run an existing agent through; confirm.

4. agent.template_warning emits as a REAL event in the JSONL, not
   just a log line.

Functional checks:

5. End-to-end smoke: edit bundles/kolsch/agents/helper.yaml to use
   Jinja. Run a chat session. Confirm the model receives the
   rendered prompt (inspect model.request event).

6. Run TC-CHAT and TC-ENG and TC-ENV tests; no regressions in
   any.

7. The warning surfaces in the chat's system-notice capsule per
   Phase 2.1's renderer. Manual verification.

8. The F5 fix from qol-chat-capsule-layout.md still holds —
   _THINKING_INDICATOR is not reintroduced by the rendering
   changes. grep src/cli/_renderer.py.

Verdict: PASS or FAIL.
```

---

## Step 15 — Regression tests + manual end-to-end

- **Builds:** TC-ENV-01..12 (per ENV-R8 in Step 1) implemented; manual end-to-end captured in the PR description.
- **Depends on:** Step 14 PASS.
- **Acceptance:** all 12 tests pass; coordinator-confirmed real session against the updated helper agent.

### Developer prompt

```
[standing instructions]

Step 15 — TC-ENV-01..12 + manual verification.

Read first:
- process/test-catalog.md Part 9.10 (the 12 test cases).

Tests in tests/integration/test_agent_envelope.py (or equivalent):

For each TC-ENV-01..12, follow the Setup/Action/Expected/Verifies
spec. These are integration-level tests through the engine's
public API.

Manual end-to-end:

1. Edit bundles/kolsch/agents/helper.yaml to use the new fields:
   - extends a small _base.yaml (write it if needed)
   - context_files with at least one kolsch: and one workspace:
     entry
   - permissions with a couple of rules that override platform's
     defaults
   - Jinja-templated prompt using all five top-level variables

2. Run `kolsch chat` against the updated helper.

3. Verify by inspecting the session JSONL:
   - model.request event includes the fully rendered system prompt
     with tools described, docs included, workspace summary
     present.
   - Agent permissions override platform permissions (test by
     constructing a conflict).
   - agent.template_warning surfaces if you intentionally create
     an inconsistency.

4. Capture the rendered prompt + relevant JSONL excerpts in the
   PR description.

Scope: ~250 LOC test code + manual verification (no production
code unless Step 14 left a gap).

Acceptance: TC-ENV-01..12 all pass; coordinator reads the captured
artifact and confirms.
```

### Reviewer prompt

```
[standing instructions]

Review Step 15 — final.

Checks:

1. All 12 TC-ENV-* pass.

2. PR description includes a real session capture: agent YAML,
   rendered system prompt (from model.request event), and any
   warnings emitted.

3. Captured rendered prompt visibly includes:
   - Tool descriptions (from {{ tools }})
   - Workspace summary + path (from {{ workspace }})
   - Inlined primary docs + listed secondary docs (from {{ docs }})

4. Full test suite green — Phase 1.x, 2.0, 2.1, 2.2 all pass.

5. Existing Phase 1.x agents (test fixtures) continue to work
   unchanged.

Verdict: PASS or FAIL. On PASS, Phase 2.2 is closed.
```

---

## How to escalate

```markdown
### Escalation E-2.2-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–15
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-2.2-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate (high-likelihood triggers):
- Jinja's StrictUndefined breaks the `default` filter (Step 2).
- importlib.resources.files("kolsch") fails in the source-tree
  context (Steps 1/12).
- AuthzProvider's check signature resists adding agent_permissions
  cleanly (Step 9).
- Engine pre-flight is a single long function, not discrete steps
  (Step 14).
- Step 15 reveals a behavioral gap pointing back at Step 14 or
  earlier.

When NOT to escalate:
- Exact error message wording.
- Choice of dataclass vs Pydantic for item structures.
- Naming bikeshed (`_platform.py` vs alternatives).

---

## Coordinator Escalation Log

*(no entries yet)*

---

## Notes on running these 15 steps

- **Strict order: 1 → 2 → 3 → ... → 15.** Step 14 is the user-
  visible pivot; coordinator should review carefully there.
- **Backward compatibility is non-negotiable.** Existing Phase 1.x
  agent prompts MUST continue to work unchanged. Step 14's reviewer
  Q3 enforces this; Step 15's reviewer also confirms.
- **The F5 / qol-chat-capsule-layout.md fix discipline must hold
  through Step 14.** `_THINKING_INDICATOR` removal is in a parallel
  fix; Step 14 should NOT reintroduce it. Step 14's reviewer Q8
  enforces.
- **Steps 3–7 are independent of each other in principle** (each
  variable object stands alone) but listed in dependency order for
  clarity. Could be parallelized across developers if needed.
- **Each step's reviewer prompt has ≤6 checks** — small enough for
  one focused review session.

---

## What this build plan does NOT include

- **Mid-execution permission prompts** for tools (patch §12 item 1).
- **Multi-parent inheritance** (patch §12 item 2).
- **`abstract: true` field on parent agents** (patch §12 item 3).
- **Strictness escalation** (`agent_template_strictness: fatal`).
- **Selective field merging in inheritance** — wholesale-only.
- **Custom Jinja filters** beyond stdlib.
- **`{{ memory }}` / `{{ session }}` / `{{ now }}` variables** —
  Phase 2.5+.
- **`examples` / `category` fields on `@tool`** — minimal decorator
  in 2.2.
- **Workspace tree rendering** — explicitly NOT added per patch
  §12 item 10.
