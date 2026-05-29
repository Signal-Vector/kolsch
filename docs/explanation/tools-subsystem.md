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
- **Built-in tools**: `read`, `write`, `edit`, `bash`. Four tools, full spec
  in Part 4.
- **MCP lifecycle**: `lazy` by default; `eager` opt-in.
- **Large tool results**: truncate inline at ~50KB; store full content in
  a sidecar file referenced by content-hash for events that need it.
- **Tool invocation context** contains `secrets`, `session_id`, `agent_id`.
  Nothing else. Authorization happens before invocation; tools don't write
  to the log.
- Tool actions are named **`tool.<name>`** (builtin/declared) or
  **`mcp.<server>.<tool>`** (MCP) in the Authorization namespace.

---

## Part 1 — What the Subsystem Is

The Tools subsystem is the agent-level configuration of available actuators:
the things an agent can *do* in the world beyond just generating text. Tools
include reading and writing files, running commands, querying APIs,
invoking other systems via MCP, and anything else the platform or user-installed
bundles make available.

### 1.1 The three tool sources

**Builtin tools** ship with the platform. v1 has four: `read`, `write`,
`edit`, `bash`. They are available to any agent that lists them.

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
  (which then registers them with the underlying framework, CrewAI in
  v1).
- The invocation flow that calls a tool when the model requests it,
  routing through Authorization, Secrets, and Sandbox.
- The MCP server lifecycle (spawn, ready-check, tool listing).
- The action namespace (`tool.<name>` / `mcp.<server>.<tool>`).

### 1.3 What the subsystem doesn't own

- Tool *execution* mechanics — the underlying framework (CrewAI) does
  the actual function call; this subsystem prepares and observes.
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
    args_schema       BaseModel?    optional; if absent, tool takes no args
    secrets_needed    [str]         optional; default []
    returns_schema    BaseModel?    optional; for tools returning structured data
    action            str?          optional; overrides default action name
                                    (default: f"tool.{name}")
    timeout_seconds   int?          optional; tool-specific timeout override
)
```

**`name`** — the tool's identifier. Used in the action namespace, in
references, in the model's tool-call requests. Must match `[a-z][a-z0-9_]*`.

**`description`** — what the tool does. This text goes to the LLM as part
of the tool definition. Quality of description directly affects whether
the LLM uses the tool correctly. Tool authors are responsible for writing
clear, trigger-rich descriptions.

**`args_schema`** — the Pydantic model describing the tool's parameters.
The platform exports this as JSON Schema for the LLM. Optional for tools
that take no arguments.

**`secrets_needed`** — list of secret names the tool needs. The platform
validates these exist at registration time (via `secrets.exists()`),
resolves them at invocation time, and passes them via `ctx.secrets`.

**`returns_schema`** — optional Pydantic model describing structured
return value. Used by the platform for validation and for showing the
LLM what structure to expect. Tools returning unstructured text omit this.

**`action`** — overrides the default action name. Almost never needed;
exists for tools that legitimately want to live in a different namespace
(e.g., a sandboxed tool that should be authorized as `system.X`). Default
is `tool.<name>`.

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
      builtin: ["read", "write", "edit", "bash"]
      declared:
        - ref: "local/tools/web-search"
        - ref: "acme-research/tools/scholarly-search"
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
`<package>/tools/<tool-name>`. Reserved packages:
- `local` — tools defined in the user's own (un-packaged) `tools/`
  directory.
- `core` — system tools (reserved; v1 has none beyond the builtins).

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

This is closer to pi's `edit` semantics. The safety property is that
`occurrence: 1` (the common case) fails loudly on ambiguity rather than
silently picking wrong.

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

Per the Authorization namespace:
- `tool.read`
- `tool.write`
- `tool.edit`
- `tool.bash`

The v1 default allow-all rule covers all of them. Restrictive rule sets
(e.g., "no bash for any agent") would add `who: ".*", what: "tool\\.bash", effect: deny`.

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
5. **Register all tools with the framework** (CrewAI). The platform
   wraps each tool in a framework-compatible adapter that:
   - Performs the authorization check before the tool's actual function.
   - Resolves secrets and constructs the `ToolContext`.
   - Routes the actual execution through the Sandbox provider.
   - Captures the result for event emission.

After setup, the framework has a list of available tools and can be
handed to the agent loop. The agent's tool use is then native to the
framework, with the platform's adapters doing the cross-cutting work
(authorization, secrets, sandbox, observation) invisibly.

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

Every tool MUST have an action name. Default is `tool.<name>` for
declared and builtin tools, `mcp.<server>.<tool>` for MCP tools.
Authorization MUST be checked against this action name before every
invocation.

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
   `ls` and similar pi-typical tools are NOT in v1's builtin set per
   the v1 scope. They could be built as declared tools and shipped in
   a `core` bundle. Whether to do this in v1 or defer is a small
   open question (the answer is probably "defer; users can declare
   them locally if needed").
