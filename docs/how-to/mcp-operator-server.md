# MCP operator server (`kolsch mcp serve`)

`kolsch mcp serve` exposes a small, read-only slice of the kolsch
runtime over the [Model Context Protocol](https://modelcontextprotocol.io/)
as a stateless **stdio server**. Pair it with a headless [Claude
Code](https://docs.claude.com/en/docs/claude-code/overview) — under a
Pro or Max subscription — to let an external agent list your sessions
without booting a workflow.

This is a walking skeleton (spec-105 Phase 1). It exposes exactly one
tool, **`list_sessions`**, mirroring the existing `GET /sessions`
FastAPI surface. Write/execute tools land in Phase 2 alongside a
principal rework.

## Why this exists

Two MCP surfaces exist in kolsch:

- **Surface A — tool bridge** (in-workflow, session-scoped): lets Claude
  Code run *as a kolsch agent inside a workflow* and call the running
  session's per-agent tools. High cost/risk; not shipped yet.
- **Surface B — operator server** (this page): lets an *external* agent
  orchestrate/observe kolsch over stdio. Low cost, additive, no engine
  change.

## Prerequisites

- A logged-in `claude` binary (`claude auth login`, subscription auth
  — no `ANTHROPIC_API_KEY` in the child env).
- An initialised workspace (`kolsch setup --non-interactive` or the
  interactive flow).

## The `mcp-config.json`

Ship the config alongside your workspace, or drop it in the current
working directory when you invoke `claude`:

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

## Run the round-trip

```bash
claude -p "list my kolsch sessions" \
  --output-format stream-json \
  --mcp-config ./mcp-config.json \
  --allowedTools "mcp__kolsch__list_sessions"
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

## Permissions posture

**Two composing gates.**

- **Client side (Claude Code):** headless callers cannot prompt for
  approval, so the tool is **pre-authorised least-privilege**. The
  shipped recipe uses `--allowedTools "mcp__kolsch__list_sessions"`
  and does not use the broader "bypass all permissions" mode Claude
  Code exposes.
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

**bug-102 orthogonality.** bug-102 (CANCELLED — not solvable via
input redaction) concerns workflow config previews / `inputs.*` /
JSONL leaking secrets on the *data plane*. `list_sessions` exposes
only the **Session entity record** (`session_id`, `agent_id`,
`name`, `starred`, `archived`, `created_at`, `updated_at`;
`from_workflow_run` dropped) — no `inputs`, no bindings, no JSONL,
no secrets. The only user-authored field is `name`. It does not
touch the bug-102 surface.

**Future direction (recorded, not built):**

- **Stricter-of gating** — a session grant may narrow but never
  widen kolsch's authz. The protected `rl_default_deny` tail already
  gives the non-loosening floor.
- **Interactive approval on a side channel** — OpenClaw's
  `permissions_respond`; for kolsch, the chat UI resolving a
  `prompt`-effect tool with the approval pinned to the canonical
  `{tool, args, session}` so it can't be mutated post-approval.

Both are the **Phase 2 / Surface A** path.

**No secrets in tool arguments.** Even though `list_sessions` takes
no secret input, the tool description carries the standing kolsch
convention that secrets never go through tool arguments — they route
through `secrets_needed` on the tool definition. This convention is
in place before write tools arrive.

## Troubleshooting

**"MCP tool not found" / no `mcp__kolsch__list_sessions` in the
frame stream.** The `--allowedTools` name is
`mcp__<server-name>__<tool-name>` where `<server-name>` is the key
in `mcpServers`. Our config uses `"kolsch"`, so the flag value is
`mcp__kolsch__list_sessions`.

**"claude asks for approval / hangs."** You passed `-p` (print mode)
but omitted `--allowedTools`. Headless mode cannot prompt; without
the allowlist the tool never authorises.

**"JSON-RPC framing error / weird output".** kolsch's stdout is
guarded so a stray `print(...)` in a dependency can't corrupt the
frame stream, but a corrupted `mcp-config.json` (e.g. invalid JSON)
would still trip Claude Code before the server ever runs. Validate
the config with `jq .` first.

**"claude: no subscription auth found."** Run `claude auth login`
against your Pro/Max subscription. Do NOT set `ANTHROPIC_API_KEY` in
the child env — the CLI treats an API key as a distinct auth path
and won't fall back to the subscription flow.
