"""bug-094 T8/T9 — WorkflowNodeRow rendered preview + workflow-inputs-used.

- T8: ``set_command_preview(command, rendered=...)`` renders the RENDERED
  form as the primary ``command:`` line with the raw template on a
  subordinate ``template:`` line.
- T9: ``set_workflow_inputs_used`` surfaces a ``workflow inputs used:``
  sub-block below ``bound inputs:`` on the live bash input body.

Body text is read out of the mounted ``input`` NodeSection's body Static
(same pattern as ``test_bug063_failed_node_sections``).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


class _Host(App[None]):
    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


def _input_body_text(row: WorkflowNodeRow) -> str:
    """Combined body text of the row's mounted ``input`` section Statics."""
    parts: list[str] = []
    contents = row.query_one(Collapsible.Contents)
    for child in contents.walk_children():
        if isinstance(child, Static) and child.has_class("node-command-preview"):
            parts.append(str(getattr(child, "content", "") or child.render()))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_rendered_command_primary_template_subordinate() -> None:
    """T8: rendered command is the primary line; raw template subordinate."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="fetch_jd", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview(
            "python -c '{{ inputs.url }}'",
            rendered="python -c 'https://x'",
        )
        await pilot.pause()

        body = _input_body_text(row)
        assert "command:\npython -c 'https://x'" in body
        assert "template:\npython -c '{{ inputs.url }}'" in body
        # Ordering: rendered command must come BEFORE the template line.
        assert body.index("python -c 'https://x'") < body.index("template:")


@pytest.mark.asyncio
async def test_command_single_line_when_no_rendered() -> None:
    """T8 corollary: no `rendered` ⇒ single `command:` line, no `template:`."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="fetch_jd", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("echo hi")
        await pilot.pause()

        body = _input_body_text(row)
        assert "command:\necho hi" in body
        assert "template:" not in body


@pytest.mark.asyncio
async def test_command_single_line_when_rendered_equals_raw() -> None:
    """T8 corollary: rendered == raw (string equality) ⇒ single line."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="fetch_jd", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("echo hi", rendered="echo hi")
        await pilot.pause()

        body = _input_body_text(row)
        assert "command:\necho hi" in body
        assert "template:" not in body


@pytest.mark.asyncio
async def test_workflow_inputs_used_sub_block() -> None:
    """T9: `workflow inputs used:` block sits below `bound inputs:`."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="fetch_jd", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # Stash bound inputs + workflow-inputs-used BEFORE the command
        # mounts the section (renderer ordering), then mount the command.
        row.set_bound_inputs({"job_description": "a JD"})
        row.set_workflow_inputs_used({"url": "https://x"})
        row.set_command_preview("get {{ inputs.url }}", rendered="get https://x")
        await pilot.pause()

        body = _input_body_text(row)
        assert "bound inputs:" in body
        assert "job_description: a JD" in body
        assert "workflow inputs used:" in body
        assert "url: https://x" in body
        # Ordering: bound inputs block precedes workflow-inputs-used block.
        assert body.index("bound inputs:") < body.index("workflow inputs used:")


@pytest.mark.asyncio
async def test_workflow_inputs_used_out_of_order_refreshes_body() -> None:
    """T9 corollary: a `set_workflow_inputs_used` AFTER the section mounts
    refreshes the live body in place (adopted-placeholder / racy ordering)."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="fetch_jd", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("get {{ inputs.url }}", rendered="get https://x")
        await pilot.pause()
        # Arrives AFTER the section is already mounted.
        row.set_workflow_inputs_used({"url": "https://x"})
        await pilot.pause()

        body = _input_body_text(row)
        assert "workflow inputs used:" in body
        assert "url: https://x" in body


@pytest.mark.asyncio
async def test_value_preview_compute_row() -> None:
    """bug-094: compute `value` preview renders rendered + template lines."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="parse", node_type="kolsch/nodes/compute")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_value_preview("{{ inputs.n }}", rendered='"42"')
        await pilot.pause()

        body = _input_body_text(row)
        assert "value:" in body
        assert '"42"' in body
        assert "template:" in body


@pytest.mark.asyncio
async def test_workflow_binds_preview_row() -> None:
    """bug-094: nested-workflow row shows ref + per-bind rendered/template."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="sub", node_type="kolsch/nodes/workflow")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_workflow_binds_preview(
            "demo/workflows/child",
            {"url": "{{ inputs.url }}"},
            {"url": '"https://x"'},
        )
        await pilot.pause()

        body = _input_body_text(row)
        assert "ref: demo/workflows/child" in body
        assert "url:" in body
        assert '"https://x"' in body
        assert "template:" in body
