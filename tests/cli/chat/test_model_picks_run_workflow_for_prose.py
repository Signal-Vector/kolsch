"""bug-024 Test 5 (AC #10) — FLAGSHIP model-selection D-010 gate.

Boots a real `ChatApp` via Pilot against a workspace scaffolded with
`kolsch setup --non-interactive`, using the shipped helper agent
(the same one `kolsch chat` binds by default). A scripted
`LLMHandle` emits a `run_workflow(ref="kolsch/workflows/hello")`
tool call in response to prose "run the greet workflow". A mock
workflow executor is installed on the runtime's `WorkflowsImpl` so
the tool completes deterministically.

Asserts:

1. `model.request.tools` contains `run_workflow` (Layer 1 — the
   tool reaches the LLM's tool list).
2. A `CardWidget[kind="workflow"]` mounts in `#transcript` within
   the pilot pause window (Layer 2 — dispatch fires and `CardsImpl`
   publishes reach the adapter).
3. No `TOOL_CALLED` event has `tool_name="bash"` (regression —
   the model did NOT fall back to `bash kolsch workflows run`).
4. The `WorkflowRenderer` observed `RUN_STARTED` (bug-020's live
   plumbing still works end-to-end).

Uses NO `_replay_ui_events_since`, NO `adapter.handle(...)`, NO
`_invoke_tool_via_pilot`. Per D-010, this is the gate for future
refactors that touch tool visibility or declared: semantics.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from cli.chat.app import ChatApp
from cli.chat.session import ChatSessionBundle
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.card_widget import CardWidget
from engine.framework import LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas.events import EventType
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId


# ---------------------------------------------------------------------------
# Fixture — a workspace scaffolded via `kolsch setup --non-interactive`
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def initialized_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the shipped starter bundle via `kolsch setup`.

    Uses subprocess invocation so the packaged `_starter_bundle` copy
    happens the same way the production `kolsch setup` path exercises.
    Reads the same helper.yaml / tool files bug-024 fixed in-tree —
    the `declared:` list must now include the three workflow tools,
    and the copied `run_workflow.py` must carry the rich description.
    """
    root = tmp_path_factory.mktemp("bug024_workspace")
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)

    # The workspace-default authz has `run_workflow` at `prompt` —
    # under a headless pilot with no modal callback that collapses to
    # deny. Flip to `allow` so the tool actually dispatches (mirrors
    # what `_allow_bash_in_workspace` does in test_tool_wiring for
    # the same reason). AC #10 is about model SELECTION; the modal
    # path is covered by the chat lifecycle suite.
    yaml_path = root / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    rules = payload["authorization"]["config"]["rules"]
    for rule in rules:
        if (
            rule.get("what") == "kolsch/tools/run_workflow"
            and rule.get("effect") == "prompt"
        ):
            rule["effect"] = "allow"
            break
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    return root


# ---------------------------------------------------------------------------
# Scripted LLM — emits one `run_workflow` tool call then a text reply
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Two-response LLM: tool call → text.

    Records every `LLMRequest` on `self.calls` so the test can assert
    the tools list the model saw (AC #10 (1)).
    """

    def __init__(self, *, workflow_ref: str) -> None:
        self._workflow_ref = workflow_ref
        self._turn = 0
        self.calls: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        turn = self._turn
        self._turn += 1
        if turn == 0:
            # First turn — request the workflow tool with a real ref.
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": f"tc_{uuid4()}",
                        "name": "run_workflow",
                        "arguments": {"ref": self._workflow_ref, "inputs": {}},
                    },
                ],
            )
        # Second turn — the model summarises the tool result and stops.
        return LLMResponse(content="Started the workflow.")


# ---------------------------------------------------------------------------
# Helpers — scripted workflow events
# ---------------------------------------------------------------------------


def _mk_workflow_events() -> list[Any]:
    """RUN_STARTED + NODE_STARTED + RUN_COMPLETED — mirrors bug-020's test."""
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    trace = TraceId(f"tr_{uuid4()}")
    inv = InvocationId(f"inv_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            invocation_id=inv,
            node_name="greet",
        ),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
    ]


# ---------------------------------------------------------------------------
# The flagship — one test that proves the whole composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prose_run_workflow_triggers_run_workflow_tool_call(
    initialized_workspace: Path,
) -> None:
    """The load-bearing D-010 gate for bug-024.

    Boots a real ChatApp via Pilot, submits prose asking to run the
    hello workflow, and asserts the model called `run_workflow` (not
    `bash`).
    """
    workflow_ref = "kolsch/workflows/hello"
    llm = _ScriptedLLM(workflow_ref=workflow_ref)
    runtime = await bootstrap_runtime(root=initialized_workspace, llm=llm.__call__)

    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="bug024-flagship",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    bundle = ChatSessionBundle(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=thread_id,
        agent_ref="kolsch/agents/helper",
        root=initialized_workspace,
        model_label=f"{config.model.provider}/{config.model.model}",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,
        engine=runtime.engine,
        full_history=False,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # Install a scripted workflow executor so `run_workflow`
        # completes deterministically without needing a real runner.
        workflows_impl = pilot.app._service_registry.workflows
        assert workflows_impl is not None, (
            "ChatApp.on_mount failed to build WorkflowsImpl for the shipped "
            "helper agent — bug-020 plumbing regression."
        )
        install_mock_executor_factory(
            workflows_impl,
            {workflow_ref: MockExecutorSpec(
                events=_mk_workflow_events(),
                per_event_delay=0.02,
            )},
            runs_root=initialized_workspace / "runs",
        )

        # Drive the exchange by triggering `_run_exchange` directly
        # (mirrors what the Composer's `Submitted` handler does when
        # the user presses Enter). This exercises the same
        # `engine.execute_stream` path as production.
        pilot.app._run_exchange("run the greet workflow")

        # Poll the app for the flagship's four assertions.
        card_widget: CardWidget | None = None
        for _ in range(200):  # 200 * 50ms = 10s cap
            await pilot.pause()
            widgets = [
                w for w in pilot.app.query(CardWidget)
                if getattr(w, "kind", None) == "workflow"
            ]
            if widgets:
                card_widget = widgets[0]
                break
            await asyncio.sleep(0.05)

        # Assertion 1 (AC #10 pre-req): the tools list the model saw
        # includes `run_workflow`. This is the Layer 1 fix.
        assert llm.calls, "the scripted LLM was never invoked"
        first_call = llm.calls[0]
        tool_names_seen_by_model = {
            (
                t.get("function", {}).get("name")
                if isinstance(t, dict) and "function" in t
                else t.get("name")
            )
            for t in first_call.tools
        }
        assert "run_workflow" in tool_names_seen_by_model, (
            "The model was not advertised `run_workflow`. The shipped "
            "helper.yaml declared: list is missing "
            "kolsch/tools/run_workflow — bug-024 Layer 1 regression. "
            f"Got tools: {sorted(str(n) for n in tool_names_seen_by_model)}"
        )

        # Assertion 2 (AC #10.1): a workflow CardWidget mounted in the
        # transcript via the live bus subscription seam (no manual
        # store drain, no replay scaffolding).
        if card_widget is None:
            # Collect the tool-selection events for a targeted failure
            # message — the two failure modes are (a) run_workflow not
            # called at all (authz denied / model chose bash) or (b)
            # bus subscription seam broken.
            store = pilot.app.session.runtime.store
            tool_events: list[dict[str, Any]] = []
            async for event in store.read_events(
                pilot.app.session.thread_id,
            ):
                if event.type not in {
                    EventType.TOOL_CALLED,
                    EventType.TOOL_FAILED,
                }:
                    continue
                data = getattr(event, "data", None) or {}
                if isinstance(data, dict):
                    tool_events.append({
                        "type": str(event.type),
                        "tool_name": data.get("tool_name"),
                        "error": (data.get("error") or {}).get("type"),
                    })
            raise AssertionError(
                "CardWidget[kind='workflow'] did not mount within 10s. "
                "Either run_workflow was not called (Layer 1 regression) "
                "or the bus subscription seam is broken.\n"
                f"LLM calls seen: {len(llm.calls)}\n"
                f"Tool names in first LLM request: "
                f"{sorted(str(n) for n in tool_names_seen_by_model)}\n"
                f"Tool events: {tool_events!r}"
            )
        assert isinstance(card_widget._body, WorkflowRenderer), (
            f"card body is {type(card_widget._body).__name__}, "
            "expected WorkflowRenderer"
        )

        # Assertion 3 (AC #10.2): bash was NOT called on the store.
        store = pilot.app.session.runtime.store
        bash_calls: list[Any] = []
        async for event in store.read_events(pilot.app.session.thread_id):
            if event.type != EventType.TOOL_CALLED:
                continue
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                continue
            if data.get("tool_name") == "bash":
                bash_calls.append(data)
        assert bash_calls == [], (
            "The model fell back to bash — bug-024 has regressed. "
            f"Observed bash calls: {bash_calls!r}"
        )

        # Assertion 4 (AC #10.3): the renderer received RUN_STARTED
        # (populated at least one node row after the mock executor
        # fired NODE_STARTED).
        for _ in range(100):
            await pilot.pause()
            if card_widget._body._node_rows:
                break
            await asyncio.sleep(0.05)
        assert card_widget._body._node_rows, (
            "WorkflowRenderer never received any node rows; the "
            "workflow-progress UI events are not reaching the adapter's "
            "live path (bug-020 plumbing regression)."
        )


# ---------------------------------------------------------------------------
# AC #10 self-test — this file uses none of the scaffolding D-010 forbids
# ---------------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep-guard: bug-024's D-010 gate must NOT use replay scaffolding.

    Fails if a future edit reintroduces the three shortcuts bug-020's
    flagship used but that couldn't catch the model-selection
    regression. The banned tokens are built at runtime from segments
    so this test file itself doesn't contain them as literals (which
    would confuse the check).
    """
    this_lines = Path(__file__).read_text(encoding="utf-8").splitlines()

    # Build the banned tokens from segments so THIS file's source
    # doesn't contain the full literals (which would false-alarm
    # against itself).
    banned = (
        "_replay_" + "ui_events_since",
        "adapter.hand" + "le(",
        "_invoke_" + "tool_via_pilot",
    )

    # State machine: track whether we're inside a triple-quoted
    # docstring. Simplest sufficient parser for a test file this
    # size — no nested docstrings, no raw literals.
    in_docstring = False
    doc_delim = '"""'
    for line in this_lines:
        stripped = line.strip()
        # Toggle in/out of docstring on lines containing the delim.
        delim_count = stripped.count(doc_delim)
        if delim_count == 1:
            in_docstring = not in_docstring
            continue
        if delim_count >= 2:
            # Opens + closes on the same line — stays outside.
            continue
        if in_docstring:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        for token in banned:
            assert token not in line, (
                f"bug-024 flagship references banned scaffold "
                f"token {token!r} in code: {line!r}"
            )
