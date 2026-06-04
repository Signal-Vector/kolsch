"""Cross-tool overflow + pagination — TC-TOOLS-OVERFLOW-01..03.

Locks in the QoL §9 behaviour at the integration boundary:

- bash overflow writes to a workspace tmp path AND the path is
  readable via read_file with offset/limit (TC-TOOLS-OVERFLOW-01).
- read_file paginates through a 10k-line file with next_offset
  correctly threaded, never raising the pre-QoL file.too_large
  (TC-TOOLS-OVERFLOW-02).
- A single 2 MiB line raises the new file.line_too_long with the
  correct detail fields (TC-TOOLS-OVERFLOW-03).

Tests go through ``ToolsSubsystem.invoke_tool`` — the same dispatch
the engine uses — so the validate → authz → secrets → sandbox →
execute pipeline is exercised end-to-end. The ToolContext carries
``tool_call_id`` to match the engine's observer.

Spec: process/qol-tool-output-truncation-and-persistence.md §9 +
process/test-catalog.md Part 9.8.1.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from errors import FileLineTooLong
from files import PathPolicy
from providers import (
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
)
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext, ToolsSubsystem

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    """Lay out the minimum workspace dirs the tools expect."""
    for sub in ("data", "bundles", "logs"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _policy() -> PathPolicy:
    """Default-style policy: ``data/`` for reads, ``logs/`` for the
    persisted-output handoff (the pinned location, §9.2)."""
    return PathPolicy(
        allowed_paths=["data/", "logs/"],
        denied_paths=[".env"],
    )


def _ctx(
    tmp_path: Path,
    *,
    tool_call_id: str = "tc_int_01",
    session_id: str | None = None,
) -> ToolContext:
    sid = session_id or f"ses_{uuid4()}"
    return ToolContext(
        agent_id=AgentId("ag_int"),
        session_id=SessionId(sid),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_int",
        secrets={},
        tool_call_id=tool_call_id,
    )


def _allow_all() -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
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
# TC-TOOLS-OVERFLOW-01 — bash overflow handoff to read_file
# ===========================================================================


async def test_tc_tools_overflow_01_bash_overflow_persists_and_read_file_drills_in(
    tmp_path: Path,
) -> None:
    """A bash command producing >64 KiB stdout writes the full bytes
    to the persisted path; a subsequent read_file against that path
    returns the requested line range with pagination metadata."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_int"),
        builtin_names=["bash_v2", "read_file"],
    )
    ctx = _ctx(tmp_path, tool_call_id="tc_bash_overflow_01")

    # Action 1 — produce >64 KiB stdout (seq 1..100000 ≈ 580 KiB).
    bash_result = await subsystem.invoke_tool(
        "bash_v2",
        {"command": "seq 1 100000"},
        ctx,
    )
    persisted = bash_result.result["stdout_persisted_to"]
    assert persisted is not None, (
        f"expected stdout_persisted_to to be set; got bash result: "
        f"{bash_result.result!r}"
    )
    assert Path(persisted).exists()
    # The file IS the full sequence — verify by line count.
    assert len(Path(persisted).read_bytes().splitlines()) == 100_000

    # Action 2 — read a slice via the new line-based read_file.
    # Persisted path is absolute; pass as-is (it's already under the
    # workspace root and the policy allows logs/ which is the prefix).
    read_result = await subsystem.invoke_tool(
        "read_file",
        {"path": persisted, "offset": 50000, "limit": 100},
        ctx,
    )
    payload = read_result.result
    assert payload["total_lines"] == 100_000
    assert payload["lines_returned"] == 100
    assert payload["truncated"] is True
    assert payload["next_offset"] == 50100
    # First rendered line is index 50000 → number 50001 → "50001".
    first = payload["content"].splitlines()[0]
    assert first.endswith("\t50001")
    last = payload["content"].splitlines()[-1]
    assert last.endswith("\t50100")


# ===========================================================================
# TC-TOOLS-OVERFLOW-02 — read_file paginates a previously-refused file
# ===========================================================================


async def test_tc_tools_overflow_02_read_file_paginated_full_read(
    tmp_path: Path,
) -> None:
    """A 10000-line file with default limit=2000 paginates across 5
    calls. Sum of returned lines == total; final call is
    truncated=False, next_offset=None.

    Pre-QoL this size would have hit ``file.too_large`` (the bare-
    string return refused at ``size > max_bytes``). The QoL replaces
    refusal with soft truncation — the whole point of the change.
    """
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_int"),
        builtin_names=["read_file"],
    )

    body = "\n".join(f"L{i}" for i in range(1, 10_001))
    (tmp_path / "data" / "huge.txt").write_text(body + "\n", encoding="utf-8")

    ctx = _ctx(tmp_path, tool_call_id="tc_paginate_02")
    pages: list[dict] = []
    offset = 0
    for _ in range(5):
        r = await subsystem.invoke_tool(
            "read_file",
            {"path": "data/huge.txt", "offset": offset, "limit": 2000},
            ctx,
        )
        pages.append(r.result)
        if not r.result["truncated"]:
            break
        offset = r.result["next_offset"]

    assert len(pages) == 5
    assert sum(p["lines_returned"] for p in pages) == 10_000
    # All pages agree on total_lines (a stable property of the file).
    assert {p["total_lines"] for p in pages} == {10_000}
    # Final page closes pagination.
    assert pages[-1]["truncated"] is False
    assert pages[-1]["next_offset"] is None


# ===========================================================================
# TC-TOOLS-OVERFLOW-03 — line-too-long surfaces cleanly
# ===========================================================================


async def test_tc_tools_overflow_03_read_file_single_huge_line(
    tmp_path: Path,
) -> None:
    """A 2 MiB single line with max_bytes=1 MiB raises file.line_too_long
    (Recoverable) with the catalogued detail fields. Confirms the
    binary-masquerading-as-text guard surfaces the NEW error code
    rather than the old file.too_large."""
    subsystem = _subsystem(tmp_path)
    await subsystem.register_for_agent(
        agent_id=AgentId("ag_int"),
        builtin_names=["read_file"],
    )
    (tmp_path / "data" / "binary.txt").write_bytes(b"x" * (2 * 1024 * 1024))

    ctx = _ctx(tmp_path, tool_call_id="tc_line_too_long_03")
    with pytest.raises(FileLineTooLong) as excinfo:
        await subsystem.invoke_tool(
            "read_file",
            {"path": "data/binary.txt", "max_bytes": 1 * 1024 * 1024},
            ctx,
        )
    detail = excinfo.value.detail
    assert detail["line_number"] == 0
    assert detail["line_byte_length"] == 2 * 1024 * 1024
    assert detail["max_bytes"] == 1 * 1024 * 1024
