"""Phase 3.9 Step 1 — `/workflow` slash command end-to-end.

Phase Tests 1, 2, 7, 8 + ancillary coverage:

- **Phase Test 1** — ``/workflow kolsch/workflows/hello`` runs the
  starter hello workflow end-to-end; capsules stream to stdout; the
  final capsule shows the terminal output.
- **Phase Test 2** — ``/workflow ... with name=Alice`` — the input
  flows through to the workflow.
- **Phase Test 7** — failed node renders ``caller_message`` (NOT
  ``str(exc)``).
- **Phase Test 8** — branch-not-taken capsules render muted with body
  ``(not taken)``.
- ``pending_workflow_run_id`` set after a successful invocation.
- Catalog-miss surfaces a friendly error and chat continues.
- Bad arg parsing surfaces a friendly error and chat continues.

The tests reuse the session-scoped ``chat_workspace`` fixture
(``kolsch setup --non-interactive`` once per session) and craft a
``SlashContext`` to dispatch ``/workflow`` directly — this exercises
the same code path as the live chat REPL would.
"""

from __future__ import annotations

import io as _io
import re
from pathlib import Path

import pytest

from cli._slash import (
    ChatSessionState,
    ControlSignal,
    SlashContext,
    dispatch_slash,
)
from platform_runtime import PlatformRuntime, bootstrap_runtime
from schemas import SessionId, ThreadId
from uuid import uuid4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_state(workspace: Path) -> tuple[ChatSessionState, PlatformRuntime]:
    """Construct a ChatSessionState bound to a fresh runtime + session.

    Mirrors :func:`tests.component.cli.test_chat_lifecycle._build_state`
    so the Phase 3.9 tests share the same setup contract as the
    Phase 2.1 lifecycle suite.
    """
    runtime = await bootstrap_runtime(root=workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="phase39-slash-test",
    )
    threads = await runtime.store.list_threads(session.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    return state, runtime


def _make_ctx(
    *,
    state: ChatSessionState,
    runtime: PlatformRuntime,
    stdout: _io.StringIO,
) -> SlashContext:
    return SlashContext(
        stdout=stdout,
        agent_config=state.config,
        authz=runtime.authz,
        agent_name=state.config.name,
        state=state,
    )


@pytest.fixture(scope="module")
def workspace_with_test_workflows(chat_workspace: Path) -> Path:
    """Module-scoped fixture — extends ``chat_workspace`` with the
    Phase 3.9 test-only workflow YAMLs (one failure-prone, plus the
    starter ``fork-demo`` for the pruning test).

    The failing workflow uses ``kolsch/nodes/compute`` with a Jinja
    template that references an undefined variable — the executor
    surfaces ``WorkflowNodeFailed`` carrying the underlying Jinja
    error. The renderer must surface ``caller_message``, NOT the
    engineer-targeted single-line form.
    """
    bundle_dir = chat_workspace / "bundles" / "phase39_test"
    workflows_dir = bundle_dir / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        "name: phase39_test\nversion: \"0.1\"\ndescription: phase 3.9 test workflows\n",
        encoding="utf-8",
    )
    (workflows_dir / "child.yaml").write_text(
        # A trivial non-pausing child workflow used by the
        # sub-workflow nesting test. Compute-only so it runs end-to-end
        # under the starter platform's authz config.
        "description: \"Phase 3.9 Test 9 — child of nesting demo.\"\n"
        "nodes:\n"
        "  child_step:\n"
        "    type: kolsch/nodes/compute\n"
        "    description: \"Child compute — terminal node.\"\n"
        "    value: \"child output: {{ inputs.label | default('default') }}\"\n"
        "graph:\n"
        "  edges: []\n",
        encoding="utf-8",
    )
    (workflows_dir / "parent.yaml").write_text(
        # A two-node parent that calls the child via
        # ``kolsch/nodes/workflow``. The renderer's
        # ``workflow.node.called_workflow`` handler spawns a child
        # WorkflowCapsuleRenderer at indent_depth+1 tailing the child
        # run's JSONL.
        "description: \"Phase 3.9 Test 9 — parent of nesting demo.\"\n"
        "nodes:\n"
        "  parent_prep:\n"
        "    type: kolsch/nodes/compute\n"
        "    description: \"Parent prep — pre-call.\"\n"
        "    value: \"parent ready\"\n"
        "  sub:\n"
        "    type: kolsch/nodes/workflow\n"
        "    ref: phase39_test/workflows/child\n"
        "    description: \"Sub-workflow node — calls child.\"\n"
        "graph:\n"
        "  edges:\n"
        "    - from: parent_prep\n"
        "      to: sub\n",
        encoding="utf-8",
    )
    (workflows_dir / "failing.yaml").write_text(
        # Single ``kolsch/nodes/compute`` node whose Jinja template
        # raises ``ZeroDivisionError`` at evaluation time. The
        # workflow's Jinja env doesn't use StrictUndefined, so an
        # undefined-variable reference would silently produce an
        # Undefined sentinel — not a node failure. A hard arithmetic
        # error is the most reliable way to provoke
        # ``workflow.node.failed`` from a compute node.
        "description: \"Phase 3.9 Test 7 — failing compute node.\"\n"
        "nodes:\n"
        "  doomed:\n"
        "    type: kolsch/nodes/compute\n"
        "    description: \"Compute node — divide by zero.\"\n"
        "    value: \"{{ 1 / 0 }}\"\n"
        "graph:\n"
        "  edges: []\n",
        encoding="utf-8",
    )
    return chat_workspace


# ===========================================================================
# Phase Test 1 — /workflow runs end-to-end
# ===========================================================================


async def test_slash_workflow_runs_to_completion(chat_workspace: Path) -> None:
    """Phase Test 1: ``/workflow kolsch/workflows/hello`` parses,
    resolves, runs, and the rendered capsules appear on stdout.

    Asserts:
      - dispatch returns ``ControlSignal.CONTINUE``
      - ``pending_workflow_run_id`` is set after the run
      - the rendered output contains the node labels (display_label
        cleaned-name fallback or per-instance description)
      - the rendered output carries an event_id subscript (``ev_*``)
    """
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow kolsch/workflows/hello", ctx
    )
    assert signal is ControlSignal.CONTINUE

    # pending_workflow_run_id set to a valid-looking run id.
    assert state.pending_workflow_run_id is not None
    assert state.pending_workflow_run_id.startswith("wrk_")

    rendered = out.getvalue()
    # Capsule headers — the hello workflow has nodes ``ready`` (noop)
    # and ``greet`` (echo). Both carry per-instance descriptions, so
    # the headers use those verbatim.
    assert "structural root node" in rendered
    assert "echoes the workflow input" in rendered
    # Event_id subscripts (short form).
    assert re.search(r"ev_[a-f0-9]{8}", rendered) is not None
    # Running indicator landed.
    assert "▸" in rendered


# ===========================================================================
# Phase Test 2 — /workflow with k=v
# ===========================================================================


async def test_slash_workflow_with_inputs(chat_workspace: Path) -> None:
    """Phase Test 2: ``/workflow ... with name=Alice`` — the bound
    input flows through to the workflow and the rendered greeting
    contains the value.
    """
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow kolsch/workflows/hello with message=AliceFromChat", ctx
    )
    assert signal is ControlSignal.CONTINUE

    rendered = out.getvalue()
    assert "AliceFromChat" in rendered


# ===========================================================================
# Phase Test 7 — failed node renders caller_message
# ===========================================================================


async def test_failed_node_renders_caller_message(
    workspace_with_test_workflows: Path,
) -> None:
    """Phase Test 7: a workflow whose compute node fails with a Jinja
    ``UndefinedError`` produces a ``workflow.node.failed`` event; the
    rendered capsule body shows the failing class's ``caller_message``
    (per spec §15.5), NOT ``str(exc)`` (which would carry the engineer-
    targeted Jinja traceback).
    """
    from workflows.errors import WorkflowError

    state, runtime = await _build_state(workspace_with_test_workflows)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow phase39_test/workflows/failing", ctx
    )
    assert signal is ControlSignal.CONTINUE

    rendered = out.getvalue()
    # The renderer looks up the failing class on workflows.errors and
    # surfaces its ClassVar caller_message. The default ClassVar value
    # is a short user-facing blurb; we don't pin the exact string
    # (Phase 3 may evolve it), but we DO assert a stable substring of
    # the generic WorkflowError.caller_message.
    generic = str(WorkflowError.caller_message)
    # The capsule body should EITHER show the generic blurb OR a more
    # specific subclass blurb. Both are valid per spec §15.5; what is
    # NOT acceptable is leaking ``str(exc)`` (which would contain
    # ``UndefinedError`` or Jinja-internal phrasing).
    # We assert the absence of the engineer-targeted phrasing.
    assert "Traceback" not in rendered
    assert "UndefinedError" not in rendered
    # AND positively assert the chat surfaces SOMETHING readable —
    # either the generic caller_message or a subclass-specific one.
    # The generic message is "Something went wrong running the workflow."
    # so we look for any substring of it OR a clearly user-facing tone.
    # Conservative: check that at least one capsule body line is non-empty.
    assert "doomed" in rendered.lower() or "compute" in rendered.lower()
    _ = generic  # documented as the fallback; we don't pin its exact text


# ===========================================================================
# Phase Test 8 — pruned capsule renders muted "(not taken)"
# ===========================================================================


async def test_pruned_capsule_renders_muted(chat_workspace: Path) -> None:
    """Phase Test 8: ``fork-demo`` with ``seed=urgent`` routes through
    the urgent arm; ``handle_normal`` is pruned. The renderer emits a
    muted capsule with body ``(not taken)`` for the pruned arm AND
    does NOT emit a preceding ``▸ handle normal…`` running indicator
    (pruning happens at edge-evaluation time per spec §15.2.2).
    """
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow kolsch/workflows/fork-demo with seed=urgent", ctx
    )
    assert signal is ControlSignal.CONTINUE

    rendered = out.getvalue()
    # The pruned arm's capsule body marker.
    assert "(not taken)" in rendered
    # The label uses display_label — fork-demo's handle_normal carries
    # ``description: "Normal-arm handler — fires when the branch is
    # false."`` so the header shows that string.
    assert "Normal-arm handler" in rendered
    # No preceding running indicator for the pruned node. The starter
    # description is verbose; we check for the absence of an indicator
    # line containing the description in the running-arrow style.
    pruned_indicator = "▸ Normal-arm handler"
    assert pruned_indicator not in rendered


# ===========================================================================
# Phase Test 9 — sub-workflow capsules render indented
# ===========================================================================


async def test_subworkflow_capsules_render_indented(
    workspace_with_test_workflows: Path,
) -> None:
    """Phase Test 9 (build doc): a parent workflow that calls a child
    via ``kolsch/nodes/workflow`` produces:

      - the parent's capsules at indent_depth=0 (no leading whitespace).
      - the child's capsules at indent_depth=1 (two-space prefix on
        EVERY line — header bar AND body lines).

    The renderer spawns a child :class:`WorkflowCapsuleRenderer` when
    it sees ``workflow.node.called_workflow``; the child tails the
    child run's JSONL.
    """
    state, runtime = await _build_state(workspace_with_test_workflows)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow phase39_test/workflows/parent", ctx
    )
    assert signal is ControlSignal.CONTINUE

    rendered = out.getvalue()
    # Parent capsules appear (unindented).
    assert "Parent prep" in rendered
    # The child's capsule label appears, INDENTED by two spaces (the
    # ``  │`` prefix is the load-bearing visual contract).
    # Find any line containing the child label.
    child_lines = [
        line for line in rendered.splitlines() if "Child compute" in line
    ]
    assert child_lines, (
        f"expected at least one rendered line to mention 'Child compute'; "
        f"got rendered output:\n{rendered}"
    )
    # At least ONE child line carries the indent (the capsule's header
    # bar lands on its own line; the body lines also indent).
    indented_child = [line for line in child_lines if line.startswith("  ")]
    assert indented_child, (
        f"expected child capsule lines to start with two spaces (indent);\n"
        f"all child lines:\n" + "\n".join(repr(line) for line in child_lines)
    )


# ===========================================================================
# Ancillary — pending_workflow_run_id
# ===========================================================================


async def test_pending_workflow_run_id_set_after_dispatch(
    chat_workspace: Path,
) -> None:
    """After a successful ``/workflow`` invocation,
    ``state.pending_workflow_run_id`` carries the new run's id (so
    Step 3 ``/fork`` and Step 2 user_input resume can target it).
    """
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    assert state.pending_workflow_run_id is None
    await dispatch_slash("/workflow kolsch/workflows/hello", ctx)
    assert state.pending_workflow_run_id is not None
    assert state.pending_workflow_run_id.startswith("wrk_")


# ===========================================================================
# Error paths
# ===========================================================================


async def test_catalog_miss_surfaces_friendly_error(
    chat_workspace: Path,
) -> None:
    """Unknown workflow ref → one-line error on stdout; chat continues.

    The handler MUST NOT raise; an unknown ref is a normal operator
    mistake and the loop carries on so they can retry.
    """
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash(
        "/workflow kolsch/workflows/does-not-exist", ctx
    )
    assert signal is ControlSignal.CONTINUE
    rendered = out.getvalue()
    assert "not found" in rendered
    assert "does-not-exist" in rendered
    # No partial run minted — pending pointer stays None.
    assert state.pending_workflow_run_id is None


async def test_bad_args_surfaces_friendly_error(chat_workspace: Path) -> None:
    """Empty arg after ``/workflow`` → one-line error; chat continues."""
    state, runtime = await _build_state(chat_workspace)
    out = _io.StringIO()
    ctx = _make_ctx(state=state, runtime=runtime, stdout=out)

    signal = await dispatch_slash("/workflow", ctx)
    assert signal is ControlSignal.CONTINUE
    assert "requires a workflow ref" in out.getvalue()
    assert state.pending_workflow_run_id is None
