# Phase 2.2 Patch — Agent Envelope (Permissions, Templated Prompts, Inheritance)

**Draft 0.1**

> The third patch of Phase 2 (interactive agent experience).
> Phase 2.0 shipped the four builtin tools + workspace-only
> permissions; Phase 2.1 shipped the chat REPL that consumes the
> approval callback. Phase 2.2 fills in everything else about
> "what an agent IS" — the operating envelope:
>
> 1. **Per-agent permission rules** that override the platform
>    defaults, plus a `permissions.check/require` API for tool
>    authors.
> 2. **Jinja2-templated prompts** with a standardized variable
>    API (`tools`, `docs`, `workspace`, `agent`, `platform`).
> 3. **`@tool` decorator extension** with a `summary` field, so
>    the same metadata serves multiple rendering modes.
> 4. **Agent context files** declared on the agent YAML —
>    two-tier (primary inlined, secondary referenced) with
>    install-relative (`kolsch:`) and workspace-relative
>    (`workspace:`) path resolution.
> 5. **Agent inheritance via `extends:`** — wholesale YAML field
>    override + Jinja `{% extends parent %}` for prompt structure.
>
> Plus the support mechanism: template-vs-config consistency
> checks at agent-load that emit `agent.template_warning` events
> when a prompt references data the agent doesn't have.
>
> Larger surface than 2.0 or 2.1, but every piece is small in
> isolation and they compose into a coherent "agent envelope" —
> the contract for what an agent knows, what it can touch, and
> how its prompt is assembled.

---

## Part 0 — Why This Patch Exists

After Phase 2.1, agents are chattable but limited in expressiveness:

- **All agents share the platform's authorization rules.** A
  `summarizer` agent and a `deploy` agent get the same effects
  for `bash` and `write_file`. Per-agent fine-tuning isn't
  possible (Phase 2.0 Part 7 deferred this exactly).
- **Prompts are static text.** Agent authors manually describe
  the tools available, the workspace layout, the project docs.
  Every tool update means every prompt needs review. No
  composability.
- **Tool authors have no way to programmatically check
  permissions.** A composite tool that runs multiple operations
  either does pre-flight checks by hand (fragile) or fails at
  the OS layer (ugly).
- **Agents have no way to know about workspace context.** The
  helper agent in the Kolsch dev workspace has no idea what
  docs exist; it reverse-engineers the project structure via
  `list_files` every time.
- **Agent files don't compose.** Every agent is a standalone
  YAML; no way to share a common base. Bundles ship 5+ agents
  with mostly-duplicate prompts.

Phase 2.2 addresses all five with one coherent patch around the
"agent envelope" concept: everything in the agent YAML that
defines what the agent IS — its model, its tools, its
permissions, its context, its prompt — composes via a small set
of standardized mechanisms.

### Headline design decisions

1. **Agent permissions override platform permissions.** Agent's
   `permissions:` field is evaluated first; falls through to
   platform rules; default-deny tail catches the rest. Phase 2.0
   §3.2's first-match-wins semantics extend cleanly to the
   composed list.
2. **Prompts become Jinja2 templates.** Backward-compatible —
   plain-text prompts continue to work. New capability: object-
   based variables with standardized methods (`.list()`,
   `.summary()`, `.names()`, `.get(name)`, `.count`).
3. **`@tool` gains a `summary` field; `tool_help` is NOT
   added.** The model already receives full descriptions via
   the API's `tools=` parameter; the prompt's tool section is
   conversational context, not the model's lookup source.
4. **Context files are declared on the agent, not the platform.**
   Per-agent flexibility; install-relative paths (`kolsch:`)
   resolve to the kolsch package's data directory so kolsch's
   own docs ship with the install.
5. **Agent inheritance is wholesale.** No selective field
   merging; the only "merge" mechanism is Jinja block override
   in the prompt. Keeps the resolution rules trivial to reason
   about.

> **Spec amendment — bug-phase2.2-envelope-schema-gap §9 / E-2.2-001
> (2026-06-02):** the Phase 2.2 draft 0.1 examples in §1.1, §2.1,
> and §2.2 had `permissions[].who` / `permissions[].what` swapped
> against the implementation. `who` matches the principal (e.g.
> `agent.ag_helper`, `user.local`); `what` matches the action or
> target (e.g. `tool.bash`, `<workspace>/data/output.txt`). The
> examples and `permissions.require(who, what)` docstrings below
> were corrected as part of Step 1; the Phase 1 / 2.0 authz
> implementation, every deployed `platform.yaml`, and the
> chat-modal callback all already follow this orientation.

---

## Part 1 — Per-Agent Permissions

### 1.1 The `permissions:` field on agent YAML

```yaml
# bundles/kolsch/agents/helper.yaml
agent_id: ag_helper
name: helper
model: opus
tools: [read_file, write_file, list_files, bash]
permissions:
  # `who` matches the principal (this agent's identity); `what` matches
  # the action OR the target. The pair below catches every bash call
  # this agent makes and routes it through the prompt modal.
  - who: ".*"
    what: "tool\\.bash"
    effect: prompt
  # Targeted allow: this agent may write inside the workspace's data/
  # tree without prompting; everything else falls through to platform
  # rules + the default-deny tail.
  - who: ".*"
    what: "<workspace>/data/.*"
    effect: allow
```

Same shape as `platform.yaml`'s `authorization.config.rules`
entries: `who` (principal pattern — matches the agent or user
identity), `what` (action OR target pattern — matches the
action string `tool.<name>` AND the resolved target a tool was
asked to touch), `effect` (one of `allow` / `prompt` / `deny`).
Order matters within the agent's rule list (first-match-wins).

### 1.2 Composition order (agent overrides platform)

When the runtime checks an authz decision:

1. **Agent's `permissions:` rules** in declaration order.
   First match returns its effect.
2. **Platform's `authorization.config.rules`** in declaration
   order. Falls through here if no agent rule matched.
3. **Platform's `rl_default_deny` tail** catches anything left.

The agent can widen, narrow, or shift the effect for any tool/
target combination. The platform's default-deny tail still
provides the safety net for unhandled cases.

**Why agent overrides platform (not the reverse):** the agent
is the most specific actor — it knows its job and what it
needs. Platform rules are workspace-wide defaults; agent rules
are role-specific refinements.

### 1.3 Why this design over Phase 2.0's deferral

Phase 2.0 Part 7 §1 explicitly deferred per-agent rule scopes,
saying "wait for the Workflow Engine to make the discrimination
useful." Phase 2.2 brings it forward because the same use case
already exists in interactive chat — different reference agents
(OpenClaw, Pi, helper) have different appropriate capabilities,
and Phase 2.3's bundled-agents work needs to express this. The
Workflow Engine isn't required; reference-agent differentiation
is.

### 1.4 Validation at agent-load

Agent permissions are validated when the agent is resolved:

- Each rule must have a valid `effect` (one of the three).
- `who` and `what` must be parsable as regex.
- Rules referencing tools NOT in the agent's `tools:` list are
  permitted but emit `agent.template_warning` ("rule references
  `tool.bash` but bash is not in agent's tools list").

The warning catches over-specification without blocking. Useful
when an agent inherits permission rules from a parent but
narrows its tools list.

---

## Part 2 — Permission API for Tool Authors

### 2.1 The API

Tool authors call into a small module:

```python
# bundles/<bundle>/tools/my_complex_tool.py
from kolsch.tools import tool
from kolsch.tools import permissions

@tool(
    description="Build the target then run tests.",
    summary="Build + test in one step.",
)
async def build_and_test(target: str) -> dict:
    """Build target then test it."""
    # `who` is the principal (the agent calling the tool); `what` is
    # the action OR target the tool is about to perform.
    principal = "agent.ag_helper"
    permissions.require(who=principal, what=f"make {target}")
    permissions.require(who=principal, what="pytest")
    # ... actually run them
```

### 2.2 The two functions

**`permissions.check(who: str, what: str) -> AuthzEffect`**

Returns the effect (one of `ALLOW`, `PROMPT`, `DENY`). Tool
decides what to do. `who` is the principal pattern (the agent
calling the tool — e.g. `"agent.ag_helper"`); `what` is the
action OR target pattern (e.g. `"tool.write_file"` for the
action, or the resolved path the tool is about to write).
Useful for branching:

```python
if permissions.check(principal, target) == AuthzEffect.ALLOW:
    # write the file
else:
    # return data instead
```

**`permissions.require(who: str, what: str) -> None`**

Raises `PlatformError("authz.denied", ...)` on `DENY` or on
`PROMPT` when no callback is available (non-chat callers).
Returns normally on `ALLOW`. Same `who` / `what` orientation
as `check`. Most common case; the engine catches the raised
error and emits a clean `tool.failed` event.

### 2.3 Behavior

- **Uses the same composed rules** as the engine's pre-dispatch
  check. Walks agent permissions → platform rules → default-deny.
  Tool authors don't need to know about the composition; they
  just get the effective answer.
- **Returns `DENY` when no rule matches.** Mirrors the engine's
  pre-dispatch behavior and the platform's `rl_default_deny`
  tail semantics.
- **Treats `PROMPT` as `DENY` for `require` in non-chat
  context.** The chat's modal callback isn't reachable from
  inside a tool; surfacing a permission prompt mid-execution is
  out of scope for the initial API. Tool authors who want
  interactive approval should rely on the engine's pre-dispatch
  modal at the tool-call boundary (via Phase 2.0's `prompt`
  effect on the agent's tool call itself).
- **Async by default.** Matches the Phase 2.0 dispatcher and
  Phase 1.1 `build_llm` patterns.

### 2.4 Implementation sketch

The `kolsch.tools.permissions` module reads the current
`AuthzProvider` from a contextvar set by the engine's tool
dispatcher. The contextvar is set immediately before the tool
function is called and cleared after; tools have access only
during their own execution.

If a tool calls `permissions.check/require` outside that context
(e.g., a test that imports the tool function directly without
running it through the engine), it raises
`tool.permissions_no_context` so the developer knows to set up
a fixture.

### 2.5 What this is NOT

- **NOT a permission-prompt mechanism mid-tool.** Tools that
  want approval should rely on the agent's tool call being
  approved at dispatch time (via the agent's `prompt`-effect
  rule). Mid-execution prompts are deferred per §2.3's note.
- **NOT a new rule system.** Reuses agent + platform rules
  unchanged. Tool authors learn one grammar; same patterns
  apply.

---

## Part 3 — Jinja2 Templating in Prompts

### 3.1 The change

The agent's `prompt:` field is now a Jinja2 template. The
engine renders it at exchange pre-flight with a standardized
variable context. Authors can use Jinja syntax (variables,
filters, control flow, blocks) to compose the system prompt
dynamically.

### 3.2 Backward compatibility

Existing agent prompts contain no Jinja syntax (`{{`, `{%`).
Rendering a prompt with no Jinja markers returns the prompt
unchanged. **All Phase 1.x and 2.0/2.1 agent files continue to
work without modification.**

### 3.3 Adding the dep

`pyproject.toml` gains `jinja2 >= 3.1.0` as a runtime
dependency. Jinja2 3.1 is required for the native `required`
block syntax used by agent inheritance (Part 8).

### 3.4 Rendering semantics

- **StrictUndefined.** Typo'd variable names (`{{ workspce }}`)
  raise at render time and surface as
  `config.template_render_failed` errors. The agent fails to
  load with a clear pointer to the typo's location.
- **Standard filters available.** `default`, `length`, `join`,
  `upper`, `lower`, `truncate`, etc. — Jinja2's bundled filter
  set. No custom additions in 2.2.
- **Object attribute access:**
  - Typo'd attributes (`workspace.allowd_paths`) raise
    `AttributeError`, caught and reported as
    `config.template_render_failed`.
  - Known-but-unset attributes (`workspace.summary` when no
    `workspace_summary` is set in `platform.yaml`) return
    `None`, allowing `| default` to substitute.
- **Caching per exchange.** Methods that touch I/O (reading
  context files, listing tools, etc.) are computed once per
  render. The cache lives for the duration of the prompt
  render only.

### 3.5 Two usage styles

Authors can mix as preferred.

**Compact (canonical rendering):**

```yaml
prompt: |
  You are {{ agent.name }}, a helpful assistant.

  {{ workspace }}

  {{ tools }}

  {{ docs }}
```

**Explicit (logic clarity via for-loops — recommended for
prompts with conditionals or per-item formatting):**

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

Both produce valid system prompts. Compact uses defaults; explicit
gives full control.

---

## Part 4 — Variable API

### 4.1 The five top-level objects

Every agent prompt has access to these as top-level variables:

| Variable | Represents | Default rendering (`{{ obj }}`) |
|---|---|---|
| `tools` | The agent's resolved tool set | Full descriptions per `tools.list()` |
| `docs` | The agent's resolved `context_files` | Primaries inlined + secondaries listed |
| `workspace` | The active workspace (path, summary, allow/deny lists) | Summary + path + lists |
| `agent` | The agent itself (introspection) | Name + id + model |
| `platform` | The active `platform.yaml` (introspection) | Workspace + provider count + rule count |

`memory` is reserved for Phase 2.5+; not exposed in 2.2.

### 4.2 The standardized method family

Every list-shaped object exposes the same family of methods so
authors learn one convention:

| Method | Returns | Use case |
|---|---|---|
| `.list()` | All items with full detail | Default verbose view |
| `.summary()` | One-line-per-item compact view | Smaller context; names + 5-word hints |
| `.names()` | Just identifiers (comma-separated) | Minimum context |
| `.get(name)` | A single item, or `None` if not found | Drill-down |
| `.count` | Integer count of items | Use in prose ("You have N tools.") |

Direct rendering (`{{ obj }}`) is shorthand for `{{ obj.list() }}`.

### 4.3 Pythonic protocols

All list-shaped objects also support:

- `for item in obj` → iteration over `.list()` items
- `len(obj)` / `{{ obj | length }}` → count
- `obj[name]` → subscript (equivalent to `.get(name)`)
- `name in obj` → containment check

Methods are recommended for prompt-rendering intent (the
author signals "I want the summary view"); protocols are for
logic (`{% if 'bash' in tools %}`).

### 4.4 Per-object details

#### `tools`

Items represent declared tools from the agent's resolved
config.

| Method / attribute | Returns |
|---|---|
| `tools.list()` | Each tool as a `### <name>(<signature>)\n<description>` block |
| `tools.summary()` | One-line per tool: `<name>: <summary>` |
| `tools.names()` | Comma-separated names: `"read_file, write_file, list_files, bash"` |
| `tools.get(name)` | Tool item, or `None` |
| `tools.count` | Integer count |

Per-tool item attributes:
- `name` — tool name
- `description` — full description from `@tool(description=...)`
- `summary` — short summary from `@tool(summary=...)` or auto-derived from first sentence of description
- `signature` — rendered parameters from `args_schema`
- `schema` — JSON schema dict (advanced)

#### `docs`

Items represent entries from the agent's `context_files:`
declaration.

| Method / attribute | Returns |
|---|---|
| `docs.list()` | Primaries inlined with full content + secondaries listed by path + summary |
| `docs.primaries()` | Only primary docs, full content |
| `docs.secondaries()` | Only secondary docs, path + summary, no content |
| `docs.summary()` | All docs (both tiers), path + summary, no content |
| `docs.names()` | Comma-separated paths |
| `docs.get(path)` | Doc item, or `None` |
| `docs.count` | Integer count (all tiers) |

Per-doc item attributes:
- `path` — resolved path (with `kolsch:` or `workspace:` prefix)
- `summary` — from `context_files[].summary` or first non-empty line of file
- `primary` — boolean (default `false`)
- `content` — file content as string
- `size_bytes` — file size

#### `workspace`

Single object — no list-shaped methods.

| Attribute | Returns |
|---|---|
| `workspace.summary` | Text from `platform.yaml:workspace_summary` or `None` |
| `workspace.path` | Absolute workspace root path as string |
| `workspace.allowed_paths` | List of strings from `file_access.allowed_paths` |
| `workspace.denied_paths` | List of strings from `file_access.denied_paths` |

Default rendering (`{{ workspace }}`):

```
## Active workspace

[workspace.summary content here, if set]

Path: /path/to/workspace
You can read from: ., data/, bundles/, ...
You cannot access: .env, .platform/
```

If `workspace.summary` is unset, the summary block is omitted
(no fallback prose).

#### `agent`

Single object — agent introspecting itself.

| Attribute | Returns |
|---|---|
| `agent.name` | The agent's display name (`config.name`) |
| `agent.id` | The agent's id (`config.agent_id`) |
| `agent.model` | `ModelSpec` resolved object (see below) |
| `agent.tools` | Alias for top-level `tools` |
| `agent.permissions` | List of `Rule` objects from agent's `permissions:` |
| `agent.context_files` | List of `ContextFile` objects |

`agent.model` exposes:
- `agent.model.ref` — canonical ref string
- `agent.model.provider` — provider name
- `agent.model.model` — model name
- `agent.model.context_length` — int or `None`
- `agent.model.supports_tools` — bool or `None`
- `agent.model.fallback` — list of `ModelSpec` (Phase 1.3)

Default rendering: `<name> (<id>) — <provider>/<model>`.

#### `platform`

Single object — agent introspecting the platform.

| Attribute | Returns |
|---|---|
| `platform.providers` | List-shaped: connected `ProviderConfig` objects |
| `platform.authorization` | Object: `variant`, `rules` (list of `Rule`) |
| `platform.workspace_summary` | Same as `workspace.summary` |
| `platform.file_access` | Object: `allowed_paths`, `denied_paths` |
| `platform.aliases` | Dict-shaped: `get(alias)` returns canonical ref |

`platform.providers` supports the standardized method family:
- `platform.providers.list()` — full info per provider
- `platform.providers.names()` — comma-separated provider names
- `platform.providers.get(name)` — `ProviderConfig` or `None`
- `len(platform.providers)`, iteration

Default rendering: `Workspace: <path> · Providers: <names> · <N> rules`.

**Note:** `platform.providers.get('X')` returning `None` for an
unconnected provider doesn't raise; it returns `None` so
`| default` works. If the agent's prompt statically references
a specific provider that isn't connected, the agent-load
warning system (Part 9) catches it.

### 4.5 The `agent.template_warning` event mechanism

See Part 9.

---

## Part 5 — `@tool` Decorator: `summary` Field

### 5.1 The new field

```python
# bundles/kolsch/tools/read_file.py
from kolsch.tools import tool

@tool(
    description="""Read a file's full text contents and return them verbatim.

    When to use:
    - The user asks to "read", "see", "show", "display", or "cat" a file.
    - You need a file's content to answer a specific question.

    Presenting the result:
    - When asked to SEE the file, return content VERBATIM in a fenced
      code block. Do NOT summarize unless explicitly asked.

    Raises file.access_denied for paths outside allowed scope.""",
    summary="Read a file's text contents.",
)
def read_file(path: str, max_bytes: int = 1_048_576) -> str:
    ...
```

`summary` is the one-line summary used by `tools.summary()`
rendering and by the item's `.summary` attribute. Optional;
defaults to the first sentence of `description` (everything up
to the first period).

### 5.2 Why not `tool_help`

A previous draft of this patch proposed a `tool_help(name)`
builtin tool that the agent could call to look up specific tool
details. **Dropped from this patch** because:

The model **always** receives the full description via the
`tools=` parameter of the LLM API call. The system prompt's
description is conversational context (when/how to use); the
API's structured tools array is what the model uses to construct
tool_use calls. Summary mode in the prompt doesn't blind the
model — it just sets less verbose context.

So `tools.summary()` produces a prompt with one-liners while
the model still has full schemas via the API. No discovery tool
needed.

### 5.3 What this changes about existing tools

The four Phase 2.0 builtin tools gain `summary` fields. Already
spec'd as part of the `bug-tool-output-summarization-bias.md`
fix; this patch lands them.

### 5.4 What's deliberately NOT in the decorator

To avoid speculative API growth:

- **`examples`** — example invocations. Useful when a real need
  surfaces; not in 2.2.
- **`category`** — for grouping in `tools.list_by_category()`.
  Niche; deferred.
- **`when_not_to_use`** — already in the description per the
  Bug 2 pi-style guidance; structured field not needed yet.

Adding any of these later is non-breaking (item attributes get
new optional fields with `None` defaults).

---

## Part 6 — Agent Field: `context_files`

### 6.1 The schema

```yaml
# bundles/kolsch/agents/helper.yaml
context_files:
  - path: kolsch:docs/INDEX.md
    summary: "Master index of all Kolsch documentation."
    primary: true

  - path: kolsch:AGENTS.md
    summary: "Operating rules for AI agents in this codebase."
    primary: true

  - path: kolsch:docs/explanation/agent-engine-contract.md
    summary: "Engine invariants and exchange flow — consult when working on engine internals."
    primary: false

  - path: workspace:CONVENTIONS.md
    summary: "This workspace's specific coding conventions."
    primary: false
```

Each entry has three fields:

| Field | Required | Default | Notes |
|---|---|---|---|
| `path` | yes | — | `kolsch:` or `workspace:` prefixed |
| `summary` | no | first non-empty line of file | Author-written summary preferred |
| `primary` | no | `false` | Primary = inlined content; secondary = listed by summary |

### 6.2 Path prefix resolution

Two prefixes, no plain paths:

- **`kolsch:<path>`** — resolves to `importlib.resources.files("kolsch") / <path>`. The kolsch installation's bundled data directory. Works whether kolsch was installed via `pip`, `uv tool install`, or run via `uv run` from a source checkout.
- **`workspace:<path>`** — resolves to `<resolved_workspace_root> / <path>`. The user's workspace per Phase 1.2's `resolve_workspace`.

**Plain paths (no prefix) are rejected at agent-load** with
`config.context_file_unprefixed`. Forces authors to be explicit
about which root they mean — no ambiguity, no silent surprises.

### 6.3 Two-tier semantics

**Primary** (`primary: true`):
- File content is inlined in the system prompt under `{{ docs }}` /
  `{{ docs.list() }}` / `{{ docs.primaries() }}` rendering.
- Costs context tokens every exchange.
- Use for small, always-relevant content (the INDEX, the AGENTS.md
  hard rules).

**Secondary** (`primary: false`, default):
- File is mentioned in the system prompt by path + summary only,
  NOT inlined.
- The agent reads it on demand via `read_file` if it needs the
  content.
- Use for large or situationally-relevant content (specific
  architecture docs, phase specs).

Default `{{ docs }}` rendering shows both tiers — primaries inlined,
secondaries listed in a "Also available" section. Authors who want
just one tier use `docs.primaries()` or `docs.secondaries()`.

### 6.4 File size limit

Each context file is capped at 64 KiB. Loading a file larger than
that raises `context.file_too_large` at agent-load. The cap prevents
accidental "the whole architecture doc is in every exchange"
inflation; agents that need huge content read it on demand via
tools.

### 6.5 Packaging kolsch's docs as data

`pyproject.toml` is updated to include `docs/` as package data via
the build backend's `package-data` mechanism. `importlib.resources`
then finds it in both source-tree and installed contexts.

This is what makes `kolsch:docs/INDEX.md` work for every user
without per-workspace configuration.

---

## Part 7 — Platform Field: `workspace_summary`

### 7.1 The new field

```yaml
# platform.yaml
workspace_summary: |
  Active development workspace for the Kolsch platform. Source code
  lives under bundles/ as agent YAML + tool Python; sessions/ holds
  append-only event-log JSONL files (read-only ground truth);
  docs/ is reference material referenced by agents via context_files.
```

Optional. Text. Multi-line allowed.

Used by:
- `{{ workspace.summary }}` (returns the text or `None`).
- `{{ workspace }}` default rendering (includes summary block if set).
- `{{ platform.workspace_summary }}` (same value via the platform
  variable).

### 7.2 When to set it

Workspaces meant to be human-readable (the Kolsch dev workspace,
shared team workspaces, project-specific workspaces) benefit from
a summary that orients the agent. Ephemeral workspaces (tmp, CI
scratch) can omit it.

### 7.3 What goes in it

Recommended content (per the Kolsch dev workspace example):

- One sentence on the workspace's purpose.
- A short note on each top-level directory's role.
- Any conventions the agent should follow (file naming, where
  outputs go).

NOT for: secrets (use `.env`), per-agent context (use that agent's
`context_files`), large content (use a `docs:` directory and
reference via context_files).

---

## Part 8 — Agent Inheritance via `extends:`

### 8.1 The mechanism

An agent YAML can declare:

```yaml
extends: kolsch:agents/_base.yaml
```

Resolution:
1. Load the parent YAML file via the same prefix scheme as
   `context_files`.
2. Apply the child's YAML fields on top of the parent's,
   **wholesale per field** — any field the child defines
   completely replaces the parent's value (no merging within
   lists or dicts).
3. The child's `prompt:` field is rendered as a Jinja2 template
   with the parent's prompt available as the `parent` variable.

### 8.2 Wholesale field override

If the child specifies a field, the child's value wins —
completely. Examples:

| Field | Parent has | Child has | Resolved |
|---|---|---|---|
| `tools` | `[read_file, write_file, list_files, bash]` | `[bash]` | `[bash]` (parent ignored) |
| `permissions` | `[{...prompt rule...}]` | `[{...allow rule...}]` | `[{...allow rule...}]` (parent ignored) |
| `context_files` | `[INDEX.md, AGENTS.md]` | not specified | `[INDEX.md, AGENTS.md]` (parent value inherited) |
| `model` | `opus` | `haiku` | `haiku` |

No "add one more tool to parent's list." Either inherit (don't
specify) or replace entirely (specify your own complete list).

**Silence vs explicit-empty semantics** (per E-2.2-001):

| Layer says | Meaning | Effect on lower layers |
|---|---|---|
| Field absent (not in YAML) | Silence — defer to lower layers | Lower layer's value carries |
| Explicit `[]` (or `null` per RFC 7386) | Present-but-empty | Wins wholesale; lower layers ignored |
| Explicit value (`[a, b]` etc.) | Present-and-populated | Wins wholesale; lower layers ignored |

The RFC 7386 merge that the kernel applies (`src/kernel/resolver.py`)
preserves this distinction: an absent key in a partial does NOT
contribute, and the next partial in the merge order owns the slot.
The combined composition order — incorporating the
`qol-setup-evolution-and-defaults.md` `defaults:` block — is
**binding**:

```
schema default  ←  defaults.<field>  ←  extends chain  ←  agent
        (lowest precedence)                      (highest precedence)
```

A child silent on a field is transparent; the next layer is
consulted. Once any layer is present (even as `[]`), that layer's
value wins wholesale and lower layers are ignored.

**Worked examples.** Resolving the `permissions:` field for
agent `child.yaml`:

| Scenario | `defaults.permissions` | `parent.permissions` (via extends) | `child.permissions` | Resolved |
|---|---|---|---|---|
| All silent | absent | absent | absent | `[]` (schema default) |
| Defaults only | `[rule_X]` | absent | absent | `[rule_X]` |
| Parent silent, agent silent, defaults set | `[rule_X]` | absent | absent | `[rule_X]` |
| Parent explicit `[]`, agent silent, defaults set | `[rule_X]` | `[]` | absent | `[]` (parent wins) |
| Agent explicit value | `[rule_X]` | `[rule_Y]` | `[rule_Z]` | `[rule_Z]` (agent wins) |
| Agent `null` (RFC 7386 delete) | `[rule_X]` | `[rule_Y]` | `null` | `[]` (field cleared) |

Same rules apply to `context_files`, `tools`, `model`, and every
other agent-envelope field.

### 8.3 Prompt inheritance via Jinja blocks

The child's prompt is a Jinja template. If it calls
`{% extends parent %}`, the parent's prompt structure is used
and the child fills in declared blocks. If the child writes a
prompt without `{% extends parent %}`, the parent's prompt is
ignored.

**Parent template** (declares blocks; some required, some
optional):

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

  ## Your documentation
  {{ docs }}
```

**Child using inheritance** (overrides `role`, leaves
`special_instructions` empty):

```yaml
extends: kolsch:agents/_base.yaml
agent_id: ag_helper
name: helper
description: A friendly general-purpose helper agent.
prompt: |
  {% extends parent %}

  {% block role %}
  You are helper, a friendly assistant. Answer clearly and concisely.
  If a question is ambiguous, ask one clarifying question first.
  {% endblock %}
```

**Child writing a fresh prompt** (parent's prompt ignored):

```yaml
extends: kolsch:agents/_base.yaml
agent_id: ag_deploy
name: deploy
prompt: |
  You are a deployment agent. Run kubectl and helm commands the user asks for.
  Confirm any destructive operation by repeating the command.
```

The child inherits parent's `tools`, `model`, `context_files`,
etc. (assuming not redefined), but uses its own `prompt:`.

### 8.4 Required blocks

Parent declares `{% block X required %}{% endblock %}`. When a
child uses `{% extends parent %}` and doesn't override `X`,
Jinja 3.1 raises `TemplateSyntaxError` at parse time. The engine
catches this and surfaces as `config.required_block_missing`
with the block name and the offending child agent's path.

Required blocks only fire when the child actually inherits the
prompt template (i.e., uses `{% extends parent %}`). If the
child writes a fresh prompt, parent's required blocks are
irrelevant.

### 8.5 Multi-level inheritance

`A extends B extends C` is supported. Resolution walks the
chain:

1. Load C.
2. Apply B's overrides (wholesale per field).
3. Apply A's overrides.
4. Render A's prompt with B's prompt as `parent` (B's prompt
   itself may have `{% extends "C" %}` — handled recursively
   by Jinja).

No accumulation of lists across levels. If A, B, and C all
define `tools:`, A's value wins; B's and C's are ignored.

### 8.6 Cycle detection

`A extends B extends A` is a cycle. Detected at agent-load by
tracking visited paths during chain resolution. Raises
`config.agent_inheritance_cycle` with the cycle path.

### 8.7 Parent file not found

`extends: kolsch:agents/nonexistent.yaml` raises
`config.parent_agent_not_found` at agent-load with the resolved
absolute path that didn't exist.

### 8.8 Parents need not be complete

A parent agent file might omit `agent_id`, `name`, `description`
— those are typically child-specific. Validation runs against
the RESOLVED config (after the child fills in fields), not the
parent in isolation.

Loading a parent directly (without a child inheriting from it)
fails the normal "complete agent config" validation. No
special `abstract: true` marker needed — natural failure mode
handles it.

### 8.9 The `parent` variable in the child's prompt

Only available if `extends:` is set. A child that writes
`{% extends parent %}` without an `extends:` field raises
`config.extends_template_without_inheritance`.

---

## Part 9 — Template-Config Consistency Warnings

### 9.1 The mechanism

At agent-load time (during `build_resolved_config`), the engine:

1. Parses the resolved prompt template's Jinja AST.
2. Walks all variable accesses (`tools`, `docs`, `workspace`, etc.)
   and method calls (`platform.providers.get('X')`, etc.).
3. Cross-checks against the resolved config:
   - `tools` referenced + agent's `tools:` is empty → warning.
   - `docs` referenced + agent's `context_files:` is empty → warning.
   - `platform.providers.get('X')` referenced + provider X not
     connected → warning.
   - `workspace.summary` referenced + `workspace_summary` unset
     → warning.
4. Each warning emits an `agent.template_warning` event with:
   ```json
   {
     "type": "agent.template_warning",
     "data": {
       "reference": "<the template variable or method call>",
       "reason": "<one-sentence description>",
       "hint": "<actionable next step>"
     }
   }
   ```

### 9.2 Why agent-load, not render-time

Catches the problem ONCE per session, at session start, before
any user interaction. Inconsistent templates fail loudly the
first time the agent is used, not silently mid-conversation.

### 9.3 Why warning, not fatal

Empty rendering is sometimes intentional — a parent template
references `docs`, and child agents may or may not declare
`context_files`. Hard error would prevent legitimate scaffolding.
Warning is loud enough that users notice but doesn't block.

### 9.4 Surfacing to the user

The chat (per Phase 2.1) renders `agent.template_warning` events
as system-notice capsules at session start (dark gray bar, one
per warning). Stays in the JSONL so replay shows them too.

Non-chat callers (`kolsch run`, server) print warnings to stderr
in the same one-line-per-warning format.

### 9.5 Optional strictness escalation

A platform-level knob (deferred for Phase 2.X):

```yaml
defaults:
  agent_template_strictness: warn  # default
  # or: fatal
```

`fatal` upgrades the warnings to errors that block agent load.
For workspaces that want stricter discipline. Not shipped in
2.2; the default-warn is the right default.

---

## Part 10 — Failure Modes

New error codes. Adds §4.18 "Agent Envelope" to
`docs/explanation/error-model.md`.

| Code | Level | Detail | Where |
|---|---|---|---|
| `config.context_file_unprefixed` | Fatal | `{path}` | agent-load |
| `context.file_not_found` | Fatal | `{path, resolved}` | agent-load (reading context_files) |
| `context.file_too_large` | Fatal | `{path, size_bytes, max_bytes}` | agent-load |
| `config.template_render_failed` | Fatal | `{template, reason}` | agent-load (Jinja render error) |
| `config.required_block_missing` | Fatal | `{block_name, agent_path}` | agent-load (Jinja 3.1 required-block check) |
| `config.agent_inheritance_cycle` | Fatal | `{cycle: [path1, path2, ...]}` | agent-load (cycle detection) |
| `config.parent_agent_not_found` | Fatal | `{path, resolved}` | agent-load |
| `config.extends_template_without_inheritance` | Fatal | `{agent_path}` | agent-load (child uses `{% extends parent %}` without `extends:`) |
| `agent.template_warning` | Warning (Recoverable) | `{reference, reason, hint}` | agent-load (template-config consistency) |
| `authz.denied` | Recoverable | `{tool, target, rule_id, who_chain}` | engine + tool's permissions.require |
| `tool.permissions_no_context` | Fatal | `{tool, caller}` | tool calls permissions.check/require outside engine dispatch |

The `who_chain` detail on `authz.denied` names whether the
agent's rules or platform's rules matched — useful for
debugging which layer caused the denial.

---

## Part 11 — Required Spec Revisions

Mechanical edits to existing docs. Apply as a single revision
pass.

### ENV-R1. `docs/reference/schemas.md` (Agent config)

Add to the agent config schema:
- `extends: str` (optional, prefixed path).
- `permissions: list[Rule]` (optional, same shape as platform's
  `authorization.config.rules` entries).
- `context_files: list[ContextFile]` (optional, with
  `path/summary/primary` fields).

Document the prompt field as a Jinja2 template (backward-
compatible — plain text continues to work).

### ENV-R2. `docs/explanation/asset-management-contract.md`

Add to the `platform.yaml` schema:
- `workspace_summary: str` (optional, multi-line).

Document the install-relative path resolution
(`importlib.resources.files("kolsch")`) for `kolsch:` prefixed
references.

### ENV-R3. `docs/explanation/secrets-sandbox-authz-contract.md`

Add §X "Per-agent permissions" covering the composition order
(agent → platform → default-deny). Update the AuthzProvider's
documented surface to include the agent_permissions parameter.

### ENV-R4. `docs/explanation/agent-engine-contract.md`

Pre-flight gains two new steps:
- **5.6.5 — Resolve inheritance:** walk the `extends:` chain
  (with cycle detection), apply field overrides wholesale,
  surface the parent template for Jinja rendering.
- **5.6.6 — Render prompt template:** Jinja2 render with the
  standardized variable context.

Document the `agent.template_warning` event emission.

### ENV-R5. `docs/explanation/tools-subsystem.md`

Add §X "Permission API for tool authors":
- `permissions.check(who, what) -> AuthzEffect`
- `permissions.require(who, what) -> None`

Update the `@tool` decorator documentation to mention the
`summary` field.

### ENV-R6. `docs/explanation/error-model.md`

Add §4.18 "Agent Envelope" with the eleven codes from Part 10.

### ENV-R7. `process/development-testing-handbook.md`

Add success criteria §10.17 "Agent Envelope":
- Agent permissions override platform permissions in composition
  order.
- Jinja2 templating renders agent prompts at pre-flight.
- The five top-level variables (`tools`, `docs`, `workspace`,
  `agent`, `platform`) all expose the standardized method
  family.
- `tools.summary()` reads from `@tool(summary=...)` or first
  sentence of description.
- `context_files` two-tier rendering (primary inlined, secondary
  listed).
- Agent inheritance via `extends:` resolves correctly through
  multi-level chains; cycles detected.
- Template-config consistency warnings emit
  `agent.template_warning` events at agent-load.

### ENV-R8. `process/test-catalog.md`

Add Part 9.10 "Agent Envelope (ENV)" with TC-ENV-01..N cases:
- TC-ENV-01: agent permissions override platform permissions.
- TC-ENV-02: `permissions.require` from inside a tool body
  raises on deny.
- TC-ENV-03: Jinja prompt renders with all five top-level
  variables.
- TC-ENV-04: plain-text prompt (no Jinja markers) renders
  unchanged.
- TC-ENV-05: `tools.summary()` reads `summary` field from
  `@tool` decorator.
- TC-ENV-06: context_files primary tier inlined; secondary tier
  listed.
- TC-ENV-07: `kolsch:` prefix resolves to install location;
  `workspace:` to workspace root.
- TC-ENV-08: `extends:` inherits parent's fields; child override
  wins wholesale.
- TC-ENV-09: cycle detection raises `config.agent_inheritance_cycle`.
- TC-ENV-10: required block missing raises
  `config.required_block_missing`.
- TC-ENV-11: `agent.template_warning` emits at agent-load for
  empty-data references.
- TC-ENV-12: StrictUndefined raises on typo'd variable names.

### ENV-R9. `docs/explanation/v1-scope.md`

Note that prompts are Jinja templates (backward-compatible);
agents can extend other agents via `extends:`. Update §2.3 to
list `permissions`, `context_files`, `extends` as agent fields.

### ENV-R10. `AGENTS.md`

Add two Hard Rules:

> Agent prompts are Jinja2 templates. Variable names are
> resolved via the standardized API (`tools`, `docs`, `workspace`,
> `agent`, `platform`); typo'd variables raise at agent-load.

> Never put secrets in `platform.yaml`. The `platform` variable
> in agent prompts can read it; the YAML is for non-sensitive
> configuration only. Secrets live in `.env` per Phase 1.1.

### ENV-R11. New how-to: `docs/how-to/agent-templating.md`

Recipes for: writing your first templated prompt, using the
five variables, mixing compact and explicit styles, extending
a parent agent, declaring required blocks.

### ENV-R12. `docs/reference/module-api-reference.md`

Add Part 11.10 "Agent Envelope" covering:
- `kolsch.tools.permissions` module API.
- The Jinja template rendering context.
- `extends:` chain resolution semantics.

Update the dependency matrix (Part 12) to reflect the engine's
new pre-flight steps.

### ENV-R13. `pyproject.toml`

Add `jinja2 >= 3.1.0` to runtime dependencies.

Add `docs/` to package data so `importlib.resources.files("kolsch")`
finds it in installed contexts.

---

## Part 12 — Explicitly Deferred

1. **Mid-execution permission prompts** for tools. The
   `permissions.prompt(reason=...)` API that pauses a tool's
   execution and surfaces a modal. Useful but raises async /
   cancellation / timeout questions. Punt until a real use
   case demands it.
2. **Multi-parent inheritance.** `extends: [a.yaml, b.yaml]`.
   Powerful but the merge order and conflict-resolution rules
   are messy. Single parent is enough for Phase 2.2's needs;
   revisit if reference-agent patterns demand it.
3. **`abstract: true` field on parent agents.** A marker that
   prevents direct instantiation. Validation already handles
   incomplete parents (missing `agent_id` fails the same way as
   any incomplete agent); explicit marker is sugar.
4. **Strictness escalation** (`agent_template_strictness: fatal`).
   Warnings stay warnings in 2.2. If a workspace wants stricter
   discipline, the knob ships later.
5. **Selective field merging** in inheritance. "Inherit parent's
   tools but add one more." Increases complexity sharply;
   wholesale override is the discipline of 2.2.
6. **Custom Jinja filters** beyond the standard library set.
   Standard filters cover everything 2.2 needs.
7. **The `{{ memory }}` variable.** Phase 2.5+ when conversation
   memory + compaction lands.
8. **The `{{ session }}` / `{{ now }}` / `{{ model }}` top-level
   variables.** Useful but not load-bearing for Phase 2.2.
   Cheap to add later when a use case surfaces.
9. **`examples` and `category` fields on `@tool`.** Decorator
   stays minimal in 2.2 (`description` + `summary`).
10. **Workspace tree rendering.** `{{ workspace.tree(depth=N) }}`
    method explicitly dropped — trees without summaries are
    noise; `workspace.summary` carries the curated structure.

---

## Part 13 — Open Items

1. **The `parent` variable's surface in child templates.** Per
   §8.3, child writes `{% extends parent %}`. Alternatively
   could be quoted-path style `{% extends "kolsch:agents/_base.yaml" %}`.
   The variable form is preferred (no path duplication with
   the `extends:` YAML field) but Jinja's native `extends`
   expects a string. Implementation detail to confirm:
   register `parent` as a Loader-resolved template name, or
   use a custom template-loader that recognizes the bound
   variable.
2. **Backwards compatibility with `_THINKING_INDICATOR` removal.**
   Per QoL `qol-chat-capsule-layout.md`, the renderer no longer
   emits the thinking indicator. If a Phase 1.x agent prompt
   referenced model.request timing implicitly, behavior shifts.
   No known impacts; flagged for visibility.
3. **Caching of rendered prompts across exchanges.** If the
   resolved config + workspace state are unchanged between
   exchanges, the rendered prompt is identical. Could cache
   to save tokens (via Anthropic prompt caching, which natively
   supports this). Out of scope for 2.2 — future polish; the
   rendering itself is cheap.
4. **Tool-author API for accessing the runtime context.**
   `permissions` is the first such API; tools may also want
   access to `workspace`, `secrets`, `event log writer`, etc.
   Defer until a clear use case; the `kolsch.tools` namespace
   is the natural home if/when this grows.
5. **The `agent.template_warning` event's surface in
   non-chat callers.** §9.4 says "print to stderr." Format and
   exit-code semantics need a decision; probably one-line per
   warning + exit 0 (not a fatal error in the unattended path).
   Flagged.

---

## Part 14 — Status

This is **Phase 2.2 patch draft 0.1**. The patch:

- Closes the per-agent-permissions gap (deferred from Phase 2.0
  Part 7 §1).
- Establishes Jinja2 templating as the prompt-composition
  mechanism — DRY tools/docs/workspace handling, ergonomic
  inheritance, future-proof for the memory / scheduled-runs
  variables that arrive in later phases.
- Adds the `permissions.check/require` API so tool authors get
  the same authz primitives the engine uses.
- Lands `context_files` as the way agents declare their
  documentation context — both kolsch's bundled docs (via
  `kolsch:` prefix) and per-workspace customizations (via
  `workspace:` prefix).
- Ships the `summary` field on `@tool` so the same decorator
  feeds both verbose and compact tool rendering modes.
- Ships agent inheritance via `extends:` so reference agents
  (helper, openclaw, pi shipping in Phase 2.3) can share a
  common base without duplication.

**Apply in two passes:**

1. **Revisions.** ENV-R1 through ENV-R13 mechanically edit
   existing docs and create the one new how-to. Same pattern
   as the LLM-R / UX-R / FB-R / TOOL-R / CHAT-R passes before
   it.
2. **Implementation.** Multi-step per the build plan
   (`phase2.2-agent-envelope-build.md`). Roughly: jinja2 dep +
   capsule rendering layer; permission composition; `@tool`
   `summary`; `context_files` resolution; `extends:` resolution;
   warning emission; TC-ENV-* tests.

After this patch lands, agents are fully expressive: they
declare their tools, their permissions, their context, their
prompt structure, and inheritance relationships in one YAML.
Phase 2.3 (the kolsch bundle + drift detection) can then ship
helper / openclaw / pi as concrete examples of the full
envelope.

> **Post-2.2 amendment ([qol-setup-evolution-and-defaults.md](qol-setup-evolution-and-defaults.md)):**
> `platform.yaml` gains a `defaults:` block; agent field
> resolution falls through to `defaults.<field>` when the agent
> and its `extends:` chain don't specify it. Composition order:
> **agent → extends chain → defaults → schema default**. The
> first non-empty source wins per field, matching the wholesale-
> per-field override semantics §8.2 establishes for the agent
> ↔ extends edge — `defaults` slots into the chain as a
> lowest-priority tier, not a separate merge mode. The starter
> helper agent's `model:` field is the first concrete user of
> this fall-through (the agent omits `model:` and
> `defaults.model` carries the value chosen at `kolsch setup`
> time). See the QoL doc for the migration mechanism and the
> Step-15 capture for an end-to-end illustration.
