"""Coverage regressions for `WorkflowNodeRow` uncovered branches.

Drives the module-private formatting helpers, the sink map-row guards,
the two-phase ``mount_tool_call`` transition, ``mount_notice``, the
non-bash config previews (compute/map/nested-workflow), the failure
map-skip, the ``add_iteration_child`` duplicate-slot guard, and the
``resolve_state`` KeyError fallbacks — each asserting an observable
render / state outcome, not a coverage-only touch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Collapsible, Static

from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import (
    WorkflowNodeRow,
    _extract_content_text,
    _format_content_dict,
    _format_input_value,
    _format_inputs_summary,
    _is_bash_shape,
    _is_failed_exit,
    _render_bash_blocks,
    _render_bound_inputs_body,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget

    _AppFactory = Callable[[Widget], App[None]]

_WORKFLOW_TYPE = "kolsch/nodes/workflow"
_MAP_TYPE = "kolsch/nodes/map"


def _row_body_text(row: WorkflowNodeRow) -> str:
    """Join every ``Static`` body under ``row`` into one string."""
    return "\n".join(
        str(getattr(s, "content", "") or s.render())
        for s in row.walk_children()
        if isinstance(s, Static)
    )


# ---------------------------------------------------------------------------
# Pure formatting helpers (no App required)
# ---------------------------------------------------------------------------


def test_format_inputs_summary_empty_and_multi() -> None:
    """Empty inputs → ""; multi-key inputs → ``N inputs: k=v`` prefix."""
    assert _format_inputs_summary({}) == ""
    summary = _format_inputs_summary({"a": 1, "b": 2})
    assert summary.startswith("2 inputs: a=")
    single = _format_inputs_summary({"a": 1})
    assert single == "a=1"


def test_format_input_value_long_string_and_json_fallback() -> None:
    """Long strings carry a char-count suffix; bad JSON falls to repr."""
    long_value = "x" * 100
    rendered = _format_input_value(long_value)
    assert "(100 chars)" in rendered
    # A circular reference raises ValueError inside json.dumps → repr fallback.
    circular: list[object] = []
    circular.append(circular)
    bad = _format_input_value(circular)
    assert bad  # repr of the circular list, truncated — non-empty is enough.


def test_format_content_dict_json_fallback_uses_repr() -> None:
    """A non-bash dict json.dumps failure falls back to ``repr``."""
    circular: dict[str, object] = {}
    circular["self"] = circular
    rendered = _format_content_dict(circular)
    assert rendered  # repr fallback yields a non-empty string.


def test_is_bash_shape_rejects_non_dict_and_empty() -> None:
    """Non-dict / empty inputs are not bash-shaped."""
    assert _is_bash_shape("nope") is False
    assert _is_bash_shape({}) is False
    assert _is_bash_shape({"stdout": "hi"}) is True


def test_render_bash_blocks_includes_stdout_and_stderr() -> None:
    """Both stdout and stderr labelled blocks render with real newlines."""
    rendered = _render_bash_blocks(
        {"exit_code": 1, "stdout": "out-line", "stderr": "err-line"}
    )
    assert "exit_code: 1" in rendered
    assert "stdout:" in rendered
    assert "out-line" in rendered
    assert "stderr:" in rendered
    assert "err-line" in rendered


def test_render_bash_blocks_stdout_only_no_exit_code() -> None:
    """No exit_code + stdout only → only the stdout block renders."""
    rendered = _render_bash_blocks({"stdout": "just-out"})
    assert "exit_code:" not in rendered
    assert "stdout:" in rendered
    assert "just-out" in rendered
    assert "stderr:" not in rendered


def test_render_bash_blocks_stderr_only() -> None:
    """stderr only (no stdout) → only the stderr block renders."""
    rendered = _render_bash_blocks({"exit_code": 3, "stderr": "just-err"})
    assert "exit_code: 3" in rendered
    assert "stdout:" not in rendered
    assert "stderr:" in rendered
    assert "just-err" in rendered


def test_render_bound_inputs_body_empty_and_populated() -> None:
    """Empty → the failed-input note; populated → per-key lines."""
    empty = _render_bound_inputs_body({})
    assert "no bound inputs" in empty
    populated = _render_bound_inputs_body(
        {"resume": "cv.pdf"},
        workflow_inputs_used={"role": "eng"},
    )
    assert "bound inputs:" in populated
    assert "· resume:" in populated
    assert "workflow inputs used:" in populated
    assert "· role:" in populated


def test_is_failed_exit_variants() -> None:
    """None / non-int / 0 are not failures; non-zero ints are."""
    assert _is_failed_exit(None) is False
    assert _is_failed_exit("not-a-number") is False
    assert _is_failed_exit(0) is False
    assert _is_failed_exit(2) is True


def test_extract_content_text_empty_dict_and_scalar() -> None:
    """Empty content → ""; empty dict → ""; scalar → its truncated str."""
    assert _extract_content_text(None) == ""
    assert _extract_content_text("") == ""
    assert _extract_content_text({}) == ""
    assert _extract_content_text(1234) == "1234"


def test_extract_content_text_dict_without_known_keys_falls_to_dump() -> None:
    """A dict lacking text/output/result keys routes to _format_content_dict."""
    result = _extract_content_text({"custom": "payload", "n": 2})
    assert "custom" in result
    assert "payload" in result


def test_extract_content_text_dict_with_known_key_returns_value() -> None:
    """A dict carrying an ``output`` string returns that value directly."""
    assert _extract_content_text({"output": "the-value"}) == "the-value"


# ---------------------------------------------------------------------------
# __init__ / title fallbacks
# ---------------------------------------------------------------------------


def test_unknown_initial_state_glyph_falls_back_to_question_mark() -> None:
    """A non-animated, non-canonical ``initial_state`` paints ``?``."""
    row = WorkflowNodeRow(node_name="mystery", initial_state="not-a-state")
    # resolve_state raised KeyError → leading glyph is `?`.
    assert row.title.startswith("? mystery")
    assert row.state == "not-a-state"


@pytest.mark.asyncio
async def test_update_title_unknown_state_paints_question_mark(
    mock_app_factory: _AppFactory,
) -> None:
    """A stray reactive state repaints the title with the ``?`` fallback."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("bogus-state")
        await pilot.pause()
        assert row.title.startswith("? n")


def test_truncate_preview_short_and_long() -> None:
    """Short text is returned verbatim; long text tail-truncates with ..."""
    row = WorkflowNodeRow(node_name="n")
    assert row._truncate_preview("short") == "short"
    long_text = "z" * 100
    truncated = row._truncate_preview(long_text)
    assert truncated.endswith("...")
    assert len(truncated) == 60


# ---------------------------------------------------------------------------
# set_output — non-dict JSON path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_output_list_renders_json_preview(
    mock_app_factory: _AppFactory,
) -> None:
    """A list output falls through the non-dict json.dumps branch."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_output(["alpha", "beta"])
        await pilot.pause()
        assert row._output_widget is not None
        body = _row_body_text(row)
        assert "alpha" in body
        assert "beta" in body


@pytest.mark.asyncio
async def test_set_output_circular_list_falls_back_to_repr(
    mock_app_factory: _AppFactory,
) -> None:
    """A non-dict output that raises in json.dumps falls back to ``repr``."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    circular: list[object] = ["marker"]
    circular.append(circular)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_output(circular)
        await pilot.pause()
        assert row._output_widget is not None
        assert "marker" in _row_body_text(row)


@pytest.mark.asyncio
async def test_set_output_agent_gate_short_circuits(
    mock_app_factory: _AppFactory,
) -> None:
    """An agent row with prior assistant text suppresses set_output's preview."""
    row = WorkflowNodeRow(node_name="n", node_type="kolsch/nodes/agent")
    row.collapsed = False
    row.set_last_assistant_text("already rendered")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_output({"summary": "should be skipped"})
        await pilot.pause()
        # Gate fired → no flat output widget mounted.
        assert row._output_widget is None


# ---------------------------------------------------------------------------
# set_child_status gates
# ---------------------------------------------------------------------------


def test_set_child_status_non_workflow_row_is_noop() -> None:
    """A bash-type row ignores ``set_child_status`` (gate short-circuit)."""
    row = WorkflowNodeRow(node_name="run_bash", node_type="kolsch/nodes/bash")
    # No crash, no link mounted.
    row.set_child_status("succeeded")
    assert row._child_link is None


def test_set_child_status_workflow_row_without_link_is_noop() -> None:
    """A workflow row with no prior CALLED_WORKFLOW no-ops on completion."""
    row = WorkflowNodeRow(node_name="call_child", node_type=_WORKFLOW_TYPE)
    # Passes the node-type gate but `_child_link is None` → early return.
    row.set_child_status("succeeded")
    assert row._child_link is None


# ---------------------------------------------------------------------------
# Non-bash config previews (compute value / map over / nested workflow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_value_preview_renders_rendered_and_template(
    mock_app_factory: _AppFactory,
) -> None:
    """A compute ``value`` preview shows rendered primary + template sub-line."""
    row = WorkflowNodeRow(node_name="compute", node_type="kolsch/nodes/compute")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_workflow_inputs_used({"seed": 7})
        row.set_value_preview("{{ seed }}", rendered="7")
        await pilot.pause()
        body = _row_body_text(row)
        assert "value:" in body
        assert "template:" in body
        assert "{{ seed }}" in body
        assert "workflow inputs used:" in body


@pytest.mark.asyncio
async def test_map_over_preview_single_line_when_no_render(
    mock_app_factory: _AppFactory,
) -> None:
    """A map ``over`` preview with no differing rendered form is one line."""
    row = WorkflowNodeRow(node_name="scan", node_type=_MAP_TYPE)
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_over_preview("items")
        await pilot.pause()
        body = _row_body_text(row)
        assert "over:" in body
        assert "items" in body
        # No differing rendered form → no `template:` sub-line.
        assert "template:" not in body


@pytest.mark.asyncio
async def test_nested_workflow_binds_preview_renders_ref_and_binds(
    mock_app_factory: _AppFactory,
) -> None:
    """A nested-workflow preview shows ref + per-bind rendered/template lines."""
    row = WorkflowNodeRow(node_name="call_child", node_type=_WORKFLOW_TYPE)
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_workflow_inputs_used({"env": "prod"})
        row.set_workflow_binds_preview(
            "b/workflow/child",
            {"target": "{{ env }}", "same": "literal"},
            {"target": "prod", "same": "literal"},
        )
        await pilot.pause()
        body = _row_body_text(row)
        assert "ref: b/workflow/child" in body
        assert "binds:" in body
        # `target` rendered differs from template → both lines present.
        assert "· target: prod" in body
        assert "template: {{ env }}" in body
        # `same` rendered == template → single line, no template sub-line.
        assert "· same: literal" in body
        assert "workflow inputs used:" in body


@pytest.mark.asyncio
async def test_stash_config_preview_ignored_when_command_already_mounted(
    mock_app_factory: _AppFactory,
) -> None:
    """A config preview after a bash command section mounts is dropped."""
    row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_command_preview("echo hi")
        await pilot.pause()
        assert row._command_widget is not None
        # Now the bash section is mounted; a value preview must be a no-op.
        row.set_value_preview("ignored")
        assert row._pending_config_preview is None


# ---------------------------------------------------------------------------
# set_command_preview out-of-order refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_command_preview_out_of_order_refreshes_body(
    mock_app_factory: _AppFactory,
) -> None:
    """A second ``set_command_preview`` after mount refreshes the body in place."""
    row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_command_preview("echo first")
        await pilot.pause()
        assert row._command_widget is not None
        # Out-of-order setter path: refresh the already-mounted section body.
        row.set_command_preview("echo second", rendered="echo SECOND")
        await pilot.pause()
        body = _row_body_text(row)
        assert "echo SECOND" in body
        assert "template:" in body
        assert "echo second" in body


@pytest.mark.asyncio
async def test_bound_inputs_after_command_refreshes_in_place(
    mock_app_factory: _AppFactory,
) -> None:
    """``set_bound_inputs`` arriving after the section mounts refreshes it."""
    row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_command_preview("echo hi")
        await pilot.pause()
        # Late bound inputs → in-place refresh (bug-094).
        row.set_bound_inputs({"greeting": "hello"})
        await pilot.pause()
        body = _row_body_text(row)
        assert "bound inputs:" in body
        assert "· greeting:" in body


# ---------------------------------------------------------------------------
# bash output sections — stderr + duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_output_section_includes_stderr_and_duration(
    mock_app_factory: _AppFactory,
) -> None:
    """A non-zero bash exit surfaces stderr + a formatted duration line."""
    row = WorkflowNodeRow(node_name="run", node_type="kolsch/nodes/bash")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_completed_at(duration_ms=1500.0)
        row.set_output({"stdout": "out", "stderr": "boom", "exit_code": 2})
        await pilot.pause()
        assert row._bash_exit_section is not None
        body = _row_body_text(row)
        assert "exit code: 2" in body
        assert "duration:" in body
        assert "stderr:" in body
        assert "boom" in body


# ---------------------------------------------------------------------------
# Failure sections — map rows are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_map_row_skips_failure_sections(
    mock_app_factory: _AppFactory,
) -> None:
    """A failed ``kolsch/nodes/map`` row mounts NO Input/Output/Error sections."""
    row = WorkflowNodeRow(node_name="scan", node_type=_MAP_TYPE)
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("failed", error_preview="boom")
        await pilot.pause()
        assert row._failure_sections_mounted is True
        assert row._failure_input_section is None
        assert row._failure_output_section is None
        assert row._failure_error_section is None


# ---------------------------------------------------------------------------
# _formatted_duration — started/completed delta path
# ---------------------------------------------------------------------------


def test_formatted_duration_from_start_and_completion() -> None:
    """With no ``duration_ms`` the start→completion delta drives the format."""
    row = WorkflowNodeRow(node_name="n")
    assert row._formatted_duration() is None
    start = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 7, 27, 12, 0, 3, tzinfo=UTC)
    row.set_started_at(start)
    row.set_completed_at(end)
    dur = row._formatted_duration()
    assert dur is not None
    assert "3" in dur


# ---------------------------------------------------------------------------
# add_iteration_child — duplicate-slot guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_iteration_slot_keeps_original_and_warns(
    mock_app_factory: _AppFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second invocation claiming a used slot warns + keeps the original."""
    parent = WorkflowNodeRow(node_name="map", node_type=_MAP_TYPE)
    app = mock_app_factory(parent)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WorkflowNodeRow(node_name="child", invocation_id="inv_a")
        parent.add_iteration_child(first, iteration_index=0, iteration_total=3)
        await pilot.pause()

        second = WorkflowNodeRow(node_name="child", invocation_id="inv_b")
        with caplog.at_level(logging.WARNING):
            returned = parent.add_iteration_child(
                second, iteration_index=0, iteration_total=3
            )
        await pilot.pause()

        # Original kept; the second (different invocation_id) is rejected.
        assert returned is first
        assert parent._iteration_children[0] is first
        assert any("duplicate iteration slot" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_duplicate_iteration_slot_same_invocation_is_silent(
    mock_app_factory: _AppFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replay with the SAME invocation_id returns the child, no warning."""
    parent = WorkflowNodeRow(node_name="map", node_type=_MAP_TYPE)
    app = mock_app_factory(parent)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = WorkflowNodeRow(node_name="child", invocation_id="inv_a")
        parent.add_iteration_child(first, iteration_index=0, iteration_total=3)
        await pilot.pause()

        replay = WorkflowNodeRow(node_name="child", invocation_id="inv_a")
        with caplog.at_level(logging.WARNING):
            returned = parent.add_iteration_child(
                replay, iteration_index=0, iteration_total=3
            )
        await pilot.pause()
        assert returned is first
        assert not any("duplicate iteration slot" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Sink map-row guards + dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_row_drops_all_sink_events(
    mock_app_factory: _AppFactory,
) -> None:
    """A map row drops user / assistant / tool / notice sink events (AC #8)."""
    row = WorkflowNodeRow(node_name="scan", node_type=_MAP_TYPE)
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_user_message("hi", event_id="e1")
        await row.mount_assistant_message("yo", event_id="e2")
        await row.mount_tool_call("bash", "{}", "", event_id="e3")
        await row.mount_notice("n1", "warn", event_id="e4")
        await pilot.pause()
        # Nothing mounted, counter never bumped.
        assert row._sub_agent_turn_count == 0
        contents = row.query_one(Collapsible.Contents)
        assert not list(contents.query(NodeSection))


@pytest.mark.asyncio
async def test_assistant_message_duplicate_event_id_dropped(
    mock_app_factory: _AppFactory,
) -> None:
    """A repeated ``event_id`` is deduped before section construction."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_assistant_message("first", event_id="dup")
        await pilot.pause()
        # Same event_id, different text → still deduped by _is_duplicate.
        await row.mount_assistant_message("second", event_id="dup")
        await pilot.pause()
        contents = row.query_one(Collapsible.Contents)
        sections = [
            s for s in contents.query(NodeSection) if "response" in str(s.title)
        ]
        assert len(sections) == 1


@pytest.mark.asyncio
async def test_assistant_message_duplicate_last_text_dropped(
    mock_app_factory: _AppFactory,
) -> None:
    """A repeated assistant text (distinct event_id) is deduped by last-text."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_assistant_message("same body", event_id="a1")
        await pilot.pause()
        # Different event_id, SAME text → dedup on _last_assistant_text.
        await row.mount_assistant_message("same body", event_id="a2")
        await pilot.pause()
        assert row._response_count == 1
        contents = row.query_one(Collapsible.Contents)
        responses = [
            s for s in contents.query(NodeSection) if "response" in str(s.title)
        ]
        assert len(responses) == 1


@pytest.mark.asyncio
async def test_notice_duplicate_event_id_dropped(
    mock_app_factory: _AppFactory,
) -> None:
    """A repeated notice ``event_id`` is deduped."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_notice("w", "warn", event_id="dup-n")
        await pilot.pause()
        await row.mount_notice("w", "warn again", event_id="dup-n")
        await pilot.pause()
        assert row._sub_agent_turn_count == 1


@pytest.mark.asyncio
async def test_user_message_content_dict_mounts_per_key_sections(
    mock_app_factory: _AppFactory,
) -> None:
    """A non-empty ``content`` dict mounts one ``input: <key>`` per key."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_user_message(
            "flat",
            event_id="u1",
            content={"resume": "cv.pdf", "role": "eng"},
        )
        await pilot.pause()
        contents = row.query_one(Collapsible.Contents)
        titles = {str(s.title) for s in contents.query(NodeSection)}
        assert any("input: resume" in t for t in titles)
        assert any("input: role" in t for t in titles)


@pytest.mark.asyncio
async def test_user_message_duplicate_event_id_dropped(
    mock_app_factory: _AppFactory,
) -> None:
    """A repeated user-message ``event_id`` is deduped."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_user_message("hello", event_id="u1")
        await pilot.pause()
        await row.mount_user_message("hello again", event_id="u1")
        await pilot.pause()
        assert row._sub_agent_turn_count == 1


# ---------------------------------------------------------------------------
# mount_tool_call — two-phase running → completed transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_running_then_completed_transition(
    mock_app_factory: _AppFactory,
) -> None:
    """A second call with the same event_id transitions the SAME section."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Phase 1: running (empty body) creates the section.
        await row.mount_tool_call("bash", '{"cmd":"ls"}', "", event_id="call-1")
        await pilot.pause()
        section = row._tool_sections["call-1"]
        # NodeSection tracks its state in `_state`; running decorates `…`.
        assert getattr(section, "_state", None) == "running"

        # Phase 2: same event_id + body → transition, no second section.
        await row.mount_tool_call(
            "bash", '{"cmd":"ls"}', "exit_code: 0\nstdout:\nok", event_id="call-1"
        )
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        tool_sections = [
            s
            for s in contents.query(NodeSection)
            if str(s.title).startswith("tool:") or "tool:" in str(s.title)
        ]
        assert len(tool_sections) == 1
        assert getattr(row._tool_sections["call-1"], "_state", None) == "success"


@pytest.mark.asyncio
async def test_tool_call_failed_transition_marks_block(
    mock_app_factory: _AppFactory,
) -> None:
    """A failed transition flips the section state to ``failed``."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_tool_call("bash", "{}", "", event_id="call-x")
        await pilot.pause()
        await row.mount_tool_call(
            "bash", "{}", "error!", failed=True, event_id="call-x"
        )
        await pilot.pause()
        assert getattr(row._tool_sections["call-x"], "_state", None) == "failed"


@pytest.mark.asyncio
async def test_tool_call_transition_while_collapsed_queues(
    mock_app_factory: _AppFactory,
) -> None:
    """A transition that arrives while collapsed is queued, not live-mounted."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_tool_call("bash", "{}", "", event_id="c1")
        await pilot.pause()
        # Collapse so the transition path queues instead of live-mounting.
        row.collapsed = True
        await pilot.pause()
        await row.mount_tool_call("bash", "{}", "done", event_id="c1")
        await pilot.pause()
        assert len(row._pending_mounts) >= 1


# ---------------------------------------------------------------------------
# mount_notice — pinned at top of Contents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_notice_pins_widget_at_top(
    mock_app_factory: _AppFactory,
) -> None:
    """A notice mounts at index 0 of Contents, before existing children."""
    from cli.chat.widgets.notice import NoticeWidget

    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Mount a response section first so the notice must go BEFORE it.
        await row.mount_assistant_message("body", event_id="a1")
        await pilot.pause()
        await row.mount_notice("warn-1", "template fallback", event_id="n1")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        notices = list(contents.query(NoticeWidget))
        assert len(notices) == 1
        # The notice is the FIRST child of Contents.
        assert isinstance(contents.children[0], NoticeWidget)


@pytest.mark.asyncio
async def test_mount_notice_empty_text_dropped(
    mock_app_factory: _AppFactory,
) -> None:
    """An empty notice text is dropped (no counter bump, no widget)."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_notice("warn-1", "", event_id="n1")
        await pilot.pause()
        assert row._sub_agent_turn_count == 0


# ---------------------------------------------------------------------------
# count_lifecycle_event + frozen / unmounted short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_lifecycle_event_bumps_then_frozen_ignored(
    mock_app_factory: _AppFactory,
) -> None:
    """Lifecycle events bump the counter until a terminal state freezes it."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.count_lifecycle_event()
        assert row._sub_agent_turn_count == 1
        # Terminal state freezes the buffer → further lifecycle bumps ignored.
        row.set_state("success")
        await pilot.pause()
        row.count_lifecycle_event()
        assert row._sub_agent_turn_count == 1


@pytest.mark.asyncio
async def test_frozen_row_drops_dispatch_mount(
    mock_app_factory: _AppFactory,
) -> None:
    """A terminal (frozen) row drops a subsequent assistant mount."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("success")
        await pilot.pause()
        await row.mount_assistant_message("late message", event_id="late")
        await pilot.pause()
        contents = row.query_one(Collapsible.Contents)
        assert not list(contents.query(NodeSection))


# ---------------------------------------------------------------------------
# watch_collapsed — replay pending mounts on expand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collapsed_queue_drains_on_expand(
    mock_app_factory: _AppFactory,
) -> None:
    """Expanding a collapsed row replays its queued mount closures."""
    from cli.chat.widgets.assistant_prose import AssistantProse

    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert row.collapsed is True
        await row.mount_assistant_message("queued text", event_id="q1")
        await pilot.pause()
        assert len(row._pending_mounts) == 1

        # Expand → watch_collapsed schedules the drain.
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        assert list(contents.query(AssistantProse))
