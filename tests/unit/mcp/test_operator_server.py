"""Unit tests for the MCP operator server (spec-105).

These tests drive the server IN-PROCESS via
`mcp.shared.memory.create_connected_server_and_client_session` — no
OS subprocess, no stdio pipes. Keeps the CI gate stable (no timing,
no port binding, no subprocess spawn). The real-`claude` end-to-end
round-trip lives at `tests/e2e/test_mcp_claude_roundtrip.py` under
`pytestmark = pytest.mark.e2e` so `pytest -m "not e2e"` deselects it.

Coverage map:
- Test 1 lives in `tests/component/test_cli_smoke.py` (CLI smoke).
- Test 2 (handshake + tools/list) — `test_handshake_...`.
- Test 3 (tools/call parity + from_workflow_run exclusion) — `test_parity_...`.
- Test 3b (error mapping) — `test_error_mapping_...`.
- Test 4 (no serialization helper module) — `test_no_serialization_module`.
- Test 5 (posture / stdout hygiene / no listener) — `test_posture_...`.
- Test 6 (concurrency: writer + reader) — `test_concurrency_...`.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from mcp_server.server import build_server
from mcp_server.views import session_view
from platform_runtime import bootstrap_runtime
from schemas import Session
from schemas.identifiers import AgentId, SessionId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from platform_runtime import PlatformRuntime
    from store import SessionStore


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


async def _seed_sessions_with_workflow_run_field(
    store: SessionStore,
) -> list[Session]:
    """Seed a fixture set covering the starred/archived/from_workflow_run axes.

    Returns the fresh Session list (post-insert). At least one session
    MUST carry a non-None `from_workflow_run` so Test 3 actively
    verifies the exclusion (otherwise the assertion passes silently
    on the "empty means dropped" path).
    """
    # Bypass `store.create_session` (which sets from_workflow_run only
    # when the caller passes a FromWorkflowRun instance) and go straight
    # to the backend so we can plant the field. The 3 sessions cover
    # (starred=T, arch=F, wfr=None), (starred=F, arch=T, wfr=None), and
    # (starred=F, arch=F, wfr=SET) — every filter combination exercised.
    from datetime import UTC, datetime
    from uuid import uuid4

    from schemas.entities import FromWorkflowRun, Thread
    from schemas.identifiers import (
        EventId,
        ThreadId,
    )
    from workflows.ids import InvocationId, WorkflowRunId

    now = datetime.now(UTC)
    seeded: list[Session] = []

    for idx, (starred, archived, has_wfr) in enumerate(
        [(True, False, False), (False, True, False), (False, False, True)],
    ):
        session_id = SessionId(f"ses_{uuid4()}")
        thread_id = ThreadId(f"th_{uuid4()}")
        agent_id = AgentId(f"ag_test_{idx}")

        wfr: FromWorkflowRun | None = None
        if has_wfr:
            wfr = FromWorkflowRun(
                workflow_run_id=WorkflowRunId(f"wrk_{uuid4().hex}"),
                invocation_id=InvocationId(f"inv_{uuid4().hex}"),
                event_id=EventId(f"ev_{uuid4()}"),
            )

        session = Session(
            session_id=session_id,
            agent_id=agent_id,
            name=f"fixture-{idx}",
            starred=starred,
            archived=archived,
            created_at=now,
            updated_at=now,
            from_workflow_run=wfr,
        )
        # Insert both the session and its thread via the backend so
        # the store's downstream reads are consistent.
        await store._backend.insert_session(session)
        thread = Thread(
            thread_id=thread_id,
            session_id=session_id,
            agent_id=agent_id,
            created_at=now,
        )
        await store._backend.insert_thread(thread)
        seeded.append(session)

    return seeded


# ---------------------------------------------------------------------------
# Test 2 — handshake + tools/list (wire contract, gate-safe)
# ---------------------------------------------------------------------------


class _StubRuntime:
    """Minimal `PlatformRuntime` stand-in for handshake tests.

    Handshake/tools-list never touches the store, so a stub avoids the
    tmp-workspace bootstrap for the wire-contract tests.
    """

    def __init__(self) -> None:
        class _Store:
            async def list_sessions(self, **_kw: object) -> list[Session]:
                return []

        self.store = _Store()


@pytest.mark.asyncio
async def test_handshake_returns_kolsch_server_info() -> None:
    """Test 2: `initialize` returns a spec-compliant `InitializeResult`.

    The in-memory helper auto-initialises, so we call
    `ClientSession.initialize()` explicitly here to obtain the
    `InitializeResult` value (the helper's initialise is a no-op
    against an already-initialised session).
    """
    server = build_server(_StubRuntime())

    async with create_connected_server_and_client_session(server) as client:
        result = await client.initialize()

        # AC #3 — serverInfo.name is what Claude Code keys on; the
        # version comes from the MCP SDK by design (see spec §
        # "serverInfo.version — do NOT plumb kolsch's version").
        assert result.serverInfo.name == "kolsch"
        assert result.capabilities.tools is not None
        # Do NOT advertise listChanged — kolsch's tool set is static
        # for the lifetime of the process.
        assert result.capabilities.tools.listChanged in (None, False)


@pytest.mark.asyncio
async def test_handshake_tools_list_returns_only_list_sessions() -> None:
    """Test 2 (cont.): `tools/list` returns exactly `list_sessions`
    with a flat object-rooted inputSchema."""
    server = build_server(_StubRuntime())

    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()

        assert len(tools.tools) == 1
        tool = tools.tools[0]
        assert tool.name == "list_sessions"

        schema = tool.inputSchema
        assert schema["type"] == "object"
        # Flat top-level properties — NOT wrapped under `params`
        # (that would nest under a required object and break the
        # LLM's ability to synthesize call arguments).
        assert set(schema["properties"].keys()) == {"starred", "archived", "limit"}
        # Nothing is required — every param has a default of `None`.
        assert schema.get("required", []) == []


# ---------------------------------------------------------------------------
# Test 3 — tools/call parity with GET /sessions (+ from_workflow_run drop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parity_tools_call_matches_session_view_projection(
    tmp_path: Path,
) -> None:
    """Test 3: each returned block round-trips via `_SessionView`.

    Assertions per call:
      1. The spy captured the exact kwargs (proves arg passing).
      2. Response is `isError: false` with N `TextContent` blocks.
      3. `json.loads(block.text) == session_view(session)` for each
         returned session — the parity ratchet.
      4. `"from_workflow_run" not in parsed` even for the session
         that HAS `from_workflow_run` populated (exclusion is
         actively verified, not silently passed on the drift-when-
         empty path).
    """
    runtime = await bootstrap_runtime(root=tmp_path)
    sessions = await _seed_sessions_with_workflow_run_field(runtime.store)

    captured: dict[str, object] = {}
    real_list = runtime.store.list_sessions

    async def _spy(**kwargs: object) -> list[Session]:
        captured.clear()
        captured.update(kwargs)
        # Cast for the runtime call (types match the store signature).
        return await real_list(**kwargs)

    runtime.store.list_sessions = _spy  # type: ignore[method-assign]

    server = build_server(runtime)

    async def _drive_call(
        args: dict[str, object],
        expected_kwargs: dict[str, object],
    ) -> None:
        async with create_connected_server_and_client_session(server) as client:
            resp = await client.call_tool("list_sessions", args)

            # Assertion 1 — spy captured the exact kwargs.
            assert captured == expected_kwargs, (
                f"spy captured {captured!r}; expected {expected_kwargs!r}"
            )

            # Assertion 2 — success shape.
            assert resp.isError is False

            # Filter fixture list to match the args for expected N.
            expected_sessions = [
                s
                for s in sessions
                if (
                    (args.get("starred") is None or s.starred == args["starred"])
                    and (args.get("archived") is None or s.archived == args["archived"])
                )
            ]
            assert len(resp.content) == len(expected_sessions), (
                f"got {len(resp.content)} blocks; expected "
                f"{len(expected_sessions)} for args={args!r}"
            )

            # Assertion 3+4 — each block matches session_view AND
            # from_workflow_run is absent.
            for block in resp.content:
                assert isinstance(block, TextContent)
                parsed = json.loads(block.text)
                assert "from_workflow_run" not in parsed, (
                    f"from_workflow_run leaked into MCP response — parsed={parsed!r}"
                )
                # Find the matching seeded Session and compare.
                match = next(
                    (s for s in sessions if str(s.session_id) == parsed["session_id"]),
                    None,
                )
                assert match is not None, f"unknown session id in response: {parsed!r}"
                assert parsed == session_view(match), (
                    "content-block dict does not equal session_view(...) — "
                    f"parsed={parsed!r}, expected={session_view(match)!r}"
                )

    # Three call shapes — every filter combination.
    await _drive_call(
        {},
        {"starred": None, "archived": None, "limit": None},
    )
    await _drive_call(
        {"starred": True},
        {"starred": True, "archived": None, "limit": None},
    )
    await _drive_call(
        {"archived": True, "limit": 10},
        {"starred": None, "archived": True, "limit": 10},
    )


# ---------------------------------------------------------------------------
# Test 3b — error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_mapping_sanitizes_exception_message(
    tmp_path: Path,
) -> None:
    """Test 3b: spy'd store raise → `isError: true` with sanitized text.

    The raw exception message ("bad limit: -5") MUST NOT appear in the
    response — FastMCP's default exception wrap would leak it via
    `str(exc)`; we override that via a try/except in the tool body.
    """
    runtime = await bootstrap_runtime(root=tmp_path)

    async def _raising_list_sessions(**_kwargs: object) -> list[Session]:
        msg = "bad limit: -5"
        raise ValueError(msg)

    runtime.store.list_sessions = _raising_list_sessions  # type: ignore[method-assign]

    server = build_server(runtime)

    async with create_connected_server_and_client_session(server) as client:
        resp = await client.call_tool("list_sessions", {"limit": -5})

    assert resp.isError is True
    assert len(resp.content) == 1
    block = resp.content[0]
    assert isinstance(block, TextContent)
    assert block.text == "list_sessions failed: ValueError"
    # Belt-and-braces: the raw message text and its fragments must
    # NOT appear in the response.
    assert "bad limit" not in block.text
    assert "-5" not in block.text


# ---------------------------------------------------------------------------
# Test 4 — no custom serialization module (grep guard)
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    # Walk up from this test file to the repo root
    # (kolsch/tests/unit/mcp/test_operator_server.py → 4 levels up).
    return Path(__file__).resolve().parents[3]


def test_no_serialization_module_in_mcp_package() -> None:
    """Test 4: no `serialization.py` in the MCP package.

    Spec-105's 2026-07-29 leaner revision dropped the custom helper.
    FastMCP's built-in `_convert_to_content` handles the mapping; a
    hand-rolled helper duplicates it with drift risk. This grep-guard
    catches a future Coder resurrecting the module.
    """
    pkg = _repo_root() / "src" / "mcp_server"
    assert not (pkg / "serialization.py").exists(), (
        "src/mcp_server/serialization.py should not exist — "
        "see spec-105 §2026-07-29 revision."
    )

    # Belt-and-braces: also grep for symbol names the removed helper
    # would have exported.
    result = subprocess.run(
        ["git", "grep", "-l", "-E", r"def (serialize|to_content)", "src/mcp_server/"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", (
        "found a serialize/to_content helper in src/mcp_server/ — "
        f"see grep output:\n{result.stdout}"
    )


def test_tool_decorator_carries_structured_output_false() -> None:
    """Test 4 (cont.): `list_sessions` was registered with
    `structured_output=False`.

    Without it, FastMCP auto-derives an `outputSchema` from the
    `list[TextContent]` return type. That path wraps the result in
    `{"result": ...}` and re-emits serialized content — double the
    payload plus jsonschema validation errors.
    """
    server = build_server(_StubRuntime())

    tools = server._tool_manager.list_tools()
    assert len(tools) == 1
    tool = tools[0]
    # `structured_output=False` translates to `output_schema is None`
    # on the ToolManager's `Tool` record (see FastMCP
    # `Tool.from_function` — the auto-derivation is skipped).
    assert tool.output_schema is None, (
        "`list_sessions` tool has an outputSchema — the "
        "`structured_output=False` decorator arg was dropped."
    )


# ---------------------------------------------------------------------------
# Test 5 — posture: no listener / stdout hygiene / read-only / doc allowlist
# ---------------------------------------------------------------------------


def test_posture_only_list_sessions_registered() -> None:
    """Test 5 (cont.): no mutate handler is registered."""
    server = build_server(_StubRuntime())
    tools = list(server._tool_manager.list_tools())
    assert len(tools) == 1
    assert tools[0].name == "list_sessions"


def test_posture_build_server_opens_no_listener() -> None:
    """Test 5: building the server binds no TCP/Unix listener.

    The MCP operator server is stdio-only — its trust boundary is
    "whoever spawned the process". A leaked listener would introduce
    a network trust boundary that the docs do not describe.
    """
    before = {c.laddr for c in psutil.Process().net_connections(kind="inet")}
    server = build_server(_StubRuntime())
    # Reference the server so the local isn't optimized away.
    assert server is not None
    after = {c.laddr for c in psutil.Process().net_connections(kind="inet")}
    assert before == after, (
        f"build_server() opened a network listener — new conns: {after - before}"
    )


@pytest.mark.asyncio
async def test_posture_stdout_hygiene_hard_guard(
    tmp_path: Path,
) -> None:
    """Test 5: HARD GUARD — stray `print(...)` never reaches stdout when
    the `contextlib.redirect_stdout(sys.stderr)` guard is active.

    The in-memory client/server pairing does NOT bind stdio, so a
    "the tool body prints, does JSON-RPC framing survive?" test would
    be a no-op. Instead this test proves the two moving parts:

    1. `run_stdio` in production INSTALLS the guard around the tool
       body — asserted by grepping the source for
       `contextlib.redirect_stdout(sys.stderr)`.
    2. When the guard IS installed and a stray `print("boom")` fires
       inside the guarded region, the write lands on stderr rather
       than stdout — asserted by capturing stdout + stderr around a
       simulated guarded region.
    """
    # First moving part — grep the source. A future Coder dropping
    # the guard fails this test.
    src = Path(__file__).resolve().parents[3] / "src" / "mcp_server" / "server.py"
    src_text = src.read_text(encoding="utf-8")
    assert "contextlib.redirect_stdout(sys.stderr)" in src_text, (
        "run_stdio must wrap the serve body in "
        "`contextlib.redirect_stdout(sys.stderr)` — see spec-105 "
        "§ 'stdout HARD GUARD'."
    )

    # Second moving part — mechanism proof. Capture stdout + stderr
    # (via pipe-level buffers, since `redirect_stdout` swaps
    # `sys.stdout` at the Python level). Run a simulated guarded
    # region: `redirect_stdout(sys.stderr)` around a `print(...)`.
    # The captured stdout MUST be empty; the stderr MUST carry the
    # boom text.
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Order matters: outer redirects capture stdout+stderr; inner
    # redirect_stdout(sys.stderr) is the guard-under-test. `sys.stderr`
    # at the moment of the inner redirect points at OUR stderr_capture
    # (because the outer redirect_stderr already ran).
    with (
        contextlib.redirect_stdout(stdout_capture),
        contextlib.redirect_stderr(stderr_capture),
        contextlib.redirect_stdout(sys.stderr),
    ):
        # Simulate run_stdio's guard. Under this guard, print(...)
        # routes to sys.stderr (which we've redirected too).
        print("boom-should-not-appear-on-stdout")  # noqa: T201

        # Drive a tool call under the guard too — this is the
        # combined production shape.
        runtime = await bootstrap_runtime(root=tmp_path)
        server = build_server(runtime)
        async with create_connected_server_and_client_session(server) as client:
            resp = await client.call_tool("list_sessions", {})
        assert resp.isError is False

    # Assertion: nothing landed on stdout — the guard rerouted the
    # print to stderr.
    assert "boom-should-not-appear-on-stdout" not in stdout_capture.getvalue(), (
        "stray print() reached stdout — the redirect_stdout guard "
        "is not routing correctly; JSON-RPC framing would corrupt "
        "in production. stdout_capture: "
        f"{stdout_capture.getvalue()!r}"
    )
    # Assertion: the print WAS captured on stderr — proves the
    # redirect target is the right stream.
    assert "boom-should-not-appear-on-stdout" in stderr_capture.getvalue(), (
        "stray print() didn't reach stderr under the guard — the "
        "redirect_stdout(sys.stderr) contract is broken. "
        f"stderr_capture: {stderr_capture.getvalue()!r}"
    )


def test_posture_doc_allowlist_not_bypass() -> None:
    """Test 5b: shipped `mcp-config.json` + README use the allowlist
    permission mode, not `bypassPermissions`."""
    docs = _repo_root() / "docs" / "how-to" / "mcp-operator-server.md"
    cfg = _repo_root() / "docs" / "how-to" / "mcp-config.json"

    assert docs.exists(), f"missing doc: {docs}"
    assert cfg.exists(), f"missing config: {cfg}"

    docs_text = docs.read_text(encoding="utf-8")
    cfg_text = cfg.read_text(encoding="utf-8")

    # The least-privilege allowlist name MUST appear in both files
    # so the shipped recipe boots against the tool.
    assert "mcp__kolsch__list_sessions" in docs_text
    assert "mcp__kolsch__list_sessions" in cfg_text or "kolsch" in cfg_text

    # The over-broad `bypassPermissions` mode MUST NOT appear
    # anywhere in the shipped recipe (docs OR JSON config).
    assert "bypassPermissions" not in docs_text, (
        "docs/how-to/mcp-operator-server.md mentions 'bypassPermissions' — "
        "the recipe must use only `--allowedTools ...` (least-privilege). "
        "If the doc needs to describe the banned mode, use different "
        "wording so the grep-guard doesn't false-positive."
    )
    assert "bypassPermissions" not in cfg_text


# ---------------------------------------------------------------------------
# Test 6 — concurrency: reader coexists with an active writer transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_reader_coexists_with_writer_transaction(
    tmp_path: Path,
) -> None:
    """Test 6: runtime A holds an active writer transaction; runtime B
    reads via `list_sessions` and returns successfully (no `SQLITE_BUSY`).

    Runtime A opens a `BEGIN IMMEDIATE` transaction on its SQLite
    connection to the shared workspace's `entities.db`, holds it open
    across runtime B's `list_sessions` call, and rolls back only after
    the read completes. This proves WAL let the reader coexist with a
    live writer — the earlier "two runtimes read" framing degenerated
    to "does WAL allow two readers?" (trivial).
    """
    runtime_a = await bootstrap_runtime(root=tmp_path)
    # Seed at least one session so the read has something to return.
    await _seed_sessions_with_workflow_run_field(runtime_a.store)

    runtime_b = await bootstrap_runtime(root=tmp_path)
    server = build_server(runtime_b)

    # Runtime A takes an IMMEDIATE (writer) lock. Under WAL, a reader
    # on runtime B should still succeed against this workspace.
    conn_a = runtime_a.store._backend._require_conn()

    try:
        conn_a.execute("BEGIN IMMEDIATE")

        # Drive runtime B's list_sessions via the MCP tool path.
        async with create_connected_server_and_client_session(server) as client:
            resp = await client.call_tool("list_sessions", {})

        # (1) The read returned successfully — no SQLITE_BUSY.
        assert resp.isError is False, (
            f"list_sessions failed under concurrent writer — resp={resp!r}"
        )
        # (2) The seeded sessions came back.
        assert len(resp.content) >= 1
    finally:
        # Always roll back so the fixture doesn't leak an open txn
        # into the interpreter's exit path.
        conn_a.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# Belt-and-braces: build_server + views round-trip WITHOUT the client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_view_uses_mode_json_for_datetime_coercion(
    tmp_path: Path,
) -> None:
    """`session_view` must return JSON-ready dicts — `datetime` fields
    coerced to ISO strings via `.model_dump(mode="json")`.

    Without `mode="json"` the returned dict carries raw `datetime`
    objects and `json.dumps(...)` in the tool body raises
    `TypeError: Object of type datetime is not JSON serializable`.
    Coder-prompt flagged this as the single most likely silent-drift
    site; this test pins it.
    """
    runtime = await bootstrap_runtime(root=tmp_path)
    (session, *_) = await _seed_sessions_with_workflow_run_field(runtime.store)

    view = session_view(session)

    # created_at / updated_at must be ISO-string, not datetime.
    assert isinstance(view["created_at"], str)
    assert isinstance(view["updated_at"], str)
    # The dict must be JSON-serialisable directly.
    encoded = json.dumps(view)
    assert '"session_id"' in encoded
    # And the round-trip is stable.
    assert json.loads(encoded) == view


# Silence unused-import complaints from ruff for the type-only
# TYPE_CHECKING block.
if TYPE_CHECKING:
    _: Callable[[PlatformRuntime], Awaitable[None]] | None = None
