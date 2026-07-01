# Phase 1 — The Asset Management Contract

**Draft 0.1**

> The discovery and resolution layer between bundles on disk and the
> running platform. Owns: the bundle artifact format, the platform's
> filesystem layout, the asset catalog, the reference resolution
> mechanism, and the layer assembly that feeds the kernel resolver.
>
> Builds on Phase 1 Schemas (§A asset references), the Kernel/Resolver
> contract, the Tools subsystem spec, and the Skills subsystem spec.

---

## Part 0 — Decisions Locked

- **Bundle format**: directory with a `bundle.yaml` manifest. Not an
  industry standard for whole bundles (none exists). Skills inside
  bundles follow the Agent Skills standard (agentskills.io).
- **No reserved bundle names.** The platform ships its built-in
  assets under the `kolsch` bundle name (no special enforcement —
  same uniqueness rules as any other bundle). Users create their
  own bundles with their own names.
- **Platform root = current working directory.** The user runs the
  CLI in some directory; that directory IS the platform installation.
  No environment variable. No magic `~/<platform>/` path.
- **Four catalogued asset types**: tools, skills, agents, prompts.
  Data is NOT a catalogued asset type — data files in `data/` are
  user files, accessed via tools.
- **References are pre-resolved by Asset Management** before reaching
  the kernel resolver. The `config.set` event carries fully-dereferenced
  content (Decision A from the conversation).
- **Shorthand references** for tools and skills are permitted when
  unambiguous; full namespaced refs required when ambiguous.
- **PromptSpec is a sequence of refs** (concatenated in order) or a
  single inline string. No IDs, no layer wrappers.

---

## Part 1 — What Asset Management Is

The Asset Management subsystem is the bridge between **bundles on disk**
and the **running platform**. It does four things:

1. **Discovery** — at platform startup, scan the filesystem and build an
   in-memory catalog of every asset across every installed bundle.
2. **Resolution** — translate namespaced refs (`<bundle>/<type>/<name>`)
   into concrete content from disk.
3. **Layer assembly** — build the ordered list of partial configs that
   the kernel resolver merges.
4. **Pre-resolution** — substitute referenced content into the partials
   before the kernel resolver runs, so the resolved config (and the
   `config.set` event that captures it) is fully self-contained.

It does NOT own:
- Bundle distribution. v1 bundles are git-cloned or copied manually
  into the bundles directory; the platform doesn't fetch them.
- Dependency resolution. A bundle may declare dependencies in its
  manifest, but v1 does not transitively install them.
- Validation of asset contents. Each subsystem (Tools, Skills, etc.)
  validates its own assets through its Layer 3 validator. Asset
  Management validates structural things (does the file exist, does
  the directory layout match the standard).

### 1.4 Workspace Resolution

*(Phase 1.2, UX-R5.)*

Asset Management is rooted at a workspace path. The workspace path is
resolved exactly once per process at startup by
`platform_runtime.workspace.resolve_workspace`. The resolution chain,
highest precedence first:

1. The CLI's `--root <PATH>` flag.
2. The `KOLSCH_WORKSPACE` environment variable.
3. The platform-correct default: `<platformdirs.user_documents_dir()>/kolsch`
   — typically `~/Documents/kolsch` on macOS and Linux,
   `%USERPROFILE%/Documents/kolsch` on Windows.

The resolver does NOT consult the current working directory. Users
who want CWD-relative behavior set `KOLSCH_WORKSPACE=.` or pass
`--root .` explicitly. No silent fallback.

Asset Management never reads `KOLSCH_WORKSPACE` directly and never
imports `platformdirs` — only `resolve_workspace` does. Asset
Management is constructed with the resolved path.

**Provider env vars for scripted setup.** When `kolsch setup` runs in
`--non-interactive` mode, it consults two more env vars to wire a
provider without prompting:

- `KOLSCH_PROVIDER` — the provider name (e.g., `openrouter`).
- `KOLSCH_PROVIDER_KEY` — the API key value, passed to the connector's
  validate/discover flow. The key still lands in `.env` per the LLM
  provider configuration patch — it is never written to `platform.yaml`.

Both env vars are inputs to `kolsch setup` only; the runtime itself
does not consult them at agent execution time.

---

## Part 2 — Bundle Format

### 2.1 The directory layout

A bundle is a directory with a fixed layout:

```
<bundle-name>/
├── bundle.yaml           — required; the manifest
├── tools/                — declared tools (Tools subsystem spec)
│   ├── <tool-name>.py
│   └── ...
├── skills/               — skills (Skills subsystem spec)
│   ├── <skill-name>/
│   │   ├── SKILL.md
│   │   └── ...
│   └── ...
├── agents/               — agent definitions
│   ├── <agent-name>.yaml
│   └── ...
└── prompts/              — prompt files referenced from agents
    ├── <name>.md
    └── ...
```

Each top-level subdirectory holds one asset type. Subdirectories may be
absent if the bundle doesn't ship that type. A bundle that ships only
skills has only `bundle.yaml` and `skills/`.

The directory name IS the bundle name and IS the namespace for the
bundle's assets. A bundle directory `acme-research/` is the
`acme-research` bundle; its tool `web-search` is referenced as
`acme-research/web-search`.

### 2.2 The manifest

`bundle.yaml` declares the bundle's identity and metadata:

```yaml
name: "acme-research"           # required; must match the directory name
version: "1.2.0"                # required; semver
description: "..."              # required; short description
required_env_vars: []           # optional; secret names this bundle needs
  - "SERPAPI_KEY"
  - "GITHUB_TOKEN"
dependencies: []                # optional; other bundles this depends on
  - "core-tools"
```

**`name`** — must match the directory name (validated at catalog build).
Must be lowercase alphanumeric with hyphens and underscores. The name is
reserved across the platform (no two bundles may share a name).

**`version`** — semver string. v1 records but does not enforce version
checks.

**`description`** — short text describing what the bundle provides.
Surfaces in CLI listings and (later) the IDE.

**`required_env_vars`** — list of secret names the bundle's tools or
MCP servers need. At catalog build, the platform calls `secrets.exists()`
for each and warns if any are missing. Doesn't block loading — the
bundle's tools that need missing secrets will fail at registration time
with clearer errors.

**`dependencies`** — list of other bundle names this bundle assumes are
also installed. Informational in v1; the platform warns about missing
dependencies but does NOT transitively install them. The user manages
dependencies manually.

### 2.3 What's not in the manifest

- No `entrypoints`, no Python module declarations. Tools are discovered
  by scanning the `tools/` directory for `@tool`-decorated functions.
- No skill listings. Skills are discovered by scanning `skills/` for
  subdirectories containing `SKILL.md`.
- No license field. License is the bundle author's concern; v1 doesn't
  enforce or display.
- No author/contact field. v1 records minimal metadata; richer fields
  (author, repository URL, support email) can be added later
  non-breakingly.

### 2.4 Bundle standardization, honestly

There is no industry-wide bundle standard. What exists:

- **Agent Skills** (Anthropic, open standard at agentskills.io, adopted
  by OpenAI and Google) — for *skills only*. The SKILL.md format we use.
- **AgentSpec** (agents-oss) — single-agent manifest with framework
  codegen. Not a bundle format.
No prior industry-standard bundle format covers the
agent+tool+skill+prompt grouping shape this platform needs.

So this bundle format is platform-specific. **Skills inside bundles are
portable** (any Agent Skills consumer can use them). The rest of the
bundle is ours. Honest: skills are the interoperable surface; the
bundle wrapper is platform-specific.

---

## Part 3 — Platform Filesystem Layout

The platform root is **the current working directory** when the user
runs the CLI. Multiple installations are multiple directories; copying
the directory copies the installation.

### 3.1 The layout

```
<platform-root>/
├── platform.yaml         — startup configuration
├── .env                  — secrets (dotenv variant)
├── bundles/              — installed bundles
│   ├── <bundle-name>/
│   └── ...
├── sessions/             — per-session JSONL event logs
│   ├── ses_<id>.jsonl
│   ├── ses_<id>.jsonl.lock
│   └── ...
├── content/              — large-content sidecar store, keyed by hash
│   ├── <hash>.bin
│   └── ...
├── data/                 — user data assets
│   └── (user-organized)
├── logs/                 — platform diagnostic logs (NOT the event log)
│   └── platform.log
└── .platform/            — platform-internal state (caches, indexes)
    └── ...
```

Eight top-level entries: two files, six directories.

### 3.2 `platform.yaml` — Startup Configuration

The platform's configuration file. Schema:

```yaml
secrets:
  variant: "dotenv"
  config:
    path: ".env"
sandbox:
  variant: "native"
  config: {}
authorization:
  variant: "rule_based"
  config:
    rules:
      # Each rule carries an auto-assigned `id` (Phase 2.0, TOOL-R4)
      # — a stable identifier of form `rl_<hash[:8]>` derived from
      # what+pattern+effect. The id is the handle used by
      # `kolsch permissions {remove, show}`. Effect values:
      # "allow" | "deny" | "prompt" (Phase 2.0; see
      # `docs/explanation/secrets-sandbox-authz-contract.md` §3.7).
      - what: "*"
        pattern: "*"
        effect: "allow"
storage:
  entity_backend: "sqlite"          # sqlite (default) | postgres
  entity_path: ".platform/entities.db"
  # entity_dsn: "postgresql://..."  # used when entity_backend: postgres
event_bus:
  backend: "in_process"             # in_process (default) | redis
  # redis_url: "redis://localhost:6379"   # used when backend: redis
defaults:
  model:
    provider: "anthropic"
    model: "claude-opus-4-7"
  fallback_models:                  # Phase 1.3 (FB-R2) — list[ModelSpec input]
    - haiku                         # accepts alias, provider/model, or object
  memory:
    variant: "recent_buffer"
    config:
      recent_turns: 20
      max_tokens: 4000
llm_providers:                          # Phase 1.1 — populated by `kolsch connect`
  opencode:
    type: opencode
    api_key_secret: OPENCODE_API_KEY
    base_url: https://opencode.example.com/v1
    timeout_seconds: 60
    discovered_at: "2026-05-29T14:23:01Z"
    models:
      - id: gpt-oss-120b
        context_length: 131072         # filled from openrouter metadata
        supports_tools: true
        supports_streaming: true
      - id: qwen2.5-coder-32b
        context_length: null           # unknown — no metadata source
        supports_tools: null
        supports_streaming: null
  openrouter:
    type: openrouter
    api_key_secret: OPENROUTER_API_KEY
    base_url: https://openrouter.ai/api/v1
    timeout_seconds: 60
    serves_as_metadata_source: true    # cross-provider lookup capability
    discovered_at: "2026-05-29T14:20:11Z"
    models: []                          # live OpenRouter catalog when connected
model_aliases:                          # Phase 1.1 — deployment-level shortcuts
  smart: opencode/gpt-oss-120b
  cheap: opencode/qwen2.5-coder-32b
```

Eight top-level sections (provider modules + storage + bus + defaults +
`llm_providers` + `model_aliases`). Defaults become the system-defaults
layer of any agent config (the first layer the resolver merges; see §6
below).

The **defaults require no external services**: `storage.entity_backend:
sqlite` and `event_bus.backend: in_process` mean a fresh install runs
with nothing else installed. Postgres and Redis are opt-in scale-up
backends (see the Development & Testing Handbook §6 for the storage
tiers).

The schema is owned by the platform-level Pydantic models; not enumerated
in full here. Future contracts may add sections (e.g., memory backend
config when non-`recent_buffer` variants ship).

**Phase 1.1 — `llm_providers` and `model_aliases`** (LLM-R2). Populated
by `kolsch connect <provider>` (CLI surface) and editable by hand. One
entry per connected provider; each entry's `type` must be a key in the
connector registry (see Module API Reference Part 11.5). `model_aliases`
are deployment-level shortcuts that resolve to canonical
`provider/model` strings during Layer 1 validation; aliases do not chain
(see Schemas §A.4.1). Full field reference + per-connector extensions
live in `docs/explanation/llm-providers.md`.

**Phase 1.3 — `defaults.fallback_models`** (FB-R2). Optional. A list
of `ModelSpec` inputs (same three shapes as `model:` — object,
compact `provider/model` string, or alias). Becomes the system-
defaults layer's `fallback_models` field for any agent whose own
config omits it.

Precedence is the usual agent-wins rule, with NO merging:

- Agent specifies `fallback_models: [...]` → agent value wins, even
  if it's the empty list (an empty list means "no fallback for this
  agent").
- Agent omits `fallback_models` and `defaults.fallback_models` is
  set → platform default applies.
- Both absent → no fallback chain; a Recoverable `model.*` error
  fails the exchange.

The engine resolves entries lazily — it builds `crewai.LLM` clients
for fallback models only when the chain is actually consulted. See
the Phase 1.3 patch §1.2 for the full precedence story and §2.1 for
the engine's loop.

**Phase 2.2 — `workspace_summary`** (ENV-R2). Optional multi-line
text describing the workspace's purpose. Surfaced in agent prompts
via `{{ workspace.summary }}` and `{{ platform.workspace_summary }}`
(process/phase2.2-agent-envelope-specs.md §4.4 / §7). Returns
`None` when unset so `| default("…")` in templates works.

```yaml
# platform.yaml (excerpt)
workspace_summary: |
  Active development workspace for the Kolsch platform. Source code
  lives under bundles/ as agent YAML + tool Python; sessions/ holds
  append-only event-log JSONL files; docs/ is reference material
  referenced by agents via context_files.
```

### 3.2.Y Install-relative path resolution (`kolsch:` prefix)

Phase 2.2 introduces install-relative paths for context files and
the `extends:` chain. A reference of the form `kolsch:<rel>`
resolves to a resource inside the installed kolsch package:

```python
importlib.resources.files("kolsch") / "<rel>"
```

The `kolsch` package is an umbrella package shipped from
`src/kolsch/`; its `__init__.py` is empty. `pyproject.toml`'s
`[tool.hatch.build.targets.wheel.force-include]` section ships
the repo-root `docs/` directory and `AGENTS.md` into the wheel
under `kolsch/docs/` and `kolsch/AGENTS.md`. In an installed wheel
`importlib.resources.files("kolsch")` returns a `Traversable` that
finds those files directly.

In source-tree dev (editable install) `importlib.resources` returns
`src/kolsch/`, which does NOT contain the force-included files.
Phase 2.2 Step 12's loader handles the source-tree-vs-installed
dispatch: probe `importlib.resources` first; if the resource is
absent AND the runtime is a source-tree install, fall back to
repo-root-relative resolution via the `kolsch` package's
`__file__.parent.parent.parent`.

`workspace:<rel>` resolves to `<workspace_root> / <rel>` per
Phase 1.2's `resolve_workspace`. Plain paths (no prefix) are
rejected at agent-load with `config.context_file_unprefixed`.

### 3.3 `.env` — Secrets

The dotenv variant's source file. Standard `.env` format:

```
SERPAPI_KEY=...
OPENAI_API_KEY=...
GITHUB_TOKEN=...
```

Single file at platform root. Parsed once at platform startup.

### 3.4 `bundles/` — Installed Bundles

Per Part 2. Each subdirectory is a bundle. The directory name is the
bundle's namespace.

### 3.5 `sessions/` — Session Event Logs

Per the Session/Run Store contract §3.1. One `.jsonl` plus one
`.jsonl.lock` per session. Flat directory, not nested.

### 3.6 `content/` — Large-Content Sidecar Store

When an event's payload exceeds the inline threshold (~50KB, per Tools
spec §6.4 and the Memory Provider contract), the platform stores the
full content in `content/<hash>.bin` and the event carries a
`content_ref` field pointing at the hash.

Properties:
- **Immutable**: once stored, never modified. The hash IS the identity;
  the same content always produces the same hash.
- **Flat**: one file per hash, no nesting.
- **Append-add-only**: v1 doesn't garbage-collect orphaned content
  (content not referenced by any event). The store grows monotonically.
  Eviction policy is a Phase 2+ concern.
- **Format**: `.bin` extension for simplicity; the file is the raw bytes
  of the content. A `<hash>.meta.json` sidecar could record metadata
  (original mime type, size, source event) but is not required in v1.

Hash algorithm: SHA-256, hex-encoded. 64-character filenames. Suitable
for filesystems that support long filenames (all modern ones).

### 3.7 `data/` — User Data Assets

The user's space. Documents, reference materials, working files. The
platform doesn't impose structure here — the user organizes by folder
however suits their workflow.

Tools (`read`, `write`, `edit`) operate on paths anywhere the platform
can reach, but `data/` is the conventional location for user-owned
content. The CLI's working directory for tool operations may default to
`data/`; implementation choice.

A future filesystem-tagged memory variant could use folder paths in
`data/` as memory tags. The v1 `recent_buffer` variant does not — it
holds memory in process. Both can coexist in this layout.

### 3.8 `logs/` — Platform Diagnostic Logs

Separate from the session event log. This is the platform's own
debug/info/warning output: "Started session X." "Connected to Redis."
"Loaded bundle acme-research."

Standard application logging. Goes to `logs/platform.log` (rotating,
size-capped) and optionally to stderr in development. Configurable via
`platform.yaml` (`logging.level`, `logging.path`, etc. — minor detail
not enumerated here).

NOT to be confused with `sessions/<id>.jsonl`, which is the ground-truth
agent execution record.

### 3.9 `.platform/` — Internal State

Caches and indexes the platform builds and re-uses across runs.
Examples:
- Resolved asset catalog cache (so startup doesn't re-scan all bundles
  every time).
- Session index for fast listing.
- Parsed bundle manifest cache.

Leading dot indicates platform-internal — users shouldn't need to touch
it. v1 may keep this empty (re-scan on every startup) and add caching
later as performance demands.

### 3.10 Platform Bootstrap and `platform.yaml` Ownership

Asset Management is the platform-bootstrap module. At startup, in order:

1. **Read `<platform-root>/platform.yaml`.** If absent, generate a
   default (per File Handling §4.1 for the `file_access` section, and
   sensible defaults for other sections — see §3.2 for the schema).
2. **Parse and validate the top-level structure.** Each section
   (`secrets`, `sandbox`, `authorization`, `event_bus`, `file_access`,
   `defaults`) is checked for required fields.
3. **Make config sections available to subsystems** via accessors:

```
class AssetManagement:
    def get_platform_config(self, section: str) -> dict
```

Subsystems request their section by name at their own initialization.
The Authorization provider calls `get_platform_config("authorization")`;
the Secrets provider calls `get_platform_config("secrets")`; etc.

This makes Asset Management the platform-bootstrap module. It owns
config loading, dispatches to subsystems by name, and provides the
discovery surface for the asset catalog.

After `platform.yaml` is loaded and providers are initialized, Asset
Management builds the asset catalog (per Part 4).

**Failure handling:** A malformed `platform.yaml` is a fatal startup
error. The platform refuses to start with a clear error message
pointing at the offending field.

---

## Part 4 — The Asset Catalog

### 4.1 Build process

At platform startup, the Asset Management module:

1. **Scan `bundles/`** for subdirectories. Each is a candidate bundle.
2. **Read each bundle's `bundle.yaml`**. Validate the manifest. Skip
   bundles with invalid manifests (logged as warnings).
3. **Verify name = directory** consistency. Mismatch is an error.
4. **Detect duplicate bundle names** across the bundles directory.
   Duplicate is a fatal startup error — bundles must have unique names
   because the name is the namespace.
5. **Scan each bundle's asset subdirectories** (`tools/`, `skills/`,
   `agents/`, `prompts/`). Build the asset entries.
6. **Validate references in agent files** (best-effort — check that
   referenced tools/prompts exist in the catalog). Missing
   references are warnings at catalog build, errors at agent execute.
7. **Produce the `AssetCatalog`**.

### 4.2 The catalog structure

```
AssetCatalog
├── bundles      dict[str, BundleManifest]
└── assets       dict[ref, AssetEntry]

BundleManifest
├── name
├── version
├── description
├── required_env_vars
├── dependencies
└── path                          — absolute path to the bundle directory

AssetEntry
├── ref            — "acme-research/web-search"
├── asset_type     — "tool" | "skill" | "agent" | "prompt"
├── bundle         — bundle name
├── path           — absolute path to the asset file/directory
└── content        — None at catalog build; populated lazily on first use
```

The catalog is built once at startup, held in memory, consulted by every
component that needs to resolve a ref.

### 4.3 Asset types

**`tool`** — a Python file in a bundle's `tools/` directory containing
one or more `@tool`-decorated functions. (Per Tools subsystem spec §2.)
The asset's `ref` points at the file; the tool name (from the decorator)
provides the final segment of the ref.

Note: a single file may contain multiple `@tool` decorators. Each
decorated function is a separate asset entry; the ref's final segment
is the tool's `name` from its decorator, not the filename. The filename
is incidental.

**`skill`** — a directory in a bundle's `skills/` directory containing
SKILL.md. The directory name is the skill name; the ref ends with the
directory name. At catalog build, Asset Management parses SKILL.md's
YAML frontmatter and stores the full SkillMetadata in the AssetEntry
(per Skills subsystem spec §3.3): name, description, license,
compatibility, metadata, allowed_tools, disable_model_invocation. The
skill body is NOT parsed at catalog build — it loads lazily via the
`read` tool when needed.

**`agent`** — a `.yaml` file in a bundle's `agents/` directory. The
file's stem (without `.yaml`) is the agent name. The file IS the
agent's configuration (partial AgentConfig).

**`prompt`** — a `.md` file (or any extension) in a bundle's `prompts/`
directory, referenced from an agent's PromptSpec. The file's stem is
the prompt's name.

### 4.4 The `kolsch` platform-shipped bundle

The platform ships its built-in assets under the bundle name
`kolsch`. There is no name *reservation* — `kolsch` participates in
the standard uniqueness rules (a second bundle named `kolsch` would
trip `AssetsDuplicateBundleName` like any other duplicate). The
four builtin tools (`read`, `write`, `edit`, `bash`) are registered
directly by the Tools subsystem at platform startup, not shipped as
bundle files. The 14 built-in workflow node types (`kolsch/nodes/<name>`)
are virtual entries injected at catalog scan time (Phase 3.0 Step 3);
authoring a file at one of those reserved slots
(e.g., `bundles/kolsch/nodes/bash.py`) trips
`WorkflowBuiltinShadowed`.

Phase 3.0 R3 #5 dropped a previously-speculative `core` reservation
that anticipated a `core/principals/*` ref family; that design was
abandoned and no bundle directory name is reserved today.

---

## Part 5 — Reference Resolution

### 5.1 The ref pattern

A namespaced ref has the form:

```
<bundle-name>/<asset-type>/<asset-name>
```

Examples:
- `acme-research/web-search`
- `acme-research/literature-review`
- `personal/researcher`
- `team-shared/code-style`

The `<asset-type>` is the asset directory's name (`tools`, `skills`,
`agents`, `prompts`).

### 5.2 Shorthand references

For tools and skills (only), an agent's config may use a bare name
instead of the full ref:

```yaml
tools:
  config:
    declared:
      - ref: "web-search"               # shorthand
      - ref: "acme-research/scholarly-search"   # full
```

The Tools subsystem (and Skills, respectively) resolve shorthand at
Layer 3 validation:

1. If the ref contains `/`, treat as full ref. Resolve directly.
2. If the ref has no `/`, search the catalog for assets of the
   appropriate type whose final ref segment matches.
3. If exactly one match: resolve to its full ref.
4. If multiple matches: validation error with a structured message
   listing the candidates.
5. If no match: standard "not found" error.

Shorthand is NOT supported for agents or prompts. Agent and prompt refs
must be fully qualified — they're typically authored by humans and
clarity matters more than convenience.

### 5.3 Resolution to content

When the platform needs an asset's actual content (during pre-resolution,
§6), it consults the catalog:

- **Tool**: Python file path. Asset Management returns the path string
  only — Asset Management is read-only and does NOT import the Python
  file or execute its decorator. The Tools subsystem performs the
  import at engine setup time (per Tools spec §6.1), using the path
  Asset Management provides. This preserves Asset Management's
  read-only invariant (Am-I10) — discovery scans the filesystem and
  parses YAML/JSON, but never executes Python.
- **Skill**: SKILL.md path. The skill's metadata (name, description,
  etc.) is parsed at catalog build; the body is read at use (force-invoke
  or model-initiated read).
- **Agent**: YAML file path. The file's content (a partial AgentConfig)
  is loaded as a dict.
- **Prompt**: file path. The file's text content is read.

Asset Management caches loaded content during a platform run to avoid
repeated disk reads.

---

## Part 6 — Layer Assembly for the Resolver

### 6.1 The Phase 1 layer list

For an `execute` call targeting an agent, Asset Management constructs:

```python
partials = [
    system_defaults,     # from platform.yaml's "defaults" section
    agent_definition,    # from <bundle>/agents/<name>.yaml
    call_context,        # from the ExecutionContext (may be empty)
]
```

Three layers, ordered. The kernel resolver merges left-to-right; later
layers override earlier ones (per RFC 7386).

### 6.2 Pre-resolution

Before passing to the kernel resolver, Asset Management walks each
partial and substitutes file references with content:

**PromptSpec resolution.** Each ref in `prompt` (list form) is loaded
from disk; the list of strings becomes the resolved prompt content.
Inline `prompt` strings pass through unchanged.

```yaml
# Before:
prompt:
  - "acme-research/role.md"
  - "personal/project-context.md"

# After:
prompt:
  - "<contents of role.md>"
  - "<contents of project-context.md>"
```

(In the resolved config dict, the field is the list of loaded strings.
The Agent Engine concatenates them when assembling the system prompt.)

**Other refs left as refs.** Tool and skill refs in `subsystems.tools`
and `subsystems.skills` are NOT pre-resolved to content. They remain
as namespaced refs in the resolved config. Reason: tools are
runtime-executed code, skills are runtime-loaded content; the *config*
just references which ones to enable. The Tools and Skills subsystems
handle their loading per their own contracts at engine setup time.

So pre-resolution is specifically for prompts — content the system
prompt directly assembles from files. Other asset types resolve at
the subsystem level.

### 6.3 The `config.set` event

After kernel resolution, the resolved config is captured in a
`config.set` event (per Phase 1 Schemas §B.7). The event's
`data.resolved_config` carries the full resolved config with:

- Pre-resolved prompt content inlined as strings.
- Tool refs preserved (the Tools subsystem will load these at engine
  setup and emit `tool.called`/`tool.result` events when invoked).
- Skill refs preserved (the Skills subsystem surfaces descriptions in
  the system prompt at assembly time, loads bodies via `read` on
  demand).
- All other config preserved verbatim.

This satisfies the Materialization Rule for prompt content: the actual
text the model will see in its system prompt is captured at the moment
of resolution, not re-fetched from disk later. Files changing on disk
between resolution and replay do not affect the historical record.

For tools and skills, the Materialization Rule is satisfied by the
events the subsystems emit during execution (`tool.called` with actual
arguments, `tool.result` with verbatim returns — and for skills, the
`read` tool's `tool.result` captures the skill body when loaded).

### 6.4 PromptSpec schema revision

The PromptSpec in the schemas needs revision to match this contract:

**Before** (schemas draft 0.4 §A.6):

```
PromptSpec
└── layers   [PromptLayer]   — ordered; concatenated in order

PromptLayer
├── id       string          — e.g. "agents", "soul", "tools"
├── source   enum            — file | inline
├── ref      string          — file path, when source = file
└── content  string          — literal text, when source = inline
```

**After**:

```
PromptSpec   = list[string] | string

  list[string] — ordered list of asset refs OR loaded content strings.
                 Before pre-resolution, items are refs.
                 After pre-resolution, items are content strings.
                 The Agent Engine concatenates them in order.

  string       — single inline content string. No file refs.
                 Used by agents authored entirely inline.
```

Two shapes, both natural. Schema is YAML-friendly (a list of strings
looks like a list of strings in YAML).

This is a simplification — no IDs, no per-item wrappers, no source
discriminator. The user picks list (for composability) or string (for
quick inline).

---

## Part 7 — Conformance Invariants

### Am-I1 — Bundle directory = name

A bundle's `bundle.yaml` `name` MUST match its directory name. Mismatch
is a fatal startup error.

### Am-I2 — Unique bundle names

No two bundles in the bundles directory may share a name. Duplicate
names are a fatal startup error.

### Am-I3 — Reserved names (retired)

No bundle directory name is reserved. A previously-documented `core`
reservation was speculative for a `core/principals/*` ref family that
was abandoned in Phase 3.0 R3 #5. The platform-shipped bundle name is
`kolsch`; it participates in the standard uniqueness rule (Am-I2)
with no special enforcement. Within the `kolsch` bundle, the 14
built-in workflow node slots `kolsch/nodes/<name>` are protected at
catalog scan time by `WorkflowBuiltinShadowed` (Phase 3.0 Step 3) —
that protection is per-slot, not per-bundle-name.

### Am-I4 — Pre-resolution of prompt content

PromptSpec list items that are refs MUST be resolved to actual content
before the kernel resolver runs. The resolved config (and its
`config.set` event) MUST NOT contain unresolved prompt refs.

### Am-I5 — Tool and skill refs pass through

Tool and skill refs in `subsystems.tools` and `subsystems.skills`
MUST NOT be pre-resolved to content by Asset Management. They remain
as refs in the resolved config; the respective subsystems handle
their loading.

### Am-I6 — Shorthand resolution is deterministic

Shorthand ref resolution MUST be deterministic given the same asset
catalog. Specifically: if shorthand `web-search` matches multiple
candidates, the resolution MUST fail with an explicit error listing
all candidates — NOT silently pick one.

### Am-I7 — Catalog immutable per platform run

The asset catalog is built at startup and MUST NOT mutate during the
platform's run. New bundles added to disk are not visible until restart.
This makes resolution deterministic within a session and avoids
race conditions with concurrent operations.

(A future hot-reload feature is permitted by the contract — it would
involve atomic catalog swap — but v1 doesn't implement it.)

### Am-I8 — Content store immutability

Files in `content/` MUST be immutable once written. The platform MUST
NOT modify a content file after creation. Garbage collection (removing
orphaned content) is a Phase 2+ concern; v1 keeps everything.

### Am-I9 — Platform root is CWD

The platform's directories are resolved relative to the current working
directory at startup. The platform MUST NOT use environment variables
or fixed paths (`~/<platform-name>/`) to locate its root. Running the
CLI in a different directory uses a different platform installation.

### Am-I10 — Catalog and resolution are read-only

Asset Management operations (catalog build, ref resolution, layer
assembly) MUST be read-only with respect to the filesystem. They MUST
NOT modify bundle files, write to `data/`, or create new sessions.
Asset Management is a discovery/resolution layer; it does not act.

### Am-I11 — Platform bootstrap ownership

Asset Management owns the loading and dispatch of `platform.yaml`. No
other module reads the file directly; subsystems retrieve their config
sections via Asset Management's `get_platform_config(section)` accessor
(per §3.10). This makes platform-bootstrap a single point of
responsibility.

---

## Part 8 — Open Items

1. **Bundle distribution UX.** v1 has no `install` command — users
   `git clone` (or copy) bundles into the bundles directory manually.
   A future CLI subcommand could automate this. Out of v1 scope.

2. **Bundle versioning and updates.** v1 records `version` but doesn't
   check compatibility or update. Updating a bundle is "delete the old
   directory, install the new one." Versioning becomes useful when
   dependency resolution arrives in a later phase.

3. **The `content/` GC policy.** v1 keeps all content; orphaned
   content (no longer referenced by any event) accumulates. A future
   GC pass could walk events, mark referenced hashes, sweep
   unreferenced. Not v1.

4. **First-run UX for empty installations.** A fresh `<platform-root>/`
   has no `bundles/`, no `platform.yaml`, no `.env`. The first run
   could offer to scaffold the layout. Defer to the v1 scope document's
   §2 for the actual UX; this contract just specifies what the layout
   looks like once present.

5. **Platform-shipped baseline assets live in `kolsch`.** The
   `kolsch` bundle is the home for any default agents, prompts, or
   tools the platform ships. v1 ships only the four builtin tools
   (registered directly by the Tools subsystem, not as bundle files)
   plus 14 virtual workflow node slots `kolsch/nodes/<name>`
   (injected at catalog scan time per Phase 3.0 Step 3); future
   baseline assets land in `bundles/kolsch/`. No bundle-name
   reservation enforces this; the standard uniqueness rule (Am-I2)
   does.

6. **Hot reload.** Adding a bundle mid-session is not supported in v1.
   A user adds a bundle by stopping the platform, copying the bundle
   into `bundles/`, and restarting. Phase 2+ may add atomic catalog
   swap if real cases justify it.

7. **Cross-bundle ref validation timing.** Agent files in bundle A
   referring to assets in bundle B — currently validated best-effort
   at catalog build, definitively at agent execute. v1 surfaces these
   as warnings (catalog build) and errors (execute). Strict resolution
   at catalog build would be more eager but might surface noise during
   bundle-development workflows. v1's two-tier strictness is the
   pragmatic compromise.

8. **The PromptSpec schema revision.** Schemas draft 0.4 §A.6 needs
   revision to match the simplified shape (list | string). This is
   a small follow-up.

9. **The platform.yaml schema.** Sketched in §3.2 but not formally
   specified as Pydantic models. The platform-level config schema is
   conceptually small; specification can land alongside the platform
   startup code.
