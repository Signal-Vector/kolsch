# Write a tool

Tools are the Python functions agents call to do work in the world.
The 14 built-in workflow node types cover the workflow layer; tools
cover what the model itself can invoke inside an exchange. This guide
shows you how to author one.

A tool is a `@tool`-decorated async coroutine in a bundle at
`bundles/<bundle>/tools/<name>.py`. When the LLM emits a tool-use
request, the Agent Engine hands it to the Tools subsystem, which
runs the dispatch flow: validate arguments against your Pydantic
schema, run the authz check, resolve any secrets the tool needs,
wrap the call in the sandbox provider, run your body, and write the
`tool.result` (or `tool.failed`) event.

For the conceptual overview, see
[Tools](../architecture/tools.md). For the parallel node authoring
guide, see [Write a workflow node](write-a-workflow-node.md).

## The `@tool` decorator

```python
from tools.decorator import tool, ToolContext
```

The decorator takes seven keyword-only arguments:

- `name` — the wire name. Must match `^[a-z][a-z0-9_]*$`. The LLM
  sees this in its tools list.
- `description` — long-form. Sent verbatim as the LLM API's
  `tools[].description`.
- `args_schema` — a `pydantic.BaseModel` subclass for the tool's
  arguments. Required. For a zero-argument tool, declare an empty
  `BaseModel`.
- `secrets_needed` — a list of secret names the dispatch should
  resolve into `ctx.secrets` before your body runs. Values come from
  the workspace's `.env`.
- `action` — override the authz action string. When omitted,
  the dispatch uses `<bundle>/tools/<name>` (the 3-segment ref).
- `timeout_seconds` — optional per-call timeout.
- `summary` — a one-line description for the prompt's compact tool
  listing. Falls back to the first sentence of `description`.

The decorator stamps a `ToolDefinition` dataclass onto the function
as `_tool_definition` and returns the function unchanged. Discovery
walks bundle files at startup and picks up any function carrying
that sentinel.

## The function contract

An `async def` coroutine with exactly two positional parameters:

```python
async def my_tool(args: MyArgs, ctx: ToolContext) -> <anything>:
    ...
```

`args` is the already-validated Pydantic instance — dispatch calls
`args_schema.model_validate(...)` before your function ever runs.
Return anything JSON-serialisable; the dispatch flow captures it
verbatim as `tool.result.data.result`.

## `ToolContext` fields

The context is a frozen Pydantic model. What your body can read:

- `agent_id`, `session_id`, `thread_id` — the run identifiers.
- `principal` — the authz principal the check ran under.
- `secrets` — the resolved secrets dict, populated with the keys you
  declared in `secrets_needed`.
- `workspace_root` — the workspace root path. `None` when the tool
  runs outside an engine dispatch (tests without a workspace).
- `path_policy` — the file-access policy. Same nullability rules as
  `workspace_root`. Tools that touch files should check both are
  non-None and raise `ToolExecutionFailed` if not.
- `tool_call_id` — the per-call id. Useful for naming per-call
  artifacts (bash uses it for stdout overflow files).
- `cancellation` — the exchange's `CancelToken`. Poll with
  `await ctx.cancellation.wait_cancelled()` to race long work.

## `args_schema`

A `pydantic.BaseModel` subclass with `Field(...)` descriptions on
each argument. The field descriptions surface in the LLM's
JSON-schema view of the tool, so the model can pick the right
arguments without prompt-engineering.

```python
from pydantic import BaseModel, Field

class MyArgs(BaseModel):
    path: str = Field(description="Path to the file to read.")
    max_bytes: int = Field(default=1_000_000, ge=1)
```

A `pydantic.ValidationError` at dispatch surfaces as
`ToolInvalidArguments` — a Diagnostic error the model sees on
`tool.failed` and can retry.

## Where the file lives

```
bundles/<bundle>/tools/<name>.py
```

The workflow YAML addresses the tool as `<bundle>/tools/<name>` —
the same 3-segment ref format as skills, agents, and prompts. No
manifest entry needed for the tool file itself; the catalog scanner
finds every `.py` under `tools/` at bootstrap.

Multiple `@tool` functions per file are fine — the discoverer
returns every module attribute carrying `_tool_definition`.

## A minimal working example

From the starter bundle at
`bundles/kolsch/tools/list_files.py`, trimmed for the essentials:

```python
from pathlib import Path

from pydantic import BaseModel, Field

from errors import FileNotFound, ToolExecutionFailed
from files import check_path_allowed
from tools.decorator import ToolContext, tool


class ListFilesArgs(BaseModel):
    path: str = Field(default=".", description="Directory to list.")
    glob: str = Field(default="*", description="Glob pattern.")
    max_entries: int = Field(default=500, ge=1)
    include_hidden: bool = Field(default=False)


@tool(
    name="list_files",
    summary="List entries under a workspace directory.",
    description="List the entries (files + subdirectories) at a workspace path.",
    args_schema=ListFilesArgs,
)
async def list_files(args: ListFilesArgs, ctx: ToolContext) -> list[dict]:
    if ctx.path_policy is None or ctx.workspace_root is None:
        raise ToolExecutionFailed(
            tool_name="list_files",
            error_class="RuntimeError",
        )
    check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)
    target = (
        ctx.workspace_root / args.path
        if not Path(args.path).is_absolute()
        else Path(args.path)
    )
    if not target.exists():
        raise FileNotFound(path=args.path)
    return [
        {"name": c.name, "path": str(c), "is_dir": c.is_dir()}
        for c in target.glob(args.glob)
    ]
```

## Declaring the tool on an agent

Writing the file makes it discoverable, not automatically available.
An agent YAML declares which tools it wants under
`subsystems.tools.config.declared`:

```yaml
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/tools/list_files
        - ref: kolsch/tools/bash
```

Each `ref` is the 3-segment catalog ref. The engine resolves each
ref to a file path at agent load time and binds the tool for the
agent's session. Only tools listed here are available to the model.

## A worked example — add a `word_count` tool

Say you want an agent that counts words in text. Three steps:

**1. Write the tool.** Create
`bundles/mybundle/tools/word_count.py`:

```python
from pydantic import BaseModel, Field
from tools.decorator import ToolContext, tool


class WordCountArgs(BaseModel):
    text: str = Field(description="Text to count words in.")


@tool(
    name="word_count",
    description="Count words in a string.",
    args_schema=WordCountArgs,
)
async def word_count(args: WordCountArgs, ctx: ToolContext) -> dict:
    _ = ctx
    return {"words": len(args.text.split())}
```

**2. Declare it on the agent.** Edit
`bundles/mybundle/agents/writer.yaml`:

```yaml
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: mybundle/tools/word_count
```

**3. Grant permission.** In `platform.yaml` or the agent's
`permissions:` block:

```yaml
permissions:
  - what: mybundle/tools/word_count
    pattern: "*"
    effect: allow
```

Without an `allow` rule, the tool falls through to the default-deny
tail.

Restart, then chat with `mybundle/writer`. The model sees
`word_count` in its tool list, invokes it, and the dispatch flow
records the `tool.called` → `tool.result` pair in the JSONL log.

## Common failure modes

- **`args_schema` mismatch.** A `pydantic.ValidationError` at
  dispatch surfaces as `ToolInvalidArguments` (Diagnostic). The
  model sees it on `tool.failed` and can retry with better
  arguments.
- **Authz denies.** `ToolUnauthorized` (Diagnostic). The engine
  attaches a `recovery_hint` naming the `/perm allow ...` chat
  command the user can type to unblock.
- **Missing secret at registration.** `SecretNotFound` wraps as
  `ToolSetupFailed`. The agent fails to load — fix the missing
  secret in `.env` and retry.
- **Body raises non-`PlatformError`.** Any other exception is
  wrapped in `ToolExecutionFailed` with the original attached via
  `__cause__`. The event log carries the wrapped error; the original
  is available for debugging.
- **Invalid tool name.** A `name` that doesn't match
  `^[a-z][a-z0-9_]*$` raises `ValueError` at import time.

## Testing

Tools are plain functions. Test them like plain functions —
construct a `ToolContext` with test values and call the coroutine
directly. `kolsch/tools/introspect_node`'s bundle-tool sibling
doesn't exist for tools yet; a `kolsch tools show <ref>` command is
planned.

---

The class reference is at [API: Tools](../api/index.md#tools). For
the full contract — dispatch flow, event shapes, permission
matching — see the
[Tools architecture page](../architecture/tools.md).
