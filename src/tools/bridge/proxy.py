"""`kolsch mcp bridge` — the stdio MCP proxy end of the tool bridge (spec-120).

A stateless subprocess the external MCP-client harness spawns. It
holds no engine state: it connects to the `ToolBridgeServer`'s Unix
socket (`KOLSCH_MCP_SOCKET`), authenticates with the one-binding token
(`KOLSCH_MCP_TOKEN`), then speaks MCP JSON-RPC 2.0 over stdio
(newline-delimited frames) and forwards `tools/list` / `tools/call`
to the socket. All authority stays in the running kolsch process.

Wire contract implemented here (a real client depends on it):

- `initialize` → REAL `protocolVersion` negotiation (echo the client's
  requested version when supported, else the server's latest),
  `capabilities: {"tools": {}}` (NOT `listChanged` — v1 never sends
  `notifications/tools/list_changed`), `serverInfo`.
- `notifications/initialized` → accepted, no reply (as any JSON-RPC
  notification).
- `ping` → `{}`.
- `tools/list` / `tools/call` → forwarded frames; results pass through
  (the server already produced sanitized MCP shapes) — EXCEPT the
  tool NAMES, which this proxy localizes to the bare form: the MCP
  client namespaces server tools itself (`mcp__<server>__<name>`),
  so the harness-visible name lands on T2's intended
  `mcp__<server>__<bare>` exactly once (spec-121 correction; see
  `BridgeStdioServer`).
- unknown method → `-32601`; malformed params → `-32602`; parse error
  → `-32700`; bridge/transport failure → `-32000` with a CONSTANT
  generic message (the proxy never interpolates an exception).

Stdout hygiene: replicates the operator server's HARD GUARD bind
order (`src/mcp_server/server.py::run_stdio`) — the raw
`sys.stdout.buffer` is captured FIRST for JSON-RPC framing, THEN
`contextlib.redirect_stdout(sys.stderr)` rewires the Python-level
`sys.stdout` so a stray `print(...)` cannot corrupt a frame.

Token/connect failure: ONE constant stderr line + exit 1 for every
refusal (unknown / expired / already-bound tokens are refused
uniformly by the server, and a missing socket is indistinguishable by
design) — no oracle on this side either.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from errors import (
    BridgeMalformedRequest,
    BridgeRequestRefused,
    BridgeSocketUnavailable,
    PlatformError,
)
from tools.bridge.server import MAX_FRAME_BYTES, encode_frame, read_frame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_log = logging.getLogger("tools.bridge")


# ---------------------------------------------------------------------------
# Protocol constants.
# ---------------------------------------------------------------------------

SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
)
"""MCP protocol revisions this bridge can serve."""

LATEST_PROTOCOL_VERSION = "2025-11-25"
"""Returned when the client requests an unsupported revision."""

_SERVER_INFO_NAME = "kolsch-bridge"

_GENERIC_RPC_FAILURE = "bridge request failed"
"""Constant `-32000` message — never an exception's text."""

_ENV_SOCKET = "KOLSCH_MCP_SOCKET"
_ENV_TOKEN = "KOLSCH_MCP_TOKEN"  # noqa: S105 — env var NAME, not a credential

_MISSING_ENV_MESSAGE = (
    "kolsch mcp bridge: KOLSCH_MCP_SOCKET and KOLSCH_MCP_TOKEN must be "
    "set (this command is spawned by an MCP-client harness against a "
    "running kolsch exchange; it is not run by hand).\n"
)

_REFUSED_MESSAGE = "kolsch mcp bridge: bridge connection refused\n"
"""The ONE stderr line for every connect/bind failure — uniform."""

_EXIT_MISSING_ENV = 2
_EXIT_REFUSED = 1


def _bridge_version() -> str:
    try:
        return version("kolsch")
    except PackageNotFoundError:  # pragma: no cover — dev-env oddity only
        return "0.0.0"


# ---------------------------------------------------------------------------
# Socket client — frames out, id-matched responses back.
# ---------------------------------------------------------------------------


class _SocketClient:
    """Frame client for the bridge socket; safe for concurrent requests."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, socket_path: str) -> _SocketClient:
        """Connect to the bridge socket and start the response reader.

        Raises:
            BridgeSocketUnavailable: connect failed (missing/removed
                socket, refused connection, permission).
        """
        try:
            reader, writer = await asyncio.open_unix_connection(
                socket_path,
                limit=MAX_FRAME_BYTES,
            )
        except OSError as exc:
            raise BridgeSocketUnavailable(transport_error=type(exc).__name__) from exc
        client = cls(reader, writer)
        client._reader_task = asyncio.create_task(client._read_loop())
        return client

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await read_frame(self._reader)
                frame_id = msg.get("id")
                fut = (
                    self._pending.pop(frame_id, None)
                    if isinstance(frame_id, int)
                    else None
                )
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                elif frame_id is None:
                    # Connection-scoped refusal (e.g. oversized frame)
                    # — nothing else can be trusted on this stream.
                    self._fail_pending(
                        BridgeSocketUnavailable(transport_error="refused")
                    )
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            self._fail_pending(
                BridgeSocketUnavailable(transport_error=type(exc).__name__)
            )
        except PlatformError as exc:
            self._fail_pending(exc)

    def _fail_pending(self, exc: PlatformError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def request(self, op: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401 — JSON frame fields
        """Send one frame, await the id-matched response.

        Returns the frame's `result`.

        Raises:
            BridgeRequestRefused: the server answered `ok: false`
                (reason-free by design — refusals are uniform).
            BridgeSocketUnavailable: the connection dropped first.
        """
        frame_id = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = fut
        frame = encode_frame({"id": frame_id, "op": op, **fields})
        try:
            async with self._write_lock:
                self._writer.write(frame)
                await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            self._pending.pop(frame_id, None)
            raise BridgeSocketUnavailable(transport_error=type(exc).__name__) from exc
        msg = await fut
        if not msg.get("ok"):
            raise BridgeRequestRefused(op=op)
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        # Anything still awaiting a response will never get one.
        self._fail_pending(BridgeSocketUnavailable(transport_error="closed"))
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


# ---------------------------------------------------------------------------
# The stdio JSON-RPC loop.
# ---------------------------------------------------------------------------


class BridgeStdioServer:
    """The MCP JSON-RPC method surface over injected line streams.

    Stream-agnostic on purpose: `serve` takes a `read_line` coroutine
    (returns `b""` at EOF) and a `write_message` coroutine, so
    component tests drive the full loop in-process while
    `run_bridge_stdio` wires the real stdin/stdout.

    Naming (spec-121 correction, verified against live Claude Code):
    the SOCKET leg speaks the full `mcp__<server>__<bare>` wire names,
    but this stdio surface serves the client the BARE names — a real
    MCP client namespaces server tools itself, so forwarding the
    pre-prefixed names verbatim produced
    `mcp__kolsch__mcp__kolsch__<bare>` client-side and broke
    `--allowedTools`. `tools/list` localizes via the socket's
    `wire_to_bare` map (stripped before the reply); `tools/call`
    translates the client's bare name back to the wire name. A client
    that calls with a FULL wire name still routes (the untranslated
    name is forwarded as-is and the socket accepts it) — backwards
    compatible with pre-correction clients.
    """

    def __init__(self, client: _SocketClient) -> None:
        self._client = client
        self._tasks: set[asyncio.Task[None]] = set()
        # bare (client-facing) name -> full socket wire name. Filled
        # by every tools/list; refreshed once on a call-name miss.
        self._bare_to_wire: dict[str, str] = {}

    async def serve(
        self,
        read_line: Callable[[], Awaitable[bytes]],
        write_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Run the loop until EOF on the client stream."""
        while True:
            line = await read_line()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = self._parse(line)
            if msg is None:
                await write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                continue
            if "id" not in msg or msg["id"] is None:
                # Notification — never answered. v1 only expects
                # `notifications/initialized`; others are ignored per
                # JSON-RPC.
                continue
            # Each request in its own task so a slow tools/call never
            # blocks a ping.
            task = asyncio.create_task(self._answer(msg, write_message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @staticmethod
    def _parse(line: bytes) -> dict[str, Any] | None:
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    async def _answer(
        self,
        msg: dict[str, Any],
        write_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        request_id = msg["id"]
        method = msg.get("method")
        params = msg.get("params")
        error: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._localize_tools(await self._client.request("list_tools"))
            elif method == "tools/call":
                result = await self._tools_call(params)
            else:
                error = {"code": -32601, "message": "Method not found"}
        except BridgeMalformedRequest:
            error = {"code": -32602, "message": "Invalid params"}
        except PlatformError as exc:
            # Refusal or transport loss — constant message, full
            # reason to stderr logging only.
            _log.warning("bridge proxy: %s failed: %s", method, exc.code)
            error = {"code": -32000, "message": _GENERIC_RPC_FAILURE}
        except Exception:  # noqa: BLE001 — trust boundary: never leak an exception's text onto the wire
            _log.exception("bridge proxy: %s failed", method)
            error = {"code": -32000, "message": _GENERIC_RPC_FAILURE}
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result if result is not None else {}
        await write_message(response)

    @staticmethod
    def _initialize(params: object) -> dict[str, Any]:
        """Handshake with REAL `protocolVersion` negotiation.

        Echo the client's requested version when this bridge supports
        it; otherwise answer with the latest supported version (per
        the MCP spec's negotiation rule). Never a blind echo, never a
        hardcoded constant regardless of input.
        """
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": negotiated,
            # v1 never sends notifications/tools/list_changed, so the
            # handshake MUST NOT advertise `listChanged: true`.
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": _SERVER_INFO_NAME,
                "version": _bridge_version(),
            },
        }

    def _localize_tools(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Socket `list_tools` result → client-facing bare-named catalog.

        Rebuilds the bare→wire map from the socket's `wire_to_bare`
        rider (authoritative — the server computed it from the same
        catalog read) and strips the rider from the reply. Unknown
        shapes pass through untouched (the trust boundary stays with
        the server's sanitization).
        """
        tools = raw.get("tools")
        wire_to_bare = raw.get("wire_to_bare")
        if not isinstance(tools, list) or not isinstance(wire_to_bare, dict):
            return {"tools": tools if isinstance(tools, list) else []}
        localized: list[Any] = []
        for tool in tools:
            if not isinstance(tool, dict):
                localized.append(tool)
                continue
            wire = tool.get("name")
            bare = wire_to_bare.get(wire) if isinstance(wire, str) else None
            if isinstance(bare, str) and isinstance(wire, str):
                self._bare_to_wire[bare] = wire
                localized.append({**tool, "name": bare})
            else:
                localized.append(tool)
        return {"tools": localized}

    async def _tools_call(self, params: object) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise BridgeMalformedRequest(method="tools/call")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise BridgeMalformedRequest(method="tools/call")
        wire = self._bare_to_wire.get(name)
        if wire is None:
            # Miss: either the client never listed (map empty) or the
            # name is unknown/already a full wire name. Refresh the
            # map once from the live catalog, then forward — an
            # untranslatable name goes through as-is and lands on the
            # server's sanitized unknown-tool arm.
            with contextlib.suppress(PlatformError):
                self._localize_tools(await self._client.request("list_tools"))
            wire = self._bare_to_wire.get(name, name)
        return await self._client.request(
            "call_tool",
            name=wire,
            arguments=arguments,
        )


# ---------------------------------------------------------------------------
# Entry point — env, socket bind, real stdio wiring.
# ---------------------------------------------------------------------------


async def _stdin_reader() -> (
    asyncio.StreamReader
):  # pragma: no cover — real-fd wiring; exercised by the subprocess smoke
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol,
        sys.stdin,
    )
    return reader


def _write_and_flush(buffer: Any, data: bytes) -> None:  # noqa: ANN401 — BinaryIO-ish; sys.stdout.buffer
    buffer.write(data)
    buffer.flush()


async def run_bridge_stdio() -> int:
    """Run `kolsch mcp bridge`: connect, bind, serve MCP on stdio.

    Returns the process exit code: `2` when the env contract
    (`KOLSCH_MCP_SOCKET`/`KOLSCH_MCP_TOKEN`) is absent (never boots),
    `1` on any connect/bind refusal (one constant stderr line —
    uniform across unknown/expired/already-bound), `0` on clean EOF.
    """
    socket_path = os.environ.get(_ENV_SOCKET)
    token = os.environ.get(_ENV_TOKEN)
    if not socket_path or not token:
        sys.stderr.write(_MISSING_ENV_MESSAGE)
        return _EXIT_MISSING_ENV

    try:
        client = await _SocketClient.connect(socket_path)
    except PlatformError:
        sys.stderr.write(_REFUSED_MESSAGE)
        return _EXIT_REFUSED
    try:
        await client.request("bind", token=token)
    except PlatformError:
        await client.close()
        sys.stderr.write(_REFUSED_MESSAGE)
        return _EXIT_REFUSED

    # pragma-free from here: the serve loop below IS covered — via the
    # real-subprocess smoke for the fd wiring and in-process component
    # tests for the JSON-RPC surface.
    return await _serve_real_stdio(
        client
    )  # pragma: no cover — fd wiring; the subprocess smoke drives it


async def _serve_real_stdio(
    client: _SocketClient,
) -> int:  # pragma: no cover — real-fd wiring; exercised by the subprocess smoke
    """Bind real stdio and serve until EOF.

    Bind-order replicates the operator server's HARD GUARD
    (`src/mcp_server/server.py::run_stdio`): capture the raw
    `sys.stdout.buffer` FIRST for JSON-RPC framing, THEN redirect the
    Python-level `sys.stdout` to stderr for the loop body.
    """
    stdout_buffer = sys.stdout.buffer
    write_lock = asyncio.Lock()

    async def write_message(obj: dict[str, Any]) -> None:
        data = (
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        )
        async with write_lock:
            await asyncio.to_thread(_write_and_flush, stdout_buffer, data)

    reader = await _stdin_reader()
    server = BridgeStdioServer(client)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            await server.serve(reader.readline, write_message)
    finally:
        await client.close()
    return 0
