"""Bundle ``bash`` tool — overflow persistence regression suite.

Pins the behaviour change from
``process/qol-tool-output-truncation-and-persistence.md`` §9 + Step 2:

- Streams ≤ 64 KiB return inline; ``*_persisted_to`` are ``None``.
- Streams > 64 KiB persist the full bytes to
  ``<root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt``
  and the inline preview ends with a footer naming the path.
- The persistence dir is created lazily on first overflow.

These tests load the bundle's ``bash.py`` directly via
:func:`tools.discover_tools_in_file` to exercise the same code path
``kolsch setup`` writes into a workspace — not a separate copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from files import PathPolicy
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext
from tools.discovery import discover_tools_in_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_BUNDLE_BASH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "tools"
    / "bash.py"
)


def _bundle_bash() -> Callable[..., Awaitable[object]]:
    """Load the bundle ``bash`` tool function (the @tool-decorated callable)."""
    found = discover_tools_in_file(_BUNDLE_BASH)
    for fn in found:
        defn = fn._tool_definition
        if defn.name == "bash":
            return fn
    msg = f"could not find bundle bash tool in {_BUNDLE_BASH}"
    raise RuntimeError(msg)


def _ctx(tmp_path: Path, *, tool_call_id: str | None = "tc_unit_01") -> ToolContext:
    """Build a ToolContext rooted at ``tmp_path`` with the QoL fields wired."""
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={},
        workspace_root=tmp_path,
        path_policy=PathPolicy(allowed_paths=["logs/"], denied_paths=[]),
        tool_call_id=tool_call_id,
    )


# ---------------------------------------------------------------------------
# Below-threshold path — no persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_output_under_threshold_no_persistence(
    tmp_path: Path,
) -> None:
    """A small command (well under 64 KiB) returns inline and leaves
    both ``*_persisted_to`` fields ``None``. Also confirms the new
    return shape always carries the two fields rather than omitting
    them when None (so downstream consumers can read them
    unconditionally — Step 2 reviewer check #3)."""
    bash = _bundle_bash()
    ctx = _ctx(tmp_path)
    BashArgs = bash._tool_definition.args_schema

    result = await bash(BashArgs(command="echo hello-bash"), ctx)

    assert result["exit_code"] == 0
    assert "hello-bash" in result["stdout"]
    assert "stdout_persisted_to" in result
    assert "stderr_persisted_to" in result
    assert result["stdout_persisted_to"] is None
    assert result["stderr_persisted_to"] is None


# ---------------------------------------------------------------------------
# Stdout overflow — persisted + footer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_output_over_threshold_persists_stdout(
    tmp_path: Path,
) -> None:
    """A command producing >64 KiB on stdout writes the full bytes to
    a workspace tmp path and returns the path in
    ``stdout_persisted_to``. The inline stdout preview ends with the
    pinned footer mentioning the path + read_file handoff."""
    bash = _bundle_bash()
    ctx = _ctx(tmp_path)
    BashArgs = bash._tool_definition.args_schema

    # `yes` truncated by head — 70 KiB ≈ 35000 "x\n" lines.
    result = await bash(
        BashArgs(command="yes x | head -c 70000"),
        ctx,
    )

    assert result["exit_code"] == 0
    persisted = result["stdout_persisted_to"]
    assert persisted is not None
    persisted_path = Path(persisted)
    assert persisted_path.exists()
    assert persisted_path.stat().st_size == 70000
    # Inline preview is exactly 64 KiB + footer.
    assert "full output at" in result["stdout"]
    assert "use read_file with offset/limit" in result["stdout"]
    assert str(persisted_path) in result["stdout"]
    # stderr stayed empty → no persistence on that stream.
    assert result["stderr_persisted_to"] is None


# ---------------------------------------------------------------------------
# Both streams overflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_overflow_on_both_streams(tmp_path: Path) -> None:
    """When both stdout AND stderr exceed 64 KiB, both persisted_to
    fields point at files that exist and contain the FULL stream
    bytes (not just the preview)."""
    bash = _bundle_bash()
    ctx = _ctx(tmp_path, tool_call_id="tc_both_streams")
    BashArgs = bash._tool_definition.args_schema

    # Push 70 KiB to stdout AND 70 KiB to stderr in one shell invocation.
    cmd = "yes x | head -c 70000; yes y | head -c 70000 1>&2"
    result = await bash(BashArgs(command=cmd), ctx)

    assert result["stdout_persisted_to"] is not None
    assert result["stderr_persisted_to"] is not None
    assert Path(result["stdout_persisted_to"]).stat().st_size == 70000
    assert Path(result["stderr_persisted_to"]).stat().st_size == 70000


# ---------------------------------------------------------------------------
# Path convention + lazy dir creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_persisted_path_under_workspace_logs(
    tmp_path: Path,
) -> None:
    """The persistence path resolves under
    ``<workspace_root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt``
    — the location pinned by Step 1 writeup §9.2 because
    ``.platform/`` is denied by default."""
    bash = _bundle_bash()
    call_id = "tc_path_check"
    session_id = f"ses_{uuid4()}"
    ctx = ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(session_id),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={},
        workspace_root=tmp_path,
        path_policy=PathPolicy(allowed_paths=["logs/"], denied_paths=[]),
        tool_call_id=call_id,
    )
    BashArgs = bash._tool_definition.args_schema

    result = await bash(
        BashArgs(command="yes x | head -c 70000"),
        ctx,
    )
    expected = tmp_path / "logs" / "tool-outputs" / session_id / f"{call_id}.stdout.txt"
    assert Path(result["stdout_persisted_to"]) == expected


@pytest.mark.asyncio
async def test_bash_persisted_dir_created_lazily(tmp_path: Path) -> None:
    """Before the first overflow, ``logs/tool-outputs/`` does not
    exist. After the first overflow, it does. Confirms the lazy-
    create-on-write strategy from Step 1 writeup §9.3."""
    assert not (tmp_path / "logs" / "tool-outputs").exists()

    bash = _bundle_bash()
    ctx = _ctx(tmp_path, tool_call_id="tc_lazy_create")
    BashArgs = bash._tool_definition.args_schema

    await bash(BashArgs(command="yes x | head -c 70000"), ctx)

    assert (tmp_path / "logs" / "tool-outputs").exists()
    assert (tmp_path / "logs" / "tool-outputs").is_dir()


@pytest.mark.asyncio
async def test_bash_persisted_path_fallback_when_no_call_id(
    tmp_path: Path,
) -> None:
    """When ``ctx.tool_call_id`` is None (out-of-engine call),
    overflow STILL persists — the helper synthesises ``tc_anon_*``
    so concurrent fallback calls don't collide. Important so tests
    that construct a bare ToolContext still exercise the persistence
    shape."""
    bash = _bundle_bash()
    ctx = _ctx(tmp_path, tool_call_id=None)
    BashArgs = bash._tool_definition.args_schema

    result = await bash(BashArgs(command="yes x | head -c 70000"), ctx)

    persisted = result["stdout_persisted_to"]
    assert persisted is not None
    assert "tc_anon_" in persisted
    assert Path(persisted).exists()
