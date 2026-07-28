"""Coverage for the async + edge paths of :mod:`cli._workflow_capsules`.

Complements ``test_workflow_capsules_renderer.py`` (the synchronous
skeleton) with the paths that need a real
[`WorkflowRunStore`][workflows.run_store.WorkflowRunStore] and the
event loop:

- ``render_run`` tailing a real JSONL to a terminal event.
- ``render_run`` honouring the ``done_event`` escape hatch when a
  paused run never emits a terminal ``workflow.run.*``.
- ``render_run`` raising when no store was wired.
- ``_adispatch`` / ``_spawn_child_renderer`` nesting a child renderer
  one indent deeper off a ``workflow.node.called_workflow`` event.
- ``render_pause_capsule`` (with + without a parsed workflow prompt).
- ``_render_failed_capsule`` + ``_caller_message_for`` class resolution.
- The output/id preview helpers on their non-string / empty branches.
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console

from cli._workflow_capsules import WorkflowCapsuleRenderer
from workflows.errors import WorkflowError, WorkflowNotRunnable
from workflows.parser import BashNodeDef, UserInputNodeDef, Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    from workflows.ids import WorkflowRunId


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _console() -> tuple[io.StringIO, Console]:
    """A width-narrowed, non-terminal Rich console over a StringIO."""
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    return out, console


def _seed_run(store: WorkflowRunStore) -> WorkflowRunId:
    """Mint a run and return its id (the on-disk directory exists)."""
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    return run.run_id


def _make_event(kind: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
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


# ---------------------------------------------------------------------------
# render_run — async JSONL tail.
# ---------------------------------------------------------------------------


async def test_render_run_tails_jsonl_until_terminal_event(tmp_path: Path) -> None:
    """``render_run`` streams every node event, then stops at the
    terminal ``workflow.run.completed`` — the whole pre-terminal batch
    renders, nothing after."""
    store = WorkflowRunStore(tmp_path)
    run_id = _seed_run(store)
    store.append_event(run_id, _make_event("workflow.run.started"))
    store.append_event(run_id, _make_event("workflow.node.started", node_name="alpha"))
    store.append_event(
        run_id,
        _make_event(
            "workflow.node.completed",
            node_name="alpha",
            payload={"output": "hello"},
        ),
    )
    store.append_event(run_id, _make_event("workflow.run.completed"))

    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, store=store)
    await asyncio.wait_for(renderer.render_run(run_id, poll_seconds=0.001), timeout=5)

    rendered = out.getvalue()
    assert "▸ alpha…" in rendered
    assert "hello" in rendered


async def test_render_run_skips_blank_jsonl_lines(tmp_path: Path) -> None:
    """Blank lines in the JSONL (stray newlines) are skipped by the
    tail loop without crashing; the surrounding events still render."""
    store = WorkflowRunStore(tmp_path)
    run_id = _seed_run(store)
    store.append_event(run_id, _make_event("workflow.run.started"))
    store.append_event(
        run_id,
        _make_event(
            "workflow.node.completed",
            node_name="alpha",
            payload={"output": "after-blank"},
        ),
    )
    store.append_event(run_id, _make_event("workflow.run.completed"))
    # Inject a stray blank line mid-stream (the executor's appends are
    # always newline-terminated, but a partial flush can leave one).
    events_path = store.directory(run_id).events_path
    raw = events_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    events_path.write_text(
        "\n".join([lines[0], "", *lines[1:]]) + "\n", encoding="utf-8"
    )

    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, store=store)
    await asyncio.wait_for(renderer.render_run(run_id, poll_seconds=0.001), timeout=5)

    assert "after-blank" in out.getvalue()


async def test_render_run_stops_on_done_event_without_terminal(
    tmp_path: Path,
) -> None:
    """A paused run emits no terminal ``workflow.run.*``; ``render_run``
    drains the events on disk, then exits when ``done_event`` is set
    rather than blocking forever."""
    store = WorkflowRunStore(tmp_path)
    run_id = _seed_run(store)
    store.append_event(run_id, _make_event("workflow.run.started"))
    store.append_event(
        run_id,
        _make_event(
            "workflow.node.completed",
            node_name="ask",
            payload={"output": "paused-ish"},
        ),
    )

    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, store=store)
    done_event = asyncio.Event()
    done_event.set()  # pre-set: the loop drains once, then exits.

    await asyncio.wait_for(
        renderer.render_run(run_id, done_event=done_event, poll_seconds=0.001),
        timeout=5,
    )

    assert "paused-ish" in out.getvalue()


async def test_render_run_without_store_raises_value_error(tmp_path: Path) -> None:
    """``render_run`` requires a store; a renderer built without one
    raises ``ValueError`` (the tests-only ``render_events`` path does
    not need it, but the async tail does)."""
    from uuid import uuid4

    from workflows.ids import WorkflowRunId

    _out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console)  # store defaults to None
    # The store-None guard fires before any directory lookup; the id
    # just has to be well-formed (uuid body).
    with pytest.raises(ValueError, match="requires a WorkflowRunStore"):
        await renderer.render_run(WorkflowRunId(f"wrk_{uuid4()}"))


# ---------------------------------------------------------------------------
# _adispatch / _spawn_child_renderer — sub-workflow nesting.
# ---------------------------------------------------------------------------


async def test_called_workflow_event_spawns_indented_child_renderer(
    tmp_path: Path,
) -> None:
    """A ``workflow.node.called_workflow`` event spawns a child
    renderer at ``indent_depth+1`` tailing the child run's JSONL; the
    child's capsule lands INDENTED beneath the parent tail."""
    store = WorkflowRunStore(tmp_path)
    parent_id = _seed_run(store)
    child_id = _seed_run(store)

    # Child JSONL — one completed node + terminal.
    store.append_event(child_id, _make_event("workflow.run.started"))
    store.append_event(
        child_id,
        _make_event(
            "workflow.node.completed",
            node_name="child_step",
            payload={"output": "child-out"},
        ),
    )
    store.append_event(child_id, _make_event("workflow.run.completed"))

    # Parent JSONL references the child, then terminates.
    store.append_event(parent_id, _make_event("workflow.run.started"))
    store.append_event(
        parent_id,
        _make_event(
            "workflow.node.called_workflow",
            node_name="sub",
            payload={"child_workflow_run_id": str(child_id)},
        ),
    )
    store.append_event(parent_id, _make_event("workflow.run.completed"))

    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, store=store)
    await asyncio.wait_for(
        renderer.render_run(parent_id, poll_seconds=0.001), timeout=5
    )

    rendered = out.getvalue()
    # The child capsule body appears...
    assert "child-out" in rendered
    # ...and at least one child line carries the two-space indent.
    child_lines = [ln for ln in rendered.splitlines() if "child-out" in ln]
    assert child_lines
    assert any(ln.startswith("  ") for ln in child_lines)


async def test_called_workflow_missing_child_run_id_is_ignored(
    tmp_path: Path,
) -> None:
    """A ``workflow.node.called_workflow`` event whose payload lacks a
    string ``child_workflow_run_id`` spawns no child — the parent tail
    finishes cleanly without raising."""
    store = WorkflowRunStore(tmp_path)
    parent_id = _seed_run(store)
    store.append_event(parent_id, _make_event("workflow.run.started"))
    store.append_event(
        parent_id,
        _make_event(
            "workflow.node.called_workflow",
            node_name="sub",
            payload={"child_workflow_run_id": None},
        ),
    )
    store.append_event(parent_id, _make_event("workflow.run.completed"))

    _out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, store=store)
    await asyncio.wait_for(
        renderer.render_run(parent_id, poll_seconds=0.001), timeout=5
    )
    # No child spawned; no child capsule content leaks in.
    assert renderer._child_tasks == []


async def test_child_loader_exception_falls_back_to_stub_label(
    tmp_path: Path,
) -> None:
    """When ``child_workflow_loader`` raises, the spawn swallows it and
    the child renderer falls back to the cleaned-name stub label —
    the parent tail still completes."""
    store = WorkflowRunStore(tmp_path)
    parent_id = _seed_run(store)
    child_id = _seed_run(store)
    store.append_event(child_id, _make_event("workflow.run.started"))
    store.append_event(
        child_id,
        _make_event(
            "workflow.node.completed",
            node_name="my_child_node",
            payload={"output": "ok"},
        ),
    )
    store.append_event(child_id, _make_event("workflow.run.completed"))

    store.append_event(parent_id, _make_event("workflow.run.started"))
    store.append_event(
        parent_id,
        _make_event(
            "workflow.node.called_workflow",
            node_name="sub",
            payload={"child_workflow_run_id": str(child_id)},
        ),
    )
    store.append_event(parent_id, _make_event("workflow.run.completed"))

    def _boom(_run_id: str) -> Workflow | None:
        raise RuntimeError("loader exploded")

    out, console = _console()
    renderer = WorkflowCapsuleRenderer(
        console=console,
        store=store,
        child_workflow_loader=_boom,
    )
    await asyncio.wait_for(
        renderer.render_run(parent_id, poll_seconds=0.001), timeout=5
    )
    rendered = out.getvalue()
    # Cleaned-name fallback ran despite the loader raising.
    assert "my child node" in rendered


def test_spawn_child_renderer_without_store_is_noop() -> None:
    """The sync guard: ``_spawn_child_renderer`` returns early with no
    store wired (the async path can never reach here, but the guard is
    load-bearing for robustness)."""
    _out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console)  # no store
    renderer._spawn_child_renderer(
        _make_event(
            "workflow.node.called_workflow",
            payload={"child_workflow_run_id": "wrk_child"},
        )
    )
    assert renderer._child_tasks == []


# ---------------------------------------------------------------------------
# render_pause_capsule.
# ---------------------------------------------------------------------------


def test_pause_capsule_with_workflow_shows_prompt_and_arrow() -> None:
    """With a parsed workflow, the pause capsule body carries the
    node's ``prompt`` (line 1) and the dim ``→ <node_name>`` forensics
    line (line 2)."""
    node = UserInputNodeDef(
        type="kolsch/nodes/user_input",
        prompt="What would you like to say?",
        description="Ask the operator.",
    )
    workflow = Workflow(nodes={"ask": node})
    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, workflow=workflow)

    renderer.render_pause_capsule("ask")

    rendered = out.getvalue()
    assert "What would you like to say?" in rendered
    assert "→ ask" in rendered
    # The description drives the header (display_label short-circuit).
    assert "Ask the operator" in rendered


def test_pause_capsule_without_workflow_uses_stub_label_and_no_prompt() -> None:
    """Without a parsed workflow the node lookup misses; the header
    falls back to the cleaned-name stub and only the ``→ <node_name>``
    line renders (no prompt is available on disk)."""
    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console)  # workflow=None

    renderer.render_pause_capsule("await_reply")

    rendered = out.getvalue()
    assert "→ await_reply" in rendered
    # Cleaned-name fallback: snake_case → space-separated.
    assert "await reply" in rendered


def test_pause_capsule_indented_when_nested() -> None:
    """At ``indent_depth=1`` every pause-capsule line carries the
    two-space indent (the manual indented render path)."""
    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, indent_depth=1)

    renderer.render_pause_capsule("ask")

    for line in out.getvalue().splitlines():
        if not line:
            continue
        assert line.startswith("  "), f"unexpected unindented line: {line!r}"


# ---------------------------------------------------------------------------
# _render_failed_capsule + _caller_message_for.
# ---------------------------------------------------------------------------


def _render_events_sync(
    events: list[dict[str, Any]],
    *,
    workflow: Workflow | None = None,
) -> str:
    """Run the synchronous ``render_events`` and capture stdout."""
    out, console = _console()
    renderer = WorkflowCapsuleRenderer(console=console, workflow=workflow)
    renderer.render_events(events)
    return out.getvalue()


def test_failed_capsule_surfaces_named_class_caller_message() -> None:
    """A ``workflow.node.failed`` event whose payload names a real
    error class surfaces that class's ``caller_message`` ClassVar —
    not the generic ``WorkflowError`` blurb, and never ``str(exc)``."""
    events = [
        _make_event(
            "workflow.node.failed",
            node_name="doomed",
            payload={"error_class": "WorkflowNotRunnable"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert WorkflowNotRunnable.caller_message in rendered
    # The generic blurb is NOT what surfaced (subclass won).
    assert WorkflowError.caller_message not in rendered


def test_failed_capsule_unknown_class_falls_back_to_generic() -> None:
    """An unrecognised ``error_class`` (or one absent from
    ``workflows.errors``) falls back to the generic
    ``WorkflowError.caller_message``."""
    events = [
        _make_event(
            "workflow.node.failed",
            node_name="doomed",
            payload={"error_class": "NoSuchErrorClassAnywhere"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert WorkflowError.caller_message in rendered


def test_failed_capsule_non_type_attr_falls_back_to_generic() -> None:
    """When ``error_class`` names a module attribute that is NOT a
    class (e.g. a helper function), the resolver rejects it and falls
    back to the generic blurb."""
    events = [
        _make_event(
            "workflow.node.failed",
            node_name="doomed",
            # ``_identifying_names`` is a function on workflows.errors,
            # not a class — exercises the ``not isinstance(cls, type)``
            # guard.
            payload={"error_class": "_identifying_names"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert WorkflowError.caller_message in rendered


# ---------------------------------------------------------------------------
# Preview / subscript helpers.
# ---------------------------------------------------------------------------


def test_completed_capsule_non_string_output_uses_repr_preview() -> None:
    """A non-string ``payload.output`` (dict) is collapsed via ``repr``
    for an unambiguous single-line preview."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={"output": {"label": "urgent"}},
        ),
    ]
    rendered = _render_events_sync(events)
    assert "{'label': 'urgent'}" in rendered


def test_completed_capsule_missing_event_id_emits_no_subscript() -> None:
    """A completion event with an empty ``event_id`` renders the body
    but NO ``ev_`` subscript line (the empty-id short-circuit)."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            event_id="",
            payload={"output": "done"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert "done" in rendered
    assert "ev_" not in rendered


def test_short_event_id_passthrough_for_non_ev_prefix() -> None:
    """A completion event whose ``event_id`` lacks the ``ev_`` prefix
    surfaces verbatim (the truncation only applies to ``ev_`` ids)."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            event_id="raw-identifier-xyz",
            payload={"output": "done"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert "raw-identifier-xyz" in rendered


def test_short_event_id_short_body_returned_whole() -> None:
    """An ``ev_``-prefixed id whose body is at most 8 chars is returned
    intact (no truncation), proving the boundary branch."""
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            event_id="ev_short",  # body="short" ≤ 8 chars
            payload={"output": "done"},
        ),
    ]
    rendered = _render_events_sync(events)
    assert "ev_short" in rendered


def test_string_output_preview_strips_newlines() -> None:
    """A multi-line string ``output`` collapses to a single line —
    embedded newlines become spaces, edges stripped."""
    workflow = Workflow(
        nodes={
            "classify": BashNodeDef(type="kolsch/nodes/bash", command="echo hi"),
        }
    )
    events = [
        _make_event(
            "workflow.node.completed",
            node_name="classify",
            payload={"output": "  line one\nline two  "},
        ),
    ]
    rendered = _render_events_sync(events, workflow=workflow)
    assert "line one line two" in rendered
    # The collapsed preview carries no literal newline inside the body.
    body_lines = [ln for ln in rendered.splitlines() if "line one" in ln]
    assert body_lines
    assert "line two" in body_lines[0]
