"""``read_file`` line-based / soft-truncation regression suite.

Pins the QoL §9.5 / §9.6 return shape + semantics for the bundle
``read_file`` (loaded via ``discover_tools_in_file``) and — by
proxy through the shared ``_paginate`` logic — the builtin
``make_read_file`` factory.

The five paths exercised:

1. ``default_returns_numbered_lines`` — small file, defaults, the
   shape Step 1 §9.5 pinned.
2. ``offset_limit_paginates`` — 200-line file with ``offset=50,
   limit=100`` returns lines 51-150, ``truncated=True``,
   ``next_offset=150``.
3. ``eof_within_limit`` — limit exceeds total_lines, returns
   what's there, ``truncated=False``, ``next_offset=None``.
4. ``line_too_long_raises`` — pathological 2 MiB single line with
   ``max_bytes=1 MiB`` raises ``file.line_too_long`` with the
   right detail fields.
5. ``large_file_soft_truncates`` — 10000-line file with default
   ``limit=2000`` soft-truncates instead of raising the old
   ``file.too_large``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from errors import FileLineTooLong
from files import PathPolicy
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext
from tools.discovery import discover_tools_in_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_BUNDLE_READ_FILE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "tools"
    / "read_file.py"
)


def _bundle_read_file() -> Callable[..., Awaitable[object]]:
    for fn in discover_tools_in_file(_BUNDLE_READ_FILE):
        defn = fn._tool_definition  # type: ignore[attr-defined]
        if defn.name == "read_file":
            return fn
    msg = f"could not find bundle read_file tool in {_BUNDLE_READ_FILE}"
    raise RuntimeError(msg)


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={},
        workspace_root=tmp_path,
        path_policy=PathPolicy(allowed_paths=["."], denied_paths=[]),
        tool_call_id="tc_read_unit",
    )


# ---------------------------------------------------------------------------
# 1 — default shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_default_returns_numbered_lines(
    tmp_path: Path,
) -> None:
    """A 3-line file read with defaults returns numbered content,
    matching total_lines, lines_returned, truncated=False,
    next_offset=None. cat -n style numbering with right-aligned
    width based on total_lines."""
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = await fn(Args(path="a.txt"), _ctx(tmp_path))

    assert result["total_lines"] == 3
    assert result["lines_returned"] == 3
    assert result["truncated"] is False
    assert result["next_offset"] is None
    # Width = 1 because total_lines=3 → "1\talpha\n2\tbeta\n3\tgamma\n"
    assert result["content"] == "1\talpha\n2\tbeta\n3\tgamma\n"


# ---------------------------------------------------------------------------
# 2 — offset + limit paginate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_offset_limit_paginates(tmp_path: Path) -> None:
    """200-line file; offset=50, limit=100 → lines 51-150 with
    truncated=True and next_offset=150. Width is 3 because
    total_lines=200 → '<3-wide-right-aligned>\\t<line>'."""
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    body = "\n".join(f"line-{i}" for i in range(1, 201))  # 200 lines
    (tmp_path / "big.txt").write_text(body + "\n", encoding="utf-8")

    result = await fn(Args(path="big.txt", offset=50, limit=100), _ctx(tmp_path))

    assert result["total_lines"] == 200
    assert result["lines_returned"] == 100
    assert result["truncated"] is True
    assert result["next_offset"] == 150
    # First rendered line is line 51 (offset is 0-indexed, so line 51
    # is index 50). Numbering uses 1-indexed display.
    first = result["content"].splitlines()[0]
    assert first == " 51\tline-51"
    last = result["content"].splitlines()[-1]
    assert last == "150\tline-150"


# ---------------------------------------------------------------------------
# 3 — EOF within limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_eof_within_limit(tmp_path: Path) -> None:
    """50-line file with default limit=2000 returns all 50 lines,
    truncated=False, next_offset=None."""
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    body = "\n".join(f"row-{i}" for i in range(1, 51))
    (tmp_path / "short.txt").write_text(body + "\n", encoding="utf-8")

    result = await fn(Args(path="short.txt"), _ctx(tmp_path))

    assert result["total_lines"] == 50
    assert result["lines_returned"] == 50
    assert result["truncated"] is False
    assert result["next_offset"] is None


# ---------------------------------------------------------------------------
# 4 — single-line cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_line_too_long_raises(tmp_path: Path) -> None:
    """A file with one 2 MiB line + max_bytes=1 MiB raises
    file.line_too_long with line_number=0, the actual byte length,
    and the configured max_bytes."""
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    huge = b"x" * (2 * 1024 * 1024)  # 2 MiB, no newline
    (tmp_path / "huge.txt").write_bytes(huge)

    with pytest.raises(FileLineTooLong) as excinfo:
        await fn(
            Args(path="huge.txt", max_bytes=1 * 1024 * 1024),
            _ctx(tmp_path),
        )
    detail = excinfo.value.detail
    assert detail["line_number"] == 0
    assert detail["line_byte_length"] == 2 * 1024 * 1024
    assert detail["max_bytes"] == 1 * 1024 * 1024
    assert detail["path"] == "huge.txt"


# ---------------------------------------------------------------------------
# 5 — soft truncation of a previously-refused case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_large_file_soft_truncates(tmp_path: Path) -> None:
    """10000-line file with default limit=2000: returns the first
    2000 lines, truncated=True, next_offset=2000, total_lines=10000.

    Pre-QoL this raised file.too_large. The whole point of the QoL
    is that no read returns 'nothing' — large files paginate.
    """
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]

    body = "\n".join(f"L{i}" for i in range(1, 10_001))
    (tmp_path / "huge-but-paginates.txt").write_text(body + "\n", encoding="utf-8")

    result = await fn(Args(path="huge-but-paginates.txt"), _ctx(tmp_path))

    assert result["total_lines"] == 10_000
    assert result["lines_returned"] == 2000
    assert result["truncated"] is True
    assert result["next_offset"] == 2000
    # Width = 5 because total_lines has 5 digits.
    first = result["content"].splitlines()[0]
    assert first == "    1\tL1"


# ---------------------------------------------------------------------------
# Backward-compat: a call WITHOUT offset/limit still works (defaults)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_backward_compat_no_offset_limit(
    tmp_path: Path,
) -> None:
    """An existing caller that passes only ``path`` (no offset, no
    limit, no max_bytes) gets the defaults — confirms the args
    schema didn't break old configs."""
    fn = _bundle_read_file()
    Args = fn._tool_definition.args_schema  # type: ignore[attr-defined]
    (tmp_path / "x.txt").write_text("only line", encoding="utf-8")

    # Mimics an older config dispatching with the legacy single arg.
    args_obj = Args.model_validate({"path": "x.txt"})
    result = await fn(args_obj, _ctx(tmp_path))

    assert result["total_lines"] == 1
    assert result["content"] == "1\tonly line\n"
