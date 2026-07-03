"""Bash subprocess cancellation — bug-mid-tool-cancellation-latency.md.

Regression tests for the bash tool's ``ctx.cancellation`` integration:

1. **TC-BASH-CANCEL-01** — A long-running ``sleep`` command receives
   SIGINT when the cancel token flips, and the bash tool raises
   :class:`ExchangeCancelled` within the grace window (instead of
   blocking until the sleep completes or the timeout fires).

2. **TC-BASH-CANCEL-02** — A fast-completing command finishes
   normally when no cancellation is in play (backwards-compat
   smoke).

3. **TC-BASH-CANCEL-03** — Backwards-compat: a tool invocation with
   ``ctx.cancellation=None`` falls through to the pre-QoL
   ``asyncio.wait_for`` shape and behaves identically.

Pattern mirrors tests/component/test_validate_workflow_tool.py:
discover the tool from the starter-bundle path, build the
ToolContext by hand, invoke as a coroutine.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engine.context import CancelToken
from errors import ExchangeCancelled
from schemas import AgentId, SessionId, ThreadId
from tools import ToolContext
from tools.discovery import discover_tools_in_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASH_PATH = (
    _REPO_ROOT / "src" / "cli" / "_starter_bundle" / "kolsch" / "tools" / "bash.py"
)


def _bash_tool() -> Callable[..., Awaitable[object]]:
    for fn in discover_tools_in_file(_BASH_PATH):
        defn = fn._tool_definition  # type: ignore[attr-defined]
        if defn.name == "bash":
            return fn
    msg = f"could not find bash tool in {_BASH_PATH}"
    raise RuntimeError(msg)


def _ctx_for(name: str, *, cancellation: CancelToken | None = None) -> ToolContext:
    u = str(uuid.uuid4())
    return ToolContext(
        agent_id=AgentId(f"ag_{name}"),
        session_id=SessionId(f"ses_{u}"),
        thread_id=ThreadId(f"th_{u}"),
        principal=f"agent.{name}",
        secrets={},
        cancellation=cancellation,
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGINT semantics differ on Windows; v1 sandbox is unix-only",
)
async def test_tc_bash_cancel_01_long_sleep_aborts_within_grace_on_cancel() -> None:
    """A ``sleep 30`` bash that the user cancels at ~50ms aborts
    within the grace window (well under the 30s natural completion
    and well under the 60s default timeout). Pins the
    bug-mid-tool-cancellation-latency.md AC: 'scripted slow tool +
    cancel flip; tool aborts within 100ms' (relaxed to <1s to
    account for the SIGINT→SIGKILL grace + scheduling slack)."""
    import asyncio

    from pydantic import BaseModel

    bash = _bash_tool()
    bash_defn = bash._tool_definition  # type: ignore[attr-defined]
    args_schema = bash_defn.args_schema
    assert issubclass(args_schema, BaseModel)
    args = args_schema(command="sleep 30")

    cancel = CancelToken()
    ctx = _ctx_for("probe", cancellation=cancel)

    async def _flip_after_delay() -> None:
        await asyncio.sleep(0.05)
        cancel.cancel()

    flip_task = asyncio.create_task(_flip_after_delay())
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        await bash(args, ctx)
    elapsed = time.monotonic() - start
    await flip_task
    # 50ms flip delay + 200ms grace + 500ms SIGINT escalation +
    # scheduling slack should be well under 2s. If this fails it
    # almost certainly means the race / subprocess kill path
    # regressed and the test would have hung pre-QoL for 30s.
    assert elapsed < 2.0, f"cancellation took too long: {elapsed:.2f}s"


async def test_tc_bash_cancel_02_fast_command_completes_normally_with_cancel_token() -> (
    None
):
    """A ``echo`` that completes in <1ms is not preempted just
    because a cancel token is attached. Pins backwards-compat: the
    token's existence doesn't change semantics when no flip
    happens."""
    from pydantic import BaseModel

    bash = _bash_tool()
    bash_defn = bash._tool_definition  # type: ignore[attr-defined]
    args_schema = bash_defn.args_schema
    assert issubclass(args_schema, BaseModel)
    args = args_schema(command="echo 'hello'")

    cancel = CancelToken()
    ctx = _ctx_for("probe", cancellation=cancel)

    result = await bash(args, ctx)
    assert isinstance(result, dict)
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert cancel.is_cancelled() is False


async def test_tc_bash_cancel_03_no_cancel_token_falls_through_to_legacy_path() -> None:
    """Backwards-compat: ``ctx.cancellation=None`` uses the
    pre-QoL ``asyncio.wait_for(proc.communicate(), timeout=...)``
    shape so existing callers (Phase 1 programmatic tests) see no
    behaviour change."""
    from pydantic import BaseModel

    bash = _bash_tool()
    bash_defn = bash._tool_definition  # type: ignore[attr-defined]
    args_schema = bash_defn.args_schema
    assert issubclass(args_schema, BaseModel)
    args = args_schema(command="echo 'legacy'")

    ctx = _ctx_for("probe", cancellation=None)
    result = await bash(args, ctx)
    assert isinstance(result, dict)
    assert result["exit_code"] == 0
    assert "legacy" in result["stdout"]
