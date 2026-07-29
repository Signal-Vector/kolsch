"""qol-108 — WorkflowNodeRow write_file preview + bash section split.

Tests 3, 4, 5, 7, 8, 9, 10 from the spec:
- Test 3: write_file row mounts ``▶ path`` + ``▶ content`` (in order).
- Test 4: bash row splits into ``▶ input`` (binds) + ``▶ command``.
- Test 5: content body caps at ``_CAP_MAX_CHARS`` with an exact JSONL-
  tail marker substring.
- Test 7: path with a differing rendered form shows a ``template:`` sub-line.
- Test 8: no ``template:`` sub-line when rendered == raw for either
  ``path`` or ``content``.
- Test 9: DOM ordering pinned for both write_file and bash rows.
- Test 10: bash ``▶ input`` NOT mounted when both binds and workflow-
  inputs-used are empty (empty-block-suppressed convention).

The body-Static reader mirrors ``test_workflow_node_row_rendered_preview.py``:
walk ``Collapsible.Contents`` and collect every ``node-command-preview``
Static's body text.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import (
    _CAP_MARKER_TEMPLATE,
    _CAP_MAX_CHARS,
    WorkflowNodeRow,
)


class _Host(App[None]):
    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


def _section_body(section: NodeSection) -> str:
    """Concatenate every ``node-command-preview`` Static inside ``section``.

    Filters out the section's own ``CollapsibleTitle`` (a bare ``Static``
    sibling of the body) so tests can assert on the body text alone.
    """
    parts: list[str] = []
    for child in section.walk_children():
        if isinstance(child, Static) and child.has_class("node-command-preview"):
            parts.append(str(getattr(child, "content", "") or child.render()))
    return "\n".join(parts)


def _sections_in_order(row: WorkflowNodeRow) -> list[NodeSection]:
    """Return the row's top-level ``NodeSection`` children in DOM order.

    ``Collapsible.Contents`` is the row's inner container; top-level
    sections are its direct children (or grandchildren via the
    Collapsible chevron path).
    """
    contents = row.query_one(Collapsible.Contents)
    return list(contents.query(NodeSection))


@pytest.mark.asyncio
async def test_write_file_row_mounts_two_sections() -> None:
    """Test 3: write_file row mounts ``▶ path`` + ``▶ content``, in order."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="scratch", node_type="kolsch/nodes/write_file")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_write_file_preview(
            "a.md",
            "hello\nworld",
            rendered_path=None,
            rendered_content=None,
        )
        await pilot.pause()

        sections = _sections_in_order(row)
        titles = [str(s.title).strip() for s in sections]
        # Exactly two sections in order: path → content.
        assert titles == ["path", "content"], titles
        # Both collapsed by default.
        assert all(s.collapsed for s in sections), [s.collapsed for s in sections]
        # Bodies carry the raw text.
        assert "a.md" in _section_body(sections[0])
        assert "hello\nworld" in _section_body(sections[1])


@pytest.mark.asyncio
async def test_bash_command_lands_in_own_section() -> None:
    """Test 4: bash row splits into ``▶ input`` + ``▶ command`` sections."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"file": "x.txt"})
        row.set_command_preview("cat {{ inputs.file }}", rendered="cat x.txt")
        await pilot.pause()

        sections = _sections_in_order(row)
        titles = [str(s.title).strip() for s in sections]
        assert titles == ["input", "command"], titles

        input_body = _section_body(sections[0])
        assert "bound inputs:" in input_body
        assert "· file: x.txt" in input_body
        # qol-108 ownership shift: ``▶ input`` no longer carries the command.
        assert "command:" not in input_body
        assert "cat" not in input_body

        command_body = _section_body(sections[1])
        assert "cat x.txt" in command_body
        assert "template:" in command_body
        assert "cat {{ inputs.file }}" in command_body


@pytest.mark.asyncio
async def test_content_cap_marks_truncation() -> None:
    """Test 5: content > ``_CAP_MAX_CHARS`` → exact JSONL-tail marker substring."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="dump", node_type="kolsch/nodes/write_file")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        big = "A" * 25_000
        row.set_write_file_preview(
            "big.txt", big, rendered_path=None, rendered_content=None
        )
        await pilot.pause()

        sections = _sections_in_order(row)
        assert [str(s.title).strip() for s in sections] == ["path", "content"]
        content_body = _section_body(sections[1])
        # Starts with 20 000 A's.
        assert content_body.startswith("A" * _CAP_MAX_CHARS)
        # Ends with the exact marker substring (n_more = 5000).
        expected_marker = _CAP_MARKER_TEMPLATE.format(n_more=5000)
        assert expected_marker == "\n… (5000 more chars; see session JSONL)"
        assert content_body.endswith(expected_marker)
        # Total length = 20 000 + len(marker).
        assert len(content_body) == _CAP_MAX_CHARS + len(expected_marker)


@pytest.mark.asyncio
async def test_path_shows_template_when_rendered_differs() -> None:
    """Test 7: rendered path differs from raw → ``template:`` sub-line."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="scratch", node_type="kolsch/nodes/write_file")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_write_file_preview(
            "scratch/{{ inputs.name }}.md",
            "hi",
            rendered_path="scratch/alice.md",
            rendered_content=None,
        )
        await pilot.pause()

        sections = _sections_in_order(row)
        path_body = _section_body(sections[0])
        # Rendered form primary; raw template on a ``template:`` sub-line.
        assert "scratch/alice.md" in path_body
        assert "template:" in path_body
        assert "scratch/{{ inputs.name }}.md" in path_body
        # Ordering: rendered form appears BEFORE the template sub-line.
        assert path_body.index("scratch/alice.md") < path_body.index("template:")


@pytest.mark.asyncio
async def test_no_template_line_when_rendered_matches_raw() -> None:
    """Test 8: rendered == raw → no ``template:`` sub-line on either section."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="scratch", node_type="kolsch/nodes/write_file")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_write_file_preview(
            "static.md",
            "static body",
            rendered_path="static.md",
            rendered_content="static body",
        )
        await pilot.pause()

        sections = _sections_in_order(row)
        assert "template:" not in _section_body(sections[0])
        assert "template:" not in _section_body(sections[1])


@pytest.mark.asyncio
async def test_section_dom_ordering_bash() -> None:
    """Test 9 (bash): ``▶ input`` DOM index < ``▶ command`` DOM index."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"file": "x.txt"})
        row.set_command_preview("cat {{ inputs.file }}", rendered="cat x.txt")
        await pilot.pause()

        sections = _sections_in_order(row)
        titles = [str(s.title).strip() for s in sections]
        assert titles.index("input") < titles.index("command")


@pytest.mark.asyncio
async def test_section_dom_ordering_write_file() -> None:
    """Test 9 (write_file): ``▶ path`` DOM index < ``▶ content`` DOM index."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="scratch", node_type="kolsch/nodes/write_file")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_write_file_preview(
            "a.md", "hi", rendered_path=None, rendered_content=None
        )
        await pilot.pause()

        sections = _sections_in_order(row)
        titles = [str(s.title).strip() for s in sections]
        assert titles.index("path") < titles.index("content")


@pytest.mark.asyncio
async def test_bash_input_section_absent_when_no_binds() -> None:
    """Test 10: bash row with no binds + no workflow-inputs → no ``▶ input``.

    Only the ``▶ command`` section is mounted (empty-block-suppressed
    convention per AC #5).
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_command_preview("echo hi", rendered=None)
        await pilot.pause()

        sections = _sections_in_order(row)
        titles = [str(s.title).strip() for s in sections]
        assert titles == ["command"], titles
