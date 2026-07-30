"""MCP operator server — the FastMCP wrapper exposing read-only tools.

Builds a stateless stdio MCP server on top of the same
[`PlatformRuntime`][platform_runtime.runtime.PlatformRuntime] the CLI
and FastAPI surface use. Exposes two read-only tools that mirror
existing FastAPI surfaces:

- ``list_sessions`` (spec-105) — wraps
  ``runtime.store.list_sessions(...)`` and mirrors ``GET /sessions``
  through the 7-field :class:`api.views._SessionView` projection.
- ``read_session_events`` (spec-111) — wraps
  ``runtime.store.read_events(...)`` and mirrors
  ``GET /sessions/{session_id}/events`` by returning the full
  :class:`schemas.events.Event` envelope per event. Adds two cheap
  post-filters (``event_types``, ``include_ui_events``) that the
  HTTP surface doesn't need but an LLM stream-consumer wants.

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

from api.views import session_view
from schemas import SessionId
from schemas.events import EventType

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


_READ_SESSION_EVENTS_DESCRIPTION = (
    "Read events from a kolsch session's JSONL log. Mirrors the "
    "`GET /sessions/{session_id}/events` FastAPI surface — same store "
    "call, same full-`Event` projection (event_id, parent_event_id, "
    "thread_id, session_id, type, data, actor, created_at, "
    "content_ref, trace_id). Requires `session_id` (obtain from "
    "`list_sessions`).\n"
    "\n"
    "Store knobs (spec-003 semantics — `tail` applies first, then "
    "cursor, then `limit`):\n"
    "- `after_event_id`: return only events strictly after this event "
    "id (renamed at the tool boundary from the store's "
    "`from_event_id` — they mean the same thing).\n"
    "- `tail`: consider only the last N events of the log.\n"
    "- `limit`: cap the result count.\n"
    "To page a long session, call repeatedly with "
    "`after_event_id=<last returned event's event_id>`.\n"
    "\n"
    "Post-filters (cheap, applied in-tool):\n"
    "- `event_types`: return only events whose `type` matches one of "
    'the given strings (e.g. `["tool.failed", "workflow.run.failed"]`). '
    "Empty list or None = no filter.\n"
    "- `include_ui_events` (default False): drop `ui.event` records. "
    "Defaulted off because `ui.event`s dominate agent-debugging logs "
    "(a representative sample: 418/679 = ~62% in one debugging "
    "session). BUT: operators asking chat-UX questions "
    '("what did the user see?") should pass `include_ui_events=True` — '
    "the default suppresses the widget-mutation stream that IS the "
    "interesting content for chat sessions.\n"
    "\n"
    "`content_ref` is returned as a pointer (out-of-band blobs are not "
    "dereferenced); large payloads in `data` are returned in full — "
    "use `event_types`, `tail`, or `limit` to bound response size.\n"
    "\n"
    "Read-only. Secrets never go through tool arguments; kolsch tools "
    "that need secrets read them via `secrets_needed` on the tool "
    "definition."
)


def build_server(runtime: PlatformRuntime) -> FastMCP:
    """Build a `FastMCP` server bound to `runtime` and return it.

    Registers two read-only tools that close over ``runtime.store``:
    ``list_sessions`` (spec-105) and ``read_session_events``
    (spec-111). Extracted from :func:`run_stdio` so tests can drive
    the server in-memory via
    ``mcp.shared.memory.create_connected_server_and_client_session``
    without booting a stdio transport.

    Args:
        runtime: The composition-root runtime. Its ``store`` provides
            the ``list_sessions(...)`` and ``read_events(...)`` calls
            the tools wrap.

    Returns:
        A configured `FastMCP` server with both tools registered. Not
        yet running — call ``server.run_stdio_async()`` to serve.
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
            each `.text` is
            `json.dumps(session_view(session).model_dump(mode="json"))`.
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
                text=json.dumps(
                    session_view(s).model_dump(mode="json"),
                    ensure_ascii=False,
                ),
            )
            for s in sessions
        ]

    @mcp.tool(
        name="read_session_events",
        description=_READ_SESSION_EVENTS_DESCRIPTION,
        structured_output=False,
    )
    async def read_session_events(  # noqa: PLR0913 — flat params, spec-111 §Scope
        session_id: str,
        after_event_id: str | None = None,
        tail: int | None = None,
        limit: int | None = None,
        event_types: list[str] | None = None,
        include_ui_events: bool = False,
    ) -> list[TextContent] | CallToolResult:
        """Return the session's events as one JSON `TextContent` per row.

        Mirrors ``GET /sessions/{session_id}/events``: same store call
        (:meth:`SessionStore.read_events`), same full-``Event``
        projection (``event.model_dump(mode="json")``). Adds two
        post-filters — ``event_types`` and ``include_ui_events``
        (default ``False``) — because an LLM stream-consumer pays for
        every byte the HTTP handler's ambient caller can afford to
        ignore.

        Note the argname rename at the tool boundary:
        ``after_event_id`` (this tool) maps to the store's
        ``from_event_id`` — both mean "strictly after this event id".
        The tool docstring uses spec-003 vocabulary; the store keeps
        its legacy name.

        Args:
            session_id: Required. Obtain from ``list_sessions``.
            after_event_id: Optional cursor — see docstring.
            tail: Optional last-N-events window.
            limit: Optional result count cap.
            event_types: Optional allowlist of ``EventType`` string
                values (e.g. ``["tool.failed"]``). ``None`` and ``[]``
                are treated identically as "no filter".
            include_ui_events: When ``False`` (default) drop events
                whose ``type == EventType.UI_EVENT``.

        Returns:
            On success: one ``TextContent`` per matching event, each
            ``.text`` is ``json.dumps(event.model_dump(mode="json"))``.
            Empty list on zero matches is a valid response.
            On failure: a ``CallToolResult`` with ``isError=True`` and
            a sanitized ``read_session_events failed: <ExceptionType>``
            message.
        """
        try:
            # Step 1: coerce to SessionId. Raises ValueError for BOTH
            # a bad prefix AND a valid-prefix-non-uuid body (SessionId
            # is a PrefixedId with body_must_be_uuid=True).
            sid = SessionId(session_id)
            # Step 2: verify the session exists. Raises SessionNotFound
            # → sanitized to the type name in the outer handler,
            # mirroring the HTTP surface's 404 contract.
            await runtime.store.get_session(sid)
            # Step 3: iterate every thread (multi-thread forward-compat).
            threads = await runtime.store.list_threads(sid)
            # Wrap once so per-event membership is O(1); an empty list
            # / None is treated as "no filter" per the docstring.
            event_type_filter: set[str] | None = (
                set(event_types) if event_types else None
            )
            results: list[TextContent] = []
            for thread in threads:
                async for event in runtime.store.read_events(
                    thread.thread_id,
                    from_event_id=after_event_id,
                    tail=tail,
                    limit=limit,
                ):
                    # Post-filter 1: event_types allowlist. Applied
                    # BEFORE the ui-event filter so a future widening
                    # of `event_types` to a callable predicate lands
                    # in a predictable slot.
                    if (
                        event_type_filter is not None
                        and event.type.value not in event_type_filter
                    ):
                        continue
                    # Post-filter 2: ui-event default drop. `==` not
                    # `is` — Pydantic re-validates JSONL-replayed
                    # StrEnum values through the enum constructor and
                    # singleton identity is not guaranteed to hold
                    # across future Pydantic versions.
                    if not include_ui_events and event.type == EventType.UI_EVENT:
                        continue
                    results.append(
                        TextContent(
                            type="text",
                            text=json.dumps(
                                event.model_dump(mode="json"),
                                ensure_ascii=False,
                            ),
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — sanitize + surface, don't leak
            # Sanitize: return the exception TYPE only, not the message
            # or traceback. Covers ValueError from SessionId(...) (bad
            # prefix OR bad uuid body), SessionNotFound from
            # get_session, and any downstream store errors.
            _log.warning(
                "read_session_events failed: %s",
                type(exc).__name__,
                exc_info=exc,
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"read_session_events failed: {type(exc).__name__}",
                    )
                ],
                isError=True,
            )
        return results

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
