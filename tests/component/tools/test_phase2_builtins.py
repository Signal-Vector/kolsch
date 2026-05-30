"""Component tests for the Phase 2.0 builtin tools — TC-TOOL-01..11.

Spec: docs/process/phase2.0-tools-specs.md Part 1 +
docs/process/test-catalog.md Part 9.8.

The four Phase 2.0 builtins (``read_file``, ``write_file``,
``list_files``, ``bash_v2``) ship as ``@tool``-decorated factories
in ``src/tools/builtins.py``. Each test goes through the full Tools
subsystem (``ToolsSubsystem.invoke_tool``) so the dispatch path
(validate → authz → secrets → sandbox → execute) is exercised end
to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from errors import (
    FileAccessDenied,
    FileNotFound,
    FileTooLarge,
    ToolTimeout,
)
from files import PathPolicy
from providers import (
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
)
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext, ToolsSubsystem

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures (mirrored from tests/component/test_tools.py)
# ---------------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    for sub in ("data", "bundles", ".platform", "logs"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _policy() -> PathPolicy:
    return PathPolicy(
        allowed_paths=["data/"],
        denied_paths=[".env"],
    )


def _context() -> ToolContext:
    agent_id = "ag_test"
    return ToolContext(
        agent_id=AgentId(agent_id),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal=f"agent.{agent_id}",
        secrets={},
    )


def _allow_all() -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]}
    )


def _subsystem(tmp_path: Path) -> ToolsSubsystem:
    root = _root(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return ToolsSubsystem(
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        authz=_allow_all(),
        path_policy=_policy(),
        root=root,
    )


# ===========================================================================
# TC-TOOL-01..04 — read_file
# ===========================================================================


async def test_tc_tool_01_read_file_inside_workspace_succeeds(
    tmp_path: Path,
) -> None:
    """TC-TOOL-01: read_file inside workspace → success."""
    subsystem = _subsystem(tmp_path)
    (tmp_path / "data" / "hello.txt").write_text("kölsch", encoding="utf-8")
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["read_file"],
    )
    result = await subsystem.invoke_tool(
        "read_file",
        {"path": "data/hello.txt"},
        _context(),
    )
    assert result.result == "kölsch"


async def test_tc_tool_02_read_file_outside_workspace_denied(
    tmp_path: Path,
) -> None:
    """TC-TOOL-02: read_file outside workspace → file.access_denied."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["read_file"],
    )
    with pytest.raises(FileAccessDenied):
        await subsystem.invoke_tool(
            "read_file",
            {"path": "/etc/passwd"},
            _context(),
        )


async def test_tc_tool_03_read_file_too_large(tmp_path: Path) -> None:
    """TC-TOOL-03: read_file → file.too_large when size > max_bytes.

    Verified via stat BEFORE the open — the file content is never
    read for an over-sized target.
    """
    subsystem = _subsystem(tmp_path)
    big = tmp_path / "data" / "big.txt"
    big.write_bytes(b"x" * 1024)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["read_file"],
    )
    with pytest.raises(FileTooLarge) as excinfo:
        await subsystem.invoke_tool(
            "read_file",
            {"path": "data/big.txt", "max_bytes": 100},
            _context(),
        )
    detail = excinfo.value.detail
    assert detail["size_bytes"] == 1024
    assert detail["max_bytes"] == 100


async def test_tc_tool_04_read_file_not_found(tmp_path: Path) -> None:
    """TC-TOOL-04: read_file → file.not_found for missing path."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["read_file"],
    )
    with pytest.raises(FileNotFound) as excinfo:
        await subsystem.invoke_tool(
            "read_file",
            {"path": "data/nonexistent.txt"},
            _context(),
        )
    assert excinfo.value.detail["path"] == "data/nonexistent.txt"


# ===========================================================================
# TC-TOOL-05..07 — write_file
# ===========================================================================


async def test_tc_tool_05_write_file_inside_workspace_succeeds(
    tmp_path: Path,
) -> None:
    """TC-TOOL-05 (callback-free path): write_file with allow-all
    auth writes the content + returns the path+bytes+created result.
    The prompt-effect interplay is exercised in test_phase2_authz.py
    (TC-TOOL-05 / TC-TOOL-06).
    """
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["write_file"],
    )
    result = await subsystem.invoke_tool(
        "write_file",
        {"path": "data/out.txt", "content": "hello"},
        _context(),
    )
    target = tmp_path / "data" / "out.txt"
    assert target.read_text(encoding="utf-8") == "hello"
    assert result.result["bytes_written"] == 5
    assert result.result["created"] is True


async def test_tc_tool_07_write_file_atomic_via_temp_rename(
    tmp_path: Path,
) -> None:
    """TC-TOOL-07: write_file uses temp + rename; prior content
    survives a write that fails between temp creation and rename.

    Simulated by patching ``os.replace`` to raise after the temp
    has been written but before the rename completes.
    """
    import os as _os
    from unittest.mock import patch

    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["write_file"],
    )
    existing = tmp_path / "data" / "ex.txt"
    existing.write_text("ORIGINAL", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        msg = "simulated rename failure"
        raise OSError(msg)

    with (
        patch.object(_os, "replace", side_effect=boom),
        pytest.raises(Exception, match=r"(?i)(failed|rename|tool)"),
    ):
        await subsystem.invoke_tool(
            "write_file",
            {"path": "data/ex.txt", "content": "NEW"},
            _context(),
        )
    # Prior content preserved.
    assert existing.read_text(encoding="utf-8") == "ORIGINAL"
    # No stray temp file left over.
    temps = list((tmp_path / "data").glob("ex.txt.tmp.*"))
    assert temps == []


# ===========================================================================
# TC-TOOL-08..09 — list_files
# ===========================================================================


async def test_tc_tool_08_list_files_non_recursive_default(
    tmp_path: Path,
) -> None:
    """TC-TOOL-08: list_files default glob ``"*"`` is non-recursive.

    A descendant of ``data/sub/`` MUST NOT appear unless the caller
    explicitly opts into ``**/*``.
    """
    subsystem = _subsystem(tmp_path)
    (tmp_path / "data" / "a.txt").write_text("a")
    sub = tmp_path / "data" / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("b")
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["list_files"],
    )
    result = await subsystem.invoke_tool(
        "list_files",
        {"path": "data"},
        _context(),
    )
    names = {entry["name"] for entry in result.result}
    assert "a.txt" in names
    assert "sub" in names
    assert "b.txt" not in names


async def test_tc_tool_09_list_files_recursive_hits_max_entries(
    tmp_path: Path,
) -> None:
    """TC-TOOL-09: list_files with ``"**/*"`` recursive and a
    ``max_entries`` cap returns at most ``max_entries`` results.

    Writes 10 files; caps at 3; expects exactly 3 returned.
    """
    subsystem = _subsystem(tmp_path)
    data = tmp_path / "data"
    for i in range(10):
        (data / f"f{i}.txt").write_text("x")
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["list_files"],
    )
    result = await subsystem.invoke_tool(
        "list_files",
        {"path": "data", "glob": "**/*", "max_entries": 3},
        _context(),
    )
    assert len(result.result) == 3


# ===========================================================================
# TC-TOOL-10..11 — bash (Phase 2.0 shape: bash_v2)
# ===========================================================================


async def test_tc_tool_10_bash_v2_returns_exit_code_stdout_stderr(
    tmp_path: Path,
) -> None:
    """TC-TOOL-10: bash_v2 captures exit_code, stdout, stderr,
    duration_seconds, timed_out. Trivial `echo` exercise."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["bash_v2"],
    )
    result = await subsystem.invoke_tool(
        "bash_v2",
        {"command": "echo hello"},
        _context(),
    )
    payload = result.result
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() == "hello"
    assert payload["timed_out"] is False
    assert payload["duration_seconds"] >= 0


async def test_tc_tool_11_bash_v2_timeout_raises_tool_timeout(
    tmp_path: Path,
) -> None:
    """TC-TOOL-11: bash_v2 exceeds ``timeout_seconds`` → ToolTimeout
    (Recoverable); the subprocess is killed; partial output is
    captured in the error chain when present."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_test"),
        builtin_names=["bash_v2"],
    )
    with pytest.raises(ToolTimeout) as excinfo:
        await subsystem.invoke_tool(
            "bash_v2",
            {"command": "sleep 5", "timeout_seconds": 1},
            _context(),
        )
    assert excinfo.value.detail["tool_name"] == "bash_v2"
    assert excinfo.value.detail["timeout_seconds"] == 1
