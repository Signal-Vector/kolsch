"""`ToolBridgeServer` — the in-process end of the MCP tool bridge (spec-120).

Opened once per exchange by the running kolsch process. Binds a Unix
socket under a freshly-minted private directory, mints a one-binding
token, and serves an internal length-prefixed frame protocol
(`bind` / `list_tools` / `call_tool`). Every `call_tool` runs in its
own task through T1's [`invoke_observed`][tools.observed.invoke_observed]
with the bound context and emit sink, so dispatch, authz, sandbox, and
event emission are identical to an in-engine call.

Security posture (the trust boundary rules):

- **Allowlist sanitization.** A `Denied` / `Failed` outcome maps to a
  constant generic literal with ZERO `Error` fields interpolated — the
  mapping never even reads `outcome.error`. `Denied` and `Failed`
  produce the SAME text so the wire cannot become an allow/deny/prompt
  oracle. (`prompt`-effect denial needs no special arm: dispatch's
  authz call passes **no callback, ever** — `src/tools/dispatch.py:277`
  — so `prompt` resolves to deny structurally and arrives here as the
  `Denied` arm. Do NOT wire a prompt callback; that would be an authz
  bypass.)
- **Uniform refusal.** Every refused frame — bad/expired/already-bound
  token, op-before-bind, malformed body — is answered with one
  constant error payload. The typed reason goes to the server log only.
- **Private dir is the primary gate.** `mkdtemp` (0700, geteuid-owned,
  under `$XDG_RUNTIME_DIR` when sane, else the tmpdir) guards the
  socket; socket mode 0600 with a randomized name, umask-tightened
  bind (TOCTOU closed) + post-bind verification are defense-in-depth.
- **Framing.** 4-byte **big-endian** unsigned length prefix
  (`_FRAME_HEADER`), size-capped on the DECLARED length before the
  body is read.

Naming contract (frozen here for T3): a registered tool's bare
`ToolInfo.name` is exposed as `mcp__<server>__<bare-name>`; the
reverse map (wire name → bare name) routes `tools/call`; a wire-name
collision fails loudly (`BridgeNameCollision`). The ≤64-char length
budget is T3's concern.

This module imports nothing from `bundles/*/tools/`, hardcodes no
tool names, and never imports `engine`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import stat
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import (
    BridgeAuthFailed,
    BridgeFrameMalformed,
    BridgeFrameTooLarge,
    BridgeNameCollision,
    BridgeNotBound,
    BridgeSocketInsecure,
    PlatformError,
)
from tools.observed import Ok, invoke_observed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from tools.decorator import ToolInfo

_log = logging.getLogger("tools.bridge")


# ---------------------------------------------------------------------------
# Wire constants — frozen contract shared by server, proxy, and tests.
# ---------------------------------------------------------------------------

_FRAME_HEADER = struct.Struct(">I")
"""Frame prefix: 4-byte big-endian unsigned length of the JSON body."""

MAX_FRAME_BYTES = 8 * 1024 * 1024
"""Hard cap on a frame body; enforced on the DECLARED length before
the body is read so an oversized declaration cannot OOM the server."""

_GENERIC_REFUSAL = {"message": "bridge request refused"}
"""The ONE error payload every refused frame carries — bad token,
expired token, already-bound, op-before-bind, malformed body. Uniform
by construction: no oracle."""

_GENERIC_CALL_FAILED_TEXT = "The kolsch platform did not complete this tool call."
"""Constant text for ANY `Denied`/`Failed` outcome. An allowlist: no
`Error` field is ever interpolated, and denial vs failure vs
prompt-collapse are indistinguishable on the wire."""

_GENERIC_UNKNOWN_TOOL_TEXT = "Unknown tool for this kolsch bridge."
"""Constant text for a `tools/call` naming a tool outside the bound
catalog. No dispatch runs and no event is emitted for these."""


# ---------------------------------------------------------------------------
# Tool-name contract — mcp__<server>__<bare-name> + reverse map.
# ---------------------------------------------------------------------------


def mcp_wire_name(server_name: str, bare_name: str) -> str:
    """Return the MCP wire name for a registered tool's bare name.

    Frozen format `mcp__<server>__<bare-name>` — `ToolInfo.name` is
    always the bare registered name (`bash`, `read_file`); the
    `/`-qualified form is `action_name` and is never surfaced. The
    ≤64-char client budget is deferred to T3.
    """
    return f"mcp__{server_name}__{bare_name}"


def build_wire_catalog(
    infos: Sequence[ToolInfo],
    *,
    server_name: str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Map `ToolInfo`s to MCP tool dicts + the wire→bare reverse map.

    `inputSchema` is `ToolInfo.args_schema` verbatim — already an
    object-rooted JSON-schema `dict` (`tools/decorator.py:216` calls
    `.model_json_schema()` once at `to_info()` time; calling it again
    here would be the `ToolDefinition` path, not this one).

    Raises:
        BridgeNameCollision: two entries map to one wire name — a
            routing + authz bug; fail loudly, never disambiguate
            silently at serve time.
    """
    tools: list[dict[str, Any]] = []
    reverse: dict[str, str] = {}
    for info in infos:
        wire = mcp_wire_name(server_name, info.name)
        if wire in reverse:
            raise BridgeNameCollision(
                wire_name=wire,
                first=reverse[wire],
                second=info.name,
            )
        reverse[wire] = info.name
        tools.append(
            {
                "name": wire,
                "description": info.description,
                "inputSchema": info.args_schema,
            }
        )
    return tools, reverse


def outcome_to_mcp(outcome: Any) -> dict[str, Any]:  # noqa: ANN401 — ToolOutcome union; Any keeps isinstance narrowing local
    """Map a `ToolOutcome` to the MCP `tools/call` result shape.

    `Ok` → the raw result serialized into one text content block
    (`str` passes through; anything else is JSON-serialized).
    `Denied` / `Failed` → the constant generic `isError: true` block.
    The failure arm deliberately never touches `outcome.error` or
    `outcome.recovery_hint` — sanitization as an allowlist, not a
    field filter.
    """
    if isinstance(outcome, Ok):
        raw = outcome.result
        text = (
            raw
            if isinstance(raw, str)
            else json.dumps(raw, ensure_ascii=False, default=str)
        )
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
    return {
        "content": [{"type": "text", "text": _GENERIC_CALL_FAILED_TEXT}],
        "isError": True,
    }


# ---------------------------------------------------------------------------
# Socket-path security helpers.
# ---------------------------------------------------------------------------


def _pick_base_dir() -> Path:
    """Choose the base under which the private socket dir is minted.

    `$XDG_RUNTIME_DIR` when it is a real, geteuid-owned directory
    (not a symlink); otherwise the process tempdir. The security
    burden does NOT rest on this choice — `mkdtemp` mints a fresh
    0700, geteuid-owned dir underneath either way (the primary gate).
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        try:
            st = os.lstat(xdg)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid():
            return Path(xdg)
    return Path(tempfile.gettempdir())


def _make_private_dir(base: Path) -> Path:
    """Mint + verify the 0700 geteuid-owned private parent dir.

    Raises:
        BridgeSocketInsecure: the minted dir fails the ownership /
            mode / is-a-dir check (e.g. a hostile base racing us).
    """
    # Short prefix on purpose: sun_path is capped (~104 bytes on
    # macOS) and the base tmpdir already spends most of the budget.
    private_dir = Path(tempfile.mkdtemp(prefix="kb-", dir=str(base)))
    private_dir.chmod(0o700)
    st = os.lstat(private_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise BridgeSocketInsecure(check="parent-not-a-dir", path=str(private_dir))
    if st.st_uid != os.geteuid():
        raise BridgeSocketInsecure(check="parent-not-owned", path=str(private_dir))
    if st.st_mode & 0o077:
        raise BridgeSocketInsecure(check="parent-mode-open", path=str(private_dir))
    return private_dir


def _ensure_absent(path: Path) -> None:
    """Reject a pre-existing node (symlink, file, socket) at `path`.

    Raises:
        BridgeSocketInsecure: anything already exists there — with a
            randomized name in a fresh private dir this only happens
            under attack or a serious bug; never unlink-and-continue.
    """
    if os.path.lexists(path):
        raise BridgeSocketInsecure(check="path-preexists", path=str(path))


def _bind_socket(socket_path: Path) -> socket.socket:
    """Create + bind the Unix socket with the TOCTOU window closed.

    The filesystem node's mode comes from the umask AT `bind()` time,
    so the umask is tightened to 0o177 across the (fully synchronous,
    no event-loop yield) bind, then restored; `chmod` afterwards is
    belt-and-braces. Post-bind, the node is verified to be a
    geteuid-owned socket.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(socket_path))
    finally:
        os.umask(old_umask)
    try:
        _verify_bound_socket(socket_path)
    except BaseException:
        sock.close()
        raise
    return sock


def _verify_bound_socket(socket_path: Path) -> None:
    """Post-bind check: the node is a geteuid-owned 0600 socket.

    Raises:
        BridgeSocketInsecure: the node is not a socket or not owned
            by the current uid.
    """
    socket_path.chmod(0o600)
    st = os.lstat(socket_path)
    if not stat.S_ISSOCK(st.st_mode):
        raise BridgeSocketInsecure(check="not-a-socket", path=str(socket_path))
    if st.st_uid != os.geteuid():
        raise BridgeSocketInsecure(check="socket-not-owned", path=str(socket_path))


# ---------------------------------------------------------------------------
# Framing helpers (shared shape with the proxy's client).
# ---------------------------------------------------------------------------


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize one frame: 4-byte big-endian length + JSON body."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return _FRAME_HEADER.pack(len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one length-prefixed frame; partial-read safe.

    Raises:
        BridgeFrameTooLarge: declared length above `MAX_FRAME_BYTES`
            (raised BEFORE the body is read).
        BridgeFrameMalformed: body is not a JSON object.
        asyncio.IncompleteReadError: peer closed mid-frame / EOF.
    """
    header = await reader.readexactly(_FRAME_HEADER.size)
    (length,) = _FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise BridgeFrameTooLarge(declared=length, max_bytes=MAX_FRAME_BYTES)
    body = await reader.readexactly(length)
    try:
        msg = json.loads(body)
    except ValueError as exc:
        raise BridgeFrameMalformed() from exc
    if not isinstance(msg, dict):
        raise BridgeFrameMalformed()
    return msg


# ---------------------------------------------------------------------------
# The server.
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Connection:
    """Per-connection state: streams + a write lock for task-safe replies."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ToolBridgeServer:
    """Per-exchange bridge server: socket + token + frame loop.

    Constructed with the bound exchange context; `open()` binds the
    socket and mints the token; `config_fragment()` hands T3 the
    `{"socket_path", "token"}` pair for the proxy's env
    (`KOLSCH_MCP_SOCKET` / `KOLSCH_MCP_TOKEN`); `close()` revokes the
    token, abandons in-flight calls cleanly (no `session.interrupted`
    — that terminal is engine-only), and removes socket + dir.

    Lifecycle is **per-exchange**: one `run_exchange` = one open bridge
    = one external subprocess; `close()` runs in the exchange's
    `finally`. Reconnect (a fresh `bind` with the same token after the
    previous connection dropped) is allowed within the exchange; a
    second bind while one connection is bound is refused.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        session_id: str,
        thread_id: str,
        tools_subsystem: Any,  # noqa: ANN401 — duck-typed facade, mirrors invoke_observed
        emit: Callable[..., Awaitable[None]],
        cancel: Any = None,  # noqa: ANN401 — CancelToken; loosely typed, no engine import
        server_name: str = "kolsch",
    ) -> None:
        self._agent_id = agent_id
        self._session_id = session_id
        self._thread_id = thread_id
        self._tools_subsystem = tools_subsystem
        self._emit = emit
        self._cancel = cancel
        self._server_name = server_name

        self._token: str | None = None
        self._socket_path: Path | None = None
        self._private_dir: Path | None = None
        self._server: asyncio.Server | None = None
        self._bound_conn: _Connection | None = None
        self._connections: set[_Connection] = set()
        self._inflight: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- lifecycle -------------------------------------------------------

    async def open(self) -> None:
        """Bind the socket under a fresh private dir and mint the token.

        Raises:
            BridgeSocketInsecure: a dir/socket security check failed.
            RuntimeError: called twice, or after `close()`.
        """
        if self._closed or self._server is not None:
            msg = "ToolBridgeServer.open() on a closed or already-open server"
            raise RuntimeError(msg)
        private_dir = _make_private_dir(_pick_base_dir())
        socket_path = private_dir / f"s{secrets.token_hex(8)}.sock"
        _ensure_absent(socket_path)
        sock = _bind_socket(socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            sock=sock,
        )
        self._token = secrets.token_urlsafe(32)
        self._socket_path = socket_path
        self._private_dir = private_dir

    def config_fragment(self) -> dict[str, str]:
        """Return `{"socket_path", "token"}` for the proxy's env.

        The token goes ONLY into the spawned subprocess's environment
        — never into the event log or any world-readable path.
        """
        if self._token is None or self._socket_path is None:
            msg = "config_fragment() before open()"
            raise RuntimeError(msg)
        return {"socket_path": str(self._socket_path), "token": self._token}

    async def close(self) -> None:
        """Revoke the token, abandon in-flight calls, remove socket + dir.

        Idempotent. In-flight `call_tool` tasks are cancelled; the
        core lets `CancelledError` propagate and this server writes NO
        terminal for it (`session.interrupted` is engine-only).
        """
        if self._closed:
            return
        self._closed = True
        self._token = None
        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        if self._server is not None:
            self._server.close()
        for conn in list(self._connections):
            conn.writer.close()
        if self._server is not None:
            await self._server.wait_closed()
        self._bound_conn = None
        if self._socket_path is not None:
            with contextlib.suppress(OSError):
                self._socket_path.unlink()
        if self._private_dir is not None:
            with contextlib.suppress(OSError):
                self._private_dir.rmdir()

    # -- connection / frame loop ----------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        conn = _Connection(reader=reader, writer=writer)
        self._connections.add(conn)
        try:
            await self._frame_loop(conn)
        finally:
            self._connections.discard(conn)
            if self._bound_conn is conn:
                # Reconnect-within-the-exchange: dropping the bound
                # connection frees the single binding slot.
                self._bound_conn = None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _frame_loop(self, conn: _Connection) -> None:
        while True:
            try:
                msg = await read_frame(conn.reader)
            except BridgeFrameTooLarge as exc:
                # Cannot resync after refusing to read a body — answer
                # generically and drop the connection.
                _log.warning("bridge: oversized frame refused: %s", exc.detail)
                await self._send(
                    conn, {"id": None, "ok": False, "error": _GENERIC_REFUSAL}
                )
                return
            except BridgeFrameMalformed:
                # Framing is intact (the body WAS read) — answer
                # generically and keep serving.
                _log.warning("bridge: malformed frame body")
                await self._send(
                    conn, {"id": None, "ok": False, "error": _GENERIC_REFUSAL}
                )
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return  # peer disconnected
            await self._dispatch_frame(conn, msg)

    async def _dispatch_frame(self, conn: _Connection, msg: dict[str, Any]) -> None:
        frame_id = msg.get("id")
        op = msg.get("op")
        try:
            if op == "bind":
                self._check_bind(conn, msg.get("token"))
                self._bound_conn = conn
                await self._send(conn, {"id": frame_id, "ok": True, "result": {}})
            elif op == "list_tools":
                self._require_bound(conn, op)
                tools, _reverse = build_wire_catalog(
                    self._list_infos(),
                    server_name=self._server_name,
                )
                await self._send(
                    conn,
                    {"id": frame_id, "ok": True, "result": {"tools": tools}},
                )
            elif op == "call_tool":
                self._require_bound(conn, op)
                await self._start_call(conn, frame_id, msg)
            else:
                _log.warning("bridge: unknown op %r refused", op)
                await self._send(
                    conn, {"id": frame_id, "ok": False, "error": _GENERIC_REFUSAL}
                )
        except PlatformError as exc:
            # Typed refusal (auth, not-bound, name collision, ...) —
            # full reason to the log, uniform generic to the wire.
            _log.warning("bridge: %s refused: %s (%s)", op, exc.code, exc.detail)
            await self._send(
                conn, {"id": frame_id, "ok": False, "error": _GENERIC_REFUSAL}
            )
        except Exception:  # noqa: BLE001 — trust boundary: never leak, never die
            _log.exception("bridge: %s failed", op)
            await self._send(
                conn, {"id": frame_id, "ok": False, "error": _GENERIC_REFUSAL}
            )

    # -- op handlers -----------------------------------------------------

    def _check_bind(self, conn: _Connection, token: object) -> None:
        """Validate a bind; ONE exception class for every refusal reason.

        The `reason` strings differ (server log only); the raised type
        and the resulting wire bytes are identical — no token oracle.
        """
        if self._closed or self._token is None:
            raise BridgeAuthFailed(reason="expired")
        if not isinstance(token, str) or token != self._token:
            raise BridgeAuthFailed(reason="unknown")
        if self._bound_conn is not None and self._bound_conn is not conn:
            raise BridgeAuthFailed(reason="already-bound")

    def _require_bound(self, conn: _Connection, op: object) -> None:
        if self._bound_conn is not conn:
            raise BridgeNotBound(op=str(op))

    def _list_infos(self) -> list[ToolInfo]:
        """The bound agent's live catalog — always read fresh.

        Recomputed per `list_tools` AND per `call_tool` so the reverse
        map can never go stale against the live subsystem (a stale map
        would be a routing/authz drift, not just a UX bug). Exactly
        `list_registered_tools(agent_id)` — virtual per-workflow tools
        appear only when the bound subsystem registered them for this
        agent; the bridge adds and removes nothing.
        """
        infos: list[ToolInfo] = list(
            self._tools_subsystem.list_registered_tools(self._agent_id)
        )
        return infos

    async def _start_call(
        self,
        conn: _Connection,
        frame_id: object,
        msg: dict[str, Any],
    ) -> None:
        name = msg.get("name")
        arguments = msg.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self._send(
                conn, {"id": frame_id, "ok": False, "error": _GENERIC_REFUSAL}
            )
            return
        _tools, reverse = build_wire_catalog(
            self._list_infos(),
            server_name=self._server_name,
        )
        bare_name = reverse.get(name)
        if bare_name is None:
            # Not in the bound catalog: no dispatch, no event — a
            # generic tool-level error, not a transport error.
            await self._send(
                conn,
                {
                    "id": frame_id,
                    "ok": True,
                    "result": {
                        "content": [
                            {"type": "text", "text": _GENERIC_UNKNOWN_TOOL_TEXT}
                        ],
                        "isError": True,
                    },
                },
            )
            return
        # Each call in its own task: dispatch binds/resets the
        # `permissions._BINDING` ContextVar per call internally
        # (`src/tools/dispatch.py:340/354`), and per-task isolation
        # keeps concurrent calls over one socket from sharing a
        # binding context.
        task = asyncio.create_task(
            self._run_call(conn, frame_id, bare_name, dict(arguments))
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_call(
        self,
        conn: _Connection,
        frame_id: object,
        bare_name: str,
        arguments: dict[str, Any],
    ) -> None:
        try:
            outcome = await invoke_observed(
                emit=self._emit,
                agent_id=self._agent_id,
                session_id=self._session_id,
                thread_id=self._thread_id,
                tools_subsystem=self._tools_subsystem,
                tool_name=bare_name,
                args=arguments,
                tool_call_id=None,
                cancel=self._cancel,
            )
        except asyncio.CancelledError:
            # Abandoned cleanly (server close / task cancel): the core
            # emitted no terminal and neither do we —
            # `session.interrupted` is engine-only. No response is
            # written; the closing connection is the signal.
            raise
        except Exception:  # noqa: BLE001 — trust boundary: pre/post-dispatch failures (e.g. the emit sink) must not leak or kill the loop
            _log.exception("bridge: call_tool %r failed outside dispatch", bare_name)
            await self._send(
                conn,
                {
                    "id": frame_id,
                    "ok": True,
                    "result": {
                        "content": [
                            {"type": "text", "text": _GENERIC_CALL_FAILED_TEXT}
                        ],
                        "isError": True,
                    },
                },
            )
            return
        await self._send(
            conn,
            {"id": frame_id, "ok": True, "result": outcome_to_mcp(outcome)},
        )

    # -- plumbing --------------------------------------------------------

    async def _send(self, conn: _Connection, payload: dict[str, Any]) -> None:
        """Write one frame; per-connection lock; disconnects are absorbed."""
        frame = encode_frame(payload)
        async with conn.write_lock:
            if conn.writer.is_closing():
                return
            try:
                conn.writer.write(frame)
                await conn.writer.drain()
            except (ConnectionError, OSError):
                # Peer went away mid-write — the frame loop will
                # observe the disconnect; nothing to leak or wedge.
                _log.debug("bridge: response dropped, peer disconnected")
