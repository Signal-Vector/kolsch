# Extending an Agent

A focused walkthrough for declaring a child agent that
inherits from an existing one — the Phase 2.2 `extends:`
feature. Worked example: a personal butler agent that
inherits from the kolsch starter helper.

For the wider templating surface (variables, blocks, render
context) see
[Templated Agent Prompts](agent-templating.md). For the formal
field schema see the
[Schemas reference](../reference/schemas.md) — `ResolvedAgentConfig`
carries `extends`, `permissions`, and `context_files`.

---

## What `extends:` gives you

A child with `extends: <bundle>/<name>` inherits:

1. **Field-level** (RFC 7386, wholesale-per-field). `model`,
   `tools`, `permissions`, `context_files` etc. that the
   child doesn't redefine come from the parent. Lists
   replace wholesale — no merging.
2. **Prompt template** (Jinja `{% extends %}`). The child's
   `prompt:` may use `{% extends parent %}` to inherit the
   parent's prose and override `{% block %}` segments.

Both are optional. The three patterns below cover each path.

---

## Pick a parent

The canonical starter is the kolsch helper that `kolsch setup`
materialises into every workspace at
`bundles/kolsch/agents/helper.yaml`. Reference it by the
canonical 2-segment catalog ref:

```yaml
extends: kolsch/agents/helper
```

That resolves against the workspace's `bundles/kolsch/`
bundle. The helper ships:

- A Jinja prompt with two extensible blocks — `role` (the
  agent's self-description) and `special_instructions` (the
  per-agent operating rules).
- Four built-in tools: `read_file`, `write_file`, `list_files`,
  `bash`.
- Two `context_files` entries (`kolsch:docs/index.md` and
  `kolsch:AGENTS.md`).
- A `prompt`-effect permission on `kolsch/tools/bash`.

Any agent in any bundle on disk is a valid parent — the
resolver looks up the ref against the catalog, not the
package. <!-- lint-allowed-kolsch-agents: intentional warning callout -->
`acme/research-base` is as legitimate a parent as
`kolsch/agents/helper`. The form
`extends: kolsch:agents/<name>.yaml` (using the `kolsch:`
prefix) does NOT work — that prefix routes through
`importlib.resources` against the installed kolsch package,
which ships only `docs/` and `AGENTS.md` (no agents). Use the
catalog-ref form above.

---

## Decide what to override

Three patterns cover the common cases. Each is pinned to an
EXT-CORPUS fixture so the docs and the test corpus can't
drift.

### Pattern A — inherit everything; only the name changes

(EXT-CORPUS B1 shape.) The child declares `extends:` and
nothing else of substance — the resolved config is the parent
with the child's `name` and `agent_id` substituted. Useful
when you want a "renamed" instance of an existing agent so
session JSONLs and observability surfaces show a distinct
identity.

```yaml
extends: kolsch/agents/helper
name: assistant
description: A renamed helper.
```

### Pattern B — fresh prompt, inherited tools/permissions/etc.

(EXT-CORPUS B4 shape.) The child writes a brand-new prompt
without `{% extends parent %}`. The parent's prompt is
ignored entirely; every other field the child doesn't
redefine is inherited. Right when the child wants the
parent's *capability set* (tools, permissions) but a
distinct operating style.

```yaml
extends: kolsch/agents/helper
name: deploy
description: A deployment agent with the kolsch helper's tool set.
prompt: |
  You are a deployment agent. Run kubectl and helm commands the
  user asks for. Confirm any destructive operation by repeating
  the command first.
```

### Pattern C — extend the parent's prompt; override specific blocks

(EXT-CORPUS C1 shape.) The child uses `{% extends parent %}`
in its `prompt:` and overrides only the blocks it wants to
change. The parent's surrounding prose, tool listing, and
non-overridden blocks all flow through. This is the butler
example below.

---

## Define your child agent

Worked example: a butler that extends `kolsch/agents/helper` via
Pattern C. Drop this file at
`bundles/<your-bundle>/agents/butler.yaml`. (If you don't
have your own bundle yet, the kolsch starter bundle works
fine — drop it at `bundles/kolsch/agents/butler.yaml`.)

```yaml
# bundles/<your-bundle>/agents/butler.yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_butler
name: butler
description: A personal butler agent inheriting from kolsch helper.
extends: kolsch/agents/helper
prompt: |
  {% extends parent %}
  {% block role %}a personal butler — attentive, precise, and proactive{% endblock %}
  {% block special_instructions %}
  Manage files, run commands, keep the workspace organized.
  {% endblock %}
```

What happens at agent-load: (1) the catalog resolves
`kolsch/agents/helper` to `bundles/kolsch/agents/helper.yaml`; (2)
the resolver merges fields per RFC 7386 — `model`, `tools`,
`permissions`, `context_files` flow from the parent because
the child doesn't redefine them; (3) the engine renders the
child's prompt via the chain-aware `render_with_parents` —
the parent's template loads under a synthetic name, the
child's `{% extends parent %}` resolves to it, and the
child's `role` + `special_instructions` blocks replace the
parent's defaults while the parent's surrounding prose
(`You are {{ agent.name }}, …`) wraps around them.

---

## Verify it loads

The fastest in-process check uses
`platform_runtime.bootstrap_runtime` — the same entry point
`kolsch chat` and `kolsch run` go through. It wires every
Layer-3 subsystem validator (`tools`, `skills`, `memory`) so
a child agent declaring `subsystems.tools.config` validates
against the production dispatch.

```python
import asyncio
from pathlib import Path
from platform_runtime import bootstrap_runtime

async def main():
    runtime = await bootstrap_runtime(root=Path.cwd())
    cfg = await runtime.build_resolved_config("<your-bundle>/butler")
    print("model:", cfg.model)
    print("tools:", [d["ref"] for d in cfg.subsystems["tools"].config["declared"]])
    print("context_files:", [c.path for c in cfg.context_files])
    print("permissions:", cfg.permissions)
    print("extends preserved:", cfg.extends)
    print("--- prompt template (unrendered) ---")
    print(cfg.prompt)

asyncio.run(main())
```

For the butler against a fresh-from-`kolsch setup` workspace
you should see four inherited tools (`kolsch/tools/read_file`,
`kolsch/tools/write_file`, `kolsch/tools/list_files`,
`kolsch/tools/bash` — plus any extras you've added to your own
`bundles/kolsch/agents/helper.yaml`), two inherited
`context_files`, the inherited bash-prompt permission, and
`cfg.extends == "kolsch/agents/helper"`. The leaf's `extends:` is
preserved on the resolved config so the engine's render
dispatch can route through `render_with_parents`.

The `cfg.prompt` you see here is the **raw Jinja template**
(`{% extends parent %}` is still literal). The asset-manager
layer doesn't render; the engine does, at pre-flight. To see
the rendered system prompt the LLM receives, run `kolsch run`
(below) and read the `config.set` event from the session
JSONL — its `resolved_config.prompt` field is the
post-render output.

Common load-time errors and what they mean:

| Error code | Meaning | Fix |
|---|---|---|
| `config.parent_agent_not_found` | `extends:` ref didn't resolve | Confirm `bundles/kolsch/agents/helper.yaml` exists (run `kolsch setup` if not); check the ref shape (`<bundle>/<name>`, no `kolsch:` prefix for catalog refs). |
| `config.agent_inheritance_cycle` | Loop in the chain | Two YAMLs extend each other (or A→B→A); fix one to terminate at a real parent. |
| `config.extends_template_without_inheritance` | Prompt uses `{% extends parent %}` but YAML has no `extends:` field | Add the `extends: <bundle>/<name>` field, or remove the Jinja directive. |
| `config.required_block_missing` | Parent declares `{% block X required %}{% endblock %}`; child doesn't override `X` | Override the required block in the child's prompt. |
| `config.template_render_failed` | Typo'd variable name under StrictUndefined | Check the variable name in the child's prompt against the [six envelope vars](agent-templating.md#use-the-six-top-level-variables). |

---

## Run a chat session against it

Once `build_resolved_config` returns cleanly, kick off an
exchange:

Interactive: `uv run kolsch chat <your-bundle>/butler`.
Non-interactive (good for inspecting the rendered output):
`uv run kolsch run <your-bundle>/butler --input content="Hello"`.

Both modes write the session JSONL under
`sessions/<thread>/<session>/events.jsonl`. The `config.set`
event's `data.resolved_config.prompt` field is the rendered
system prompt the LLM receives. Confirm:

- the parent's prose surrounds the butler's blocks
  (`You are {{ agent.name }}, …`, `## Where you are`,
  `## What you can do`, `Be concise.` closer);
- `{{ agent.name }}` substitutes to `butler`;
- the butler's `role` / `special_instructions` content
  appears in place of the parent's defaults;
- the four kolsch tools are listed in the `## What you can do`
  section;
- `docs/index.md` + `AGENTS.md` content is inlined into
  `## Your documentation`;
- the `config.set` event's `resolved_config.permissions`
  carries the inherited bash-`prompt` rule.

The `config.set` event is the source of truth — read it
directly if any verification fails.

---

## Related

- [Templated Agent Prompts](agent-templating.md) — the
  envelope-variable surface (`{{ agent.name }}`,
  `{{ tools }}`, `{{ skills }}`, etc.) and the broader Jinja
  patterns including `{{ super() }}` and multi-level chains.
- [Schemas reference](../reference/schemas.md) — the
  `ResolvedAgentConfig` model carrying `extends`, `permissions`,
  and `context_files`, generated from source.
