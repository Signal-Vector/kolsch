"""bug-063 — Failed workflow node renders Input / Output / Error as peer chevron sections.

These tests encode the NEW contract for a failed
:class:`~cli.chat.widgets.workflow_node_row.WorkflowNodeRow`:

* An **Input** :class:`~cli.chat.widgets.node_section.NodeSection` is
  present for both bash and agent node types on failure — agent-node
  Input on failure is a new render (pre-bug-063 there was no reusable
  agent bound-inputs section).
* An **Output** :class:`NodeSection` is always present on failure with
  the exact
  :data:`~cli.chat.widgets.workflow_node_row._FAILED_OUTPUT_EMPTY_NOTE`
  body — ``node.failed`` payloads carry no output.
* An **Error** :class:`NodeSection` carries the FULL error text
  (``self._error_preview`` is un-re-truncated). The full error is
  NOT in ``row.title`` anymore — only a short marker (``✗ <node>``)
  remains.

All three sections use the same :class:`NodeSection` styling so they
render as visual peers, matching the "After" ASCII mockup in the spec.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import (
    _FAILED_INPUT_EMPTY_NOTE,
    _FAILED_OUTPUT_EMPTY_NOTE,
    WorkflowNodeRow,
)


class _Host(App[None]):
    """Minimal host that mounts a WorkflowNodeRow into a scroll slot."""

    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    return row.query_one(Collapsible.Contents)


def _section_titles(row: WorkflowNodeRow) -> list[str]:
    return [str(s.title) for s in _contents_of(row).query(NodeSection)]


def _section_body_text(section: NodeSection) -> str:
    """Combined body text of a NodeSection's body-slot Static children.

    Walks the section's Contents slot only — this excludes the
    ``CollapsibleTitle`` (which is a sibling of Contents inside the
    Collapsible) so the returned text is the section's body only.
    """
    parts: list[str] = []
    try:
        contents = section.query_one(Collapsible.Contents)
    except Exception:  # noqa: BLE001
        contents = None
    if contents is None:
        return ""
    for child in contents.walk_children():
        if isinstance(child, Static):
            parts.append(str(getattr(child, "content", "") or child.render()))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Input section on failure — bash + agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_bash_node_has_input_section() -> None:
    """Bash node fails → Input NodeSection carries the bound inputs.

    Bash rows already own an ``input`` NodeSection built by
    ``set_command_preview`` on the happy path. bug-063 keeps that
    section on the failure path so the row still exposes bound
    inputs the user can inspect.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"url": "https://example.com"})
        row.set_command_preview("curl {{ inputs.url }}")
        await pilot.pause()

        row.set_state("failed", error_preview="boom")
        await pilot.pause()

        titles = _section_titles(row)
        # Order: Input first (Input / Output / Error).
        assert titles[0] == "input"
        # Input section body carries the bound inputs.
        input_section = next(iter(_contents_of(row).query(NodeSection)))
        body = _section_body_text(input_section)
        assert "bound inputs:" in body
        assert "url" in body
        assert "example.com" in body


@pytest.mark.asyncio
async def test_failed_agent_node_has_input_section() -> None:
    """Agent node fails → Input NodeSection carries the bound inputs.

    Pre-bug-063 agent rows had NO reusable bound-inputs section (agent
    input rows only appear from sub-agent ``USER_MESSAGE`` events, which
    never fire if the node fails early). bug-063 adds a small new
    render so agent Input on failure is a first-class section.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # NODE_STARTED delivered bound inputs (via set_bound_inputs)
        # before the node crashed.
        row.set_bound_inputs(
            {
                "resume_files": ["resume-backend.yaml", "resume-frontend.yaml"],
                "jd_url": "https://example.com/jd",
            }
        )
        await pilot.pause()

        row.set_state("failed", error_preview="jinja2.runtime.Undefined")
        await pilot.pause()

        titles = _section_titles(row)
        assert "input" in titles
        assert titles.index("input") == 0
        sections = list(_contents_of(row).query(NodeSection))
        input_section = next(s for s in sections if str(s.title) == "input")
        body = _section_body_text(input_section)
        assert "bound inputs:" in body
        assert "resume_files" in body
        assert "jd_url" in body


@pytest.mark.asyncio
async def test_failed_node_input_section_empty_note_when_no_bound_inputs() -> None:
    """No bound inputs → Input section still mounted with an empty note.

    Rare case (node failed before ``NODE_STARTED`` delivered inputs).
    The Input section is still present so the failure's three-section
    peer structure stays consistent.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="early_fail",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_state("failed", error_preview="boom")
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        input_section = next(s for s in sections if str(s.title) == "input")
        body = _section_body_text(input_section)
        assert _FAILED_INPUT_EMPTY_NOTE in body


# ---------------------------------------------------------------------------
# Output section on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_node_has_output_section_empty_note() -> None:
    """Failure path → Output NodeSection with the exact empty-note constant.

    ``node.failed`` carries no output — the Output section always
    renders the ``_FAILED_OUTPUT_EMPTY_NOTE`` module constant so
    tests can assert the exact string.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"jd_url": "https://example.com"})
        row.set_state("failed", error_preview="boom")
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        output_section = next(s for s in sections if str(s.title) == "output")
        body = _section_body_text(output_section)
        assert body == _FAILED_OUTPUT_EMPTY_NOTE


# ---------------------------------------------------------------------------
# Error section: full error in body, not in title
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_node_error_is_section_not_title() -> None:
    """Full error lives in the Error NodeSection body, not the row title.

    Pre-bug-063 the error was tail-appended (truncated to 60 chars)
    onto ``row.title``. The new contract: title carries only the
    ``✗ <node>`` short marker; full error is in the Error section.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        err = "RegistryError: unresolvable ref"
        row.set_bound_inputs({"x": 1})
        row.set_state("failed", error_preview=err)
        await pilot.pause()

        # Title: short marker only — no full error.
        assert "✗" in row.title
        assert "identify_missing" in row.title
        assert err not in row.title

        # Error section present with full error in body.
        sections = list(_contents_of(row).query(NodeSection))
        titles = [str(s.title) for s in sections]
        # NodeSection prefixes a `✗ ` when state is "failed" — the base
        # title is "error"; the decorated title is "✗ error".
        assert "✗ error" in titles
        error_section = next(s for s in sections if str(s.title) == "✗ error")
        body = _section_body_text(error_section)
        assert err in body


@pytest.mark.asyncio
async def test_error_section_carries_full_untruncated_error() -> None:
    """Error section body carries the FULL error, un-re-truncated.

    ``self._error_preview`` holds the untruncated string; only the
    (pre-bug-063) title truncated it to 60 chars. The Error section
    body must NOT re-truncate — a 200-char error appears in full.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        long_err = "oops-" + "x" * 200
        row.set_bound_inputs({"x": 1})
        row.set_state("failed", error_preview=long_err)
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        error_section = next(s for s in sections if str(s.title) == "✗ error")
        body = _section_body_text(error_section)
        # FULL error present — not truncated to 60 chars.
        assert long_err in body
        assert len(long_err) > 60
        # And it's not in the title.
        assert long_err not in row.title


@pytest.mark.asyncio
async def test_failure_sections_are_peer_node_sections() -> None:
    """Input / Output / Error are peer :class:`NodeSection`s in order.

    Same widget class, mounted as siblings under the row's
    ``Collapsible.Contents``, in the order Input → Output → Error.
    This encodes AC #4 (visual peer structure) so a future regression
    that switches one to a different collapsible style fails loud.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"a": 1})
        row.set_state("failed", error_preview="boom")
        await pilot.pause()

        titles = _section_titles(row)
        assert titles == ["input", "output", "✗ error"]


@pytest.mark.asyncio
async def test_failure_sections_are_idempotent() -> None:
    """A repeat ``set_state("failed")`` does NOT duplicate the sections.

    Guards the mount-once latch: two failure events on the same row
    (rare, but possible on replay or noisy emit) leave the row with
    exactly three sections, not six.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="identify_missing",
            node_type="kolsch/nodes/agent",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"a": 1})
        row.set_state("failed", error_preview="first")
        await pilot.pause()
        row.set_state("failed", error_preview="second")
        await pilot.pause()

        titles = _section_titles(row)
        # Still exactly three sections.
        assert titles == ["input", "output", "✗ error"]
