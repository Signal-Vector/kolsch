"""spec-120 Tests 2/3/4/6/7/8/9 — the MCP tool bridge end to end.

Drives a real `ToolBridgeServer` bound to a SYNTHETIC exchange
context: a stub `ToolsSubsystem` exposing known tools plus a REAL
`SessionStore`/bus/lock so the bridge emit sink appends for real.
The wire tests spawn `kolsch mcp bridge` as a REAL subprocess over
stdio and speak MCP JSON-RPC with a generic hand-rolled client — no
`claude` anywhere (that's T3).

Coverage split on purpose: the JSON-RPC surface (`BridgeStdioServer`)
is ALSO driven in-process (subprocess execution is invisible to
coverage), while the subprocess tests prove the real fd wiring,
env contract, exit codes, and stdout hygiene.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import struct
import sys
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bus import InProcessEventBus
from errors import (
    BridgeNameCollision,
    BridgeRequestRefused,
    BridgeSocketInsecure,
    BridgeSocketUnavailable,
    ToolUnauthorized,
)
from store import SessionStore, SqliteBackend
from tools.bridge import (
    MAX_FRAME_BYTES,
    ToolBridgeServer,
    build_wire_catalog,
    make_bridge_emit_sink,
    mcp_wire_name,
)
from tools.bridge.proxy import (
    LATEST_PROTOCOL_VERSION,
    BridgeStdioServer,
    _SocketClient,
)
from tools.bridge.server import _ensure_absent, _pick_base_dir, encode_frame
from tools.decorator import ToolInfo, ToolResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from schemas import Event

_TRACE = "tr_00000000-0000-0000-0000-0000000000ab"
_AGENT_ID = "ag_test"
_TIMEOUT = 20

# Markers that must NEVER appear in any externally-visible payload for
# a denied/failed call (Test 4's no-leak grep). `rules.yaml` and
# `who-chain` come from the juicy matched_rule the stub plants.
_LEAK_MARKERS = (
    "/perm",
    "recovery_hint",
    "matched_rule",
    "principal",
    "agent.",
    "where",
    "tool.unauthorized",
    "rules.yaml",
)


# ---------------------------------------------------------------------------
# The synthetic exchange context.
# ---------------------------------------------------------------------------


class _StubSubsystem:
    """Duck-typed `ToolsSubsystem` facade with three known tools.

    - ``echo``    → allowed; body runs and returns a dict result.
    - ``blocked`` → `ToolUnauthorized` from a deny rule (juicy
      `matched_rule` detail so a leak is detectable).
    - ``guarded`` → `ToolUnauthorized` whose matched rule has
      ``effect: prompt`` — the shape dispatch produces when a
      `prompt` effect structurally collapses to deny (dispatch's
      authz call passes no callback, ever — `tools/dispatch.py:277`).
    - ``slow``    → gated on an `asyncio.Event` for disconnect/cancel
      scenarios.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.bodies_run: list[str] = []
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    def list_registered_tools(self, agent_id: str) -> list[ToolInfo]:
        assert agent_id == _AGENT_ID
        schema = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "title": "EchoArgs",
        }
        empty = {"type": "object", "properties": {}, "title": "NoArgs"}
        return [
            ToolInfo(
                name="echo", description="Echo a message back.", args_schema=schema
            ),
            ToolInfo(name="blocked", description="Deny-ruled tool.", args_schema=empty),
            ToolInfo(
                name="guarded", description="Prompt-ruled tool.", args_schema=empty
            ),
            ToolInfo(name="slow", description="Gated tool.", args_schema=empty),
        ]

    async def invoke_tool(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        context: Any,
    ) -> ToolResult:
        self.calls.append(
            {"tool_name": tool_name, "args": args, "principal": context.principal}
        )
        if tool_name == "echo":
            self.bodies_run.append(tool_name)
            return ToolResult(result={"echoed": args.get("message")})
        if tool_name == "slow":
            self.started.set()
            await self.gate.wait()
            self.bodies_run.append(tool_name)
            return ToolResult(result={"done": True})
        # blocked / guarded: authz refusal BEFORE any body runs, with
        # deliberately juicy detail — if any of it reaches the wire,
        # Test 4's grep fails.
        effect = "prompt" if tool_name == "guarded" else "deny"
        raise ToolUnauthorized(
            tool_name=tool_name,
            action=f"kolsch/tools/{tool_name}",
            principal=context.principal,
            matched_rule={
                "what": f"kolsch/tools/{tool_name}",
                "pattern": "*",
                "effect": effect,
                "id": "rl_secret_rule",
                "source_file": "/secret/path/rules.yaml",
                "source_line": 42,
                "from_": "who-chain-user.local",
            },
        )


@dataclass
class _Bound:
    server: ToolBridgeServer
    store: SessionStore
    stub: _StubSubsystem
    thread_id: Any
    session_id: str
    socket_path: str
    token: str

    async def events(self) -> list[Event]:
        return [e async for e in self.store.read_path_to_leaf(self.thread_id)]


@contextlib.asynccontextmanager
async def _bound_server(tmp_path: Path) -> AsyncIterator[_Bound]:
    """A `ToolBridgeServer` bound to the synthetic exchange context."""
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session(_AGENT_ID, name="bridge", trace_id=_TRACE)
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    sink = make_bridge_emit_sink(
        store=store,
        bus=InProcessEventBus(),
        lock=asyncio.Lock(),
        session_id=str(session.session_id),
        thread_id=str(thread_id),
        trace_id=_TRACE,
    )
    stub = _StubSubsystem()
    server = ToolBridgeServer(
        agent_id=_AGENT_ID,
        session_id=str(session.session_id),
        thread_id=str(thread_id),
        tools_subsystem=stub,
        emit=sink,
    )
    await server.open()
    fragment = server.config_fragment()
    bound = _Bound(
        server=server,
        store=store,
        stub=stub,
        thread_id=thread_id,
        session_id=str(session.session_id),
        socket_path=fragment["socket_path"],
        token=fragment["token"],
    )
    try:
        yield bound
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# A generic MCP client over a real `kolsch mcp bridge` subprocess.
# ---------------------------------------------------------------------------


class _McpProc:
    """Minimal generic MCP stdio client — newline-delimited JSON-RPC."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self._ids = count(1)

    @classmethod
    async def spawn(cls, *, socket_path: str, token: str) -> _McpProc:
        env = dict(os.environ)
        env["KOLSCH_MCP_SOCKET"] = socket_path
        env["KOLSCH_MCP_TOKEN"] = token
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "cli",
            "mcp",
            "bridge",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=MAX_FRAME_BYTES,
        )
        return cls(proc)

    async def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj).encode() + b"\n")
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def request_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        """Send a request; return `(parsed response, raw wire bytes)`."""
        rid = next(self._ids)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)
        return await self.read_response(rid)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed, _raw = await self.request_raw(method, params)
        return parsed

    async def read_response(self, rid: int) -> tuple[dict[str, Any], bytes]:
        assert self.proc.stdout is not None
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=_TIMEOUT)
            assert line, "proxy closed stdout unexpectedly"
            parsed = json.loads(line)
            if parsed.get("id") == rid:
                return parsed, line

    async def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), timeout=_TIMEOUT)


# ---------------------------------------------------------------------------
# Test 2 — wire_handshake: real subprocess, full MCP handshake.
# ---------------------------------------------------------------------------


async def test_wire_handshake_full_round_trip(tmp_path: Path) -> None:
    """`initialize` (negotiated version), `notifications/initialized`
    no-reply, `ping` → `{}`, `tools/list` → the bound catalog with
    BARE names (spec-121 correction: the MCP client namespaces server
    tools itself, so the client-visible name lands on T2's intended
    `mcp__<server>__<bare>` exactly once) + object-rooted inputSchema."""
    async with _bound_server(tmp_path) as bound:
        client = await _McpProc.spawn(socket_path=bound.socket_path, token=bound.token)
        try:
            init = await client.request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "generic-test-client", "version": "1.0"},
                },
            )
            result = init["result"]
            # REAL negotiation: a supported requested version is echoed.
            assert result["protocolVersion"] == "2025-03-26"
            # v1 never sends tools/list_changed → must not advertise it.
            assert result["capabilities"]["tools"] == {}
            assert result["serverInfo"]["name"]
            assert "version" in result["serverInfo"]

            # Notification: accepted, no reply — proven by the NEXT
            # line on stdout being the ping's response, not anything
            # answering the notification.
            await client.notify("notifications/initialized")
            pong = await client.request("ping")
            assert pong["result"] == {}

            tools = (await client.request("tools/list"))["result"]["tools"]
            names = [t["name"] for t in tools]
            assert names == ["echo", "blocked", "guarded", "slow"]
            for tool in tools:
                assert tool["description"]
                assert tool["inputSchema"]["type"] == "object"
                # The socket-internal wire_to_bare rider never reaches
                # the MCP client.
                assert "wire_to_bare" not in tool
        finally:
            await client.close()


async def test_wire_handshake_version_mismatch(tmp_path: Path) -> None:
    """An unsupported requested `protocolVersion` is answered with the
    server's latest supported version — not an echo, not an error."""
    async with _bound_server(tmp_path) as bound:
        client = await _McpProc.spawn(socket_path=bound.socket_path, token=bound.token)
        try:
            init = await client.request("initialize", {"protocolVersion": "1990-01-01"})
            assert init["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Test 3 — allowed_call: via invoke_observed, events land, principal right.
# ---------------------------------------------------------------------------


async def test_allowed_call_runs_and_emits_engine_equivalent_events(
    tmp_path: Path,
) -> None:
    """An allowed `tools/call` (by the client-visible BARE name — the
    proxy translates back to the socket wire name, refreshing its map
    on a first-call miss) returns `isError: false` with the raw
    result serialized; `tool.called` → `tool.result` land in the REAL
    store with the bound actor + trace_id + parent chain; the dispatch
    saw principal `agent.<agent_id>`."""
    async with _bound_server(tmp_path) as bound:
        client = await _McpProc.spawn(socket_path=bound.socket_path, token=bound.token)
        try:
            await client.request("initialize", {"protocolVersion": "2025-03-26"})
            resp = await client.request(
                "tools/call",
                {
                    "name": "echo",
                    "arguments": {"message": "hi"},
                },
            )
            result = resp["result"]
            assert result["isError"] is False
            assert json.loads(result["content"][0]["text"]) == {"echoed": "hi"}
        finally:
            await client.close()

        # The tool ran, exactly once, under the bound principal — the
        # SAME principal string an in-engine call authorizes with.
        assert bound.stub.bodies_run == ["echo"]
        assert bound.stub.calls == [
            {
                "tool_name": "echo",
                "args": {"message": "hi"},
                "principal": f"agent.{_AGENT_ID}",
            }
        ]

        events = await bound.events()
        assert [e.type.value for e in events] == [
            "session.opened",
            "tool.called",
            "tool.result",
        ]
        called, result_evt = events[1], events[2]
        assert called.data["tool_name"] == "echo"
        assert called.data["arguments"] == {"message": "hi"}
        assert result_evt.data["result"] == {"echoed": "hi"}
        for evt in (called, result_evt):
            assert evt.actor.kind == "agent"
            assert evt.actor.id == _AGENT_ID
            assert evt.trace_id == _TRACE
        # Store-assigned ordering: strict parent chain + logical_seq.
        for i in range(1, len(events)):
            assert events[i].parent_event_id == events[i - 1].event_id
        assert [called.data["logical_seq"], result_evt.data["logical_seq"]] == [2, 3]


# ---------------------------------------------------------------------------
# Test 4 — denied_call: sanitized, no leak, prompt == deny on the wire.
# ---------------------------------------------------------------------------


async def test_denied_call_sanitized_no_leak(tmp_path: Path) -> None:
    """Deny-rule AND prompt-effect denials: `isError: true` with a
    constant generic text; no tool body runs; `tool.failed` lands in
    the store (ground truth keeps the full detail); the WIRE payload
    carries none of the internal markers and cannot distinguish
    deny from prompt. Deliberately calls with the FULL wire names —
    a pre-spec-121 client shape — proving the proxy's untranslated
    pass-through still routes."""
    async with _bound_server(tmp_path) as bound:
        client = await _McpProc.spawn(socket_path=bound.socket_path, token=bound.token)
        raw_by_tool: dict[str, bytes] = {}
        results: dict[str, dict[str, Any]] = {}
        try:
            await client.request("initialize", {"protocolVersion": "2025-03-26"})
            for bare in ("blocked", "guarded"):
                parsed, raw = await client.request_raw(
                    "tools/call",
                    {"name": mcp_wire_name("kolsch", bare), "arguments": {}},
                )
                raw_by_tool[bare] = raw
                results[bare] = parsed["result"]
        finally:
            await client.close()

        for bare in ("blocked", "guarded"):
            result = results[bare]
            assert result["isError"] is True
            assert result["content"][0]["type"] == "text"
            # ALLOWLIST no-leak: grep the raw wire bytes.
            wire_text = raw_by_tool[bare].decode()
            for marker in _LEAK_MARKERS:
                assert marker not in wire_text, (
                    f"leak marker {marker!r} on the wire for {bare!r}: {wire_text}"
                )
        # No allow/deny/prompt oracle: both refusals are byte-identical
        # apart from the JSON-RPC id.
        assert results["blocked"] == results["guarded"]

        # No tool body ran; dispatch was reached (authz is dispatch's
        # first act), and each refusal emitted tool.failed.
        assert bound.stub.bodies_run == []
        events = await bound.events()
        assert [e.type.value for e in events] == [
            "session.opened",
            "tool.called",
            "tool.failed",
            "tool.called",
            "tool.failed",
        ]
        # Ground truth is NOT sanitized: the JSONL keeps the full
        # denial detail (rule id, principal, hint) for the operator.
        failed = [e for e in events if e.type.value == "tool.failed"]
        for evt in failed:
            assert evt.data["error"]["detail"]["matched_rule"]["id"] == (
                "rl_secret_rule"
            )
            assert evt.data["error"]["detail"]["principal"] == f"agent.{_AGENT_ID}"


# ---------------------------------------------------------------------------
# Test 6 — protocol_errors (driven in-process for coverage).
# ---------------------------------------------------------------------------


async def _drive_stdio(bound: _Bound, lines: list[bytes]) -> list[dict[str, Any]]:
    """Run the REAL `BridgeStdioServer` loop over injected stdio lines."""
    client = await _SocketClient.connect(bound.socket_path)
    await client.request("bind", token=bound.token)
    server = BridgeStdioServer(client)
    feed = iter([*lines, b""])
    written: list[dict[str, Any]] = []

    async def read_line() -> bytes:
        return next(feed)

    async def write_message(msg: dict[str, Any]) -> None:
        written.append(msg)

    try:
        await server.serve(read_line, write_message)
    finally:
        await client.close()
    return written


def _rpc(rid: int, method: str, params: Any = None) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


async def test_protocol_errors_unknown_method_and_bad_params(
    tmp_path: Path,
) -> None:
    """Unknown method → `-32601`; malformed `tools/call` params →
    `-32602`; a parse-error line → `-32700`. All constant messages."""
    async with _bound_server(tmp_path) as bound:
        written = await _drive_stdio(
            bound,
            [
                _rpc(1, "resources/list"),
                _rpc(2, "tools/call", ["not", "a", "dict"]),
                _rpc(3, "tools/call", {"arguments": {}}),  # no name
                _rpc(4, "tools/call", {"name": "x", "arguments": "nope"}),
                b"this is not json",
            ],
        )
        by_id = {m.get("id"): m for m in written}
        assert by_id[1]["error"]["code"] == -32601
        assert by_id[2]["error"]["code"] == -32602
        assert by_id[3]["error"]["code"] == -32602
        assert by_id[4]["error"]["code"] == -32602
        assert by_id[None]["error"]["code"] == -32700
        # Nothing reached dispatch; nothing was emitted.
        assert bound.stub.calls == []
        assert [e.type.value for e in await bound.events()] == ["session.opened"]


async def test_protocol_errors_unknown_tool_no_dispatch_no_event(
    tmp_path: Path,
) -> None:
    """A `tools/call` for a tool outside the bound catalog: generic
    `isError: true`, NO dispatch, NO event."""
    async with _bound_server(tmp_path) as bound:
        written = await _drive_stdio(
            bound,
            [
                _rpc(
                    1,
                    "tools/call",
                    {"name": mcp_wire_name("kolsch", "nope"), "arguments": {}},
                )
            ],
        )
        (resp,) = written
        assert resp["result"]["isError"] is True
        wire_text = json.dumps(resp)
        for marker in _LEAK_MARKERS:
            assert marker not in wire_text
        assert bound.stub.calls == []
        assert [e.type.value for e in await bound.events()] == ["session.opened"]


async def test_in_process_initialize_and_list_mirror_subprocess(
    tmp_path: Path,
) -> None:
    """The in-process loop (coverage twin of Test 2): negotiation for
    supported + unsupported versions, ping, and tools/list."""
    async with _bound_server(tmp_path) as bound:
        written = await _drive_stdio(
            bound,
            [
                _rpc(1, "initialize", {"protocolVersion": "2024-11-05"}),
                _rpc(2, "initialize", {"protocolVersion": "not-a-version"}),
                _rpc(3, "ping"),
                _rpc(4, "tools/list"),
            ],
        )
        by_id = {m["id"]: m for m in written}
        assert by_id[1]["result"]["protocolVersion"] == "2024-11-05"
        assert by_id[2]["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
        assert by_id[3]["result"] == {}
        names = [t["name"] for t in by_id[4]["result"]["tools"]]
        # Client-visible names are BARE (spec-121 correction).
        assert names[0] == "echo"


# ---------------------------------------------------------------------------
# Wire-name contract — collision detection.
# ---------------------------------------------------------------------------


def test_wire_catalog_reverse_map_and_collision() -> None:
    """`build_wire_catalog` freezes the `mcp__<server>__<bare>` format
    + reverse map, and a duplicate wire name fails loudly."""
    infos = [
        ToolInfo(name="alpha", description="a", args_schema={"type": "object"}),
        ToolInfo(name="beta", description="b", args_schema={"type": "object"}),
    ]
    tools, reverse = build_wire_catalog(infos, server_name="kolsch")
    assert [t["name"] for t in tools] == ["mcp__kolsch__alpha", "mcp__kolsch__beta"]
    assert reverse == {
        "mcp__kolsch__alpha": "alpha",
        "mcp__kolsch__beta": "beta",
    }
    with pytest.raises(BridgeNameCollision):
        build_wire_catalog([*infos, infos[0]], server_name="kolsch")


async def test_wire_name_collision_refused_generically_on_wire(
    tmp_path: Path,
) -> None:
    """A colliding catalog surfaces as a uniform generic refusal over
    the socket — the collision detail stays server-side."""
    async with _bound_server(tmp_path) as bound:
        infos = bound.stub.list_registered_tools(_AGENT_ID)
        bound.stub.list_registered_tools = (  # type: ignore[method-assign]
            lambda agent_id: [*infos, infos[0]]
        )
        client = await _SocketClient.connect(bound.socket_path)
        try:
            await client.request("bind", token=bound.token)
            with pytest.raises(BridgeRequestRefused):
                await client.request("list_tools")
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Test 7 — bad_token: uniform failure, no oracle.
# ---------------------------------------------------------------------------


async def test_bad_token_uniform_failure(tmp_path: Path) -> None:
    """Unknown / already-bound / expired tokens: the proxy exits
    non-zero with IDENTICAL wire behaviour (empty stdout, one constant
    stderr line, same exit code) — no oracle distinguishes them."""
    outcomes: list[tuple[int | None, bytes, bytes]] = []

    async with _bound_server(tmp_path) as bound:
        # (a) unknown token.
        proc = await _McpProc.spawn(socket_path=bound.socket_path, token="wrong")
        stdout, stderr = await asyncio.wait_for(
            proc.proc.communicate(), timeout=_TIMEOUT
        )
        outcomes.append((proc.proc.returncode, stdout, stderr))

        # (b) already-bound: a healthy client holds the binding...
        holder = await _McpProc.spawn(socket_path=bound.socket_path, token=bound.token)
        try:
            await holder.request("initialize", {"protocolVersion": "2025-03-26"})
            # ...so a second bind with the CORRECT token is refused.
            second = await _McpProc.spawn(
                socket_path=bound.socket_path, token=bound.token
            )
            stdout, stderr = await asyncio.wait_for(
                second.proc.communicate(), timeout=_TIMEOUT
            )
            outcomes.append((second.proc.returncode, stdout, stderr))
        finally:
            await holder.close()

        socket_path, token = bound.socket_path, bound.token

    # (c) expired: the exchange closed — token revoked, socket gone.
    proc = await _McpProc.spawn(socket_path=socket_path, token=token)
    stdout, stderr = await asyncio.wait_for(proc.proc.communicate(), timeout=_TIMEOUT)
    outcomes.append((proc.proc.returncode, stdout, stderr))

    for returncode, out, err in outcomes:
        assert returncode == 1
        assert out == b""  # initialize never answered; no tools exposed
        assert b"refused" in err
    # Uniformity: all three failures are byte-identical.
    assert len(set(outcomes)) == 1


# ---------------------------------------------------------------------------
# Test 8 — socket_security + per-exchange lifecycle.
# ---------------------------------------------------------------------------


async def test_socket_security_and_lifecycle(tmp_path: Path) -> None:
    """Dir 0700 + geteuid-owned; socket 0600, randomized name;
    reconnect-within-exchange allowed; `close()` removes socket + dir
    and revokes the token (a later bind fails)."""
    async with _bound_server(tmp_path / "a") as bound:
        sock = Path(bound.socket_path)
        parent = sock.parent
        dir_stat = parent.lstat()
        assert stat.S_ISDIR(dir_stat.st_mode)
        assert stat.S_IMODE(dir_stat.st_mode) == 0o700
        assert dir_stat.st_uid == os.geteuid()
        sock_stat = sock.lstat()
        assert stat.S_ISSOCK(sock_stat.st_mode)
        assert stat.S_IMODE(sock_stat.st_mode) == 0o600
        assert sock_stat.st_uid == os.geteuid()
        assert set(bound.server.config_fragment()) == {"socket_path", "token"}

        # Randomized names: a second server never reuses a path.
        async with _bound_server(tmp_path / "b") as other:
            assert other.socket_path != bound.socket_path
            assert Path(other.socket_path).name != sock.name

        # Reconnect within the exchange: drop the bound connection,
        # bind again with the SAME token.
        first = await _SocketClient.connect(bound.socket_path)
        await first.request("bind", token=bound.token)
        await first.close()
        second = await _SocketClient.connect(bound.socket_path)
        try:
            for attempt in range(40):
                try:
                    await second.request("bind", token=bound.token)
                    break
                except BridgeRequestRefused:
                    # The server may not have observed the first
                    # connection's EOF yet.
                    assert attempt < 39, "reconnect-bind never accepted"
                    await asyncio.sleep(0.05)
        finally:
            await second.close()

    # close() ran (context exit): socket + dir removed, token revoked.
    assert not sock.exists()
    assert not parent.exists()
    with pytest.raises(BridgeSocketUnavailable):
        await _SocketClient.connect(str(sock))


async def test_close_abandons_inflight_without_interrupted_terminal(
    tmp_path: Path,
) -> None:
    """`close()` during an in-flight `call_tool`: the per-call task is
    cancelled cleanly and NO `session.interrupted` terminal is written
    (that terminal is engine-only)."""
    async with _bound_server(tmp_path) as bound:
        client = await _SocketClient.connect(bound.socket_path)
        await client.request("bind", token=bound.token)
        call = asyncio.create_task(
            client.request(
                "call_tool",
                name=mcp_wire_name("kolsch", "slow"),
                arguments={},
            )
        )
        await asyncio.wait_for(bound.stub.started.wait(), timeout=_TIMEOUT)
        await bound.server.close()
        with pytest.raises((BridgeRequestRefused, BridgeSocketUnavailable)):
            await asyncio.wait_for(call, timeout=_TIMEOUT)
        await client.close()

        events = await bound.events()
        types = [e.type.value for e in events]
        assert "session.interrupted" not in types
        # The call was abandoned mid-body: tool.called with no terminal
        # for it (the engine-side drain owns interruption terminals).
        assert types == ["session.opened", "tool.called"]
        assert bound.stub.bodies_run == []


def test_base_dir_and_prebind_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_pick_base_dir` honours a sane `$XDG_RUNTIME_DIR` and falls
    back otherwise; `_ensure_absent` rejects any pre-existing node
    (symlink included) at the socket path."""
    owned = tmp_path / "runtime"
    owned.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(owned))
    assert _pick_base_dir() == owned
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    assert _pick_base_dir() != tmp_path / "missing"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert _pick_base_dir().exists()

    target = tmp_path / "existing.sock"
    target.write_text("not a socket")
    with pytest.raises(BridgeSocketInsecure):
        _ensure_absent(target)
    link = tmp_path / "sneaky.sock"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BridgeSocketInsecure):
        _ensure_absent(link)


# ---------------------------------------------------------------------------
# Test 9 — framing: split frames, oversized, disconnect, double-bind.
# ---------------------------------------------------------------------------


async def test_framing_split_frame_reassembles(tmp_path: Path) -> None:
    """A length-prefixed frame split across two writes reassembles."""
    async with _bound_server(tmp_path) as bound:
        reader, writer = await asyncio.open_unix_connection(bound.socket_path)
        try:
            frame = encode_frame({"id": 1, "op": "bind", "token": bound.token})
            writer.write(frame[:3])
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(frame[3:])
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(4), timeout=_TIMEOUT)
            (length,) = struct.unpack(">I", header)
            body = json.loads(await reader.readexactly(length))
            assert body == {"id": 1, "ok": True, "result": {}}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def test_framing_oversized_rejected_before_body(tmp_path: Path) -> None:
    """An oversized DECLARED length is refused without reading a body
    (no OOM), the connection drops, and the server stays healthy."""
    async with _bound_server(tmp_path) as bound:
        reader, writer = await asyncio.open_unix_connection(bound.socket_path)
        try:
            writer.write(struct.pack(">I", MAX_FRAME_BYTES + 1))
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(4), timeout=_TIMEOUT)
            (length,) = struct.unpack(">I", header)
            refusal = json.loads(await reader.readexactly(length))
            assert refusal["ok"] is False
            assert refusal["id"] is None
            # The server cannot resync — it closes this connection.
            assert await asyncio.wait_for(reader.read(1), timeout=_TIMEOUT) == b""
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        # Server alive and serving: a fresh connection binds fine.
        client = await _SocketClient.connect(bound.socket_path)
        try:
            assert await client.request("bind", token=bound.token) == {}
        finally:
            await client.close()


async def test_framing_malformed_body_keeps_connection(tmp_path: Path) -> None:
    """A well-framed but non-object body gets a generic refusal and the
    connection keeps serving (framing is still in sync)."""
    async with _bound_server(tmp_path) as bound:
        reader, writer = await asyncio.open_unix_connection(bound.socket_path)
        try:
            bad = b"[1, 2, 3]"
            writer.write(struct.pack(">I", len(bad)) + bad)
            writer.write(encode_frame({"id": 7, "op": "bind", "token": bound.token}))
            await writer.drain()
            replies = []
            for _ in range(2):
                header = await asyncio.wait_for(reader.readexactly(4), timeout=_TIMEOUT)
                (length,) = struct.unpack(">I", header)
                replies.append(json.loads(await reader.readexactly(length)))
            assert replies[0]["ok"] is False
            assert replies[1] == {"id": 7, "ok": True, "result": {}}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def test_framing_disconnect_mid_call_does_not_wedge(tmp_path: Path) -> None:
    """A client that disconnects mid-`tools/call`: the in-flight call
    completes against the store (events stay honest), no socket leaks,
    and the server keeps serving new connections."""
    async with _bound_server(tmp_path) as bound:
        client = await _SocketClient.connect(bound.socket_path)
        call = None
        try:
            await client.request("bind", token=bound.token)
            call = asyncio.create_task(
                client.request(
                    "call_tool",
                    name=mcp_wire_name("kolsch", "slow"),
                    arguments={},
                )
            )
            await asyncio.wait_for(bound.stub.started.wait(), timeout=_TIMEOUT)
        finally:
            await client.close()  # disconnect while the call is running
        if call is not None:
            with pytest.raises(BridgeSocketUnavailable):
                await asyncio.wait_for(call, timeout=_TIMEOUT)

        # Release the tool; the abandoned call runs to completion so
        # the JSONL stays terminal-complete (called → result).
        bound.stub.gate.set()
        for _ in range(100):
            events = await bound.events()
            if [e.type.value for e in events][-1] == "tool.result":
                break
            await asyncio.sleep(0.05)
        assert [e.type.value for e in await bound.events()] == [
            "session.opened",
            "tool.called",
            "tool.result",
        ]

        # Not wedged: a fresh connection binds and lists.
        fresh = await _SocketClient.connect(bound.socket_path)
        try:
            for attempt in range(40):
                try:
                    await fresh.request("bind", token=bound.token)
                    break
                except BridgeRequestRefused:
                    assert attempt < 39, "rebind never accepted"
                    await asyncio.sleep(0.05)
            tools = await fresh.request("list_tools")
            assert len(tools["tools"]) == 4
        finally:
            await fresh.close()


async def test_run_bridge_stdio_env_and_refusal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In-process twin of the subprocess exit-code contract (coverage):
    missing env → 2 without booting; unreachable socket and refused
    bind → 1 with the one constant stderr line."""
    from tools.bridge import run_bridge_stdio

    monkeypatch.delenv("KOLSCH_MCP_SOCKET", raising=False)
    monkeypatch.delenv("KOLSCH_MCP_TOKEN", raising=False)
    assert await run_bridge_stdio() == 2
    assert "KOLSCH_MCP_SOCKET" in capsys.readouterr().err

    monkeypatch.setenv("KOLSCH_MCP_SOCKET", str(tmp_path / "gone.sock"))
    monkeypatch.setenv("KOLSCH_MCP_TOKEN", "t")
    assert await run_bridge_stdio() == 1
    missing_socket_err = capsys.readouterr().err

    async with _bound_server(tmp_path) as bound:
        monkeypatch.setenv("KOLSCH_MCP_SOCKET", bound.socket_path)
        monkeypatch.setenv("KOLSCH_MCP_TOKEN", "wrong-token")
        assert await run_bridge_stdio() == 1
        refused_err = capsys.readouterr().err
    # Uniform: a dead socket and a refused token print the same line.
    assert refused_err == missing_socket_err


# ---------------------------------------------------------------------------
# spec-121 Test 6 — decision-#2 denylist: workflow control never crosses
# the bridge.
# ---------------------------------------------------------------------------


def _with_workflow_tools(bound: _Bound) -> None:
    """Extend the stub catalog with the two denylisted workflow tools."""
    infos = bound.stub.list_registered_tools(_AGENT_ID)
    empty = {"type": "object", "properties": {}, "title": "NoArgs"}
    extended = [
        *infos,
        ToolInfo(name="run_workflow", description="Run a workflow.", args_schema=empty),
        ToolInfo(
            name="cancel_workflow", description="Cancel a workflow.", args_schema=empty
        ),
    ]
    bound.stub.list_registered_tools = (  # type: ignore[method-assign]
        lambda agent_id: list(extended)
    )


async def test_denylist_absent_from_list_and_wire_names(tmp_path: Path) -> None:
    """`run_workflow`/`cancel_workflow` registered on the subsystem are
    absent from `tools/list` AND from `wire_tool_names()` (the
    `--allowedTools` feed) — one filter in `_list_infos` covers both."""
    async with _bound_server(tmp_path) as bound:
        _with_workflow_tools(bound)
        client = await _SocketClient.connect(bound.socket_path)
        try:
            await client.request("bind", token=bound.token)
            listed = await client.request("list_tools")
        finally:
            await client.close()
        names = [t["name"] for t in listed["tools"]]
        assert mcp_wire_name("kolsch", "run_workflow") not in names
        assert mcp_wire_name("kolsch", "cancel_workflow") not in names
        # Plain tools still list.
        assert mcp_wire_name("kolsch", "echo") in names
        # The framework's --allowedTools feed sees the same filter.
        assert bound.server.wire_tool_names() == names


async def test_denylist_direct_call_hits_unknown_tool_arm(tmp_path: Path) -> None:
    """A direct `call_tool` for a denylisted tool: generic unknown-tool
    error, NO dispatch, NO event — indistinguishable from a tool that
    was never registered. A plain tool still dispatches."""
    async with _bound_server(tmp_path) as bound:
        _with_workflow_tools(bound)
        client = await _SocketClient.connect(bound.socket_path)
        try:
            await client.request("bind", token=bound.token)
            for bare in ("run_workflow", "cancel_workflow"):
                result = await client.request(
                    "call_tool",
                    name=mcp_wire_name("kolsch", bare),
                    arguments={},
                )
                assert result["isError"] is True
                assert result["content"][0]["text"] == (
                    "Unknown tool for this kolsch bridge."
                )
            # No dispatch was reached, no event emitted for either.
            assert bound.stub.calls == []
            assert [e.type.value for e in await bound.events()] == ["session.opened"]

            # A plain tool on the SAME connection still round-trips.
            ok = await client.request(
                "call_tool",
                name=mcp_wire_name("kolsch", "echo"),
                arguments={"message": "still works"},
            )
            assert ok["isError"] is False
        finally:
            await client.close()
        assert bound.stub.bodies_run == ["echo"]
        assert [e.type.value for e in await bound.events()] == [
            "session.opened",
            "tool.called",
            "tool.result",
        ]


async def test_framing_second_concurrent_bind_rejected(tmp_path: Path) -> None:
    """While one connection holds the binding, a second concurrent
    `bind` (same, correct token) is refused."""
    async with _bound_server(tmp_path) as bound:
        first = await _SocketClient.connect(bound.socket_path)
        second = await _SocketClient.connect(bound.socket_path)
        try:
            await first.request("bind", token=bound.token)
            with pytest.raises(BridgeRequestRefused):
                await second.request("bind", token=bound.token)
            # The first binding is unaffected.
            assert await first.request("list_tools")
        finally:
            await second.close()
            await first.close()
