"""MCP operator server — the FastMCP wrapper exposing `list_sessions`.

Builds a stateless stdio MCP server on top of the same
[`PlatformRuntime`][platform_runtime.runtime.PlatformRuntime] the CLI
and FastAPI surface use. Exposes exactly one read-only tool,
`list_sessions`, that wraps `runtime.store.list_sessions(...)` — the
call `GET /sessions` uses — and returns each session through the
7-field mirror in :mod:`mcp_server.views`.

Design notes:

- `FastMCP(name="kolsch")` is constructed with **no** `version=`
  kwarg because `mcp.server.fastmcp.FastMCP.__init__` in
  `mcp==1.26.0` does not accept one; passing it would `TypeError` at
  construction. `serverInfo.version` falls back to the SDK's own
  package version, which is a documented Phase-1 shortcoming. Claude
  Code keys on `serverInfo.name` (via `mcp__kolsch__list_sessions`),
  so the version-string source doesn't affect the round-trip.
- The tool is registered with `@mcp.tool(structured_output=False)`
  and returns `list[TextContent]` directly. FastMCP's built-in
  `_convert_to_content` already maps dict/list to JSON `TextContent`
  blocks; a hand-rolled serialization helper is redundant. Setting
  `structured_output=False` also prevents FastMCP from auto-deriving
  an `outputSchema` from the return type — that path would wrap the
  result in `{"result": ...}` and re-emit duplicated content.
- Error mapping: FastMCP's default exception handling would leak the
  raw exception message in the `isError: true` body. The tool
  therefore catches its own exceptions and returns a `CallToolResult`
  with sanitized text (`f"list_sessions failed: {type(exc).__name__}"`
  — no message or traceback) so secrets or path fragments can never
  slip out via the error channel.
- Stdout hygiene HARD GUARD: `run_stdio` wraps the user-code body in
  `contextlib.redirect_stdout(sys.stderr)`. `mcp.server.stdio.stdio_server`
  binds `sys.stdout.buffer` for JSON-RPC framing; any stray
  `print(...)` — from a transitive dependency banner, `warnings.warn(...,
  file=sys.stdout)`, or a mid-request dependency stdout-write — would
  corrupt the frame and hang Claude Code. Redirecting to stderr keeps
  the framing intact and pushes such writes to a channel the client
  ignores.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from mcp_server.views import session_view

if TYPE_CHECKING:
    from platform_runtime import PlatformRuntime


_log = logging.getLogger("mcp_server")


_TOOL_DESCRIPTION = (
    "List kolsch sessions from the local workspace. Mirrors the "
    "`GET /sessions` FastAPI surface — same store call, same 7-field "
    "projection (session_id, agent_id, name, starred, archived, "
    "created_at, updated_at). Read-only. "
    "Secrets never go through tool arguments; kolsch tools that need "
    "secrets read them via `secrets_needed` on the tool definition."
)


def build_server(runtime: PlatformRuntime) -> FastMCP:
    """Build a `FastMCP` server bound to `runtime` and return it.

    Registers exactly one tool, `list_sessions`, that closes over
    ``runtime.store``. Extracted from :func:`run_stdio` so tests can
    drive the server in-memory via
    ``mcp.shared.memory.create_connected_server_and_client_session``
    without booting a stdio transport.

    Args:
        runtime: The composition-root runtime. Its ``store`` provides
            the ``list_sessions(...)`` call the tool wraps.

    Returns:
        A configured `FastMCP` server with `list_sessions` registered.
        Not yet running — call ``server.run_stdio_async()`` to serve.
    """
    # `FastMCP.__init__` in `mcp==1.26.0` has NO `version` kwarg; passing
    # one raises `TypeError` at construction. `serverInfo.version`
    # falls back to the SDK version — Phase 1 accepts that.
    mcp = FastMCP(name="kolsch")

    @mcp.tool(
        name="list_sessions",
        description=_TOOL_DESCRIPTION,
        structured_output=False,
    )
    async def list_sessions(
        starred: bool | None = None,
        archived: bool | None = None,
        limit: int | None = None,
    ) -> list[TextContent] | CallToolResult:
        """Return every matching session as one JSON `TextContent` block per row.

        Args:
            starred: Filter to starred / un-starred / both.
            archived: Same filter shape on archived.
            limit: Cap the result count. `None` returns every match.

        Returns:
            On success: one `TextContent` per matching session,
            each `.text` is `json.dumps(session_view(session))`.
            On failure: a `CallToolResult` with `isError=True` and a
            sanitized `list_sessions failed: <ExceptionType>` message.
        """
        try:
            sessions = await runtime.store.list_sessions(
                starred=starred,
                archived=archived,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — sanitize + surface, don't leak
            # Sanitize: return the exception TYPE only, not the message
            # or traceback. This is the outward-facing MCP error channel
            # — a raw `str(exc)` could leak workspace paths, secret
            # values threaded into ValueError messages, etc.
            _log.warning("list_sessions failed: %s", type(exc).__name__, exc_info=exc)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"list_sessions failed: {type(exc).__name__}",
                    )
                ],
                isError=True,
            )
        return [
            TextContent(
                type="text",
                text=json.dumps(session_view(s), ensure_ascii=False),
            )
            for s in sessions
        ]

    return mcp


async def run_stdio(runtime: PlatformRuntime) -> None:
    """Build the server and serve it over stdio until the client disconnects.

    Serves the JSON-RPC frames on the raw ``sys.stdout.buffer`` that
    ``mcp.server.stdio.stdio_server`` captures at bind time, then
    installs a HARD GUARD: ``contextlib.redirect_stdout(sys.stderr)``
    rewires the Python-level ``sys.stdout`` text wrapper to stderr for
    the duration of the user-code body. Any stray `print(...)` from a
    transitive-crewai dependency banner, a lazy import warning, or a
    mid-`tools/call` dependency stdout-write ends up on stderr rather
    than corrupting the JSON-RPC frames on stdout. `stdio_server`
    itself is unaffected because it holds a direct reference to
    `sys.stdout.buffer` captured before the redirect.

    Bind-order matters: `stdio_server` MUST enter first so it snapshots
    the pre-redirect `sys.stdout.buffer`; the `redirect_stdout` context
    manager wraps the inner body only. This is the reason `run_stdio`
    reimplements the two-line body of `FastMCP.run_stdio_async` rather
    than calling it directly.

    Called from the CLI dispatcher (`_cmd_mcp_serve`) inside
    `asyncio.run(...)`; must `await` an async server call rather than
    call `server.run()` (which spins its own event loop and raises
    `RuntimeError: asyncio.run() cannot be called from a running event
    loop` when nested inside `asyncio.run`).

    Args:
        runtime: The composition-root runtime the tools close over.
    """
    from mcp.server.stdio import stdio_server  # noqa: PLC0415

    server = build_server(runtime)
    # pragma: no cover — the stdio bind + `_mcp_server.run(...)` loop
    # only unwinds on client EOF; reachable only from the real-`claude`
    # e2e round-trip (Test 7). The pieces this line composes are each
    # unit-tested: `build_server` (Test 2/3/4/5), the tool body
    # (Test 3/3b), and the `contextlib.redirect_stdout(sys.stderr)`
    # guard mechanism (Test 5).
    async with stdio_server() as (read_stream, write_stream):  # pragma: no cover
        with contextlib.redirect_stdout(sys.stderr):  # pragma: no cover
            await server._mcp_server.run(  # pragma: no cover
                read_stream,
                write_stream,
                server._mcp_server.create_initialization_options(),
            )
