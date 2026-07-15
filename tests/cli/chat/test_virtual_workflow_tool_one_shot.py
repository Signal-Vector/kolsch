"""bug-028 flagship (AC #10) — D-010 gate: model one-shots virtual tool.

Session ``ses_6962c187`` proved that even with bug-027's rich prose
+ retry template, Kimi K2.6 could not synthesise ``{"url": "..."}``
for the generic ``run_workflow``'s opaque ``dict[str, object]``
``inputs`` slot. It called ``workflow_inspect`` as advised, read the
error template, and still emitted ``inputs={}`` byte-identically 13
times.

bug-028's fix is structural: emit one virtual per-workflow tool per
catalogued workflow at tool-list-assembly time, each with a TYPED
Pydantic args model. This flagship proves the model can now
one-shot the workflow — no ``workflow_inspect`` roundtrip, no
retry, no bash fallback — using the virtual tool.

The scripted LLM emits:

- Turn 1: ``run_workflow_kolsch_workflows_hello_agent(greeting="hi")``
  — the per-workflow virtual tool with a typed ``greeting`` slot.
- Turn 2: text summary "Started the workflow."

Asserts (per AC #10):

1. ``run_workflow_kolsch_workflows_hello_agent`` is in the model's
   tool list on the first ``LLMRequest``.
2. Turn 1's ``tool.called`` event carries the virtual tool name +
   the typed arg populated (``greeting="hi"``).
3. Workflow starts, ``CardWidget[kind="workflow"]`` mounts.
4. No fallback to the generic ``run_workflow`` OR to ``bash``.
5. No ``TOOL_FAILED`` events.

Uses NO ``_replay_ui_events_since``, NO ``adapter.handle(...)``, NO
``_invoke_tool_via_pilot``. Grep-guard self-test enforces D-010.
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
# Fixture — workspace scaffolded via ``kolsch setup --non-interactive``.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def initialized_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the shipped starter bundle via ``kolsch setup``.

    Mirrors bug-024/-027's flagship fixture. Post-copy the fixture
    flips the ``run_workflow`` authz rule from ``prompt`` to
    ``allow`` so the headless pilot doesn't collapse dispatch to
    ``deny`` at the modal seam. The virtuals share the generic
    tool's authz action bundle (``kolsch``), so the same rule
    covers them.
    """
    root = tmp_path_factory.mktemp("bug028_workspace")
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
# Scripted LLM — 2-turn: virtual tool one-shot + text summary.
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Scripted 2-turn LLM emitting the virtual tool call one-shot.

    Records every ``LLMRequest`` on ``self.calls`` so the test can
    verify turn 1's request tool list included the virtual name.
    """

    def __init__(
        self,
        *,
        virtual_tool_name_: str,
        greeting_value: str,
    ) -> None:
        self._virtual_tool_name = virtual_tool_name_
        self._greeting_value = greeting_value
        self._turn = 0
        self.calls: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        turn = self._turn
        self._turn += 1
        if turn == 0:
            # bug-028's happy path — the model sees a typed
            # ``greeting: str`` slot on the virtual tool and calls
            # it directly. NO ``workflow_inspect`` roundtrip; NO
            # ``inputs={}`` retry loop.
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": f"tc_{uuid4()}",
                        "name": self._virtual_tool_name,
                        "arguments": {
                            "greeting": self._greeting_value,
                        },
                    },
                ],
            )
        # Turn 1 — the model summarises the tool result and stops.
        return LLMResponse(content="Started the workflow.")


# ---------------------------------------------------------------------------
# Mock workflow events for the one-shot invocation.
# ---------------------------------------------------------------------------


def _mk_workflow_events() -> list[Any]:
    """RUN_STARTED + NODE_STARTED + RUN_COMPLETED — matches bug-024/-027's mix."""
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    trace = TraceId(f"tr_{uuid4()}")
    inv = InvocationId(f"inv_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=trace,
            invocation_id=inv,
            node_name="prep",
        ),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
    ]


# ---------------------------------------------------------------------------
# The flagship — bug-028's virtual tool one-shot.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_calls_virtual_tool_directly(
    initialized_workspace: Path,
) -> None:
    """AC #10 — one-shot end-to-end proof of the virtual tool path.

    The shipped ``kolsch/workflows/hello-agent`` workflow references
    ``{{ inputs.greeting }}`` in its ``prep`` node's ``command:``.
    bug-028's per-workflow virtual (name
    ``run_workflow_kolsch_workflows_hello_agent``) surfaces
    ``greeting: str`` as a required arg on the virtual tool itself,
    so the scripted LLM's one-shot ``greeting="hi"`` call flows
    through validation, opens a card, and starts the run — no
    retry, no ``workflow_inspect``, no ``bash`` fallback.
    """
    workflow_ref = "kolsch/workflows/hello-agent"
    # Deterministic virtual-tool name — matches
    # ``workflows.tool_schema.virtual_tool_name()``.
    virtual_name = "run_workflow_kolsch_workflows_hello_agent"

    llm = _ScriptedLLM(
        virtual_tool_name_=virtual_name,
        greeting_value="hi",
    )
    runtime = await bootstrap_runtime(root=initialized_workspace, llm=llm.__call__)

    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="bug028-flagship",
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

        # Install a scripted workflow executor so turn 1's dispatched
        # call reaches something rather than blocking on a real
        # executor.
        workflows_impl = pilot.app._service_registry.workflows
        assert workflows_impl is not None
        install_mock_executor_factory(
            workflows_impl,
            {
                workflow_ref: MockExecutorSpec(
                    events=_mk_workflow_events(),
                    per_event_delay=0.02,
                )
            },
            runs_root=initialized_workspace / "runs",
        )

        # Drive the exchange.
        pilot.app._run_exchange(
            "run the hello-agent workflow with greeting=hi",
        )

        # Poll for the CardWidget to mount (turn 1's dispatched
        # success reaches the adapter's live path).
        card_widget: CardWidget | None = None
        for _ in range(200):  # 200 * 50ms = 10s cap
            await pilot.pause()
            widgets = [
                w
                for w in pilot.app.query(CardWidget)
                if getattr(w, "kind", None) == "workflow"
            ]
            if widgets:
                card_widget = widgets[0]
                break
            await asyncio.sleep(0.05)

        # ------------------------------------------------------------
        # Collect the tool-related events from the store.
        # ------------------------------------------------------------
        store = pilot.app.session.runtime.store
        tool_called: list[dict[str, Any]] = []
        tool_failed: list[dict[str, Any]] = []
        async for event in store.read_events(pilot.app.session.thread_id):
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                continue
            if event.type == EventType.TOOL_CALLED:
                tool_called.append(data)
            elif event.type == EventType.TOOL_FAILED:
                tool_failed.append(data)

        # ------------------------------------------------------------
        # Assertion 1 (AC #10.1) — virtual name is in the model's
        # tool list on the first request.
        # ------------------------------------------------------------
        assert llm.calls, "expected at least one LLM request"
        first_request = llm.calls[0]
        tool_names_seen = _extract_tool_names(first_request)
        assert virtual_name in tool_names_seen, (
            f"virtual tool {virtual_name!r} must appear in the model's "
            f"tool list on turn 1. Got: {sorted(tool_names_seen)!r}"
        )

        # ------------------------------------------------------------
        # Assertion 2 (AC #10.2) — turn 1 tool.called has the virtual
        # tool + typed arg populated.
        # ------------------------------------------------------------
        virtual_calls = [c for c in tool_called if c.get("tool_name") == virtual_name]
        assert len(virtual_calls) == 1, (
            f"expected exactly one virtual-tool call (turn 1). Got: {virtual_calls!r}"
        )
        virtual_args = _extract_arguments(virtual_calls[0])
        assert isinstance(virtual_args, dict)
        assert virtual_args.get("greeting") == "hi", (
            "virtual-tool call must carry the typed ``greeting`` arg. "
            f"Got: {virtual_args!r}"
        )

        # ------------------------------------------------------------
        # Assertion 3 (AC #10.3) — workflow starts, card mounts.
        # ------------------------------------------------------------
        assert card_widget is not None, (
            "CardWidget[kind='workflow'] did not mount within 10s. "
            f"Virtual tool calls seen: {virtual_calls!r}"
        )

        # ------------------------------------------------------------
        # Assertion 4 (AC #10.4) — NO fallback to generic run_workflow
        # OR bash.
        # ------------------------------------------------------------
        generic_calls = [c for c in tool_called if c.get("tool_name") == "run_workflow"]
        assert generic_calls == [], (
            "bug-028 gate: model must NOT fall back to generic "
            f"run_workflow. Observed: {generic_calls!r}"
        )
        bash_calls = [c for c in tool_called if c.get("tool_name") == "bash"]
        assert bash_calls == [], (
            f"bug-028 gate: model must NOT fall back to bash. Observed: {bash_calls!r}"
        )

        # ------------------------------------------------------------
        # Assertion 5 (AC #10.5) — no TOOL_FAILED events.
        # ------------------------------------------------------------
        assert tool_failed == [], (
            "bug-028 one-shot must not produce any TOOL_FAILED "
            f"events. Observed: {tool_failed!r}"
        )


# ---------------------------------------------------------------------------
# Helpers — resilient to minor event-shape drift.
# ---------------------------------------------------------------------------


def _extract_tool_names(request: LLMRequest) -> set[str]:
    """Extract every tool name visible to the model on ``request``.

    ``LLMRequest.tools`` is provider-shaped — its exact type varies
    across engine impls. This helper handles the common shapes:

    - ``list[ToolInfo]`` — each entry has ``.name``.
    - ``list[dict]`` — each entry has ``"name"``.
    - ``dict[str, Any]`` — keys are tool names.
    """
    tools = getattr(request, "tools", None)
    if tools is None:
        return set()
    names: set[str] = set()
    if isinstance(tools, dict):
        return {str(k) for k in tools}
    if isinstance(tools, list):
        for entry in tools:
            name = getattr(entry, "name", None)
            if isinstance(name, str):
                names.add(name)
                continue
            if isinstance(entry, dict):
                val = entry.get("name")
                if isinstance(val, str):
                    names.add(val)
    return names


def _extract_arguments(event_data: dict[str, Any]) -> Any:
    """Extract the ``arguments`` dict from a ``tool.called`` event."""
    args = event_data.get("arguments")
    if args is not None:
        return args
    call = event_data.get("call")
    if isinstance(call, dict):
        return call.get("arguments")
    return None


# ---------------------------------------------------------------------------
# D-010 grep-guard — flagship must not use replay scaffolding.
# ---------------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep-guard: bug-028's D-010 gate uses only real Pilot dispatch.

    Fails if a future edit reintroduces the three shortcuts bug-020's
    flagship used but that couldn't catch composition regressions.
    The banned tokens are built at runtime from segments so this
    test file itself doesn't contain them as literals.
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
                f"bug-028 flagship references banned scaffold "
                f"token {token!r} in code: {line!r}"
            )
