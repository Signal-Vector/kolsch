"""bug-027 flagship (AC #10) — D-010 gate: model corrects retry after ``inputs={}``.

Session 4778 caught the model retrying ``run_workflow(inputs={})``
11 times in a row against a workflow with a required ``url`` input,
then abandoning ``run_workflow`` entirely and reaching for ``bash
"kolsch run … --url …"``. bug-025's error surface reached the model
but the wording didn't give it a JSON shape to copy. bug-027 fixed
that (FIX-D + FIX-B + FIX-E). This test proves the wire supports
the corrected retry cleanly.

D-010 extension: bug-025 tested "wire delivers a legible error";
this test extends the gate to "wire supports the model's
corrected retry, on the same session, without re-prompting" — the
composition that bug-025's error-only test could not observe.

The scripted LLM emits:

- Turn 1: ``run_workflow(ref=..., inputs={})`` (mimics the
  session-4778 shape).
- Turn 2 (after seeing the ``TOOL_FAILED`` payload): ``run_workflow(
  ref=..., inputs={"greeting": "hi"})`` (mimics a well-behaved model
  that reads the retry template).
- Turn 3: text summary "started the workflow".

Asserts:

1. Turn 1's ``tool.failed`` event carries the bug-027 error shape
   (workflow ref + missing key + ``inputs={"greeting":`` JSON
   template).
2. Turn 2's ``tool.called`` event has ``inputs.greeting=='hi'``.
3. No ``tool_name="bash"`` event in the store (session 4778 shape
   avoided).
4. ``CardWidget[kind="workflow"]`` mounts on turn 2 — the retry
   actually reaches the workflow layer.

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

    Mirrors bug-024's flagship fixture — subprocess invocation so the
    packaged ``_starter_bundle`` copy happens the same way production
    ``kolsch setup`` exercises it. Post-copy the fixture flips the
    ``run_workflow`` authz rule from ``prompt`` to ``allow`` so the
    headless pilot doesn't collapse dispatch to ``deny`` at the modal
    seam.
    """
    root = tmp_path_factory.mktemp("bug027_workspace")
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
# Scripted LLM — 3-turn: bad-inputs → corrected-inputs → text.
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Three-turn LLM emitting the session-4778 shape then the correction.

    Records every ``LLMRequest`` on ``self.calls`` so the test can
    verify turn 2's request context saw the ``tool.failed`` payload
    from turn 1 (the wire's round-trip discipline).
    """

    def __init__(self, *, workflow_ref: str, corrected_input_key: str) -> None:
        self._workflow_ref = workflow_ref
        self._corrected_input_key = corrected_input_key
        self._turn = 0
        self.calls: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        turn = self._turn
        self._turn += 1
        if turn == 0:
            # Session 4778's shape — the model tries ``inputs={}``
            # and the tool boundary raises WrappedError.
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": f"tc_{uuid4()}",
                        "name": "run_workflow",
                        "arguments": {
                            "ref": self._workflow_ref,
                            "inputs": {},
                        },
                    },
                ],
            )
        if turn == 1:
            # After seeing the JSON retry template, the well-behaved
            # model reissues with the required key populated.
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": f"tc_{uuid4()}",
                        "name": "run_workflow",
                        "arguments": {
                            "ref": self._workflow_ref,
                            "inputs": {self._corrected_input_key: "hi"},
                        },
                    },
                ],
            )
        # Turn 2 — the model summarises the tool result and stops.
        return LLMResponse(content="Started the workflow.")


# ---------------------------------------------------------------------------
# Mock workflow events for the corrective retry.
# ---------------------------------------------------------------------------


def _mk_workflow_events() -> list[Any]:
    """RUN_STARTED + NODE_STARTED + RUN_COMPLETED — matches bug-024's mix."""
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
# The flagship — bug-025's error shape + bug-027's corrective retry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_corrects_retry_after_bug_025_error_shape(
    initialized_workspace: Path,
) -> None:
    """AC #10 — end-to-end proof of the corrected retry round-trip.

    The shipped ``kolsch/workflows/hello-agent`` workflow has an
    unguarded ``{{ inputs.greeting }}`` reference in its ``prep``
    node's ``command:`` — perfect: bug-025 validation flags
    ``inputs={}`` as missing ``'greeting'``, and turn 2's
    ``inputs={"greeting": "hi"}`` clears the validator and reaches
    the mock executor.
    """
    workflow_ref = "kolsch/workflows/hello-agent"
    corrected_key = "greeting"
    llm = _ScriptedLLM(
        workflow_ref=workflow_ref,
        corrected_input_key=corrected_key,
    )
    runtime = await bootstrap_runtime(root=initialized_workspace, llm=llm.__call__)

    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="bug027-flagship",
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

        # Install a scripted workflow executor for the corrective
        # retry (turn 2 — turn 1 fails at validation and never
        # reaches the executor factory).
        workflows_impl = pilot.app._service_registry.workflows
        assert workflows_impl is not None
        install_mock_executor_factory(
            workflows_impl,
            {workflow_ref: MockExecutorSpec(
                events=_mk_workflow_events(),
                per_event_delay=0.02,
            )},
            runs_root=initialized_workspace / "runs",
        )

        # Drive the exchange.
        pilot.app._run_exchange(
            "run the hello-agent workflow with greeting=hi",
        )

        # Poll for the CardWidget to mount (turn 2's dispatched
        # success reaches the adapter's live path).
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
        # Assertion 1 (AC #10.1) — turn 1's tool.failed carries the
        # bug-027 error shape.
        # ------------------------------------------------------------
        run_workflow_failures = [
            f for f in tool_failed if f.get("tool_name") == "run_workflow"
        ]
        assert run_workflow_failures, (
            "expected at least one tool.failed for run_workflow "
            f"(turn 1's inputs={{}} call). Got failures: {tool_failed!r}"
        )
        first_failure = run_workflow_failures[0]
        # ``error.message`` is where WrappedError.message lands in the
        # framework's tool-result payload. Different framework impls
        # may nest slightly differently; walk common shapes.
        failure_text = _extract_error_text(first_failure)
        assert workflow_ref in failure_text, (
            f"bug-027 error missing workflow ref. Got: {failure_text!r}"
        )
        assert f"'{corrected_key}'" in failure_text, (
            f"bug-027 error missing missing-input name. Got: {failure_text!r}"
        )
        assert f'inputs={{"{corrected_key}":' in failure_text, (
            f"bug-027 error missing JSON retry template. Got: {failure_text!r}"
        )

        # ------------------------------------------------------------
        # Assertion 2 (AC #10.2) — turn 2's tool.called has the
        # required input populated.
        # ------------------------------------------------------------
        run_workflow_calls = [
            c for c in tool_called if c.get("tool_name") == "run_workflow"
        ]
        # There should be two calls: turn 1 (inputs={}) and turn 2
        # (inputs={key: value}).
        assert len(run_workflow_calls) >= 2, (
            "expected at least two run_workflow tool.called events "
            f"(turn 1 failure + turn 2 retry). Got: {run_workflow_calls!r}"
        )
        second_call = run_workflow_calls[1]
        second_args = _extract_arguments(second_call)
        assert isinstance(second_args, dict)
        assert second_args.get("inputs") == {corrected_key: "hi"}, (
            "turn 2's run_workflow tool.called must carry the corrected "
            f"inputs. Got: {second_args!r}"
        )

        # ------------------------------------------------------------
        # Assertion 3 (AC #10.3) — no bash tool call in the store.
        # ------------------------------------------------------------
        bash_calls = [
            c for c in tool_called if c.get("tool_name") == "bash"
        ]
        assert bash_calls == [], (
            "session 4778 shape must NOT reappear — the model must not "
            "fall back to bash. Observed bash calls: "
            f"{bash_calls!r}"
        )

        # ------------------------------------------------------------
        # Assertion 4 (AC #10.4) — CardWidget[kind='workflow'] mounts
        # on turn 2's dispatch.
        # ------------------------------------------------------------
        assert card_widget is not None, (
            "CardWidget[kind='workflow'] did not mount within 10s. "
            "Either turn 2's run_workflow was denied at authz, the "
            "corrective retry never reached dispatch, or the bus "
            "subscription seam is broken.\n"
            f"LLM calls: {len(llm.calls)}\n"
            f"tool.called events: {run_workflow_calls!r}\n"
            f"tool.failed events: {run_workflow_failures!r}"
        )


def _extract_error_text(event_data: dict[str, Any]) -> str:
    """Extract the WrappedError.message text from a ``tool.failed`` event.

    ``tool.failed`` payload shape varies slightly across engine
    versions; this helper walks the common nesting patterns so the
    assertion is stable across minor tweaks.
    """
    # Direct message field.
    msg = event_data.get("message")
    if isinstance(msg, str) and msg:
        return msg
    # error.message shape.
    err = event_data.get("error")
    if isinstance(err, dict):
        for key in ("message", "text", "detail"):
            val = err.get(key)
            if isinstance(val, str) and val:
                return val
    # Nothing found — fall back to string form so the assertion's
    # failure message shows what we got.
    return repr(event_data)


def _extract_arguments(event_data: dict[str, Any]) -> Any:
    """Extract the ``arguments`` dict from a ``tool.called`` event."""
    # Common shape: arguments at top level.
    args = event_data.get("arguments")
    if args is not None:
        return args
    # Nested under 'call'.
    call = event_data.get("call")
    if isinstance(call, dict):
        return call.get("arguments")
    return None


# ---------------------------------------------------------------------------
# D-010 grep-guard — bug-027 flagship must not use replay scaffolding.
# ---------------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep-guard: bug-027's D-010 gate uses only real Pilot dispatch.

    Fails if a future edit reintroduces the three shortcuts bug-020's
    flagship used but that couldn't catch the model-selection /
    corrected-retry regression. The banned tokens are built at
    runtime from segments so this test file itself doesn't contain
    them as literals (which would confuse the check).
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
                f"bug-027 flagship references banned scaffold "
                f"token {token!r} in code: {line!r}"
            )
