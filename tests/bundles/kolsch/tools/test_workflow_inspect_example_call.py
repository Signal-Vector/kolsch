"""bug-028 — ``workflow_inspect.example_call`` prefers the virtual tool name.

Two scenarios (AC #8):

1. When virtuals are enabled (default), ``example_call`` uses the
   per-workflow virtual tool shape:
   ``run_workflow_<bundle>_<name>(url="<value>")``.
2. When ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0``, ``example_call`` falls
   back to bug-027's shape:
   ``run_workflow(ref="...", inputs={...})``.

Exercised against the bundle's shipped ``workflow_inspect.py`` — the
same tool ``ChatApp`` registers at runtime.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext


# ---------------------------------------------------------------------------
# Load the tool function from the bundle (mirrors test_workflow_inspect.py).
# ---------------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO / "bundles" / "kolsch" / "tools" / "workflow_inspect.py"


def _load_workflow_inspect() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.workflow_inspect_probe_028",
        _TOOL_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.workflow_inspect


class _FakeWorkflows:
    """Duck-typed workflows exposing the two seams the tool reads."""

    def __init__(self, *, yaml_by_ref: dict[str, str]) -> None:
        self._yaml_by_ref = yaml_by_ref

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)

    def list_workflow_refs(self) -> list[str]:
        return sorted(self._yaml_by_ref)


def _mk_ctx(workflows: Any) -> ToolContext:
    return ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        workflows=workflows,
    )


_JD_YAML = """\
description: 'Given a URL, review it.'
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: 'curl {{ inputs.url }}'
graph:
  edges: []
"""


# ---------------------------------------------------------------------------
# Test 1 — virtual name preferred when the flag is enabled (default).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_example_call_prefers_virtual_name_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #8 — with virtuals enabled, ``example_call`` uses the virtual name."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    workflow_inspect = _load_workflow_inspect()
    workflows = _FakeWorkflows(
        yaml_by_ref={"andre/workflows/job_description_review": _JD_YAML},
    )
    ctx = _mk_ctx(workflows)
    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
    })
    result = await workflow_inspect._tool_definition.func(args, ctx)

    example = result["example_call"]
    assert isinstance(example, str)
    # Virtual tool name shape.
    assert example.startswith("run_workflow_andre_workflows_job_description_review(")
    # Typed-arg shape — ``url="<...>"`` not ``inputs={"url": ...}``.
    assert 'url="<value from user request>"' in example
    assert "inputs=" not in example


# ---------------------------------------------------------------------------
# Test 2 — falls back to bug-027 shape when flag disabled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_example_call_falls_back_to_generic_when_virtuals_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #8 — ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0`` → generic shape only."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "0")
    workflow_inspect = _load_workflow_inspect()
    workflows = _FakeWorkflows(
        yaml_by_ref={"andre/workflows/job_description_review": _JD_YAML},
    )
    ctx = _mk_ctx(workflows)
    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
    })
    result = await workflow_inspect._tool_definition.func(args, ctx)

    example = result["example_call"]
    # bug-027 shape.
    assert example.startswith('run_workflow(ref="andre/workflows/')
    assert 'inputs={' in example
    assert '"url": "<value from user request>"' in example
    # Virtual tool name NOT used.
    assert "run_workflow_andre_workflows_" not in example
