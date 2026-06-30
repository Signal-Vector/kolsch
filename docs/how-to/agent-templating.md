# Templated Agent Prompts

Recipes for writing agent prompts that use the Phase 2.2 Jinja2
template variables — the kolsch envelope. For the *why* of the
design, read the [Phase 2.2 envelope spec](../../process/phase2.2-agent-envelope-specs.md);
for the [agent-engine contract §2.1](../explanation/agent-engine-contract.md)
covers where in pre-flight the render happens. The first-agent
tutorial in [`docs/tutorials/first-agent.md`](../tutorials/first-agent.md)
walks the loop from a fresh `kolsch setup` to a templated helper.

The six standardized variables — `tools`, `docs`, `skills`,
`workspace`, `agent`, `platform` — are all available in every
prompt; the engine populates them at pre-flight step 5.6.6 with
`StrictUndefined` so typos surface as
`config.template_render_failed` at agent-load.

---

## Write your first templated prompt

Start with a plain-text prompt and add one variable at a time. A
prompt with no Jinja markers (`{{`, `{%`) renders byte-identical
to itself, so converting in place is risk-free.

```yaml
# bundles/<bundle>/agents/helper.yaml
prompt: |
  You are {{ agent.name }}, a friendly assistant.
  Answer concisely. If a question is ambiguous, ask one
  clarifying question before acting.
```

Run `kolsch chat` against the agent and inspect the `model.request`
event in the resulting JSONL — the system prompt sent to the model
has `{{ agent.name }}` substituted for the agent's display name.

If you typo a variable name (`{{ agnet.name }}`), agent-load fails
with:

```
config.template_render_failed
  template: bundles/<bundle>/agents/helper.yaml
  reason: 'agnet' is undefined
```

…and `kolsch chat` exits before the model is ever called. That's
the StrictUndefined contract — fail loud at agent-load, never
silently send broken context to the LLM.

---

## Use the six top-level variables

### `agent`

Self-introspection. Single-object — typed attributes, no methods.

```yaml
prompt: |
  You are {{ agent.name }} ({{ agent.id }}).
  You run on {{ agent.model.provider }}/{{ agent.model.model }}.
```

### `tools`

The agent's resolved tool set. List-shaped with the standard
method family (`.list()`, `.summary()`, `.names()`, `.get(name)`,
`.count`) and the Pythonic protocols.

```yaml
prompt: |
  ## What you can do
  {% for t in tools %}
  - **{{ t.name }}**: {{ t.summary }}
  {% endfor %}

  You have {{ tools.count }} tool(s) available.
```

Compact alternative — `{{ tools }}` is shorthand for
`{{ tools.list() }}`:

```yaml
prompt: |
  ## What you can do

  {{ tools }}
```

The `summary` field is what `@tool(summary=...)` set on the tool
function; if the author omitted it, the platform derives a
default from the first sentence of the tool's `description`.
The model still receives the full `description` via the
upstream LLM API's `tools=` parameter — `tools.summary()` only
affects the prompt's conversational tool section.

### `docs`

The agent's resolved `context_files:`. List-shaped with an
extra `.primaries()` / `.secondaries()` partition.

```yaml
prompt: |
  ## Your documentation

  {{ docs }}
```

Default rendering inlines `primary: true` entries with full
content and lists `primary: false` entries by path + summary
(the agent reads them on demand via `read_file`).

To force one tier:

```yaml
prompt: |
  ## Hard rules (always read these first)

  {% for d in docs.primaries() %}
  ### {{ d.path }}
  {{ d.content }}
  ---
  {% endfor %}

  ## Also available
  {% for d in docs.secondaries() %}
  - **{{ d.path }}**: {{ d.summary }}
  {% endfor %}
```

### `skills`

The agent's enabled skill set (per `subsystems.skills.config.enabled`
— `disable-model-invocation: true` skills are excluded). Symmetric
with `tools`: list-shaped, same method family (`.list()`,
`.summary()`, `.names()`, `.get(name)`, `.count`), same Pythonic
protocols. Per-skill fields: `name`, `description`, `ref` (the
3-segment catalog ref).

```yaml
prompt: |
  ## Available skills

  {% for s in skills %}
  - **{{ s.name }}** — {{ s.description }}
  {% endfor %}
```

Compact alternative — `{{ skills }}` defaults to `{{ skills.list() }}`:

```yaml
prompt: |
  ## Available skills

  {{ skills }}
```

The default `.list()` rendering is `- **<name>** — <description>
Path: \`<ref>/SKILL.md\`` per skill — matches the pre-QoL
auto-appended block byte-identically, so existing prompts that
add a `{{ skills }}` placeholder produce the same content the
platform used to bolt on.

**Migration note** — before
qol-skills-jinja-variable-symmetric-with-tools, the engine
unconditionally appended `## Available Skills` to every rendered
prompt. That post-append is gone; the template now controls
placement / wording / whether to include the section at all. An
agent that declares skills but whose template omits
`{{ skills }}` emits `agent.template_warning` at load with a
pointer to this section.

### `workspace`

Single-object describing the active workspace. Attributes:
`workspace.summary` (text from `platform.yaml:workspace_summary`,
or `None`), `workspace.path` (absolute root), and the
`workspace.allowed_paths` / `workspace.denied_paths` lists from
`file_access`.

```yaml
prompt: |
  ## Where you are

  {{ workspace }}
```

Default rendering produces the workspace summary block + path +
allow/deny lists. To customize:

```yaml
prompt: |
  Working directory: {{ workspace.path }}
  {{ workspace.summary | default("This is an active Kolsch workspace.") }}
  You can read from: {{ workspace.allowed_paths | join(", ") }}
```

### `platform`

Introspection over the active `platform.yaml`. Nested objects
for `providers` (list-shaped), `authorization`, `file_access`,
`aliases`, plus a flat `platform.workspace_summary` accessor.

```yaml
prompt: |
  ## Available providers

  {% for p in platform.providers %}
  - **{{ p.name }}** ({{ p.type }})
  {% endfor %}

  Active rules: {{ platform.authorization.rules | length }}
```

`platform.providers.get("openrouter")` returns the
`ProviderConfig` or `None`; combined with `| default("…")` it's
useful for prompts that adapt to whether a specific provider is
connected.

---

## Mix compact and explicit styles

Two ways to render the same content:

**Compact** — relies on the default rendering of each variable.

```yaml
prompt: |
  You are {{ agent.name }}, a friendly assistant.

  {{ workspace }}

  {{ tools }}

  {{ docs }}
```

**Explicit** — full control via for-loops + conditionals.

```yaml
prompt: |
  You are {{ agent.name }}.

  ## Where you are
  {{ workspace.summary | default("Active Kolsch workspace.") }}
  Path: {{ workspace.path }}

  ## What you can do
  {% for t in tools %}
  - **{{ t.name }}**: {{ t.summary }}
  {% endfor %}

  ## Your documentation
  {% for d in docs.primaries() %}
  ### {{ d.path }}
  {{ d.content }}
  ---
  {% endfor %}
```

Mix freely. Compact for the prompt's framing; explicit when you
need conditional logic (`{% if 'bash' in tools %}…{% endif %}`)
or per-item formatting.

---

## Extend a parent agent

Phase 2.2 supports single-parent inheritance via `extends:`.
The child's fields override the parent's wholesale per field —
no merging within lists. Three patterns.

### Inherit prompt structure; fill in blocks

The parent declares a prompt with `{% block X %}…{% endblock %}`
placeholders; the child fills them via `{% extends parent %}`.

```yaml
# bundles/kolsch/agents/helper.yaml (the kolsch starter helper)
prompt: |
  You are {{ agent.name }}, {% block role %}a friendly general-purpose helper agent{% endblock %}
  running on {{ agent.model.ref }}.

  ## Where you are

  {{ workspace }}

  ## What you can do

  {% for t in tools %}
  - **{{ t.name }}**: {{ t.summary }}
  {% endfor %}

  {% block special_instructions %}{% endblock %}

  Be concise. If the question is ambiguous, ask one clarifying
  question before acting.
```

```yaml
# bundles/<bundle>/agents/butler.yaml
extends: kolsch/helper
name: butler
description: A personal butler agent inheriting from kolsch helper.
prompt: |
  {% extends parent %}
  {% block role %}a personal butler — attentive, precise, and proactive{% endblock %}
  {% block special_instructions %}
  Manage files, run commands, keep the workspace organized.
  {% endblock %}
```

`{% extends parent %}` is the Phase 2.2 wiring — the engine
binds the parent template's source under the name `"parent"`
and the child inherits all of the parent's prose plus any
non-overridden blocks. Blocks the child does override (`role`,
`special_instructions`) get replaced wholesale. This shape
matches the EXT-CORPUS C1 fixture at
`tests/fixtures/extends/c/c1_parent.yaml` +
`tests/fixtures/extends/c/c1_child.yaml`; every example in
this section corresponds to a corpus case so the docs and the
tests can't drift.

`{% block role required %}` (Jinja 3.1's required-block syntax)
forces every inheriting child to override the block — see the
[Declare required blocks](#declare-required-blocks) section
below.

### Inherit everything except the prompt

If the child writes a prompt without `{% extends parent %}`,
the parent's prompt is ignored entirely — but every other
field the child doesn't redefine is inherited (model, tools,
permissions, context_files).

```yaml
# bundles/<bundle>/agents/deploy.yaml — EXT-CORPUS B4 shape
extends: kolsch/helper
name: deploy
description: A deployment agent inheriting kolsch/helper's tool set.
prompt: |
  You are a deployment agent. Run kubectl and helm commands the
  user asks for. Confirm any destructive operation by repeating
  the command first.
```

### Multi-level chains

`A extends B extends C` is supported — see
[Multi-level chains](#multi-level-chains) below for a worked
example and how the chain composes at render time.

---

### Resolving the parent reference

`extends:` accepts the canonical 2-segment catalog ref
`<bundle>/<name>` per
[qol-prefix-defaults-and-unified-refs.md](../../process/qol-prefix-defaults-and-unified-refs.md);
asset type is inferred from the field (`extends:` → `agent`).
The resolver also accepts file-path forms for migration:

- **`kolsch/<name>`** — the canonical form. Resolves against
  the **workspace's `bundles/kolsch/` bundle** (the one `kolsch
  setup` materialised from the starter bundle on first run). So
  in a workspace that ran `kolsch setup`, `extends: kolsch/helper`
  resolves to `bundles/kolsch/agents/helper.yaml`.
- **`workspace:<rel>`** — explicit workspace-root file path
  (e.g. `extends: workspace:bundles/kolsch/agents/helper.yaml`).
  Still parses, emits a `config.deprecated_ref_form`
  Recoverable warning. Equivalent to the canonical form above.
- **`kolsch:<rel>`** — resolves against the **installed kolsch
  package** via `importlib.resources.files("kolsch") / <rel>`.
  Today the package ships only `docs/` and `AGENTS.md` (see
  `src/kolsch/__init__.py`); **no agents are reachable via
  `kolsch:`**.
  <!-- lint-allowed-kolsch-agents: intentional warning callout -->
  The pre-QoL examples in older docs that used
  `extends: kolsch:agents/<name>.yaml` were broken on
  publication and will fail with `config.parent_agent_not_found`
  — use the catalog-ref form above instead.

Missing parents raise `config.parent_agent_not_found`; cycles
raise `config.agent_inheritance_cycle`. Both are Fatal at
agent-load.

### Common gotchas

Three failure modes the EXT-CORPUS (`process/test-catalog.md`
Part 9.10.1) pins as regression cases. Each is the kind of
thing that surfaces once when you write your first inheriting
agent and never again.

**Stray text outside a block is silently discarded** (EXT-CORPUS
C3). Jinja's `{% extends %}` directive only renders block
content; everything else in the child template is dropped on
the floor — no warning, no error.

```yaml
# parent: plain prompt, no blocks
prompt: |
  Plain parent body.

# child: the "Note to self" line vanishes
prompt: |
  {% extends parent %}
  Note to self: this text never reaches the model.
```

Fix: wrap the new content in a block the parent declares (or
adds blocks to the parent). If the parent genuinely has no
extensible blocks, use the "fresh prompt" pattern from
[Inherit everything except the prompt](#inherit-everything-except-the-prompt)
instead of `{% extends parent %}`.

**Orphan `{% extends parent %}`** (EXT-CORPUS C5). Using the
directive in your prompt without an `extends:` field on the
YAML is a no-op for the resolver but a syntactic intent the
engine catches:

```
config.extends_template_without_inheritance
  agent_path: agent:ag_butler
```

Fix: either add the matching `extends: <bundle>/<name>` field,
or remove the `{% extends parent %}` directive (and write a
fresh prompt instead).

<!-- lint-allowed-kolsch-agents: intentional warning callout (EXT-CORPUS F5) -->
**`kolsch:agents/<name>.yaml` doesn't resolve** (EXT-CORPUS
F5). The installed kolsch package ships only `docs/` +
`AGENTS.md` today; the `kolsch:` prefix routes through
`importlib.resources`, so any path under `agents/` raises
`config.parent_agent_not_found`. To extend the kolsch starter
helper, use the canonical catalog ref `extends: kolsch/helper`
— that resolves against the workspace's `bundles/kolsch/`
bundle, which is exactly where the helper agent ships.

### Compose with `{{ super() }}`

To **add to** a parent block instead of replacing it, use
Jinja's `{{ super() }}` inside the child's block:

```yaml
# parent
prompt: |
  Header.
  {% block body %}PARENT_PART{% endblock %}
  Footer.

# child — EXT-CORPUS K2
extends: kolsch/helper
prompt: |
  {% extends parent %}
  {% block body %}{{ super() }} + CHILD_PART{% endblock %}
```

Rendered output: `Header.\nPARENT_PART + CHILD_PART\nFooter.`

This is the **only additive composition path** in the
envelope. Field-level inheritance (`tools:`, `permissions:`,
`context_files:`) is strictly wholesale-per-field per RFC 7386
— a child that declares `tools: [bash]` replaces the parent's
tools entirely (no union). `{{ super() }}` lives at the Jinja
block layer; nothing analogous exists at the YAML field layer.

### Multi-level chains

`A extends B extends C` is supported. Resolution walks the
chain from root (`C`) toward leaf (`A`); each layer's fields
override the layer below per Schemas §A.11.2 binding
composition order. Cycles raise
`config.agent_inheritance_cycle` at agent-load; missing parents
raise `config.parent_agent_not_found`.

Rendering walks the chain the same way: each non-root layer
may include its own `{% extends parent %}`, and the engine
loads every ancestor's template into a single Jinja
`DictLoader` so blocks resolve naturally across the chain.

```yaml
# bundles/x/agents/e1_c.yaml — the root
name: e1_root
prompt: |
  ROOT_HEADER
  {% block role %}root-default{% endblock %}
  ROOT_FOOTER
```

```yaml
# bundles/x/agents/e1_b.yaml — middle layer, no override
extends: x/e1_c
name: e1_middle
prompt: |
  {% extends parent %}
```

```yaml
# bundles/x/agents/e1_a.yaml — leaf, fills the deepest block
extends: x/e1_b
name: e1_leaf
prompt: |
  {% extends parent %}
  {% block role %}LEAF_OVERRIDE{% endblock %}
```

Rendered output for `e1_leaf`:
`ROOT_HEADER\nLEAF_OVERRIDE\nROOT_FOOTER`. This example
matches the EXT-CORPUS E1 fixture at
`tests/fixtures/extends/e/`.

## Declare required blocks

Use `{% block name required %}{% endblock %}` in a parent to
force every inheriting child to declare its own version of the
block. Useful for templates that ship a generic structure but
need a per-agent specialization.

```yaml
# parent
prompt: |
  {% block role required %}{% endblock %}
  {% block tools_summary %}{{ tools.summary() }}{% endblock %}
  Be helpful.
```

A child that uses `{% extends parent %}` and overrides `role`
(but not `tools_summary`) inherits the parent's
`tools.summary()` fallback. A child that doesn't override `role`
fails at agent-load.

Required blocks fire only when the child actually inherits the
prompt via `{% extends parent %}`. A child that writes a fresh
prompt (no extends-template) is unaffected by the parent's
required blocks — that's the "inherit everything except the
prompt" pattern above.

---

## Debug template-config inconsistencies

The engine walks the prompt's Jinja AST at agent-load and emits
`agent.template_warning` events for variable accesses whose data
is empty:

- `{{ tools }}` referenced + agent declares no tools.
- `{{ docs }}` referenced + agent has no `context_files`.
- `{{ skills }}` referenced + agent declares no skills.
- Agent declares skills but the prompt does not reference
  `{{ skills }}` (the post-QoL migration warning — the
  auto-appended `## Available Skills` block was removed in
  qol-skills-jinja-variable-symmetric-with-tools).
- `platform.providers.get('X')` referenced + provider X not
  connected.
- `{{ workspace.summary }}` referenced +
  `platform.yaml:workspace_summary` unset.

Warnings surface as system-notice capsules in `kolsch chat`
(dark gray bars at session start) and on stderr for non-chat
callers. They're warnings, not Fatals — empty rendering is
sometimes intentional (a parent template references `docs`,
and child agents may legitimately declare none). If you want
strictness, the platform-level
`defaults.agent_template_strictness: fatal` knob is reserved
for a later phase.

---

## Related

- [Phase 2.2 envelope spec](../../process/phase2.2-agent-envelope-specs.md)
  — the full design rationale and the variable API in detail.
- [Schemas §A.11](../reference/schemas.md) — the agent envelope
  fields' formal schema.
- [Agent-engine contract §2.1](../explanation/agent-engine-contract.md)
  — where in pre-flight the render happens (steps 5.6.5 and
  5.6.6).
- [Error Model §4.18](../explanation/error-model.md) — the eleven
  envelope error codes.
