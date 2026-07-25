"""qol-090 Test 6 — observer forwards `ToolExecutionFailed.recovery_hint`.

Pins the observer's generic `PlatformError` arm: when a tool raises
`ToolExecutionFailed(recovery_hint="X")`, `X` reaches both

- the emitted `tool.failed` event's `data.recovery_hint` field (the
  chat renderer surfaces it inside the failure capsule), and
- the return-payload dict at `payload["error"]["recovery_hint"]` (the
  framework forwards this to the LLM's next turn so the agent tells
  the user the exact fix instead of guessing).

Mirrors the existing `ToolUnauthorized` arm's shape, extended to
every Diagnostic tool exception that opts in via `recovery_hint`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from engine.framework import ToolCall
from engine.observers import _ObservedToolHandle
from errors import ToolExecutionFailed
from tools.decorator import ToolDefinition


class _EmptyArgs(BaseModel):
    """No-arg schema — the tool's args don't matter for this test."""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — unused
    return None


class _RaisingSubsystem:
    """Minimal tools_subsystem stub that raises on invoke_tool."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def invoke_tool(self, **_: Any) -> Any:
        raise self._exc


async def test_recovery_hint_flows_through_observer() -> None:
    """`ToolExecutionFailed(recovery_hint=...)` reaches both event + LLM payload."""
    hint_text = (
        "cd /path && uv sync --extra browse && uv run playwright install chromium"
    )
    exc = ToolExecutionFailed(
        tool_name="probe",
        error_class="BrowseUrlExtraNotInstalled",
        recovery_hint=hint_text,
    )
    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    defn = ToolDefinition(
        name="probe",
        description="d",
        args_schema=_EmptyArgs,
        func=_noop_fn,
    )
    handle = _ObservedToolHandle(
        emit=_capture_emit,
        agent_id="ag_test",
        session_id="ses_00000000-0000-0000-0000-000000000001",
        thread_id="th_00000000-0000-0000-0000-000000000002",
        tools_subsystem=_RaisingSubsystem(exc),
        defn=defn,
    )

    result = await handle(ToolCall(name="probe", arguments={}, tool_call_id="tc_1"))

    # The `tool.failed` event carries the hint on the typed data field.
    failed_events = [e for e in emitted if e["type_"].value == "tool.failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["data"]["recovery_hint"] == hint_text

    # The framework-visible return dict also carries the hint on
    # `error.recovery_hint`, so the LLM sees it on the next turn.
    assert isinstance(result, dict)
    assert result["error"]["recovery_hint"] == hint_text


async def test_platform_error_without_recovery_hint_still_works() -> None:
    """Regression: an exception without a `.recovery_hint` attribute doesn't crash."""
    exc = ToolExecutionFailed(tool_name="probe", error_class="SomeClass")
    assert exc.recovery_hint is None

    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    defn = ToolDefinition(
        name="probe",
        description="d",
        args_schema=_EmptyArgs,
        func=_noop_fn,
    )
    handle = _ObservedToolHandle(
        emit=_capture_emit,
        agent_id="ag_test",
        session_id="ses_00000000-0000-0000-0000-000000000001",
        thread_id="th_00000000-0000-0000-0000-000000000002",
        tools_subsystem=_RaisingSubsystem(exc),
        defn=defn,
    )

    result = await handle(ToolCall(name="probe", arguments={}, tool_call_id="tc_2"))

    failed_events = [e for e in emitted if e["type_"].value == "tool.failed"]
    assert len(failed_events) == 1
    # recovery_hint absent (None) — event field is None; return payload
    # omits the `recovery_hint` key entirely to match legacy shape.
    assert failed_events[0]["data"]["recovery_hint"] is None
    assert isinstance(result, dict)
    assert "recovery_hint" not in result["error"]
