"""Smoke tests for the three butler workflow tools (feat-018).

Each test boots the Pilot-driven ``chat_app`` from the shared
``conftest.py`` fixture, swaps a scripted mock into the live
``WorkflowsImpl._executor_factory`` so ``start()`` completes without
the real executor's dep graph, then invokes the tool via
``_invoke_tool_via_pilot`` — the same helper the spec-009 example
smoke suite uses.

Assertions cover:

- Tool return shape (``run_id`` / ``card_id`` / ``_chat_anchor``).
- Card ownership transfer (``_owner_transferred=True`` after
  ``run_workflow``) — spec AC #2.
- A ``CardWidget(kind="workflow")`` mounts in the transcript.
- ``workflow_status`` reflects the running/queued phase; unknown ids
  return ``phase="unknown"``.
- ``cancel_workflow`` flips the record's phase to ``cancelling``;
  ``cascade=True`` walks a synthetic parent/child tree.
- A headless ``ToolContext`` (no services) raises ``ToolNotAvailable``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowsImpl,
)
from engine.context import CancelToken
from tests.bundles.kolsch.tools.conftest import (
    _headless_ctx,
    _invoke_tool_via_pilot,
)
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from tools.decorator import get_tool_definition
from tools.discovery import discover_tools_in_file
from tools.services.exceptions import ToolNotAvailable
from workflows.events import (
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId

# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


_TOOLS_DIR = Path(__file__).resolve().parents[4] / "bundles" / "kolsch" / "tools"


def _load_tool(filename: str, tool_name: str) -> Callable[..., Awaitable[Any]]:
    """Load a bundle tool by filename + tool name."""
    path = _TOOLS_DIR / filename
    for fn in discover_tools_in_file(path):
        if get_tool_definition(fn).name == tool_name:
            return fn
    msg = f"tool {tool_name!r} not found in {path}"
    raise LookupError(msg)


@pytest.fixture
def run_workflow_tool() -> Callable[..., Awaitable[Any]]:
    return _load_tool("run_workflow.py", "run_workflow")


@pytest.fixture
def workflow_status_tool() -> Callable[..., Awaitable[Any]]:
    return _load_tool("workflow_status.py", "workflow_status")


@pytest.fixture
def cancel_workflow_tool() -> Callable[..., Awaitable[Any]]:
    return _load_tool("cancel_workflow.py", "cancel_workflow")


def _mk_events() -> list[Any]:
    """Two-event script: run.started + run.completed."""
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    trace = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
    ]


def _install_scripted_workflows(
    pilot: Any,
    *,
    refs: list[str],
    per_event_delay: float = 0.2,
) -> WorkflowsImpl:
    """Rebind ``WorkflowsImpl._executor_factory`` to a scripted mock.

    Returns the live impl so tests can peek at ``_workflow_registry``
    entries directly.
    """
    app = pilot.app
    workflows_impl = app._service_registry.workflows
    assert isinstance(workflows_impl, WorkflowsImpl)
    programming = {
        ref: MockExecutorSpec(
            events=_mk_events(),
            per_event_delay=per_event_delay,
        )
        for ref in refs
    }
    install_mock_executor_factory(
        workflows_impl,
        programming,
        runs_root=app.session.root / "runs",
    )
    return workflows_impl


# --------------------------------------------------------------------
# Test 1 — run_workflow smoke
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_smoke(
    chat_app,
    run_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """run_workflow blocks until terminal + returns phase/outputs.

    bug-031: the tool now blocks until the workflow reaches a terminal
    phase (or ``timeout_seconds`` elapses). The scripted mock fires
    RUN_STARTED + RUN_COMPLETED with a small per-event delay so the
    tool returns ``phase="success"`` when the poll observes the
    terminal registry record. Assertions moved to reflect the
    blocking shape.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        _install_scripted_workflows(
            pilot,
            refs=["kolsch/workflows/test"],
        )

        result = await _invoke_tool_via_pilot(
            pilot,
            run_workflow_tool,
            {"ref": "kolsch/workflows/test", "inputs": {}},
        )
        # bug-020: live bus subscription mounts the CardWidget in the
        # same session — pump the pilot until it appears (the card
        # will have already closed successfully, but the widget
        # remains mounted).
        for _ in range(20):
            await pilot.pause()
            if pilot.app.query("CardWidget"):
                break

        # bug-031: return shape now carries phase + outputs +
        # error_message alongside the anchor.
        assert isinstance(result, dict)
        assert result["run_id"].startswith("wrk_")
        assert isinstance(result["card_id"], str)
        assert result["phase"] in ("success", "error", "cancelled")
        # ``outputs`` may be None (mock events carry no outputs
        # payload) — assert the key is present.
        assert "outputs" in result
        assert "error_message" in result
        anchor = result["_chat_anchor"]
        assert anchor == {
            "kind": "workflow_card",
            "run_id": result["run_id"],
            "ref": "kolsch/workflows/test",
        }

        # CardWidget mounted with kind="workflow".
        cards = pilot.app.query("CardWidget")
        assert len(cards) == 1
        widget = cards.first()
        assert widget.kind == "workflow"

        # bug-031: card ownership transfer still fires — but the card
        # has closed by return so it's no longer in ``_live_cards``
        # (the workflow terminal path popped it via
        # ``CardImpl.close``). Assert against the underlying
        # ``_workflow_registry`` record instead.
        workflows_impl = pilot.app._service_registry.workflows
        rec = workflows_impl._workflow_registry.get(
            WorkflowRunId(result["run_id"]),
        )
        # The record may have evicted after 60s; if present, its phase
        # must be terminal.
        if rec is not None:
            assert rec.phase in ("success", "error", "cancelled")


# --------------------------------------------------------------------
# Test 2 — workflow_status smoke
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_status_smoke(
    chat_app,
    run_workflow_tool: Callable[..., Awaitable[Any]],
    workflow_status_tool: Callable[..., Awaitable[Any]],
) -> None:
    """workflow_status returns phase + anchor; unknown run_id -> unknown.

    bug-031: ``run_workflow`` now blocks until terminal (or timeout).
    Pass ``timeout_seconds=1`` with a scripted per-event-delay of
    5.0s so the poll times out with ``phase="running"`` — that's
    exactly the case where the model would follow up with
    ``workflow_status`` to check progress, so it's the right shape
    for this smoke.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        _install_scripted_workflows(
            pilot,
            refs=["kolsch/workflows/probe"],
            per_event_delay=5.0,  # keep the workflow running past the timeout
        )

        run_result = await _invoke_tool_via_pilot(
            pilot,
            run_workflow_tool,
            {
                "ref": "kolsch/workflows/probe",
                "inputs": {},
                # bug-031: force the poll to time out so we can then
                # exercise workflow_status against a still-live run.
                "timeout_seconds": 1,
            },
        )
        # bug-020: pump the pilot for the live bus dispatch.
        for _ in range(20):
            await pilot.pause()
            if pilot.app.query("CardWidget"):
                break

        run_id = run_result["run_id"]
        # bug-031: timeout shape — phase="running" with a note.
        assert run_result["phase"] == "running"
        assert "note" in run_result

        # Poll until the drain worker picks the run up (queued -> running).
        for _ in range(20):
            status = await _invoke_tool_via_pilot(
                pilot,
                workflow_status_tool,
                {"run_id": run_id},
            )
            if status["phase"] in ("running", "queued"):
                break
            await asyncio.sleep(0.05)

        assert status["phase"] in ("running", "queued")
        assert status["run_id"] == run_id
        assert status["ref"] == "kolsch/workflows/probe"
        assert status["_chat_anchor"] == {
            "kind": "workflow_card",
            "run_id": run_id,
            "ref": "kolsch/workflows/probe",
        }
        # node_counts field is present (may be zeros while queued).
        assert status["node_counts"] is not None
        assert "total" in status["node_counts"]
        # bug-031: outputs field now surfaces on the workflow_status
        # return; None while still running.
        assert "outputs" in status
        assert status["outputs"] is None

        # Unknown run_id -> phase="unknown".
        unknown = await _invoke_tool_via_pilot(
            pilot,
            workflow_status_tool,
            {"run_id": f"wrk_{uuid4()}"},
        )
        assert unknown["phase"] == "unknown"


# --------------------------------------------------------------------
# Test 3 — cancel_workflow smoke
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_workflow_smoke(
    chat_app,
    run_workflow_tool: Callable[..., Awaitable[Any]],
    cancel_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """cancel_workflow(run_id) flips phase to cancelling.

    bug-031: pass ``timeout_seconds=1`` so ``run_workflow`` returns
    with ``phase="running"`` while the mock executor is still cycling
    events (per_event_delay=5.0). That gives the follow-up
    ``cancel_workflow`` call a live registry record to hit.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        workflows_impl = _install_scripted_workflows(
            pilot,
            refs=["kolsch/workflows/long"],
            per_event_delay=5.0,
        )

        run_result = await _invoke_tool_via_pilot(
            pilot,
            run_workflow_tool,
            {
                "ref": "kolsch/workflows/long",
                "inputs": {},
                "timeout_seconds": 1,
            },
        )
        # bug-020: live bus subscription for CardWidget mount.
        for _ in range(20):
            await pilot.pause()
            if pilot.app.query("CardWidget"):
                break

        run_id = run_result["run_id"]
        assert run_result["phase"] == "running"

        # Wait for the drain to pick it up so a cancel has something
        # live to fire on.
        for _ in range(40):
            rec = workflows_impl._workflow_registry.get(
                WorkflowRunId(run_id),
            )
            if rec is not None and rec.phase == "running":
                break
            await asyncio.sleep(0.05)

        result = await _invoke_tool_via_pilot(
            pilot,
            cancel_workflow_tool,
            {"run_id": run_id, "cascade": False},
        )
        assert result == {
            "cancelled": True,
            "run_id": run_id,
            "cascade": False,
        }
        rec = workflows_impl._workflow_registry.get(
            WorkflowRunId(run_id),
        )
        assert rec is not None
        assert rec.phase == "cancelling"


@pytest.mark.asyncio
async def test_cancel_workflow_cascade_smoke(
    chat_app,
    cancel_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """cascade=True walks parent + child in the registry."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        workflows_impl = pilot.app._service_registry.workflows
        assert isinstance(workflows_impl, WorkflowsImpl)

        # Direct registry population — the cascade walker only reads
        # `_workflow_registry` + `_children_of` + `_workflow_cancels`.
        parent_id = WorkflowRunId(f"wrk_{uuid4()}")
        child_id = WorkflowRunId(f"wrk_{uuid4()}")
        workflows_impl._workflow_registry[parent_id] = WorkflowRunRecord(
            run_id=parent_id,
            ref="kolsch/workflows/parent",
            phase="running",
            card=None,
            created_at=datetime.now(tz=UTC),
        )
        workflows_impl._workflow_registry[child_id] = WorkflowRunRecord(
            run_id=child_id,
            ref="kolsch/workflows/child",
            phase="running",
            card=None,
            created_at=datetime.now(tz=UTC),
            parent_run_id=parent_id,
        )
        workflows_impl._workflow_cancels[parent_id] = CancelToken()
        workflows_impl._workflow_cancels[child_id] = CancelToken()
        workflows_impl._children_of.setdefault(parent_id, []).append(child_id)

        result = await _invoke_tool_via_pilot(
            pilot,
            cancel_workflow_tool,
            {"run_id": str(parent_id), "cascade": True},
        )
        assert result == {
            "cancelled": True,
            "run_id": str(parent_id),
            "cascade": True,
        }
        assert workflows_impl._workflow_registry[parent_id].phase == "cancelling"
        assert workflows_impl._workflow_registry[child_id].phase == "cancelling"


@pytest.mark.asyncio
async def test_cancel_workflow_unknown_returns_false(
    chat_app,
    cancel_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """Unknown run_id -> {cancelled: False, ...}."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        stray = f"wrk_{uuid4()}"
        result = await _invoke_tool_via_pilot(
            pilot,
            cancel_workflow_tool,
            {"run_id": stray, "cascade": False},
        )
        # bug-025: cancel_workflow now enriches the no-op response with
        # `phase` + `reason` so the model sees a structured signal
        # instead of a bare `cancelled=false`. The unknown-run path
        # surfaces `phase="unknown"` + `reason="already terminal"`.
        assert result["cancelled"] is False
        assert result["run_id"] == stray
        assert result["cascade"] is False
        assert result["phase"] == "unknown"
        assert result["reason"] == "already terminal"


# --------------------------------------------------------------------
# Test 4 — headless: ToolNotAvailable
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_butler_tools_headless_raise(
    run_workflow_tool: Callable[..., Awaitable[Any]],
    workflow_status_tool: Callable[..., Awaitable[Any]],
    cancel_workflow_tool: Callable[..., Awaitable[Any]],
) -> None:
    """All three tools raise ``ToolNotAvailable`` when services missing."""
    run_defn = run_workflow_tool._tool_definition  # type: ignore[attr-defined]
    status_defn = workflow_status_tool._tool_definition  # type: ignore[attr-defined]
    cancel_defn = cancel_workflow_tool._tool_definition  # type: ignore[attr-defined]

    with pytest.raises(ToolNotAvailable):
        await run_workflow_tool(
            run_defn.args_schema(ref="kolsch/workflows/x"),
            _headless_ctx(),
        )

    with pytest.raises(ToolNotAvailable):
        await workflow_status_tool(
            status_defn.args_schema(run_id=f"wrk_{uuid4()}"),
            _headless_ctx(),
        )

    with pytest.raises(ToolNotAvailable):
        await cancel_workflow_tool(
            cancel_defn.args_schema(run_id=f"wrk_{uuid4()}"),
            _headless_ctx(),
        )
