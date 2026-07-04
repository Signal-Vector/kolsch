"""Phase 3.9 Step 0 — coverage for :mod:`cli._workflow_capsules`.

Skeleton-level coverage:

- Empty stream → no output, no crash.
- ``workflow.node.completed`` → one capsule with the right header /
  body / event_id subscript.
- :func:`workflows.labels.display_label` is the SOLE label source —
  per-instance ``description:`` short-circuits the cleaned-name
  fallback.
- ``workflow.node.pruned`` → muted-color capsule with body
  ``(not taken)``.
- ``workflow.node.started`` → ONE dim ``▸ <label>…`` line ABOVE the
  matching completion capsule per spec §15.2.2 — single line, no
  ``│`` bar, no timestamp, no event_id subscript.
- ``workflow.node.pruned`` without a preceding ``workflow.node.started``
  produces ONLY the muted capsule — no spurious indicator line.
- ``indent_depth`` honoured — indicator AND capsule lines are prefixed
  with two spaces per level.
"""

from __future__ import annotations

import io
import re
from typing import Any

from rich.console import Console

from cli._workflow_capsules import WorkflowCapsuleRenderer
from workflows.parser import BashNodeDef, Workflow

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _render(
    events: list[dict],
    *,
    workflow: Workflow | None = None,
    indent_depth: int = 0,
) -> str:
    """Run the renderer over crafted events; return captured stdout.

    Uses a width-narrowed Rich ``Console`` so panel text doesn't
    wrap unpredictably across CI (mirrors the pattern at
    :func:`tests.component.cli.test_chat_renderer._render_events`).
    """
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    renderer = WorkflowCapsuleRenderer(
        console=console,
        workflow=workflow,
        indent_depth=indent_depth,
    )
    renderer.render_events(events)
    return out.getvalue()


def _make_event(kind: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "event_id": "ev_abcd1234567890abcd",
        "kind": kind,
        "workflow_run_id": "wrk_test",
        "trace_id": "tr_test",
        "ts": "2026-06-28T00:00:00Z",
        "invocation_id": "inv_test",
        "node_name": "classify",
        "payload": {},
    }
    base.update(overrides)
    return base


def _make_workflow_with_bash(node_name: str, **node_kwargs: Any) -> Workflow:
    """Build a minimal :class:`Workflow` carrying one bash node."""
    node = BashNodeDef(
        type="kolsch/nodes/bash",
        command="echo hi",
        **node_kwargs,
    )
    return Workflow(nodes={node_name: node})


# ---------------------------------------------------------------------------
# Empty stream
# ---------------------------------------------------------------------------


def test_empty_stream_produces_no_output() -> None:
    """Renderer with no events writes nothing — skeleton invariant."""
    output = _render([])
    assert output == ""


# ---------------------------------------------------------------------------
# workflow.node.completed
# ---------------------------------------------------------------------------


def test_completed_event_emits_capsule_with_label_header() -> None:
    """A ``workflow.node.completed`` event renders one capsule whose
    header carries the node's display label."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={"output": "refund request, urgent"},
        ),
    ]
    output = _render(events)
    # Header line: ``│ classify · HH:MM:SS``
    assert "classify" in output
    # Body line: ``│ refund request, urgent``
    assert "refund request, urgent" in output
    # Event_id subscript (short form): ``ev_abcd1234``
    assert "ev_abcd1234" in output


def test_completed_event_with_no_output_payload_shows_placeholder() -> None:
    """A completion event lacking ``payload.output`` renders body
    ``(no output)`` — Step 0 minimal handling."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={},
        ),
    ]
    output = _render(events)
    assert "(no output)" in output


# ---------------------------------------------------------------------------
# display_label is the SOLE label source
# ---------------------------------------------------------------------------


def test_per_instance_description_wins_over_cleaned_name() -> None:
    """Spec §15.4 step 1 — when the parsed node carries a non-empty
    ``description:``, the header uses it verbatim (the cleaned-name
    fallback is BYPASSED). This is the load-bearing assertion that
    proves :func:`display_label` is wired correctly — no inline
    fallback in the renderer."""
    workflow = _make_workflow_with_bash("classify", description="Take user input")
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={"output": "ok"},
        ),
    ]
    output = _render(events, workflow=workflow)
    # The description string appears verbatim — the per-instance
    # short-circuit fired.
    assert "Take user input" in output
    # The cleaned-name fallback (``bash``) does NOT fire when a
    # description is present.
    assert "bash" not in output


def test_without_workflow_falls_back_to_cleaned_node_name() -> None:
    """Spec §15.4 step 3 — without a parsed workflow, the renderer
    feeds :func:`display_label` a stub whose ``__name__`` equals the
    event's ``node_name``. The cleaned-name fallback runs."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="my_classifier_node",
            payload={"output": "ok"},
        ),
    ]
    output = _render(events)
    # ``my_classifier_node`` → cleaned to ``my classifier`` (the
    # trailing ``Node`` suffix is stripped by labels._clean_name).
    assert "my classifier" in output


# ---------------------------------------------------------------------------
# workflow.node.pruned — muted body "(not taken)"
# ---------------------------------------------------------------------------


def test_pruned_event_renders_muted_with_not_taken_body() -> None:
    """Spec §15.2 — branch-not-taken edges produce a muted capsule
    with body ``(not taken)``.

    The header uses :func:`display_label`'s cleaned-name fallback when
    no parsed workflow is supplied — ``urgent_arm`` becomes
    ``urgent arm`` (snake_case → space-separated lowercase).
    """
    events = [
        _make_event(
            "workflow.node.pruned",
            node_name="urgent_arm",
            payload={"reason": "branch_not_taken"},
        ),
    ]
    output = _render(events)
    assert "urgent arm" in output
    assert "(not taken)" in output


# ---------------------------------------------------------------------------
# workflow.node.started — spec §15.2.2 running indicator
# ---------------------------------------------------------------------------


def test_started_event_emits_dim_running_indicator() -> None:
    """Spec §15.2.2 — one dim ``▸ <label>…`` line per started node.

    The line is NOT a capsule: no ``│`` bar, no timestamp, no
    event_id subscript.
    """
    events = [
        _make_event("workflow.node.started", node_name="classify"),
    ]
    output = _render(events)
    # The arrow glyph + ellipsis are present.
    assert "▸ classify…" in output
    # NOT a capsule — no ``│`` bar prefix on the indicator line.
    indicator_line = next(line for line in output.splitlines() if "▸" in line)
    assert "│" not in indicator_line
    # No timestamp on the indicator (capsules have ``· HH:MM:SS``).
    assert not re.search(r"·\s+\d{2}:\d{2}:\d{2}", indicator_line)
    # No event_id subscript on the indicator.
    assert "ev_" not in indicator_line


def test_started_indicator_lands_above_matching_completion_capsule() -> None:
    """The started+completed pair renders in JSONL order: indicator
    line, then the capsule beneath."""
    events = [
        _make_event("workflow.node.started", node_name="classify"),
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={"output": "ok"},
        ),
    ]
    output = _render(events)
    lines = output.splitlines()
    # Find both — the indicator must precede the capsule header.
    indicator_idx = next(i for i, ln in enumerate(lines) if "▸ classify…" in ln)
    capsule_idx = next(
        i
        for i, ln in enumerate(lines)
        if "│" in ln and "classify" in ln and i > indicator_idx
    )
    assert indicator_idx < capsule_idx


def test_pruned_without_preceding_started_has_no_spurious_indicator() -> None:
    """Spec §15.2.2 hard rule — pruning happens at edge-evaluation
    time and never emits ``workflow.node.started``; the muted capsule
    therefore lands standalone. A spurious indicator before the
    muted capsule would mis-imply the node tried to run."""
    events = [
        _make_event(
            "workflow.node.pruned",
            node_name="urgent_arm",
            payload={"reason": "branch_not_taken"},
        ),
    ]
    output = _render(events)
    assert "▸ urgent_arm" not in output
    assert "▸" not in output
    # The muted capsule body still renders.
    assert "(not taken)" in output


# ---------------------------------------------------------------------------
# indent_depth — sub-workflow nesting
# ---------------------------------------------------------------------------


def test_indent_depth_prefixes_indicator_and_capsule() -> None:
    """``indent_depth=1`` prefixes EVERY rendered line with two
    spaces — both the indicator and the capsule's bars / body."""
    events = [
        _make_event("workflow.node.started", node_name="child_step"),
        _make_event(
            "workflow.node.completed",
            node_name="child_step",
            payload={"output": "done"},
        ),
    ]
    output = _render(events, indent_depth=1)
    for line in output.splitlines():
        if not line:
            continue  # blank separator between capsules is unindented
        assert line.startswith("  "), f"unexpected unindented line: {line!r}"
