# Templated Agent Prompts

Recipes for writing agent prompts that use the Phase 2.2 Jinja2
template variables — the kolsch envelope. For the *why* of the
design, read the [Phase 2.2 envelope spec](../../process/phase2.2-agent-envelope-specs.md);
for the [agent-engine contract §2.1](../explanation/agent-engine-contract.md)
covers where in pre-flight the render happens. The first-agent
tutorial in [`docs/tutorials/first-agent.md`](../tutorials/first-agent.md)
walks the loop from a fresh `kolsch setup` to a templated helper.

The five standardized variables — `tools`, `docs`, `workspace`,
`agent`, `platform` — are all available in every prompt; the engine
populates them at pre-flight step 5.6.6 with `StrictUndefined`
so typos surface as `config.template_render_failed` at agent-load.

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

## Use the five top-level variables

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
# bundles/kolsch/agents/_base.yaml
prompt: |
  {% block role required %}{% endblock %}

  {{ workspace }}

  ## Available tools
  {% for t in tools %}
  - **{{ t.name }}**: {{ t.summary }}
  {% endfor %}

  {% block special_instructions %}{% endblock %}
```

```yaml
# bundles/<bundle>/agents/helper.yaml
extends: kolsch:agents/_base.yaml
agent_id: ag_helper
name: helper
prompt: |
  {% extends parent %}

  {% block role %}
  You are helper, a friendly assistant.
  {% endblock %}
```

`{% block role required %}` is Jinja 3.1's required-block syntax:
the child MUST override it. A child that extends but doesn't
override raises `config.required_block_missing` at agent-load.

### Inherit everything except the prompt

If the child writes a prompt without `{% extends parent %}`,
the parent's prompt is ignored entirely — but every other
field the child doesn't redefine is inherited (model, tools,
permissions, context_files).

```yaml
# bundles/<bundle>/agents/deploy.yaml
extends: kolsch:agents/_base.yaml
agent_id: ag_deploy
name: deploy
prompt: |
  You are a deployment agent. Run kubectl and helm commands the
  user asks for. Confirm any destructive operation by repeating
  the command first.
```

### Multi-level chains

`A extends B extends C` is supported. Resolution walks the chain
from root (`C`) toward leaf (`A`); each layer's fields override
the layer below (Schemas §A.11.2 binding composition order).
Cycles raise `config.agent_inheritance_cycle` at agent-load.

---

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
