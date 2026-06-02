# Phase 2.2 Step 15 — Manual Verification Capture

This is the captured-artifact deliverable for Step 15. Three real
engine executions exercise the full Phase 2.2 envelope surface:

- **Scenario A** — tools-wired Jinja prompt with primary docs. Verifies
  the §4.4 standardized variable family (`tools.count`,
  `tools.summary()`, `workspace.path`, `docs.primaries()`,
  `{{ platform }}`) substitutes real data through to the model's
  system message.
- **Scenario B** — intentional template-vs-config inconsistency.
  Verifies the engine emits `agent.template_warning` events per
  §9 with the structured `{reference, reason, hint}` payload.
- **Scenario C** — `extends:` chain with parent inheritance.
  Verifies §8.2 wholesale per-field override: the leaf inherits
  the parent's `permissions` (silent on the field) while
  overriding `model` (explicitly set on the leaf).

Each scenario runs through `engine.execute()` against
`FakeFramework` + `FakeLLM`; the `model.request` system message is
captured straight from the path's events (no synthetic
reconstruction). The captures are byte-identical to what would
land in `~/.platform/sessions/<id>/events.jsonl` under a real
`kolsch chat` run against the same configs.

The capture script lives at `/tmp/capture_artifact.py` for
reproducibility; see the "Reproducing" section at the bottom.

---

## Scenario A — Tools-wired Jinja prompt with primary docs

### Agent shape

```yaml
agent_id: ag_demo
name: demo
description: Phase 2.2 envelope capture.
model: { provider: fake, model: fake-model }
prompt: |
  You are {{ agent.name }} ({{ agent.id }}).

  ## Tools available ({{ tools.count }})
  {{ tools.summary() }}

  ## Workspace
  Path: {{ workspace.path }}

  ## House rules (primary doc)
  {{ docs.primaries() }}

  ## Platform
  {{ platform }}
context_files:
  - path: workspace:docs/house_rules.md
    summary: House rules for this workspace.
    primary: true
subsystems:
  tools:
    variant: standard
    config: {}
```

Two real tools register against `ag_demo`:

- `search` — `@tool(name="search", summary="Find files matching a
  query.", ...)`
- `bash_demo` — `@tool(name="bash_demo", description="Execute a
  shell command in the workspace and return its output. Use for
  any operational task that isn't a file read/write.")` (no
  explicit summary; first-sentence derivation lands).

A workspace doc at `<root>/docs/house_rules.md`:

```markdown
# House rules

Be terse. Cite sources. Confirm destructive ops.
```

### Rendered system prompt (from `config.set.data.resolved_config.prompt`)

```
You are demo (ag_demo).

## Tools available (2)
search: Find files matching a query.
bash_demo: Execute a shell command in the workspace and return its output.

## Workspace
Path: /var/folders/.../T/k15_ocve9a9r

## House rules (primary doc)
### workspace:docs/house_rules.md
# House rules

Be terse. Cite sources. Confirm destructive ops.


## Platform
Workspace: /var/folders/.../T/k15_ocve9a9r · Providers: <none> · 0 rules
```

### `model.request` system message

Byte-identical to the rendered prompt above — the LLM literally
sees the substituted text (no `{{ }}` markers remain). This is the
load-bearing Step 14 wiring proof.

### Event sequence

```
session.opened → config.set → thread.exchange_started → user.message
→ model.request → model.response → agent.message → thread.exchange_completed
```

Zero `agent.template_warning` events — the prompt references only
data the agent actually has. ✓

### Observations

- `tools.count` substitutes the integer `2`.
- `tools.summary()` produces one line per tool with the @tool
  summary (explicit for `search`, first-sentence-derived for
  `bash_demo` per Step 11's `_derive_summary`).
- `workspace.path` substitutes the absolute root.
- `docs.primaries()` emits the spec §6.3 two-tier format —
  `### <path>` header + the file's content verbatim.
- `{{ platform }}` triggers the default-render (Phase 2.2 §4.4
  one-liner). No `Providers` connected and 0 authz rules because
  the smoke runs against minimal platform fixtures; a real
  workspace would substitute real provider names.

---

## Scenario B — Intentional inconsistency (warning events fire)

### Agent shape

```yaml
agent_id: ag_demo
prompt: "System: {{ docs }} — tools: {{ tools }}"
# no context_files
# no subsystems.tools
```

The prompt references both `{{ docs }}` and `{{ tools }}` but the
agent declares neither. Per §9.1 the engine emits one warning per
reference.

### Rendered system prompt

```
System:  — tools:
```

Both references rendered as empty strings (Phase 2.2 §3.4: empty
data is rendered as empty, not as an error — the consistency
warning system catches the inconsistency without blocking the
exchange).

### `agent.template_warning` events

Two events landed in the JSONL in declaration order:

```json
{
  "reference": "docs",
  "reason": "prompt references {{ docs }} but the agent declares no context_files",
  "hint": "add context_files entries to the agent YAML, or remove the docs reference from the prompt"
}
```

```json
{
  "reference": "tools",
  "reason": "prompt references {{ tools }} but the agent declares no tools subsystem",
  "hint": "add a tools subsystem to the agent YAML, or remove the tools reference from the prompt"
}
```

### Event sequence

```
session.opened → config.set
→ agent.template_warning (×2)
→ thread.exchange_started → user.message → model.request
→ model.response → agent.message → thread.exchange_completed
```

The warnings emit immediately after `config.set` (so they carry
`config.set` as parent_event_id, surfacing at "agent setup"
logically per §9.4). The exchange still completes — warnings are
Warning-level, not Fatal. The chat renderer (Step 14 Check 7
fix) surfaces each as a `SYSTEM_COLOR` capsule at session start.

### Observations

- Both warnings carry actionable hints — the user knows the exact
  YAML edit to make.
- Detection is via Jinja's AST walker (`meta.find_undeclared_variables`),
  so it fires once per top-level variable at agent-load, not once
  per render call (§9.2 "catches the problem ONCE per session").
- Render proceeds (no Fatal) — the empty-data case is sometimes
  intentional (parent template references `docs`, child may
  silently inherit), per §9.3.

---

## Scenario C — `extends:` chain with parent inheritance

### Agent shape

Parent at `<workspace>/agents/_base.yaml`:

```yaml
model: { provider: anthropic, model: claude-opus-4-7 }
permissions:
  - { who: ".*", what: "tool\\.read_file", effect: allow }
description: "Base agent — leaf inherits all fields it does not set."
```

Leaf agent:

```yaml
agent_id: ag_demo
name: demo
description: Phase 2.2 envelope capture.
model: { provider: fake, model: fake-model }   # leaf overrides
extends: workspace:agents/_base.yaml
prompt: |
  You are {{ agent.name }}.
  Inherited model: {{ agent.model.provider }}/{{ agent.model.model }}.
  Inherited permissions count: {{ agent.permissions | length }}.
```

The leaf is **silent on `permissions`** (no field on the YAML; the
Pydantic default would be `[]` but the engine dumps with
`exclude_unset=True` so silence is preserved) and **explicitly sets
`model`** (overrides wholesale per §8.2).

### Rendered system prompt

```
You are demo.
Inherited model: fake/fake-model.
Inherited permissions count: 1.
```

### Observations

- `Inherited model: fake/fake-model` confirms the leaf's explicit
  set won over the parent's `anthropic/claude-opus-4-7`.
- `Inherited permissions count: 1` confirms the parent's
  `permissions` list survived intact — the leaf's silence on the
  field meant "inherit", not "explicit empty".
- The `exclude_unset=True` fix (Step 15's Step 14 follow-up) is
  load-bearing here: without it, Pydantic's default-fill would
  have overridden the parent with `[]`.

### Event sequence

```
session.opened → config.set → thread.exchange_started → user.message
→ model.request → model.response → agent.message → thread.exchange_completed
```

Zero warnings, zero failures. The `config.set` event captures
the fully-merged `ResolvedAgentConfig` — `model` reflects the
leaf, `permissions` reflects the parent, `extends` is preserved
verbatim (so replay reconstructs the same merge).

---

## Cross-scenario invariants

- **`model.request.data.messages[0].content` is byte-identical to
  `config.set.data.resolved_config.prompt`** in all three captures.
  The LLM sees what the JSONL records.
- **No `{{ }}` markers leak through** to either the captured
  prompt or the model message. StrictUndefined catches typos
  before they reach the LLM (verified in TC-ENV-12).
- **`exclude_unset=True` discipline** preserves leaf silence ↔ inherit
  semantics across the full envelope.

---

## Reproducing

The captures above were produced by `/tmp/capture_artifact.py`
(the script lives outside the repo to keep this artifact a
write-once record). To regenerate, run:

```bash
uv run python /tmp/capture_artifact.py
```

The script prints the three scenario results as JSON; this
markdown reflects the live output verbatim (modulo formatting).

The same end-to-end behavior is also exercised programmatically
by `tests/integration/test_agent_envelope.py` (TC-ENV-01..12, all
passing).
