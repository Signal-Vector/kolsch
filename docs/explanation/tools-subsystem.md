# Phase 1 — The Tools Subsystem Specification

**Draft 0.1**

> Tools are the agent's actuators. The Tools subsystem owns: how a tool is
> defined, how tools are discovered and registered, how tools are invoked,
> how MCP servers integrate. The Agent Engine consumes this subsystem;
> Authorization, Secrets, and Sandbox all participate.
>
> Builds on Phase 1 Schemas §A.7 (subsystems wrapper), the Agent Engine
> contract, and the Secrets/Sandbox/Authorization Provider contracts.

---

## Part 0 — Decisions Locked

- **Tools subsystem is agent-level**, configured in `AgentConfig.subsystems.tools`.
- v1 ships one variant: `standard`.
- Three tool sources: **builtin** (platform stdlib), **declared**
  (user/bundle-supplied), **mcp** (external server tools).
- **Tool definition: `@tool` decorator** on a Python function with rich
  declarative arguments.
- **Parameter schemas: Pydantic models**, JSON Schema exported for the model.
- **Discovery: hybrid.** Tool directories scanned per bundle; agent
  configs enable by namespaced reference.
- **Built-in tools**: `read_file`, `write_file`, `list_files`, `bash`.
  Four tools, full spec in §4.5.
- **MCP lifecycle**: `lazy` by default; `eager` opt-in.
- **Large tool results**: truncate inline at ~50KB; store full content in
  a sidecar file referenced by content-hash for events that need it.
- **Tool invocation context** contains `secrets`, `session_id`, `agent_id`.
  Nothing else. Authorization happens before invocation; tools don't write
  to the log.
- Tool actions are named **`<bundle>/tools/<name>`** (builtin /
  declared — the 3-segment catalog ref per
  qol-action-namespace-bundle-scoped.md) or **`mcp.<server>.<tool>`**
  (MCP) in the Authorization namespace.

---

## Part 1 — What the Subsystem Is

The Tools subsystem is the agent-level configuration of available actuators:
the things an agent can *do* in the world beyond just generating text. Tools
include reading and writing files, running commands, querying APIs,
invoking other systems via MCP, and anything else the platform or user-installed
bundles make available.

### 1.1 The three tool sources

**Builtin tools** ship with the platform. There are four: `read_file`,
`write_file`, `list_files`, `bash`. They are available to any agent that
lists them.

**Declared tools** are Python functions provided by the user or by
installed bundles, defined with the `@tool` decorator (Part 2). They live
in files on disk and are discovered at platform startup.

**MCP server tools** are external programs implementing the Model Context
Protocol. They run as subprocesses, expose tools over MCP, and the platform
proxies tool calls to them. The MCP ecosystem provides many tools the
platform doesn't have to ship itself.

### 1.2 What the subsystem owns

- The format of a tool definition.
- The discovery mechanism for finding tools.
- The registration flow that makes tools available to the Agent Engine
  (which then registers them with the underlying framework).
- The invocation flow that calls a tool when the model requests it,
  routing through Authorization, Secrets, and Sandbox.
- The MCP server lifecycle (spawn, ready-check, tool listing).
- The action namespace (`<bundle>/tools/<name>` for builtin /
  declared tools, `mcp.<server>.<tool>` for MCP).

### 1.3 What the subsystem doesn't own

- Tool *execution* mechanics — the underlying framework does the actual
  function call; this subsystem prepares and observes.
- The session event log — the Agent Engine emits `tool.called`,
  `tool.result`, `tool.failed` events; this subsystem provides the data
  for those events.
- Authorization decisions, secret resolution, sandboxing — those are
  Provider Modules; this subsystem consults them.

---

## Part 2 — Tool Definition Format

Tools are defined as Python functions decorated with `@tool`.

### 2.1 Basic shape

```python
from platform import tool
from pydantic import BaseModel

class WebSearchArgs(BaseModel):
    query: str
    limit: int = 10

@tool(
    name="web_search",
    description="Search the web via SerpAPI and return top results.",
    args_schema=WebSearchArgs,
    secrets_needed=["SERPAPI_KEY"],
)
async def web_search(args: WebSearchArgs, ctx: ToolContext) -> dict:
    """Implementation reads args.query and args.limit;
    accesses ctx.secrets['SERPAPI_KEY']; returns structured result."""
    ...
```

### 2.2 The `@tool` decorator arguments

```
@tool(
    name              str           required
    description       str           required
    summary           str?          optional; one-line summary used by
                                    tools.summary() rendering (Phase 2.2).
                                    Default: first sentence of description.
    args_schema       BaseModel?    optional; if absent, tool takes no args
    secrets_needed    [str]         optional; default []
    returns_schema    BaseModel?    optional; for tools returning structured data
    action            str?          optional; overrides default action name
                                    (default: f"{home_bundle}/tools/{name}",
                                    e.g. "kolsch/tools/bash" — the
                                    home_bundle is stamped at tool-
                                    registration time by the engine,
                                    not by the @tool decorator)
    timeout_seconds   int?          optional; tool-specific timeout override
    hide_if_secrets_missing bool    optional; default False (feat-139, §6.1.1)
    unavailable_hint  str?          optional; recovery hint when hidden (§6.1.1)
)
```

**`name`** — the tool's identifier. Used in the action namespace, in
references, in the model's tool-call requests. Must match `[a-z][a-z0-9_]*`.

**`description`** — what the tool does. This text goes to the LLM as part
of the tool definition. Quality of description directly affects whether
the LLM uses the tool correctly. Tool authors are responsible for writing
clear, trigger-rich descriptions.

**`summary`** *(Phase 2.2, ENV-R5)* — one-line summary used by
`tools.summary()` rendering in Jinja-templated prompts (see
process/phase2.2-agent-envelope-specs.md §5). Surfaced as
`ToolItem.summary` on the `tools` variable. Optional; defaults to
the substring of `description` from start through the first period
(trimmed). The model still receives the full `description` via the
upstream LLM API's `tools=` parameter — `summary` only affects the
prompt's conversational tool section.

**`args_schema`** — the Pydantic model describing the tool's parameters.
The platform exports this as JSON Schema for the LLM. Optional for tools
that take no arguments.

**`secrets_needed`** — list of secret names the tool needs. The platform
validates these exist at registration time (via `secrets.exists()`),
resolves them at invocation time, and passes them via `ctx.secrets`.
A missing one fails registration (`tool.setup_failed`) unless the tool
sets `hide_if_secrets_missing` (§6.1.1).

**`returns_schema`** — optional Pydantic model describing structured
return value. Used by the platform for validation and for showing the
LLM what structure to expect. Tools returning unstructured text omit this.

**`action`** — overrides the default action name. Almost never needed;
exists for tools that legitimately want to live in a different namespace
(e.g., a sandboxed tool that should be authorized as `system.X`). Default
is `<home_bundle>/tools/<name>` (the 3-segment catalog ref, with
`home_bundle` stamped at registration time by the engine).

**`timeout_seconds`** — per-tool timeout. Overrides the Engine's default
timeout. For long-running tools (a thorough web search, a large file
processing) the tool can declare it needs more time.

### 2.3 The tool function

```python
async def tool_function(args: ArgsModel, ctx: ToolContext) -> ResultType:
    ...
```

Signature constraints:
- Must be `async`. (Sync tools are wrapped at registration time.)
- First parameter: the args model instance (Pydantic-validated by the
  framework before invocation). Or no args parameter, for argless tools.
- Second parameter: `ToolContext`. Optional — tools that don't need it
  can omit it.
- Return type: whatever the tool produces. Captured verbatim on
  `tool.result` (subject to large-content handling, Part 6.4).

The function raises on failure. Raised exceptions become `tool.failed`
events (not `tool.result` with `is_error=true`); the framework converts
them.

### 2.4 The `ToolContext`

```
class ToolContext:
    secrets:    dict[str, str]    # resolved secrets, keyed by name
    session_id: str               # "ses_..."
    agent_id:   str               # "ag_..."
```

Three fields, all read-only. Tools cannot:
- Write to the event log (the engine does that).
- Modify other tools' state.
- Inspect the principal or perform authorization checks (already done).
- Access platform internals beyond what's in `ctx`.

The lean context is the discipline: tools are pure-ish functions of their
args and their declared secrets. Tools that need more (cross-tool state,
external sessions) are signaling they want to be a different kind of
subsystem.

### 2.5 Example: a simple declared tool

```python
from pydantic import BaseModel
from platform import tool, ToolContext

class GreetArgs(BaseModel):
    name: str
    formal: bool = False

@tool(
    name="greet",
    description="Generate a greeting for someone by name.",
    args_schema=GreetArgs,
)
async def greet(args: GreetArgs, ctx: ToolContext) -> str:
    if args.formal:
        return f"Good day, {args.name}."
    return f"Hi, {args.name}!"
```

No secrets, no MCP server. Pure declared tool. Lives in a bundle's
`tools/` directory.

---

## Part 3 — The `standard` Variant Config

The Tools subsystem config body, in AgentConfig:

```yaml
subsystems:
  tools:
    variant: standard
    config:
      builtin: ["read_file", "write_file", "list_files", "bash"]
      declared:
        - ref: "local/web-search"
        - ref: "acme-research/scholarly-search"
      mcp:
        - name: "github"
          command: ["npx", "@modelcontextprotocol/server-github"]
          lifecycle: "lazy"
          secrets_needed: ["GITHUB_TOKEN"]
          env_inject:
            GITHUB_PERSONAL_ACCESS_TOKEN: "GITHUB_TOKEN"
        - name: "filesystem"
          command: ["mcp-server-filesystem", "--root", "/data"]
          lifecycle: "eager"
```

### 3.1 `builtin` — list of names

A list of built-in tool names to include. Each name must be one of the
four shipped builtins. Unknown names are a Layer 3 validation error.

If the agent wants no built-in tools, the list is empty.

### 3.2 `declared` — list of refs

Each `declared` entry is a reference to a declared tool by namespaced
path (Option B asset references). The format is
`<package>/tools/<tool-name>`. Conventional packages:
- `local` — tools defined in the user's own (un-packaged) `tools/`
  directory.
- `kolsch` — platform-shipped tools. The builtin tools (`read_file`,
  `write_file`, `list_files`, `bash`, and the rest of the shipping set
  in §4.5) are plain bundle files: their source is the starter bundle
  `src/cli/_starter_bundle/kolsch/tools/`, which `kolsch setup` copies
  to `<workspace>/bundles/kolsch/tools/`. No bundle-name
  reservation enforces this — `kolsch` participates in the standard
  uniqueness rule like any other bundle.

The platform's tool discovery scans known tool directories (per Part 5
below) and the agent's config enables which ones to use.

### 3.3 `mcp` — list of MCP server configurations

Each MCP server entry declares:

- **`name`** — the server's local name (used in action namespace as
  `mcp.<name>.<tool>`).
- **`command`** — argv list to spawn the server subprocess.
- **`lifecycle`** — `lazy` (default) or `eager`. See Part 5.
- **`secrets_needed`** — list of secret names. See §5.3.
- **`env_inject`** — map of env-var-name to secret-name. Resolved at
  spawn time. See §5.3.

### 3.4 Layer 3 validation

The `standard` variant's validator is a `ConfigValidator` per the Kernel
contract — `Callable[[dict], ValidationResult]`. It receives the
subsystem config body's dict and returns structured validation results.
The Kernel calls it during Layer 3 validation per its dispatch mechanism
(Kernel §4).

The validator checks:

- Each `builtin` name is a known built-in tool.
- Each `declared.ref` resolves to a discovered tool.
- Each MCP server `name` is unique within this agent's config.
- Each `mcp[].secrets_needed` exists (via `secrets.exists()`).
- Each `mcp[].env_inject` maps to a name in `secrets_needed`.

Failures produce structured `ValidationError`s with `source: "tools.standard"`
and the specific issue.

---

## Part 4 — Built-in Tools

> **Superseded by §4.5.** This early sketch specced `read`/`write`/`edit`/
> `bash`; the shipping set is `read_file`/`write_file`/`list_files`/`bash`
> (there is no `edit` tool — `list_files` replaced it). See §4.5 for the
> concrete shipping set; this part is kept for the original design notes.

The platform's standard library. Four tools, specified concretely.

### 4.1 `read`

```
@tool(
    name="read",
    description="Read the contents of a file from disk.",
    args_schema=ReadArgs,
)

class ReadArgs(BaseModel):
    path: str            # path to the file
    encoding: str = "utf-8"

returns: str             # the file's content
```

**Behavior:** opens the file at `path`, reads it, returns the content as
a string with the declared encoding. Raises `FileNotFoundError` if absent,
`PermissionError` if not readable.

**Sandboxing:** subject to the Sandbox provider's policy. The `native`
variant imposes no restriction; future sandboxed variants enforce
filesystem isolation.

### 4.2 `write`

```
@tool(
    name="write",
    description="Write text content to a file on disk, overwriting if exists.",
    args_schema=WriteArgs,
)

class WriteArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True   # create parent directories if absent
    encoding: str = "utf-8"

returns: dict   # {"bytes_written": int, "path": str}
```

**Behavior:** writes `content` to `path`. If `create_parents` is true and
parent directories don't exist, creates them. Overwrites existing files
without warning (the agent's responsibility to check first).

### 4.3 `edit`

```
@tool(
    name="edit",
    description="Edit a file by replacing a specific string with new content.",
    args_schema=EditArgs,
)

class EditArgs(BaseModel):
    path: str
    old_str: str         # exact text to find and replace
    new_str: str         # replacement text
    occurrence: int = 1  # which occurrence to replace (1-indexed)
                         # if 0, replace all occurrences

returns: dict   # {"replacements_made": int, "path": str}
```

**Behavior:** reads the file, finds `old_str`, replaces with `new_str`,
writes the file back. The `occurrence` parameter handles ambiguity:
default 1 means "replace the first occurrence and raise if `old_str`
appears more than once" — wait, that's two different things. Resolved:

- `occurrence: 1` (default) — replace the first occurrence. Raises
  `MultipleMatchesError` if `old_str` appears more than once (the
  agent must disambiguate by providing more context in `old_str`).
- `occurrence: N` (N > 1) — replace the Nth occurrence specifically.
- `occurrence: 0` — replace all occurrences.

The safety property is that `occurrence: 1` (the common case) fails
loudly on ambiguity — when `old_str` matches multiple sites and the
agent didn't pin which one — rather than silently picking the wrong
one.

### 4.4 `bash`

```
@tool(
    name="bash",
    description="Execute a shell command and return its output.",
    args_schema=BashArgs,
)

class BashArgs(BaseModel):
    command: str
    cwd: str | None = None         # working directory
    timeout_seconds: int = 60

returns: dict   # {"stdout": str, "stderr": str, "exit_code": int}
```

**Behavior:** runs `command` via the shell. Captures stdout and stderr.
Returns when the command completes or the timeout expires. On timeout,
the process is killed and `tool.failed` is emitted with cause=timeout.

**Sandboxing:** in v1's `native` sandbox, no restrictions. The user's
machine, the user's shell. Future sandbox variants would restrict
filesystem and network access for the command.

### 4.5 Action names for builtins

Per the Authorization namespace (3-segment catalog refs per
qol-action-namespace-bundle-scoped.md):

- `kolsch/tools/read_file`
- `kolsch/tools/write_file`
- `kolsch/tools/list_files`
- `kolsch/tools/bash`

The default rule set ships permissive entries for the first three
and `prompt` for `bash`. Restrictive rule sets (e.g., "no bash for
any agent") add `{ what: "kolsch/tools/bash", pattern: "*", effect: deny }`.

---

## Part 4.5 — Builtin Tools (Phase 2.0)

*(Phase 2.0, TOOL-R1. Supersedes the Phase 1 sketch in Part 4 for the
concrete shipping set; the patch source of truth is
`process/phase2.0-tools-specs.md` Part 1.)*

Phase 2.0 ships the platform's canonical builtin tool set as plain
`@tool`-decorated Python files in **`bundles/kolsch/tools/`**, not as
opaque "builtins" hidden inside the engine. Users can read each file,
copy them as templates, or swap them. The discovery pathway is the
existing Tools subsystem catalog (Part 6.1) — the same one that
discovers any bundle-supplied declared tool. There is no separate
"builtin registry" baked into the runtime (AGENTS.md hard rule per
TOOL-R10).

The shipping set is four tools: `read_file`, `write_file`, `list_files`,
`bash`. The set is deliberately small and is the foundation the rest
of Phase 2 (interactive chat, workflows) builds on.

### 4.5.1 `read_file`

```python
# bundles/kolsch/tools/read_file.py
@tool
def read_file(path: str, max_bytes: int = 1_048_576) -> str:
    """Read a UTF-8 text file from the workspace.

    :param path: Workspace-relative or absolute path. Absolute paths
        outside ``file_access.allowed_paths`` are rejected.
    :param max_bytes: Refuse to read files larger than this (default
        1 MiB). Protects context budgets.
    :returns: File contents as a UTF-8 string.
    :raises file.access_denied: Path outside allowed scope.
    :raises file.too_large: File exceeds ``max_bytes``.
    :raises file.not_found: Path doesn't exist.
    """
```

Action name: `kolsch/tools/read_file`. The 1 MiB default `max_bytes` is a
context-budget guard; users tune it via the in-chat permission flow
(Part 2 of the patch) or by editing the tool file directly.

### 4.5.2 `write_file`

```python
@tool
def write_file(path: str, content: str, *, create_parents: bool = True) -> dict:
    """Write a UTF-8 text file to the workspace.

    :param path: Workspace-relative or absolute path inside allowed scope.
    :param content: New file contents (full overwrite; not a patch).
    :param create_parents: Create missing parent directories.
    :returns: ``{"path": "<abs>", "bytes_written": N, "created": bool}``.
    :raises file.access_denied: Path outside allowed scope.
    :raises file.permission_denied: Authz returned ``deny`` / ``prompt-denied``.
    """
```

Action name: `kolsch/tools/write_file`. Semantics are **full overwrite**, not
patch-style edit — incremental editing belongs in a later phase when
the model emits reliable diffs. Full-overwrite is honest about what
it does.

### 4.5.3 `list_files`

```python
@tool
def list_files(
    path: str = ".",
    *,
    glob: str = "*",
    max_entries: int = 500,
    include_hidden: bool = False,
) -> list[dict]:
    """List entries under ``path`` (non-recursive by default).

    :param path: Directory to list. Workspace-relative or absolute.
    :param glob: Glob pattern; ``**/*`` for recursive.
    :param max_entries: Cap on returned entries.
    :param include_hidden: Include dotfiles.
    :returns: List of ``{"name", "path", "is_dir", "size_bytes"}`` dicts.
    """
```

Action name: `kolsch/tools/list_files`. Non-recursive by default; recursive
walks need an explicit `glob="**/*"`. The `max_entries` cap is the
same context-budget discipline as `read_file.max_bytes`.

### 4.5.4 `bash`

```python
@tool
def bash(
    command: str,
    *,
    cwd: str | None = None,
    timeout_seconds: int = 60,
) -> dict:
    """Run a shell command in the workspace.

    :param command: Command to execute. Single string; shell parsing
        applies (no auto-quoting of arguments).
    :param cwd: Working directory; defaults to the workspace root.
        Must resolve inside allowed scope.
    :param timeout_seconds: Kill the command after this many seconds.
    :returns: ``{"exit_code", "stdout", "stderr", "duration_seconds",
        "timed_out"}``. ``stdout`` / ``stderr`` truncated to 64 KiB each.
    :raises tool.timeout: Command exceeded ``timeout_seconds``.
    :raises file.access_denied: ``cwd`` outside allowed scope.
    """
```

Action name: `kolsch/tools/bash`. Runs through the existing Sandbox provider
(the `native` variant — subprocess, in-process). Docker sandbox is a
later phase. The `bash` path-policy gap documented in
`docs/explanation/file-handling.md` §4.7 still applies in Phase 2.0:
the path-access policy guards `read_file` / `write_file` / `list_files`
but does NOT constrain commands the shell runs. The workspace-only
default rule set (patch §3.1) defaults `kolsch/tools/bash` to `prompt`
for exactly this reason — the user authorizes each command interactively.

### 4.5.5 Error codes raised by the builtins

| Tool | Codes |
|---|---|
| `read_file` | `file.access_denied`, `file.too_large`, `file.not_found` |
| `write_file` | `file.access_denied`, `file.permission_denied` |
| `list_files` | `file.access_denied`, `file.not_found` |
| `bash` | `tool.timeout`, `file.access_denied` |

All four codes are Recoverable per `docs/explanation/error-model.md`
§4.16 (TOOL-R5). The model sees the error in its tool-result payload
and can react (try a different path, retry with smaller `max_bytes`,
give up).

### 4.5.6 Bundle layout

```
bundles/kolsch/
├── bundle.yaml
├── agents/
│   └── helper.yaml         # gains `tools:` declaration per Phase 2.0
└── tools/
    ├── read_file.py
    ├── write_file.py
    ├── list_files.py
    ├── bash.py
    ├── fetch_url.py        # HTTP GET + main-content extraction
    ├── browse_url.py       # headless Chromium (kolsch[browse] extra)
    └── jev_decide.py       # typed Jev decisions (§4.5.7)
```

The layout is workspace-relative. Its source is the starter bundle
`src/cli/_starter_bundle/kolsch/` in the repo — the only copy (bug-153)
— which `kolsch setup` copies into the workspace.

`bundles/kolsch/` is the canonical bundle name from Phase 2.0 onward.
Phase 1.2 originally scaffolded `bundles/starter/`; Phase 2.3's drift
detection handles the rename — see TOOL-R12 alignment note in
`process/phase1.2-usability-specs.md` §2.4 for the migration
mechanism.

### 4.5.7 `jev_decide` — typed decisions (spec-136)

`bundles/kolsch/tools/jev_decide.py` asks **Jev** (TypeSafe AI's
"System One" decision model) one or more named, typed questions about
a piece of content and returns calibrated answers. It is a plain
`@tool` file like the others — no engine branch.

| | |
|---|---|
| Action name | `kolsch/tools/jev_decide` (default rule: `prompt` — it sends workspace data to a third party) |
| Args | `state` (string / object / array), `questions` (non-empty map of name → `choice` / `score` / `noul` question, each with optional `fail_below`), `model` (default `typesafe/jev-1.14`) |
| Returns | `{model, answers, usage}`; `usage.cost` only when the upstream sends it |
| Secret | `OPENROUTER_API_KEY` via `secrets_needed` — the key `kolsch connect openrouter` writes to `.env`. The tool sets `hide_if_secrets_missing` (§6.1.1): without the key it is hidden and a `tool.unavailable` is recorded, instead of failing the agent |
| Transport | `POST https://openrouter.ai/api/v1/systemone`, behind a module-level profile seam; a direct-TypeSafe profile is defined as data but not selectable in v1 |
| Codes | `tool.invalid_arguments` (bad questions, before any network call), `tool.execution_failed` (`UpstreamHTTP<status>`, `NetworkError`, `MalformedResponse`), `tool.timeout`, `tool.jev_below_threshold`, `secret.not_found` |

429 / 529 responses are retried (3 attempts total, honouring
`retry-after` capped at 10 s, otherwise exponential backoff); every
request and wait races `ctx.cancellation`. A question's `fail_below`
fails the call with `tool.jev_below_threshold` when its metric
(`confidence` for choice, `score` for score, `noul` for noul) is
strictly below the threshold. See
[Use Jev for decisions](../how-to/use-jev.md).

### 4.6 The chat as canonical callback consumer (Phase 2.1, CHAT-R1)

The Phase 2.0 prompt-callback hook (see
`docs/explanation/secrets-sandbox-authz-contract.md` §3.7.2 + the
patch §4.2) is consumed by the `kolsch chat` REPL — that's its
purpose. The chat's modal IS the reference implementation; the
hook's contract was shaped specifically for the four-choice
modal that 2.1 ships (see `process/phase2.1-chat-specs.md`
§4 for the modal + callback wiring).

**Non-chat callers must NOT implement their own modal.** Per
Phase 2.0 patch §2.2 — `kolsch run` (one-shot script),
`kolsch server` (HTTP), tests, and the golden harness all treat
`prompt` as `deny` (collapsed via the no-callback path) and emit
`authz.prompt_unattended` so replay shows what would have been
asked. Building a parallel approval surface in those callers
would fork state: the chat's modal mutates `platform.yaml`
through the authz live-edit API; a second mechanism in the
server would need to coordinate with the first and the file
would become the only consistent source of truth — defeating
the in-memory live-edit guarantee that Phase 2.0's same-exchange
contract depends on. One callback consumer, one rule list, one
discipline.

---

## Part 5 — MCP Server Integration

The platform supports MCP servers as a tool source. MCP is the open
protocol for connecting LLM applications to external context and tools.

### 5.1 MCP server lifecycle

Two modes, configured per-server in the agent's config:

**`lazy` (default)** — the subprocess is NOT started at agent setup.
Instead, the platform registers placeholder tool definitions for the
server's tools (using introspection at first-use, or a manifest the
server provides — see §5.2). The subprocess starts when the model first
invokes one of its tools. Subsequent calls reuse the running subprocess.

**`eager`** — the subprocess starts at agent setup. Its tools are
introspected and registered before any model call. Cost is paid upfront;
all tools are immediately available.

Lazy is the default because most agents won't actually use most
configured MCP servers in any given exchange. Spawning subprocesses
that go unused wastes resources.

### 5.2 Tool discovery from MCP servers

When an MCP server starts, the platform queries it via the MCP protocol
for its tool list. Each tool's name, description, and parameter schema
are returned. The platform registers these with the underlying framework
(CrewAI) as proxied tools — when the model calls one, the proxy forwards
the call to the MCP server over the protocol.

For lazy servers: the platform may have cached the tool list from a
prior run, allowing tool registration without starting the subprocess
yet. If no cache, the subprocess starts at agent setup just long enough
to enumerate tools, then exits (or stays running, implementation choice).

### 5.3 Secret injection for MCP servers

MCP servers run as subprocesses and typically read credentials from
environment variables. The `secrets_needed` + `env_inject` mechanism
handles this:

```yaml
- name: "github"
  command: [...]
  secrets_needed: ["GITHUB_TOKEN"]
  env_inject:
    GITHUB_PERSONAL_ACCESS_TOKEN: "GITHUB_TOKEN"
```

At spawn time:
1. The platform calls `secrets.resolve(name)` for each name in
   `secrets_needed`.
2. For each `env_inject` mapping, the resolved secret value is set in
   the subprocess's environment under the specified env-var-name.
3. The subprocess starts with those env vars set, plus normally-inherited
   env vars (PATH, etc.).

The subprocess never sees the rest of the platform's environment beyond
what the spawn-time configuration provides. Sandboxing of the
environment is the operating system's standard subprocess isolation;
the platform doesn't add more in v1.

### 5.4 MCP tool invocation

When the model calls an MCP server tool:

1. Authorization: check `allow(principal, "mcp.<server>.<tool>")`.
2. If lazy and not running: start the subprocess (per §5.1).
3. The platform forwards the tool call to the server over the MCP
   protocol with the resolved arguments.
4. The server processes the call and returns the result.
5. The platform receives the result, emits `tool.result` per the
   standard tool result flow.

The tool's content shows up in events exactly as for in-process tools.
The fact that it ran in a subprocess is invisible to the model.

### 5.5 MCP server failure handling

If the server subprocess crashes or fails to start, the platform emits
`tool.failed` with cause indicating the server failure. For lazy
servers, a failed start might be retried on subsequent invocations
(implementation choice). For eager servers, a failed start at agent
setup is a hard error — the agent cannot be set up.

---

## Part 6 — Tool Registration and Invocation Flow

The flow from agent setup through tool invocation, end-to-end.

### 6.1 At agent setup (per Agent Engine §2.2)

The Tools subsystem participates in the engine's pre-flight:

1. **Discover available tools.** Scan known tool directories for
   `@tool`-decorated functions. Build a registry of discovered tools.
2. **Validate the agent's tool config.** Per §3.4, ensure all requested
   tools exist.
3. **Resolve secrets.** For each declared tool with `secrets_needed`,
   call `secrets.exists()` to verify availability. Don't resolve values
   yet (they're resolved at invocation time).
4. **Start eager MCP servers.** For each MCP server with
   `lifecycle: eager`, spawn the subprocess with injected env vars.
5. **Register all tools with the framework.** The platform
   wraps each tool in a framework-compatible adapter that:
   - Performs the authorization check before the tool's actual function.
   - Resolves secrets and constructs the `ToolContext`.
   - Routes the actual execution through the Sandbox provider.
   - Captures the result for event emission.

After setup, the framework has a list of available tools and can be
handed to the agent loop. The agent's tool use is then native to the
framework, with the platform's adapters doing the cross-cutting work
(authorization, secrets, sandbox, observation) invisibly.

### 6.1.1 Optional tools: `hide_if_secrets_missing` (feat-139)

Registration verifies each tool's `secrets_needed` with
`SecretsProvider.exists`. A missing secret raises `ToolSetupFailed`
(Fatal) and the exchange fails — the default, so a misconfigured
tool is never silently absent.

A tool decorated `@tool(hide_if_secrets_missing=True)` opts out of
that: when any of its secrets is missing, `register_for_agent` leaves
it out of the agent's registered tools (dropping any copy from an
earlier registration) and records
`UnavailableTool{tool_name, missing_secrets, recovery_hint}`,
readable through `ToolsSubsystem.list_unavailable_tools(agent_id)`.
Consequences:

- The model's tool list and the prompt `tools` variable are built
  from the registered tools, so neither mentions the hidden tool.
- The engine writes a durable `tool.unavailable` event (same
  payload) during exchange setup, once per session per tool,
  deduplicated against the session's event log (one scan of all its
  threads, cached per process, serialised per session) so neither a
  second thread nor a restart repeats it. The engine reads its
  registration's hidden list synchronously right after
  `register_for_agent` returns, because the per-agent registration is
  shared by every session of that agent.
- `recovery_hint` is the tool's `unavailable_hint`, or a derived
  "Set <NAMES> in the workspace .env." Secret values never appear.
- The chat renderer ignores the event for now.

Both intake channels (programmatic `tools=` and `declared_paths=`)
apply the flag; builtins carry no secrets.

### 6.2 At tool invocation

When the model decides to call a tool, the framework dispatches it.
The platform's adapter for that tool runs:

1. **Authorization check.** `allow(principal, action_name)`. If denied,
   raise `ToolNotAuthorized`; the framework receives an error.
2. **Validate arguments.** The tool's args are validated against its
   `args_schema` (Pydantic). Invalid args raise; the framework receives
   a clean validation error.
3. **Construct ToolContext.** Resolve each declared secret via
   `secrets.resolve(name)`. Build `ToolContext(secrets=..., session_id=..., agent_id=...)`.
4. **Sandbox execution.** Call `sandbox.execute(work=tool_fn(args, ctx), policy=...)`.
   The native sandbox just runs it.
5. **Receive result.** The work returns or raises.
6. **Return to framework.** The framework's tool-call cycle continues
   with the result or error.

The engine, observing the framework's lifecycle (per Agent Engine §2.4),
emits `tool.called` before invocation and `tool.result` / `tool.failed`
after. The tool function itself never touches the event log.

### 6.3 Authorization timing

The authorization check happens *inside* the adapter, just before the
actual tool execution. This is the only place it could happen — the
framework's tool-call request hasn't been validated yet, and after
execution it's too late.

If denied, the tool function never runs. The framework receives an
error and reports it to the model as a tool failure.

### 6.4 Large tool results

A tool's return value can be arbitrarily large (a `read` of a huge file,
a `bash` command that prints lots of output). The Materialization Rule
requires verbatim capture in events.

The mechanism:
- If the serialized return value is **≤ 50KB**: capture inline in
  `tool.result.data.result`.
- If **> 50KB**: store the full content in a sidecar file
  (`<session>.content/<hash>.bin` or similar — managed by the
  Session/Run Store's content area, to be detailed in that contract's
  revision). The event's `result` field contains a truncated inline
  preview plus a `content_ref` pointing at the full sidecar content
  by hash.

This is the platform's general "large content" mechanism, used here
and elsewhere (memory items with large content, file_ref handling).

A future content store could swap to object storage; v1 uses filesystem.

### 6.5 Tool failure modes

- **Authorization denied:** `tool.failed` with `error.type: "unauthorized"`.
- **Argument validation:** `tool.failed` with `error.type: "invalid_arguments"`,
  details from Pydantic.
- **Secrets unavailable:** caught at registration time, not invocation.
  Tool isn't registered if secrets missing.
- **Tool function raised:** `tool.failed` with `error.type: <exception class>`,
  message and traceback.
- **Timeout:** `tool.failed` with `error.type: "timeout"`, the configured
  timeout value.
- **Sandbox refused:** `tool.failed` with `error.type: "sandbox_denied"`,
  details from the sandbox.
- **MCP server unreachable:** `tool.failed` with `error.type: "mcp_server_error"`,
  the underlying transport error.

---

## Part 7 — Conformance Invariants

What any Tools subsystem implementation must honor.

### T-I1 — Tool action namespace

Every tool MUST have an action name. Default is the 3-segment catalog
ref `<home_bundle>/tools/<name>` for declared and builtin tools (per
qol-action-namespace-bundle-scoped.md — the `home_bundle` is stamped
on the `ToolDefinition` at registration time by the engine);
`mcp.<server>.<tool>` for MCP tools. Authorization MUST be checked
against this action name before every invocation.

### T-I2 — Argument validation before invocation

A tool's args MUST be validated against `args_schema` before the tool
function runs. Invalid args produce `tool.failed`, not partially-applied
calls.

### T-I3 — Secrets in ToolContext only

Resolved secrets MUST be provided to the tool function via the
`ToolContext`. Tools MUST NOT call `secrets.resolve()` themselves; the
platform mediates. This is what makes the "secrets never appear in
events" invariant enforceable — the platform controls what reaches
events, and tools cannot bypass it.

### T-I4 — Sandbox routing

Every tool invocation MUST be routed through the Sandbox provider's
`execute()`. The native sandbox is a passthrough; the routing is the
point. Implementations MUST NOT skip the sandbox call even when it's
a passthrough.

### T-I5 — Verbatim result capture

The tool's return value MUST be captured verbatim in `tool.result.data.result`
(subject to the large-content sidecar mechanism §6.4). No truncation,
no summarization, no normalization at this layer.

### T-I6 — MCP env injection

For MCP server subprocesses, secrets MUST be injected as environment
variables per the `env_inject` mapping. The subprocess MUST NOT receive
the platform's full environment or other secrets it didn't declare.

### T-I7 — Discovery is read-only

Tool discovery MUST be a read-only scan of declared directories. It
MUST NOT execute discovered tools, MUST NOT import modules with side
effects, MUST NOT modify the filesystem. (The `@tool` decorator's
work is done at import time; discovery imports modules to find
decorated functions — but the decorator itself MUST be side-effect-free
beyond registration.)

### T-I8 — Lazy MCP correctness

A lazy MCP server's tools MUST be registered at agent setup, even
though the subprocess hasn't started. The model sees them as available.
First invocation triggers the subprocess start. The latency is on the
first call, not on every call.

### T-I9 — Async tool functions

All tool functions are `async`. Synchronous-looking tools (file I/O,
shell commands) are still declared async; their implementation uses
async wrappers around blocking calls. This is consistent with the rest
of the platform's async-first stance.

### T-I10 — One tool per name within an agent

An agent's resolved config MUST NOT contain two tools with the same
`name`. Builtin/declared/MCP namespaces are flat from the model's
perspective. Conflicts at config resolution are Layer 3 errors.

---

## Part 7.5 — Permission API for tool authors (Phase 2.2, ENV-R5)

Phase 2.2 adds a small module — `kolsch.tools.permissions` —
that lets tool functions query the composed authz rule set
programmatically from inside a `@tool` body.

```python
permissions.check(what: str) -> AuthzEffect
permissions.require(what: str) -> None
```

`what` is the action OR target pattern (e.g.
`"kolsch/tools/bash"` for the 3-segment action, or the resolved
path the tool is about to touch). The principal + caller-chain
come from the tool dispatcher's binding — tool authors don't
need to (and can't) override them from inside the body.

**Behaviour:**

- Uses the SAME composed rules as engine pre-dispatch: walks
  agent permissions (§3.8) → platform rules → default-deny.
- Returns `DENY` when no rule matches (default-deny tail).
- Treats `PROMPT` as `DENY` for `require` in non-chat context
  (mid-tool prompts are deferred per Phase 2.2 §12 item 1).

**Implementation:** the tool dispatcher (`src/tools/dispatch.py`)
constructs a `PermissionsBinding` with the authz provider, the
agent's permissions, the bundle's permissions, the principal, and
the tool name, then binds it to a module-level `ContextVar`
immediately before invoking the tool function. The `bind` /
`reset` pair lives inside the sandbox's `_work` closure so the
binding lifetime exactly matches the tool body. Out-of-context
calls (a unit test that imports a tool directly without going
through the dispatcher) raise `tool.permissions_no_context`
(Fatal).

**Worked example — a fine-grained capability check:**

```python
from pydantic import BaseModel

from tools import permissions
from tools.decorator import tool, ToolContext


class GitForcePushArgs(BaseModel):
    branch: str


@tool(
    name="git_force_push",
    description="Force-push a local branch to origin.",
    args_schema=GitForcePushArgs,
)
async def git_force_push(args: GitForcePushArgs, ctx: ToolContext) -> str:
    # Dispatch-boundary authz already cleared us to RUN this tool
    # (someone wrote `what: kolsch/tools/git_force_push, effect: allow`).
    # The body now asks a finer question — does this caller hold the
    # `workspace.git.force_push` capability? If not, fail loud.
    await permissions.require("agent", "workspace.git.force_push")
    # …actually run `git push --force` here…
    return f"force-pushed {args.branch}"
```

To grant the capability, the rule author writes a rule with the
same `what` string the tool body passes:

```yaml
permissions:
  - { what: workspace.git.force_push, pattern: "*", effect: allow }
```

The check walks the same four-level stack the dispatch-boundary
check uses (agent → bundle → platform → default-deny per
qol-tool-permission-discovery-uniform-with-stacking.md). A
PROMPT-effect match here always collapses to deny — the in-body
check does NOT trigger the no-rule modal (mid-execution prompts
would block the tool's coroutine; per
qol-tool-permissions-binding-unused.md Step 1 design pick Q3).
Use `check` instead of `require` when the tool wants to branch
on the effect rather than raise.

---

## Part 8 — Open Items

1. **Tool versioning.** A tool's definition may evolve over time;
   different bundles may ship different versions of a tool with the
   same name. v1 has no versioning — the discovered tool is the tool,
   first match wins (or conflict error). Future bundle-dependency
   features may need versioning.

2. **Hot reload of tools.** v1 discovers tools at platform startup;
   editing a tool's code requires restart. A development-mode hot-reload
   would be ergonomic but is deferred. The platform restart cost in
   local-first is acceptable for v1.

3. **The content store mechanism (§6.4).** "A sidecar file managed by
   the Session/Run Store's content area" — this needs detailing in the
   Session/Run Store contract revision and possibly the
   file-handling/`file_ref` spec. Flagged as a known cross-contract
   dependency.

4. **Tool composition.** A tool that wants to call other tools (a
   "supervisor tool") doesn't have a clean mechanism. The
   `ToolContext` doesn't expose tool dispatch. This is intentional in
   v1 — tool composition is the workflow engine's job (Phase 2). Worth
   noting so it's not added accidentally.

5. **Tool input streaming.** Tools that produce streaming output
   (a long-running command with incremental stdout) currently
   buffer-and-return. Streaming would require an extension to the
   tool function's return type. Deferred to Phase 2 or when a real
   use case appears.

6. **Per-agent tool overrides.** If two agents in different
   configurations want the same tool with different timeout settings,
   the current shape doesn't accommodate. (The decorator's timeout is
   the tool's definition, not per-use.) Future addition: per-config
   tool parameter overrides.

7. **Built-in tool extension via skills.** Skills (Agent Skills standard)
   can effectively define tools through scripts. The relationship
   between skills' invokable scripts and the `@tool`-decorated tool
   mechanism is unclear. Handled in the Skills subsystem spec (next).

8. **The standard library beyond v1's four tools.** `grep`, `find`,
   `ls` and similar filesystem-navigation tools are NOT in v1's
   builtin set per the v1 scope. They could be built as declared
   tools and shipped in a `core` bundle. Whether to do this in v1 or
   defer is a small
   open question (the answer is probably "defer; users can declare
   them locally if needed").

---

## Part 6 — External-harness invocation: the observed core and MCP bridge

Tool invocation has **one core with two callers**. Both the in-engine
dispatch and the MCP tool bridge route through
`tools.observed.invoke_observed`, which returns an `Ok` / `Denied` /
`Failed` outcome. A tool called by an external harness (a
`SubprocessAgentFramework` agent such as Claude Code) through
`kolsch mcp bridge` (`tools.bridge.ToolBridgeServer`) traverses the
*identical* Authorization → Secrets → Sandbox → emit path as one called
from inside the engine. The bridge is a transport, not a bypass: it
sanitizes denials (dropping `recovery_hint`) before they leave the
process, and the subsystem's invariants — dispatch owns the event
sequence, secrets resolve in dispatch, only dispatch writes tool events —
hold on both paths.
