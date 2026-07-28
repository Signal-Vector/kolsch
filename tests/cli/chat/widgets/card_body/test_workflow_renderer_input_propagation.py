"""bug-094 T7 — WorkflowRenderer wires the new NODE_STARTED payload fields.

Feeds a synthetic ``NODE_STARTED`` (bash) whose nested domain payload
carries both ``node_config_preview`` (``command`` + ``rendered_command``)
and ``workflow_inputs_used``, then asserts the mounted row's input-section
body shows the rendered command AND the ``workflow inputs used:`` block.

The back-compat scenario (same event WITHOUT the two new fields) proves
AC #7 — ancient sessions render exactly as they do today.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from textual.widgets import Collapsible, Static

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


_WIRE = "kolsch.workflow.progress"


def _node_started(
    *,
    node_name: str,
    node_config_preview: dict[str, Any] | None = None,
    workflow_inputs_used: dict[str, Any] | None = None,
    bound_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a NODE_STARTED wire payload with an optional bug-094 domain."""
    ts = datetime(2026, 7, 26, tzinfo=UTC)
    inner: dict[str, Any] = {
        "node_name": node_name,
        "invocation_id": f"inv_{node_name}",
        "type": "kolsch/nodes/bash",
        "inputs": bound_inputs or {},
    }
    if node_config_preview is not None:
        inner["node_config_preview"] = node_config_preview
    if workflow_inputs_used is not None:
        inner["workflow_inputs_used"] = workflow_inputs_used
    return {
        "run_id": "wrk_test",
        "workflow_ref": "kolsch/workflows/test",
        "kind": WorkflowEventKind.NODE_STARTED.value,
        "event_id": "ev_test",
        "ts": ts.isoformat(),
        "trace_id": "tr_test",
        "node_name": node_name,
        "invocation_id": f"inv_{node_name}",
        "payload": inner,
    }


def _input_body_text(row: WorkflowNodeRow) -> str:
    parts: list[str] = []
    contents = row.query_one(Collapsible.Contents)
    for child in contents.walk_children():
        if isinstance(child, Static) and child.has_class("node-command-preview"):
            parts.append(str(getattr(child, "content", "") or child.render()))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_renderer_wires_rendered_command_and_inputs_used(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T7: the row surfaces the rendered command + workflow-inputs-used."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _node_started(
                    node_name="fetch_jd",
                    node_config_preview={
                        "command": "get {{ inputs.url }}",
                        "rendered_command": "get https://x",
                    },
                    workflow_inputs_used={"url": "https://x"},
                ),
            )
        )
        await pilot.pause()

        rows = list(renderer.query(WorkflowNodeRow))
        assert len(rows) == 1
        body = _input_body_text(rows[0])
        assert "command:\nget https://x" in body
        assert "template:\nget {{ inputs.url }}" in body
        assert "workflow inputs used:" in body
        assert "url: https://x" in body


@pytest.mark.asyncio
async def test_renderer_back_compat_without_new_fields(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T7 back-compat: a NODE_STARTED lacking the new fields renders as today
    — raw command, single line, no `workflow inputs used:` block."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _node_started(
                    node_name="fetch_jd",
                    node_config_preview={"command": "get {{ inputs.url }}"},
                ),
            )
        )
        await pilot.pause()

        rows = list(renderer.query(WorkflowNodeRow))
        assert len(rows) == 1
        body = _input_body_text(rows[0])
        assert "command:\nget {{ inputs.url }}" in body
        assert "template:" not in body
        assert "workflow inputs used:" not in body
