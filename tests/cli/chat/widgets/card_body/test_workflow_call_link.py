"""feat-104 Tests T1-T6 — parent workflow-node → child-card forward link.

The parent's ``kolsch/nodes/workflow`` :class:`WorkflowNodeRow` grows a
focusable :class:`WorkflowCallLink` that scrolls the transcript to the
child sub-workflow's peer card via the existing feat-018
``action_scroll_to_workflow_card``. These tests drive the
:class:`WorkflowRenderer` with the ``workflow.node.called_workflow`` /
``…_completed`` events (previously dropped) and assert the link is
mounted, navigates, updates its status suffix, is replay-safe, gated on
the node type, and degrades gracefully when the child card is absent.

Harness mirrors ``test_workflow_renderer.py`` (``_MockEvent`` +
``_mk_payload`` + ``mock_app_factory``). The ``child_workflow_*`` fields
are nested under ``payload["payload"]`` (the domain-event body), matching
``WorkflowNodeCalledWorkflowPayload``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App, ComposeResult

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowCallLink, WorkflowNodeRow
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from textual.widget import Widget

_WORKFLOW_NODE_TYPE = "kolsch/nodes/workflow"
_WIRE = "kolsch.workflow.progress"


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


def _mk_payload(
    workflow_event_kind: WorkflowEventKind,
    *,
    node_name: str | None = None,
    node_type: str | None = None,
    trace_id: str = "tr_test",
    run_id: str = "wrk_parent",
    ts: datetime | None = None,
    child_workflow_run_id: str | None = None,
    child_workflow_ref: str | None = None,
    child_status: str | None = None,
) -> dict[str, Any]:
    """Match the WIRE_WORKFLOW_PROGRESS payload shape.

    ``child_workflow_*`` / ``child_status`` are nested under
    ``payload["payload"]`` (the domain-event body), mirroring the real
    ``WorkflowNodeCalledWorkflow*Payload``.
    """
    ts_val = ts or datetime(2026, 7, 27, tzinfo=UTC)
    inner: dict[str, Any] = {}
    if node_type is not None:
        inner["type"] = node_type
    if child_workflow_run_id is not None:
        inner["child_workflow_run_id"] = child_workflow_run_id
    if child_workflow_ref is not None:
        inner["child_workflow_ref"] = child_workflow_ref
    if child_status is not None:
        inner["child_status"] = child_status
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "kolsch/workflows/parent",
        "kind": workflow_event_kind.value,
        "event_id": "ev_test",
        "ts": ts_val.isoformat(),
        "trace_id": trace_id,
        "payload": inner,
    }
    if node_name is not None:
        payload["node_name"] = node_name
        payload["invocation_id"] = f"inv_{node_name}"
    return payload


async def _seed_workflow_node(
    renderer: WorkflowRenderer,
    pilot: Any,
    *,
    node_name: str = "call_child",
) -> None:
    """RUN_STARTED + NODE_STARTED(workflow node) so a row exists."""
    renderer.apply_event(_MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED)))
    await pilot.pause()
    renderer.apply_event(
        _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_STARTED,
                node_name=node_name,
                node_type=_WORKFLOW_NODE_TYPE,
            ),
        )
    )
    await pilot.pause()


# ---------------------------------------------------------------------------
# T1 — renderer stamps the child link on the workflow-node row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_called_workflow_mounts_link_and_title_hint(mock_app_factory) -> None:
    """AC #1/#3/#4 — CALLED_WORKFLOW mounts a WorkflowCallLink + title hint."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()

        row = renderer._node_rows["call_child"]
        links = list(row.query(WorkflowCallLink))
        assert len(links) == 1
        link = links[0]
        assert link.run_id == "wrk_child"
        # Collapsed title carries the `▸ <child_ref>` discoverability hint.
        assert "▸ b/workflow/child" in row.title
        # Rendered label is `[▸ b/workflow/child · running]` (feat-018 family).
        rendered = str(link.render())
        assert rendered.startswith("[▸ b/workflow/child · ")
        assert rendered.endswith("]")


# ---------------------------------------------------------------------------
# T2 — link activation calls the app action with the child run_id
# ---------------------------------------------------------------------------


class _SpyApp(App[None]):
    """Host app exposing a spy ``action_scroll_to_workflow_card``."""

    def __init__(self, widget: Widget) -> None:
        self._widget = widget
        self.scroll_calls: list[str] = []
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self._widget

    def action_scroll_to_workflow_card(self, run_id: str) -> None:
        self.scroll_calls.append(run_id)


@pytest.mark.asyncio
async def test_link_activation_calls_scroll_action() -> None:
    """AC #5 — Enter/activate calls action_scroll_to_workflow_card(child)."""
    renderer = WorkflowRenderer()
    app = _SpyApp(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()

        link = next(iter(renderer.query(WorkflowCallLink)))
        link.action_activate()
        await pilot.pause()

        assert app.scroll_calls == ["wrk_child"]


# ---------------------------------------------------------------------------
# T3 — completed updates the status suffix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_called_workflow_completed_updates_status(mock_app_factory) -> None:
    """AC #2/#6 — CALLED_WORKFLOW_COMPLETED(succeeded) updates the suffix."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW_COMPLETED,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_status="succeeded",
                ),
            )
        )
        await pilot.pause()

        link = next(iter(renderer.query(WorkflowCallLink)))
        rendered = str(link.render())
        assert "✓" in rendered
        assert "succeeded" in rendered
        assert "running" not in rendered


@pytest.mark.asyncio
async def test_called_workflow_completed_failed_status(mock_app_factory) -> None:
    """AC #6 — a failed child renders `✗ failed` in the suffix."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW_COMPLETED,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_status="failed",
                ),
            )
        )
        await pilot.pause()

        rendered = str(next(iter(renderer.query(WorkflowCallLink))).render())
        assert "✗" in rendered
        assert "failed" in rendered


# ---------------------------------------------------------------------------
# T4 — non-workflow rows ignore the calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_workflow_row_ignores_child_calls(mock_app_factory) -> None:
    """AC #3 — a bash-type row mounts no link and keeps its title."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    node_name="run_bash",
                    node_type="kolsch/nodes/bash",
                ),
            )
        )
        await pilot.pause()

        row = renderer._node_rows["run_bash"]

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="run_bash",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()

        assert not list(row.query(WorkflowCallLink))
        # The title keeps its bash node identity (the leading glyph is an
        # animated spinner frame that advances each ~80 ms tick, so we
        # assert on the stable node-name + type portion, not the glyph)
        # and gains NO `▸ <child_ref>` forward-link hint.
        assert "run_bash (bash)" in row.title
        assert "▸ b/workflow/child" not in row.title


@pytest.mark.asyncio
async def test_set_child_workflow_direct_gate() -> None:
    """AC #3 — the row-level gate short-circuits a non-workflow row."""
    row = WorkflowNodeRow(node_name="run_bash", node_type="kolsch/nodes/bash")
    row.set_child_workflow("wrk_child", "b/workflow/child")
    assert row._child_link is None
    assert row._child_ref is None


# ---------------------------------------------------------------------------
# T5 — replay-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_called_workflow_mounts_one_link(mock_app_factory) -> None:
    """AC #7 — two CALLED_WORKFLOW events mount exactly one link."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)
        called = _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.CALLED_WORKFLOW,
                node_name="call_child",
                child_workflow_run_id="wrk_child",
                child_workflow_ref="b/workflow/child",
            ),
        )
        renderer.apply_event(called)
        await pilot.pause()
        renderer.apply_event(called)
        await pilot.pause()

        row = renderer._node_rows["call_child"]
        assert len(list(row.query(WorkflowCallLink))) == 1


# ---------------------------------------------------------------------------
# T6 — graceful when the child card is absent
# ---------------------------------------------------------------------------


class _NoActionApp(App[None]):
    """Host app WITHOUT ``action_scroll_to_workflow_card`` (feat-018 fallback)."""

    def __init__(self, widget: Widget) -> None:
        self._widget = widget
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_activation_without_scroll_action_no_crash(caplog) -> None:
    """AC #5 fallback — activating with no resolvable action no-ops + warns.

    Mirrors feat-018's ``WorkflowAnchorButton._activate`` fallback: when
    ``getattr(app, "action_scroll_to_workflow_card", None)`` is ``None``
    the click is logged and ignored (no exception).
    """
    import logging

    renderer = WorkflowRenderer()
    app = _NoActionApp(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_workflow_node(renderer, pilot)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="call_child",
                    child_workflow_run_id="wrk_child",
                    child_workflow_ref="b/workflow/child",
                ),
            )
        )
        await pilot.pause()

        link = next(iter(renderer.query(WorkflowCallLink)))
        with caplog.at_level(logging.WARNING):
            link.action_activate()  # must not raise
        await pilot.pause()
        assert any(
            "action_scroll_to_workflow_card" in rec.message for rec in caplog.records
        )
