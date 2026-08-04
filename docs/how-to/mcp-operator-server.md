# MCP operator server (`kolsch mcp serve`)

`kolsch mcp serve` exposes a small, read-only slice of the kolsch
runtime over the [Model Context Protocol](https://modelcontextprotocol.io/)
as a stateless **stdio server**. Pair it with a headless [Claude
Code](https://docs.claude.com/en/docs/claude-code/overview) — under a
Pro or Max subscription — to let an external agent list your sessions
without booting a workflow.

Two read-only tools ship today (spec-105 shipped the first;
spec-111 added the second):

- **`list_sessions`** — mirrors `GET /sessions`. Enumerates every
  session in the local workspace, with optional `starred` / `archived`
  / `limit` filters. Returns the 7-field session record.
- **`read_session_events`** — mirrors
  `GET /sessions/{session_id}/events`. Reads the JSONL event log for
  one session. Composes with `list_sessions`: pick a session id from
  the first tool, page events with the second.

Write/execute tools land in Phase 2 alongside a principal rework.

## Why this exists

Two MCP surfaces exist in kolsch:

- **Surface A — tool bridge** (in-workflow, session-scoped): lets an
  external harness run *as a kolsch agent inside a workflow* and call the
  running session's per-agent tools. Shipped as `kolsch mcp bridge`
  (spec-120/121).
- **Surface B — operator server** (this page): lets an *external* agent
  orchestrate/observe kolsch over stdio. Low cost, additive, no engine
  change.

## Prerequisites

- A logged-in `claude` binary (`claude auth login`, subscription auth
  — no `ANTHROPIC_API_KEY` in the child env).
- An initialised workspace (`kolsch setup --non-interactive` or the
  interactive flow).
- **`kolsch` on your PATH** — the `[project.scripts]` entry installs a
  `kolsch` console script into your venv's `bin/` directory. Verify
  with `which kolsch`; if it returns nothing, either activate the venv
  (`source .venv/bin/activate`) or use one of the alternative
  invocations below.

## The `mcp-config.json`

The default recipe assumes `kolsch` is on PATH (see Prerequisites):

```json
{
  "mcpServers": {
    "kolsch": {
      "command": "kolsch",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

The [committed
`mcp-config.json`](mcp-config.json){:target="_blank"} alongside this
page is byte-for-byte the same file.

### Alternative invocations (when `kolsch` isn't on PATH)

If you're running `claude` outside an activated venv, replace the
`command`/`args` pair with one of these — pick the one that matches
how you have kolsch installed:

- **`uv`-managed venv, no activation:** `"command": "uv"`, `"args":
  ["run", "kolsch", "mcp", "serve"]`. Requires that Claude's cwd
  contains the `uv` project (i.e. Claude invokes it from your
  workspace root).
- **Explicit venv path:** `"command": "/absolute/path/to/kolsch/.venv/bin/kolsch"`,
  `"args": ["mcp", "serve"]`. Most portable — works from any cwd, no
  environment dependencies.
- **System-installed via `uv tool install kolsch`:** `"command":
  "kolsch"` is on PATH globally; the default config works as-is.

If Claude Code reports the server as `"status": "failed"` in its init
frame, this is the first thing to check.

## Run the round-trip

### Enumerate sessions with `list_sessions`

```bash
claude -p "list my kolsch sessions" \
  --output-format stream-json --verbose \
  --mcp-config ./mcp-config.json \
  --allowedTools "mcp__kolsch__list_sessions,mcp__kolsch__read_session_events"
```

You should see:

1. A `mcp__kolsch__list_sessions` **tool-invocation frame** in the
   `--output-format stream-json` output.
2. A **tool-result frame** carrying JSON session data — one block
   per session (each block is a stringified dict with `session_id`,
   `agent_id`, `name`, `starred`, `archived`, `created_at`,
   `updated_at`).
3. The assistant's final message referencing your sessions by name
   or id.

### Read events from one session with `read_session_events`

Once you have a session id (from `list_sessions`, or from your own
records), ask the operator to summarise the JSONL log:

```bash
claude -p "read the last 5 events from session ses_<uuid>" \
  --output-format stream-json --verbose \
  --mcp-config ./mcp-config.json \
  --allowedTools "mcp__kolsch__list_sessions,mcp__kolsch__read_session_events"
```

You should see:

1. A `mcp__kolsch__read_session_events` **tool-invocation frame** —
   the LLM synthesises call arguments (`session_id`, and typically
   `tail=5` for the example prompt above).
2. A **tool-result frame** with one JSON block per event; each block
   is the full `Event` envelope (`event_id`, `parent_event_id`,
   `thread_id`, `session_id`, `type`, `data`, `actor`, `created_at`,
   `content_ref`, `trace_id`).
3. The assistant's final message summarising the returned events.

The tool takes several optional knobs on top of `session_id`:

- `after_event_id=<event_id>` — resume after a given event (page a
  long session by chaining calls; use the last returned block's
  `event_id`).
- `tail=<N>` — restrict to the last N events on the log.
- `limit=<N>` — cap the response size.
- `event_types=["tool.failed", "agent.message"]` — allowlist by
  event `type`.
- `include_ui_events=True` — include `ui.event` records (defaulted
  off because these dominate agent-debugging logs; flip it on when
  you're asking chat-UX questions like "what did the user see?").

## Permissions posture

**Two composing gates.**

- **Client side (Claude Code):** headless callers cannot prompt for
  approval, so tools are **pre-authorised least-privilege**. The
  shipped recipe uses `--allowedTools
  "mcp__kolsch__list_sessions,mcp__kolsch__read_session_events"` and
  does not use the broader "bypass all permissions" mode Claude Code
  exposes.
- **Server side (kolsch):** Phase-1 reads match the existing local
  HTTP-API posture (`GET /sessions` has no middleware / auth / CORS on
  its request path either — see `src/api/routes.py:135`). MCP-over-stdio
  binds no port at all, so it is *narrower* than the HTTP localhost
  listener: the operator server introduces no new trust boundary for
  read-only local calls.

**Transport = the local trust boundary.** stdio subprocess only: no
network listener, no bound socket, no token. "Who can call" is
"whoever spawned the subprocess" = the local user. The MCP server
opens no port; the test suite asserts this.

**Single-principal boundary — documented, not fixed.** kolsch
authorises against a hardcoded principal
(`principal = "user.local"` at `src/engine/engine.py`; `agent.{id}`
in observers). An external caller has no distinct identity yet, so
there is no privilege separation. This is why the Phase-1 surface is
**localhost / local-user + read-only** and why write tools are
blocked on plumbing a real principal.

**Data surface — narrow (`list_sessions`) vs. wide
(`read_session_events`).** `list_sessions` exposes only the
**Session entity record** (`session_id`, `agent_id`, `name`,
`starred`, `archived`, `created_at`, `updated_at`;
`from_workflow_run` dropped) — no `inputs`, no bindings, no JSONL,
no secrets. The only user-authored field is `name`.

`read_session_events` is **materially wider**: it returns the full
:class:`Event` envelope for every matching event, including
`data: dict[str, Any]`. Depending on event type, `data` can carry
`inputs.*`, tool arguments, tool results, LLM prompts, config
fragments, and other workflow-authored content. This is **not** a
new leak on top of `GET /sessions/{session_id}/events` — the HTTP
surface already exposes the same content on localhost — but it IS
a wider surface than `list_sessions` was, and an operator
integration decision worth flagging.

Two consequences to keep in mind:

- **Unbounded payload.** `Event.data` has no size ceiling.
  `MODEL_REQUEST` / `MODEL_RESPONSE` / `TOOL_RESULT` events can
  carry multi-KB bodies; a chatty session with
  `include_ui_events=True` can ship megabytes of `TextContent`
  blocks over stdio. Use `event_types`, `tail`, or `limit` to
  bound response size before the caller's context blows.
- **`content_ref` is returned as a pointer, not the blob.** If a
  payload was offloaded to `content_ref`, callers see the
  pointer (`{"hash": ..., "size": ..., "stored_at": ...}`) — the
  blob body is not fetched. A `read_content_ref` tool is a
  possible follow-up if needed; not shipped in v1.

**bug-102 relationship.** bug-102 (CANCELLED — not solvable via
input redaction; the fix is `secrets_needed` on the tool
definition) concerns workflow config previews / `inputs.*` / JSONL
leaking secrets on the *data plane*. `list_sessions` does not
touch the bug-102 surface at all. `read_session_events` inherits
whatever secrets are (or aren't) already scrubbed from the JSONL:
tools that use `secrets_needed` correctly won't leak secrets via
arguments here; tools that don't will leak here the same way they
leak via the HTTP surface and the chat UI. That's not a regression;
it's the same coupling the HTTP surface already ships with.

**Future direction (recorded, not built):**

- **Stricter-of gating** — a session grant may narrow but never
  widen kolsch's authz. The protected `rl_default_deny` tail already
  gives the non-loosening floor.
- **Interactive approval on a side channel** — OpenClaw's
  `permissions_respond`; for kolsch, the chat UI resolving a
  `prompt`-effect tool with the approval pinned to the canonical
  `{tool, args, session}` so it can't be mutated post-approval.

Both belong to the **Surface A** (tool-bridge) path.

**No secrets in tool arguments.** Even though `list_sessions` takes
no secret input, the tool description carries the standing kolsch
convention that secrets never go through tool arguments — they route
through `secrets_needed` on the tool definition. This convention is
in place before write tools arrive.

## Troubleshooting

**"MCP tool not found" / no `mcp__kolsch__list_sessions` (or
`mcp__kolsch__read_session_events`) in the frame stream.** The
`--allowedTools` name is `mcp__<server-name>__<tool-name>` where
`<server-name>` is the key in `mcpServers`. Our config uses
`"kolsch"`, so the flag values are `mcp__kolsch__list_sessions` and
`mcp__kolsch__read_session_events`. Pass them as a
comma-separated list — Claude Code accepts multiple tool names in
a single `--allowedTools` argument.

**"claude asks for approval / hangs."** You passed `-p` (print mode)
but omitted `--allowedTools`. Headless mode cannot prompt; without
the allowlist the tool never authorises.

**"JSON-RPC framing error / weird output".** kolsch's stdout is
guarded so a stray `print(...)` in a dependency can't corrupt the
frame stream, but a corrupted `mcp-config.json` (e.g. invalid JSON)
would still trip Claude Code before the server ever runs. Validate
the config with `jq .` first.

**`"mcpServers":[{"name":"kolsch","status":"failed"}]` in the init
frame.** Claude Code couldn't spawn the server — almost always a
PATH / command resolution issue. Check that `which kolsch` returns a
path (activate the venv or use one of the "Alternative invocations"
recipes above). Run `kolsch mcp serve </dev/null` directly in a shell
to confirm the binary is invocable at all; you should get a stdio
server that reads nothing and exits cleanly.

**"claude: no subscription auth found."** Run `claude auth login`
against your Pro/Max subscription. Do NOT set `ANTHROPIC_API_KEY` in
the child env — the CLI treats an API key as a distinct auth path
and won't fall back to the subscription flow.
