"""Unit tests for the MCP operator server (spec-105 + spec-111).

These tests drive the server IN-PROCESS via
`mcp.shared.memory.create_connected_server_and_client_session` — no
OS subprocess, no stdio pipes. Keeps the CI gate stable (no timing,
no port binding, no subprocess spawn). The real-`claude` end-to-end
round-trip lives at `tests/e2e/test_mcp_claude_roundtrip.py` under
`pytestmark = pytest.mark.e2e` so `pytest -m "not e2e"` deselects it.

Coverage map (spec-105):
- Test 1 lives in `tests/component/test_cli_smoke.py` (CLI smoke).
- Test 2 (handshake + tools/list) — `test_handshake_...`.
- Test 3 (tools/call parity + from_workflow_run exclusion) — `test_parity_...`.
- Test 3b (error mapping) — `test_error_mapping_...`.
- Test 4 (no serialization helper module) — `test_no_serialization_module`.
- Test 5 (posture / stdout hygiene / no listener) — `test_posture_...`.
- Test 6 (concurrency: writer + reader) — `test_concurrency_...`.

Coverage map (spec-111 — `read_session_events`):
- Test 1 (2 tools registered + inputSchema shape) — `test_tools_list_two_...`.
- Test 2 (HTTP parity) — `test_events_http_parity_...`.
- Test 3 (`include_ui_events` default filter) — `test_ui_events_default_...`.
- Test 4 (store knobs pass-through — 3 parametrized) — `test_store_knobs_...`.
- Test 4b (`event_types` post-filter — 5 parametrized) — `test_event_types_...`.
- Test 5 (error mapping — 3 parametrized) — `test_events_error_mapping_...`.
- Test 6 (view promotion — AST assertions) — `test_view_promotion_...`.
- Test 7 (docs updated — allowedTools + invocation subsection) — `test_docs_...`.
- Test 8 (real-`claude` round-trip) lives at
  `tests/e2e/test_mcp_claude_roundtrip.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from api.views import session_view
from mcp_server.server import build_server
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
async def test_handshake_tools_list_returns_list_sessions_wire_shape() -> None:
    """Test 2 (cont.): `tools/list` includes `list_sessions` with the
    same spec-105 flat object-rooted inputSchema.

    Post-spec-111 the server also registers `read_session_events`; that
    tool's schema is asserted in :func:`test_tools_list_two_tools`.
    This test remains focused on the `list_sessions` regression check —
    if its shape drifts, the spec-105 gate here catches it independent
    of whatever the second tool does.
    """
    server = build_server(_StubRuntime())

    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()

        by_name = {t.name: t for t in tools.tools}
        assert "list_sessions" in by_name
        tool = by_name["list_sessions"]

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
                expected = session_view(match).model_dump(mode="json")
                assert parsed == expected, (
                    "content-block dict does not equal "
                    "session_view(...).model_dump(mode='json') — "
                    f"parsed={parsed!r}, expected={expected!r}"
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
    """Test 4 (cont.): BOTH tools were registered with
    `structured_output=False`.

    Without it, FastMCP auto-derives an `outputSchema` from the
    `list[TextContent]` return type. That path wraps the result in
    `{"result": ...}` and re-emits serialized content — double the
    payload plus jsonschema validation errors.
    """
    server = build_server(_StubRuntime())

    tools = server._tool_manager.list_tools()
    assert len(tools) == 2
    for tool in tools:
        # `structured_output=False` translates to `output_schema is None`
        # on the ToolManager's `Tool` record (see FastMCP
        # `Tool.from_function` — the auto-derivation is skipped).
        assert tool.output_schema is None, (
            f"`{tool.name}` tool has an outputSchema — the "
            "`structured_output=False` decorator arg was dropped."
        )


# ---------------------------------------------------------------------------
# Test 5 — posture: no listener / stdout hygiene / read-only / doc allowlist
# ---------------------------------------------------------------------------


def test_posture_only_read_only_tools_registered() -> None:
    """Test 5 (cont.): only the two read-only tools are registered.

    Post-spec-111 the server registers `list_sessions` (spec-105) and
    `read_session_events` (spec-111). Both are read-only — no mutate
    handler yet (write tools are Phase 2, blocked on the principal
    rework spec-105 documented). A future accidental registration of a
    mutate tool would fail this posture check.
    """
    server = build_server(_StubRuntime())
    tools = list(server._tool_manager.list_tools())
    assert len(tools) == 2
    assert {t.name for t in tools} == {"list_sessions", "read_session_events"}


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
    """`session_view(...).model_dump(mode="json")` returns JSON-ready
    dicts — `datetime` fields coerced to ISO strings.

    Without `mode="json"` the returned dict carries raw `datetime`
    objects and `json.dumps(...)` in the tool body raises
    `TypeError: Object of type datetime is not JSON serializable`.
    Coder-prompt flagged this as the single most likely silent-drift
    site; this test pins it. Post-spec-111 `session_view` returns
    ``_SessionView``; the MCP tool body applies `.model_dump(
    mode="json")` at the callsite — this test verifies that pair.
    """
    runtime = await bootstrap_runtime(root=tmp_path)
    (session, *_) = await _seed_sessions_with_workflow_run_field(runtime.store)

    view = session_view(session).model_dump(mode="json")

    # created_at / updated_at must be ISO-string, not datetime.
    assert isinstance(view["created_at"], str)
    assert isinstance(view["updated_at"], str)
    # The dict must be JSON-serialisable directly.
    encoded = json.dumps(view)
    assert '"session_id"' in encoded
    # And the round-trip is stable.
    assert json.loads(encoded) == view


# ---------------------------------------------------------------------------
# Spec-111 — read_session_events
# ---------------------------------------------------------------------------
#
# The fixture helpers below seed a session/thread/event set on a fresh
# tmp-workspace runtime. Every spec-111 test opens a fresh runtime and
# a fresh workspace via `bootstrap_runtime(root=tmp_path)`, so cases
# never share state across parametrize invocations.


async def _seed_one_session_and_thread(
    store: SessionStore,
) -> tuple[Session, ThreadId_]:
    """Create one session + one thread on the store; return both.

    Uses the same backend-direct pattern as
    :func:`_seed_sessions_with_workflow_run_field` — bypasses
    ``store.create_session``/``store.create_thread`` so the fixture
    plants fully-controlled ids without invoking any of the platform's
    default-thread machinery.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from schemas.entities import Thread
    from schemas.identifiers import ThreadId

    now = datetime.now(UTC)
    session_id = SessionId(f"ses_{uuid4()}")
    thread_id = ThreadId(f"th_{uuid4()}")
    agent_id = AgentId("ag_test_events")

    session = Session(
        session_id=session_id,
        agent_id=agent_id,
        name="events-fixture",
        starred=False,
        archived=False,
        created_at=now,
        updated_at=now,
    )
    await store._backend.insert_session(session)
    thread = Thread(
        thread_id=thread_id,
        session_id=session_id,
        agent_id=agent_id,
        created_at=now,
    )
    await store._backend.insert_thread(thread)
    return session, thread_id


async def _seed_events(
    store: SessionStore,
    thread_id: ThreadId_,
    session: Session,
    specs: list[dict[str, object]],
) -> list[Event_]:
    """Append events per `specs` to the JSONL log.

    Each spec is a dict with keys used to build the :class:`Event`
    envelope: ``type`` (:class:`EventType`), optional ``kind`` (for
    ``ui.event``), optional ``with_parent`` (bool — attach a fake
    parent id), optional ``with_content_ref`` (bool — attach a
    :class:`ContentRef`), optional ``with_trace`` (bool — attach a
    trace id). Returns the events as they came back from
    ``append_event`` (with the store-populated `parent_event_id`).
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from schemas.entities import Actor, ContentRef
    from schemas.events import Event, EventType, UIEventData
    from schemas.identifiers import EventId

    appended: list[Event] = []
    for i, spec in enumerate(specs):
        etype = spec["type"]
        assert isinstance(etype, EventType)
        data: dict[str, object] = {}
        if etype == EventType.UI_EVENT:
            kind = spec.get("kind", "kolsch.workflow.node.awaiting_approval")
            assert isinstance(kind, str)
            data = UIEventData(
                schema_version=1,
                kind=kind,
                payload={"fixture": f"ui-{i}"},
            ).model_dump()
        else:
            data = {"fixture": f"event-{i}", "type_str": etype.value}

        # Deterministic well-formed event id — different per event.
        ev = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=(
                EventId(f"ev_{uuid4()}") if spec.get("with_parent") else None
            ),
            thread_id=thread_id,
            session_id=session.session_id,
            type=etype,
            data=data,
            actor=Actor(kind="system", id="test-fixture"),
            created_at=datetime.now(UTC),
            content_ref=(
                ContentRef(
                    hash="0" * 64,
                    size=42,
                    stored_at="test-fixture/content/deadbeef.bin",
                )
                if spec.get("with_content_ref")
                else None
            ),
            trace_id=("trc_fixture_1" if spec.get("with_trace") else None),
        )
        stored = await store.append_event(thread_id, ev)
        appended.append(stored)
    return appended


# ---------------------------------------------------------------------------
# Test 1 (spec-111) — two tools registered + read_session_events schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_two_tools_and_read_events_schema() -> None:
    """Spec-111 Test 1: `tools/list` returns 2 tools; `read_session_events`
    has a flat inputSchema with `required: ["session_id"]`.

    AC #1 + AC #2.
    """
    server = build_server(_StubRuntime())

    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()

    assert len(tools.tools) == 2
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name.keys()) == {"list_sessions", "read_session_events"}

    schema = by_name["read_session_events"].inputSchema
    assert schema["type"] == "object"
    assert set(schema["properties"].keys()) == {
        "session_id",
        "after_event_id",
        "tail",
        "limit",
        "event_types",
        "include_ui_events",
    }
    # Only session_id is required — the other five are optional.
    assert schema.get("required", []) == ["session_id"]
    # No `params` wrapper — flat top-level, matching the LLM-friendly
    # shape spec-105 established for `list_sessions`.
    assert "params" not in schema["properties"]


# ---------------------------------------------------------------------------
# Test 2 (spec-111) — HTTP parity: each block equals `event.model_dump(mode="json")`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_http_parity_full_dump(tmp_path: Path) -> None:
    """Spec-111 Test 2: MCP response byte-matches
    `[event.model_dump(mode="json") for e in read_events(...)]`.

    Seeds a mixed fixture (`ui.event`, `tool.called`, `tool.result`,
    `tool.failed`, `agent.message`, one with parent_event_id, one with
    content_ref, one with trace_id) and calls the MCP tool with
    ``include_ui_events=True`` so nothing is filtered. AC #3.
    """
    from schemas.events import EventType

    runtime = await bootstrap_runtime(root=tmp_path)
    session, thread_id = await _seed_one_session_and_thread(runtime.store)
    specs: list[dict[str, object]] = [
        {"type": EventType.UI_EVENT},
        {"type": EventType.TOOL_CALLED},
        {"type": EventType.TOOL_RESULT, "with_parent": True},
        {"type": EventType.TOOL_FAILED, "with_content_ref": True},
        {"type": EventType.AGENT_MESSAGE, "with_trace": True},
        {"type": EventType.USER_MESSAGE},
        {"type": EventType.MODEL_REQUEST},
        {"type": EventType.MODEL_RESPONSE},
    ]
    await _seed_events(runtime.store, thread_id, session, specs)

    # HTTP-parity target: read the events straight off the store and
    # dump them the exact same way the HTTP handler does.
    http_shape: list[dict[str, object]] = []
    async for e in runtime.store.read_events(thread_id):
        http_shape.append(e.model_dump(mode="json"))

    server = build_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        resp = await client.call_tool(
            "read_session_events",
            {"session_id": str(session.session_id), "include_ui_events": True},
        )

    assert resp.isError is False
    assert len(resp.content) == len(http_shape)
    for block, expected in zip(resp.content, http_shape, strict=True):
        assert isinstance(block, TextContent)
        assert json.loads(block.text) == expected


# ---------------------------------------------------------------------------
# Test 3 (spec-111) — include_ui_events=False default drops ui.event records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_events_default_filter_drops_ui_event(tmp_path: Path) -> None:
    """Spec-111 Test 3: default (`include_ui_events` omitted) filters
    every ``ui.event``. AC #4.
    """
    from schemas.events import EventType

    runtime = await bootstrap_runtime(root=tmp_path)
    session, thread_id = await _seed_one_session_and_thread(runtime.store)
    specs: list[dict[str, object]] = [
        {"type": EventType.UI_EVENT},
        {"type": EventType.TOOL_CALLED},
        {"type": EventType.UI_EVENT, "kind": "kolsch.workflow.node.render"},
        {"type": EventType.AGENT_MESSAGE},
        {"type": EventType.UI_EVENT, "kind": "kolsch.prompt.requested"},
    ]
    await _seed_events(runtime.store, thread_id, session, specs)
    total = len(specs)
    ui_count = sum(1 for s in specs if s["type"] == EventType.UI_EVENT)

    server = build_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        resp = await client.call_tool(
            "read_session_events",
            {"session_id": str(session.session_id)},
        )

    assert resp.isError is False
    assert len(resp.content) == total - ui_count
    for block in resp.content:
        assert isinstance(block, TextContent)
        assert json.loads(block.text)["type"] != "ui.event"


# ---------------------------------------------------------------------------
# Test 4 (spec-111) — store knobs pass through (tail, after_event_id, limit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "tool_kwargs_builder", "expected_kwargs_builder", "expected_len"),
    [
        (
            "tail",
            lambda _events: {"tail": 3},
            lambda _events: {"from_event_id": None, "tail": 3, "limit": None},
            3,
        ),
        (
            "after_event_id",
            lambda events: {"after_event_id": str(events[3].event_id)},
            lambda events: {
                "from_event_id": str(events[3].event_id),
                "tail": None,
                "limit": None,
            },
            6,
        ),
        (
            "limit",
            lambda _events: {"limit": 5},
            lambda _events: {"from_event_id": None, "tail": None, "limit": 5},
            5,
        ),
    ],
    ids=["tail", "after_event_id", "limit"],
)
@pytest.mark.asyncio
async def test_store_knobs_passthrough(
    tmp_path: Path,
    case_id: str,
    tool_kwargs_builder: object,
    expected_kwargs_builder: object,
    expected_len: int,
) -> None:
    """Spec-111 Test 4: each store knob (`tail`, `after_event_id`,
    `limit`) reaches `runtime.store.read_events` with the exact kwargs
    dict — unrelated knobs pinned to `None`.

    Every case gets a fresh runtime + workspace so the spy's `captured`
    dict never bleeds between cases. AC #5/6/7.
    """
    from typing import Any as _Any

    from schemas.events import EventType

    # Fresh runtime per case — no state bleed via the spy dict.
    runtime = await bootstrap_runtime(root=tmp_path / case_id)
    session, thread_id = await _seed_one_session_and_thread(runtime.store)
    specs: list[dict[str, object]] = [
        {"type": EventType.TOOL_CALLED} for _ in range(10)
    ]
    events = await _seed_events(runtime.store, thread_id, session, specs)

    captured: dict[str, _Any] = {}
    real = runtime.store.read_events

    async def _spy(tid, **kwargs):  # type: ignore[no-untyped-def]
        captured.clear()
        captured.update({"thread_id": tid, **kwargs})
        async for e in real(tid, **kwargs):
            yield e

    runtime.store.read_events = _spy  # type: ignore[method-assign, assignment]

    tool_kwargs = {
        "session_id": str(session.session_id),
        **tool_kwargs_builder(events),  # type: ignore[operator]
    }
    expected_kwargs = {
        "thread_id": thread_id,
        **expected_kwargs_builder(events),  # type: ignore[operator]
    }

    server = build_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        resp = await client.call_tool("read_session_events", tool_kwargs)

    assert resp.isError is False, (
        f"case={case_id!r} returned isError=True; content={resp.content!r}"
    )
    # STRICT: full-kwargs equality — catches "all three passed
    # unconditionally" regressions the presence-only assertion missed.
    assert captured == expected_kwargs, (
        f"case={case_id!r}: spy captured {captured!r}; expected {expected_kwargs!r}"
    )
    assert len(resp.content) == expected_len, (
        f"case={case_id!r}: got {len(resp.content)} blocks; expected {expected_len}"
    )


# ---------------------------------------------------------------------------
# Test 4b (spec-111) — event_types post-filter (5 parametrized cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_types", "expected_len", "expected_type_set"),
    [
        (["tool.failed"], 3, {"tool.failed"}),
        (["tool.failed", "agent.message"], 7, {"tool.failed", "agent.message"}),
        ([], 9, {"tool.failed", "tool.result", "agent.message"}),
        (None, 9, {"tool.failed", "tool.result", "agent.message"}),
        (["nonexistent.type"], 0, set()),
    ],
    ids=["allowlist-one", "allowlist-union", "empty-list", "none", "no-match"],
)
@pytest.mark.asyncio
async def test_event_types_filter(
    tmp_path: Path,
    event_types: list[str] | None,
    expected_len: int,
    expected_type_set: set[str],
) -> None:
    """Spec-111 Test 4b: `event_types` post-filter — empty list AND
    None both mean "no filter"; empty-match returns `isError: false`
    with an empty content list. AC #4b.
    """
    from schemas.events import EventType

    runtime = await bootstrap_runtime(root=tmp_path)
    session, thread_id = await _seed_one_session_and_thread(runtime.store)
    specs: list[dict[str, object]] = []
    for _ in range(3):
        specs.append({"type": EventType.TOOL_FAILED})
    for _ in range(2):
        specs.append({"type": EventType.TOOL_RESULT})
    for _ in range(4):
        specs.append({"type": EventType.AGENT_MESSAGE})
    await _seed_events(runtime.store, thread_id, session, specs)

    server = build_server(runtime)
    async with create_connected_server_and_client_session(server) as client:
        resp = await client.call_tool(
            "read_session_events",
            {
                "session_id": str(session.session_id),
                "event_types": event_types,
                "include_ui_events": True,
            },
        )

    assert resp.isError is False
    assert len(resp.content) == expected_len
    if expected_len > 0:
        # Per-block membership — a regression where the filter returned
        # N blocks all of a single type (filter inversion swapping the
        # union for a fixed default) would pass an aggregate set-only
        # check but fails this one.
        for block in resp.content:
            assert isinstance(block, TextContent)
            parsed_type = json.loads(block.text)["type"]
            assert parsed_type in expected_type_set, (
                f"block type {parsed_type!r} not in allowed set {expected_type_set!r}"
            )


# ---------------------------------------------------------------------------
# Test 5 (spec-111) — error mapping: SessionNotFound + 2 ValueError paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_id_builder", "expected_type_name", "forbidden_substring"),
    [
        # (a) Well-formed nonexistent uuid — passes SessionId, then
        # get_session raises SessionNotFound. THIS case is the one
        # `"ses_definitely_not_there"` never reached (that body fails
        # SessionId's body_must_be_uuid check first).
        (
            lambda: f"ses_{__import__('uuid').uuid4()}",
            "SessionNotFound",
            None,  # dynamic — checked below
        ),
        # (b) Missing `ses_` prefix — SessionId(...) raises ValueError.
        (
            lambda: "not-a-valid-id",
            "ValueError",
            "not-a-valid-id",
        ),
        # (c) Valid prefix but non-uuid body — SessionId(...) raises
        # ValueError from the body_must_be_uuid check.
        (
            lambda: "ses_not-a-uuid",
            "ValueError",
            "not-a-uuid",
        ),
    ],
    ids=["session_not_found", "value_error_bad_prefix", "value_error_bad_body"],
)
@pytest.mark.asyncio
async def test_events_error_mapping(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    session_id_builder: object,
    expected_type_name: str,
    forbidden_substring: str | None,
) -> None:
    """Spec-111 Test 5: all 3 id-error paths sanitize to
    `read_session_events failed: <ExceptionType>` with no raw message
    / offending id leaked at the caller surface, while the internal
    logging path DOES retain the full exception context via
    ``exc_info=exc`` for local debugging. AC #8/#9.

    We deliberately assert BOTH surfaces:
    * ``block.text`` — the CALLER-facing response is sanitized to the
      exception type name.
    * ``caplog.records`` — the LOCAL-debugging log record has
      ``exc_info`` populated with the concrete exception class, so
      operators debugging locally can still see what actually raised.
    """
    del forbidden_substring  # kept in parametrize IDs for readability

    runtime = await bootstrap_runtime(root=tmp_path)
    server = build_server(runtime)

    sid = session_id_builder()  # type: ignore[operator]
    assert isinstance(sid, str)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mcp_server"):
        async with create_connected_server_and_client_session(server) as client:
            resp = await client.call_tool(
                "read_session_events",
                {"session_id": sid},
            )

    # --- Caller-facing surface (sanitized) --------------------------
    assert resp.isError is True
    assert len(resp.content) == 1
    block = resp.content[0]
    assert isinstance(block, TextContent)
    assert block.text == f"read_session_events failed: {expected_type_name}"

    # --- Local-debugging surface (retains exception context) --------
    mcp_records = [r for r in caplog.records if r.name == "mcp_server"]
    assert len(mcp_records) == 1, (
        f"expected exactly 1 mcp_server log record; got {len(mcp_records)}"
    )
    record = mcp_records[0]
    # `exc_info=exc` at the log site MUST attach the exception —
    # proves the sanitize-at-response-boundary contract without
    # dropping local debuggability.
    assert record.exc_info is not None, (
        "exc_info missing from log record — the local-debugging path "
        "was skipped; operators would lose the traceback"
    )
    exc_type = record.exc_info[0]
    assert exc_type is not None and exc_type.__name__ == expected_type_name, (
        f"log record exc_info type {exc_type!r} != expected {expected_type_name!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 (spec-111) — view promotion (AST-based, plan B)
# ---------------------------------------------------------------------------


def test_view_promotion_ast_layout() -> None:
    """Spec-111 Test 6: `_SessionView` class AND `session_view` function
    both live in `src/api/views.py`; `src/mcp_server/views.py` is
    deleted; `src/api/routes.py` imports both names back; the
    `list_sessions` callsites gained `.model_dump(mode="json")`.

    AC #10. All checks are AST-based (not grep — greps under-anchor
    and miss shims) except one deliberate substring check at the
    ``list_sessions`` callsite (the substring IS the contract).
    """
    import ast

    root = _repo_root()

    views_path = root / "src" / "api" / "views.py"
    routes_path = root / "src" / "api" / "routes.py"
    mcp_views_path = root / "src" / "mcp_server" / "views.py"
    mcp_server_path = root / "src" / "mcp_server" / "server.py"

    # (a) views.py exists; mcp_server/views.py deleted.
    assert views_path.exists(), f"{views_path} should exist"
    assert not mcp_views_path.exists(), (
        f"{mcp_views_path} should be deleted by spec-111"
    )

    # (b) src/api/views.py — both symbols present at top level.
    views_ast = ast.parse(views_path.read_text(encoding="utf-8"))
    top_classes = {n.name for n in views_ast.body if isinstance(n, ast.ClassDef)}
    top_funcs = {n.name for n in views_ast.body if isinstance(n, ast.FunctionDef)}
    assert "_SessionView" in top_classes, (
        "_SessionView class should live in src/api/views.py"
    )
    assert "session_view" in top_funcs, (
        "session_view function should live in src/api/views.py"
    )

    # (c) src/api/routes.py — no local def of either; imports them back.
    routes_ast = ast.parse(routes_path.read_text(encoding="utf-8"))
    routes_top_classes = {
        n.name for n in routes_ast.body if isinstance(n, ast.ClassDef)
    }
    routes_top_funcs = {
        n.name for n in routes_ast.body if isinstance(n, ast.FunctionDef)
    }
    assert "_SessionView" not in routes_top_classes, (
        "_SessionView should be imported (not defined) in routes.py"
    )
    assert "session_view" not in routes_top_funcs, (
        "session_view should be imported (not defined) in routes.py"
    )
    assert "_session_view" not in routes_top_funcs, (
        "_session_view (underscored) must not remain in routes.py"
    )
    routes_imports_from_views: set[str] = set()
    for node in routes_ast.body:
        if isinstance(node, ast.ImportFrom) and node.module == "api.views":
            routes_imports_from_views.update(alias.name for alias in node.names)
    assert "_SessionView" in routes_imports_from_views
    assert "session_view" in routes_imports_from_views

    # (d) src/mcp_server/server.py imports session_view from api.views,
    # NOT from mcp_server.views.
    server_ast = ast.parse(mcp_server_path.read_text(encoding="utf-8"))
    imports_from_api_views: set[str] = set()
    imports_from_mcp_views: set[str] = set()
    for node in server_ast.body:
        if isinstance(node, ast.ImportFrom):
            if node.module == "api.views":
                imports_from_api_views.update(alias.name for alias in node.names)
            if node.module == "mcp_server.views":
                imports_from_mcp_views.update(alias.name for alias in node.names)
    assert "session_view" in imports_from_api_views
    assert imports_from_mcp_views == set(), (
        f"mcp_server/server.py still imports from mcp_server.views: "
        f"{imports_from_mcp_views!r}"
    )

    # (e) The one deliberate substring check — the two `list_sessions`
    # callsites now do `session_view(s).model_dump(mode="json")`. This
    # IS the wire-shape contract; a bare `session_view(s)` would ship
    # a `_SessionView` model to `json.dumps` and either serialize
    # oddly or leak datetime objects.
    server_src = mcp_server_path.read_text(encoding="utf-8")
    assert 'session_view(s).model_dump(mode="json")' in server_src, (
        "mcp_server/server.py's list_sessions callsite should be "
        'session_view(s).model_dump(mode="json") — got something else'
    )


# ---------------------------------------------------------------------------
# Test 7 (spec-111) — docs updated: both tool names + invocation subsection
# ---------------------------------------------------------------------------


def test_docs_allowed_tools_and_invocation_subsection() -> None:
    """Spec-111 Test 7: `docs/how-to/mcp-operator-server.md` names both
    tools in the `--allowedTools` example, still avoids
    `bypassPermissions`, and carries a marker-string subsection showing
    a `read_session_events` invocation. AC #11.
    """
    docs = _repo_root() / "docs" / "how-to" / "mcp-operator-server.md"
    assert docs.exists(), f"missing doc: {docs}"
    text = docs.read_text(encoding="utf-8")

    # (a) Both tool names appear (allowedTools example should list them).
    assert "mcp__kolsch__list_sessions" in text
    assert "mcp__kolsch__read_session_events" in text

    # (b) The over-broad bypass mode still absent.
    assert "bypassPermissions" not in text

    # (c) The invocation subsection — a marker string distinct from
    # the allowlist line, so a Coder can't satisfy AC #11's example
    # bullet by only updating the allowlist.
    assert "read the last" in text, (
        "docs/how-to/mcp-operator-server.md should carry a new "
        "read_session_events invocation example (marker: 'read the last')"
    )


# Silence unused-import complaints from ruff for the type-only
# TYPE_CHECKING block.
if TYPE_CHECKING:
    from schemas.events import Event as Event_
    from schemas.identifiers import ThreadId as ThreadId_

    _: Callable[[PlatformRuntime], Awaitable[None]] | None = None
    _Event_: type[Event_] | None = None
    _ThreadId_: type[ThreadId_] | None = None
