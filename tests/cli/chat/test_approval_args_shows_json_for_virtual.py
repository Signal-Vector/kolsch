"""bug-029 flagship (AC #5) — D-010 gate: approval Args shows JSON for virtual tool.

Boots a real ``ChatApp`` under Pilot on a scaffolded workspace where
``kolsch/tools/run_workflow`` is configured with effect ``prompt``.
A scripted LLM emits a per-workflow virtual tool call
(``run_workflow_kolsch_workflows_hello_agent(greeting="hi")``); the
engine's dispatch flow reaches ``authz.allow`` with the
bug-029 JSON-shaped ``target`` string
(``model_dump_json(exclude_defaults=True)`` — bug-029 §Fix). The
real ``InlineApprovalCallback`` → real ``PromptsImpl`` → real
adapter chain mounts an ``ApprovalBlock``. The test asserts:

- Exactly one ``ApprovalBlock`` mounts within a bounded 5s window.
- The mounted widget's ``Args`` row contains JSON delimiters (``{``
  and ``:``) plus the ``greeting`` field name — proving dispatch's
  ``target_shape="json"`` reached the widget through the full
  live-runtime seam.

No ``_replay_ui_events_since``. No ``adapter.handle(...)``. No
``_invoke_tool_via_pilot``. The grep-guard self-test at the end
locks D-010 in.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from textual.widgets import Static

from cli.chat.app import ChatApp
from cli.chat.session import ChatSessionBundle
from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock
from engine.framework import LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
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
# Fixture — scaffold via ``kolsch setup``; keep ``run_workflow`` at ``prompt``.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def prompt_gated_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize a fresh workspace with ``run_workflow`` gated on prompt.

    Mirrors ``tests/cli/chat/test_virtual_workflow_tool_one_shot.py``'s
    fixture but INVERTS its yaml patch: instead of flipping the
    ``kolsch/tools/run_workflow`` rule to ``allow``, we leave it as
    ``prompt`` so the model's virtual-tool call actually lands on the
    ``authz.allow`` → prompt seam and mounts an ``ApprovalBlock``.
    """
    root = tmp_path_factory.mktemp("bug029_workspace")
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

    # Sanity-check the seeded platform.yaml — the starter bundle SHOULD
    # ship ``kolsch/tools/run_workflow`` at ``prompt``. If a future
    # starter tweak flips it, this fixture wouldn't exercise the seam.
    yaml_path = root / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    rules = payload["authorization"]["config"]["rules"]
    saw_prompt = any(
        rule.get("what") == "kolsch/tools/run_workflow"
        and rule.get("effect") == "prompt"
        for rule in rules
    )
    if not saw_prompt:
        # Force-flip whichever rule matches back to ``prompt`` so the
        # flagship stays independent of starter-bundle drift.
        for rule in rules:
            if rule.get("what") == "kolsch/tools/run_workflow":
                rule["effect"] = "prompt"
                break
        else:
            rules.insert(
                0,
                {
                    "what": "kolsch/tools/run_workflow",
                    "pattern": "*",
                    "effect": "prompt",
                },
            )
        yaml_path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )

    return root


# ---------------------------------------------------------------------------
# Scripted LLM — turn 1 emits the virtual tool call; turn 2 stops.
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Emit a per-workflow virtual tool call on turn 1, then stop."""

    def __init__(
        self,
        *,
        virtual_tool_name: str,
        greeting_value: str,
    ) -> None:
        self._virtual_tool_name = virtual_tool_name
        self._greeting_value = greeting_value
        self._turn = 0
        self.calls: list[LLMRequest] = []

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
                        "arguments": {"greeting": self._greeting_value},
                    },
                ],
            )
        return LLMResponse(content="done.")


# ---------------------------------------------------------------------------
# Mock workflow events — kept minimal; the flagship never needs a full run.
# ---------------------------------------------------------------------------


def _mk_workflow_events() -> list[Any]:
    """A minimal RUN_STARTED → NODE_STARTED → RUN_COMPLETED sequence.

    The approval prompt fires BEFORE dispatch reaches the invoker
    body, so these events are only used if the widget resolves and
    the workflow actually starts. Kept in case a future test wires
    the approval answer too.
    """
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
# ChatApp bootstrap helper.
# ---------------------------------------------------------------------------


async def _boot_chat_app(
    workspace: Path,
    llm: _ScriptedLLM,
) -> AsyncIterator[ChatApp]:  # pragma: no cover — helper for tests
    """Bootstrap a real runtime + ChatApp against ``workspace``."""
    runtime = await bootstrap_runtime(root=workspace, llm=llm.__call__)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="bug029-flagship",
    )
    threads = await runtime.store.list_threads(session.session_id)
    bundle = ChatSessionBundle(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/helper",
        root=workspace,
        model_label=f"{config.model.provider}/{config.model.model}",
        is_resume=False,
    )
    yield ChatApp(
        session=bundle,
        engine=runtime.engine,
        full_history=False,
    )


# ---------------------------------------------------------------------------
# AC #5 — the flagship.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_args_line_contains_json_for_virtual_workflow_tool(
    prompt_gated_workspace: Path,
) -> None:
    """The mounted ApprovalBlock's ``Args`` row contains JSON delimiters.

    Full live path: scripted LLM → engine → dispatch →
    ``_extract_target_from_args`` (with ``target_shape="json"`` from
    the virtual's ``ToolDefinition``) → ``authz.allow`` with a
    JSON-shaped target → real ``InlineApprovalCallback`` fires →
    real ``PromptsImpl.choice`` emits ``WIRE_PROMPT_REQUESTED`` on
    the live bus → real adapter subscription mounts a real
    ``ApprovalBlock``. We then read the widget's ``Args`` row text
    and assert JSON delimiters + the ``greeting`` field are present.
    """
    workflow_ref = "kolsch/workflows/hello-agent"
    virtual_name = "run_workflow_kolsch_workflows_hello_agent"
    greeting_value = "hi"

    llm = _ScriptedLLM(
        virtual_tool_name=virtual_name,
        greeting_value=greeting_value,
    )

    app: ChatApp | None = None
    async for candidate in _boot_chat_app(prompt_gated_workspace, llm):
        app = candidate
        break
    assert app is not None

    async with app.run_test() as pilot:
        await pilot.pause()

        # Install a scripted workflow executor in case the workflow
        # actually starts (defensive — the ``prompt`` seam should
        # intercept BEFORE the invoker body runs).
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
            runs_root=prompt_gated_workspace / "runs",
        )

        # Drive an exchange — turn 1 emits the virtual tool call.
        pilot.app._run_exchange(
            "run the hello-agent workflow with greeting=hi",
        )

        # Poll for the ApprovalBlock (bounded 5s — the LLM roundtrip
        # + dispatch + wire emission takes measurably longer than the
        # 2s pass-through window bug-021's flagship uses).
        block: ApprovalBlock | None = None
        for _ in range(100):  # 100 * 50ms = 5s
            await pilot.pause()
            widgets = list(pilot.app.query(ApprovalBlock))
            if widgets:
                block = widgets[0]
                break
            await asyncio.sleep(0.05)

        assert block is not None, (
            "ApprovalBlock did not mount within 5s. Either the virtual "
            "tool call never reached dispatch, or the prompt seam did "
            "not fire — inspect llm.calls + store events for the run."
        )

        # Read the Args row's text. qol-047 replaced the ``.context``
        # Static for Args with a custom ``ApprovalArgsRow``; the
        # rendered target string sits inside a nested ``Static``.
        args_rows = list(block.query(ApprovalArgsRow))
        assert len(args_rows) == 1, (
            f"expected exactly one ApprovalArgsRow; got {len(args_rows)}"
        )
        args_texts = [str(sub.content) for sub in args_rows[0].query(Static)]
        args_joined = " | ".join(args_texts)

        # AC #5 — Args line contains JSON delimiters.
        assert "{" in args_joined, (
            f"Args row missing JSON opening delimiter `{{`. Row: {args_joined!r}"
        )
        assert ":" in args_joined, (
            f"Args row missing JSON key-value delimiter `:`. Row: {args_joined!r}"
        )
        # The virtual tool's typed field name should be present — proves
        # the full model_dump_json (not just a truncated field value)
        # reached the widget.
        assert "greeting" in args_joined, (
            "Args row missing the `greeting` field name from the JSON "
            f"dump. Row: {args_joined!r}"
        )


# ---------------------------------------------------------------------------
# D-010 grep-guard — flagship must not use replay scaffolding.
# ---------------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep-guard: bug-029 flagship uses only real Pilot dispatch.

    Fails if a future edit reintroduces the three shortcuts bug-020's
    flagship used but couldn't catch composition regressions. The
    banned tokens are built at runtime from segments so this file
    itself doesn't contain them as literals.
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
                f"bug-029 flagship references banned scaffold token "
                f"{token!r} in code: {line!r}"
            )
