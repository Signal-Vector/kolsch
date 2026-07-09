"""bug-031 flagship (AC #10) — D-010 gate: blocking run_workflow → prose.

Session ``ses_af5980ab`` proved that the pre-bug-031 fire-and-forget
``run_workflow`` shape left the model with no path back to narrating
the outcome:

- Workflow started at 15:53:18 ("I'll keep an eye on it").
- ``thread.exchange_completed`` fired at 15:53:23.
- Workflow finished at 15:56:41 with full ``outputs.synthesize`` on
  the RUN_COMPLETED payload.
- 12 min 59s dead zone until the user asked "what was the outcome?"
- Model bash-grepped the JSONL to reconstruct — ``workflow_status``
  returned ``phase=unknown`` (registry aged out after 60s).

bug-031's fix is threefold:

1. ``WorkflowRunStatusView.outputs`` — new field.
2. Cache ``RUN_COMPLETED.payload.outputs`` on the record; surface via
   ``status()``.
3. Blocking ``run_workflow`` — long-poll ``status()`` at 500ms until
   the phase transitions to terminal or ``timeout_seconds`` elapses.
   Returns ``{run_id, card_id, phase, outputs, error_message,
   _chat_anchor}`` so the model's turn 2 sees the outputs and prose-
   narrates in the same exchange.

This flagship scripts a two-turn ``_ScriptedLLM``:

- Turn 1: emits ``run_workflow_kolsch_workflows_hello_agent(
  greeting="hi")`` — the per-workflow virtual tool.
- Turn 2: emits prose that mentions the workflow's output.

Asserts (per AC #10):

1. Turn 1 tool.result has a non-null ``outputs`` dict.
2. Turn 2 model prose mentions the output content.
3. ``CardWidget(kind='workflow')`` mounts.
4. No ``TOOL_FAILED`` events.

D-010 grep-guard: no ``_replay_ui_events_since``, no
``adapter.handle(...)``, no ``_invoke_tool_via_pilot``.
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
from cli.chat.widgets.card_widget import CardWidget
from engine.framework import LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas.events import EventType
from tests.cli.chat.services.fakes import (
    FakeExecutorSpec,
    install_fake_executor_factory,
)
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId


# ---------------------------------------------------------------------------
# Fixture — workspace scaffolded via ``kolsch setup``.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def initialized_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the shipped starter bundle via ``kolsch setup``.

    Mirrors bug-028's flagship fixture. Flips the ``run_workflow``
    authz rule from ``prompt`` to ``allow`` so the headless pilot
    doesn't collapse dispatch to ``deny`` at the modal seam.
    """
    root = tmp_path_factory.mktemp("bug031_workspace")
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
# Scripted LLM — 2-turn: virtual tool call + prose summary.
# ---------------------------------------------------------------------------


_EXPECTED_GREETING_STDOUT = "hi, world"
"""The bash node's expected stdout — the fake RUN_COMPLETED payload
carries this under ``outputs.prep.stdout`` so the model's turn 2
prose can mention it and the assertion has something concrete to
match.
"""


class _ScriptedLLM:
    """Scripted 2-turn LLM: virtual tool call → prose summary of outputs.

    Records every ``LLMRequest`` so the test can inspect the turn 2
    request's assistant/tool_result messages (which the engine
    populates from the completed turn 1 tool call).
    """

    def __init__(self, *, virtual_tool_name: str) -> None:
        self._virtual_tool_name = virtual_tool_name
        self._turn = 0
        self.calls: list[LLMRequest] = []
        # Captured by the test to assert the model saw the outputs
        # before emitting its turn 2 prose.
        self.turn2_saw_output_content: bool = False

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        turn = self._turn
        self._turn += 1
        if turn == 0:
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": f"tc_{uuid4()}",
                        "name": self._virtual_tool_name,
                        "arguments": {"greeting": "hi"},
                    },
                ],
            )
        # Turn 2: inspect the request's messages for the tool_result
        # carrying the outputs. The framework serialises tool_result
        # bodies as JSON strings inside the message; a substring
        # search is resilient to the exact shape.
        for msg in getattr(request, "messages", ()) or ():
            body = _stringify_message(msg)
            if _EXPECTED_GREETING_STDOUT in body:
                self.turn2_saw_output_content = True
                break
        # Narrate the outputs — this is exactly the "cell from the
        # main agent with the result" the user asked for in af59.
        return LLMResponse(
            content=(
                f"The workflow completed successfully — the bash node "
                f"produced {_EXPECTED_GREETING_STDOUT!r} and the "
                f"classify agent picked it up."
            ),
        )


def _stringify_message(msg: object) -> str:
    """Serialize a message shape to a string for substring search.

    ``LLMRequest.messages`` is provider-shaped — its exact type
    varies across engine impls (list of dicts, pydantic models with
    ``.content``, etc.). This helper walks the common shapes and
    concatenates every string field it finds.
    """
    parts: list[str] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)
        else:
            # Pydantic model — walk ``model_dump`` if present.
            dump = getattr(obj, "model_dump", None)
            if callable(dump):
                try:
                    _walk(dump())
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            else:
                content = getattr(obj, "content", None)
                if content is not None:
                    _walk(content)

    _walk(msg)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Fake workflow events — RUN_STARTED + NODE_STARTED/COMPLETED + RUN_COMPLETED
# with a populated ``outputs`` payload so the tool's poll observes it.
# ---------------------------------------------------------------------------


def _mk_workflow_events() -> list[Any]:
    """Emit RUN_STARTED → NODE(prep) → RUN_COMPLETED with outputs.

    The RUN_COMPLETED payload carries ``outputs`` keyed by node
    name — matches the executor's ``_assemble_terminal_outputs``
    shape (see ``src/workflows/executor.py:1570``). This is exactly
    what ``WorkflowsImpl._drain_workflow_bridge`` reads via bug-031's
    ``_maybe_capture_outputs`` seam.
    """
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    trace = TraceId(f"tr_{uuid4()}")
    inv = InvocationId(f"inv_{uuid4()}")
    outputs_payload: dict[str, Any] = {
        "outputs": {
            "prep": {"stdout": _EXPECTED_GREETING_STDOUT, "exit_code": 0},
        },
    }
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            invocation_id=inv,
            node_name="prep",
        ),
        WorkflowNodeCompletedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            invocation_id=inv,
            node_name="prep",
        ),
        WorkflowRunCompletedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            payload=outputs_payload,
        ),
    ]


# ---------------------------------------------------------------------------
# The flagship — bug-031's blocking run_workflow + prose summary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_blocks_and_model_narrates_outputs(
    initialized_workspace: Path,
) -> None:
    """AC #10 — real Pilot ChatApp exercises the full round-trip.

    Boots ChatApp with a scripted 2-turn LLM. Turn 1 emits the
    per-workflow virtual tool ``run_workflow_kolsch_workflows_hello_agent``
    with ``greeting="hi"``. The tool blocks until the fake executor
    fires RUN_COMPLETED (with a populated ``outputs`` dict). Turn 2
    emits a prose summary that mentions the output content.

    Asserts:

    1. Turn 1 tool.result carries a non-null ``outputs`` dict —
       bug-031's blocking + outputs surfacing wired end-to-end.
    2. Turn 2 model prose mentions the output content — the model
       had the outputs in hand when it composed its response.
    3. ``CardWidget[kind='workflow']`` mounts.
    4. No ``TOOL_FAILED`` events.
    """
    workflow_ref = "kolsch/workflows/hello-agent"
    virtual_name = "run_workflow_kolsch_workflows_hello_agent"

    llm = _ScriptedLLM(virtual_tool_name=virtual_name)
    runtime = await bootstrap_runtime(
        root=initialized_workspace, llm=llm.__call__,
    )

    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="bug031-flagship",
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

        # Install a scripted executor so RUN_COMPLETED carries the
        # ``outputs`` dict the tool needs to surface.
        workflows_impl = pilot.app._service_registry.workflows
        assert workflows_impl is not None
        install_fake_executor_factory(
            workflows_impl,
            {workflow_ref: FakeExecutorSpec(
                events=_mk_workflow_events(),
                # Small per-event delay — enough to visit the poll
                # loop's ``asyncio.sleep(0.5)`` at least once.
                per_event_delay=0.05,
            )},
            runs_root=initialized_workspace / "runs",
        )

        # Drive the exchange.
        pilot.app._run_exchange(
            "run the hello-agent workflow with greeting=hi",
        )

        # Poll for the CardWidget to mount and for the 2 LLM turns to
        # complete. The blocking tool holds turn 1 open until
        # RUN_COMPLETED lands (~0.15s) plus the 500ms poll interval —
        # give it up to 20s to be safe under Pilot's scheduler.
        card_widget: CardWidget | None = None
        for _ in range(400):  # 400 * 50ms = 20s cap
            await pilot.pause()
            if card_widget is None:
                widgets = [
                    w for w in pilot.app.query(CardWidget)
                    if getattr(w, "kind", None) == "workflow"
                ]
                if widgets:
                    card_widget = widgets[0]
            # Turn 2 completion — the scripted LLM's ``_turn``
            # increments to 2 after emitting the prose summary.
            if llm._turn >= 2 and card_widget is not None:
                break
            await asyncio.sleep(0.05)

        # ------------------------------------------------------------
        # Collect tool + agent-message events from the store.
        # ------------------------------------------------------------
        store = pilot.app.session.runtime.store
        tool_called: list[dict[str, Any]] = []
        tool_result: list[dict[str, Any]] = []
        tool_failed: list[dict[str, Any]] = []
        agent_messages: list[dict[str, Any]] = []
        async for event in store.read_events(pilot.app.session.thread_id):
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                continue
            if event.type == EventType.TOOL_CALLED:
                tool_called.append(data)
            elif event.type == EventType.TOOL_RESULT:
                tool_result.append(data)
            elif event.type == EventType.TOOL_FAILED:
                tool_failed.append(data)
            elif event.type == EventType.AGENT_MESSAGE:
                agent_messages.append(data)

        # ------------------------------------------------------------
        # AC #10.4 — no TOOL_FAILED events (fastest gate).
        # ------------------------------------------------------------
        assert tool_failed == [], (
            f"bug-031 flagship must not produce TOOL_FAILED events. "
            f"Observed: {tool_failed!r}"
        )

        # ------------------------------------------------------------
        # AC #10.3 — workflow card mounted.
        # ------------------------------------------------------------
        assert card_widget is not None, (
            "CardWidget[kind='workflow'] did not mount within 20s. "
            f"tool_called={tool_called!r} tool_result={tool_result!r}"
        )

        # ------------------------------------------------------------
        # AC #10.1 — turn 1 tool.result carries non-null ``outputs``.
        # ------------------------------------------------------------
        virtual_results = [
            r for r in tool_result
            if r.get("tool_name") == virtual_name
        ]
        assert virtual_results, (
            f"expected a tool_result for {virtual_name!r}. "
            f"tool_result={tool_result!r}"
        )
        first_result = virtual_results[0]
        # The tool return dict lands under the result event's
        # ``output`` field (framework populates this from the tool
        # coroutine's return value). Extract + assert ``outputs`` is
        # a non-None dict.
        output_payload = _extract_tool_output(first_result)
        assert isinstance(output_payload, dict), (
            f"tool_result output was not a dict: {output_payload!r}"
        )
        assert output_payload.get("phase") in (
            "success", "error", "cancelled",
        ), (
            f"expected terminal phase from bug-031 blocking tool. "
            f"Got: {output_payload.get('phase')!r}"
        )
        outputs_dict = output_payload.get("outputs")
        assert isinstance(outputs_dict, dict), (
            f"bug-031 AC #10.1 — turn 1 tool_result MUST carry a "
            f"non-null ``outputs`` dict. Got: {outputs_dict!r}. "
            f"Full payload: {output_payload!r}"
        )
        # Sanity: outputs contain the prep node's stdout.
        assert _EXPECTED_GREETING_STDOUT in _stringify_message(outputs_dict), (
            f"outputs dict {outputs_dict!r} did not carry the expected "
            f"prep stdout {_EXPECTED_GREETING_STDOUT!r}"
        )

        # ------------------------------------------------------------
        # AC #10.2 — turn 2 model prose mentions the output content.
        # ------------------------------------------------------------
        assert len(llm.calls) >= 2, (
            f"expected at least 2 LLM calls (tool call + prose "
            f"summary); got {len(llm.calls)}"
        )
        assert llm.turn2_saw_output_content, (
            "bug-031 AC #10.2 — turn 2's LLM request did NOT carry "
            "the tool_result's output content in its messages. The "
            "model would have narrated blind. Check the framework's "
            "tool_result serialisation and the tool's return shape."
        )
        # The scripted turn 2 response mentions the output — that
        # response should also land as an AGENT_MESSAGE.
        prose_matches = [
            m for m in agent_messages
            if _EXPECTED_GREETING_STDOUT in _stringify_message(m)
        ]
        assert prose_matches, (
            f"bug-031 AC #10.2 — no agent.message carried the output "
            f"content {_EXPECTED_GREETING_STDOUT!r}. Turn 2 prose "
            f"landed but content was elided. agent_messages="
            f"{agent_messages!r}"
        )


# ---------------------------------------------------------------------------
# Helpers — resilient to minor event-shape drift.
# ---------------------------------------------------------------------------


def _extract_tool_output(event_data: dict[str, Any]) -> Any:
    """Extract the tool's return dict from a ``tool.result`` event."""
    output = event_data.get("output")
    if output is not None:
        return output
    # Some engine paths nest under ``result`` or ``content``.
    for key in ("result", "content", "return"):
        candidate = event_data.get(key)
        if candidate is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# D-010 grep-guard — flagship must not use replay scaffolding.
# ---------------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep-guard: bug-031's D-010 gate uses only real Pilot dispatch.

    Fails if a future edit reintroduces the three shortcuts bug-020's
    flagship used but that couldn't catch composition regressions.
    The banned tokens are built at runtime from segments so this test
    file itself doesn't contain them as literals.
    """
    this_lines = Path(__file__).read_text(encoding="utf-8").splitlines()

    banned = (
        "_replay_" + "ui_events_since",
        "adapter.hand" + "le(",
        "_invoke_" + "tool_via_pilot",
    )

    in_docstring = False
    doc_delim = '"""'
    for line in this_lines:
        stripped = line.strip()
        delim_count = stripped.count(doc_delim)
        if delim_count == 1:
            in_docstring = not in_docstring
            continue
        if delim_count >= 2:
            continue
        if in_docstring:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        for token in banned:
            assert token not in line, (
                f"bug-031 flagship references banned scaffold "
                f"token {token!r} in code: {line!r}"
            )
