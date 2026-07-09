"""bug-027 Test — ``run_workflow`` error message shape (AC #1).

Session 4778 caught the model retrying ``run_workflow(inputs={})``
11 times because bug-025's error text named the missing key but
showed no JSON shape. bug-027 rewrote the message to include a
copy-pasteable retry template. This test pins the four substrings
AC #1 requires and prevents a future edit from making the error
message less actionable.

Asserted substrings:

1. Workflow ref (``andre/workflows/job_description_review``).
2. Missing input names (``'url'``).
3. Literal JSON retry template (``inputs={"url":`` — the marker
   that lets the model see the shape).
4. Supplied inputs (``inputs=[]`` when the caller sent ``{}``).

Also asserts the multi-key case: two missing inputs produce a
template with both keys, and each key appears in the ``required
input(s)`` list.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from uuid import uuid4

from errors import PlatformError
from tools.decorator import ToolContext
from schemas.identifiers import AgentId, SessionId, ThreadId


# ----------------------------------------------------------------------
# Load the tool function from the bundle.
# ----------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO / "bundles" / "kolsch" / "tools" / "run_workflow.py"


def _load_run_workflow() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.run_workflow_probe_027",
        _TOOL_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.run_workflow


# ----------------------------------------------------------------------
# Fakes.
# ----------------------------------------------------------------------


class _FakeCard:
    def __init__(self) -> None:
        self.id = "card_probe"
        self._owner_transferred = False
        self.is_closed = False


class _FakeCards:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def open(self, label: str, *, kind: str) -> Any:
        self.opened.append((label, kind))
        return _FakeCard()


class _FakeWorkflows:
    def __init__(self, *, yaml_source: str) -> None:
        self._yaml_source = yaml_source
        self.starts: list[tuple[str, dict[str, Any]]] = []

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        _ = ref
        return self._yaml_source

    async def start(
        self, ref: str, inputs: dict[str, Any], *, card: Any = None,
    ) -> str:
        _ = card
        self.starts.append((ref, inputs))
        return "wrk_probe"


def _mk_ctx(cards: Any, workflows: Any) -> ToolContext:
    return ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        cards=cards,
        workflows=workflows,
    )


# ----------------------------------------------------------------------
# AC #1 — 4-substring test.
# ----------------------------------------------------------------------


_JD_YAML = """
description: "job description review — requires inputs.url"
nodes:
  fetch_jd:
    type: kolsch/nodes/bash
    command: "curl {{ inputs.url }}"
graph:
  edges: []
"""


@pytest.mark.asyncio
async def test_error_message_contains_four_required_substrings() -> None:
    """AC #1 — ref, missing names, retry template, supplied inputs.

    Session 4778's model read the pre-bug-027 error 11 times without
    figuring out where to put ``url``. The 4-substring test pins
    the new shape so a future edit can't accidentally regress the
    error message back to text-only.
    """
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=_JD_YAML)
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
        "inputs": {},
    })
    with pytest.raises(PlatformError) as exc_info:
        await run_workflow._tool_definition.func(args, ctx)
    msg = str(exc_info.value)

    # (a) The workflow ref appears somewhere in the message.
    assert "andre/workflows/job_description_review" in msg, (
        f"error message missing workflow ref: {msg!r}"
    )
    # (b) The missing input name ``'url'`` appears.
    assert "'url'" in msg, (
        f"error message missing 'url' input name: {msg!r}"
    )
    # (c) The literal JSON retry template ``inputs={"url":`` — the
    # marker that gives the model a shape to copy.
    assert 'inputs={"url":' in msg, (
        f"error message missing JSON retry template: {msg!r}"
    )
    # (d) The supplied inputs are surfaced (empty list from
    # ``inputs={}``).
    assert "You sent: inputs=" in msg, (
        f"error message missing 'You sent: inputs=' surface: {msg!r}"
    )
    # No side effects — validation runs before enqueue.
    assert workflows.starts == []
    assert cards.opened == []


@pytest.mark.asyncio
async def test_error_message_lists_all_missing_keys_in_template() -> None:
    """Two missing inputs → both keys appear in the retry template."""
    yaml_src = """
nodes:
  a:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.first }} and {{ inputs.second }}"
graph:
  edges: []
"""
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=yaml_src)
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/two_inputs",
        "inputs": {},
    })
    with pytest.raises(PlatformError) as exc_info:
        await run_workflow._tool_definition.func(args, ctx)
    msg = str(exc_info.value)

    assert "'first'" in msg
    assert "'second'" in msg
    # Both keys land in the JSON template — the model can copy the
    # dict shape with both slots present.
    assert '"first": "<value from user request>"' in msg
    assert '"second": "<value from user request>"' in msg


@pytest.mark.asyncio
async def test_error_message_surfaces_supplied_inputs_when_partial() -> None:
    """One missing input, one supplied → supplied key surfaces in ``You sent:``."""
    yaml_src = """
nodes:
  a:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.url }} {{ inputs.title }}"
graph:
  edges: []
"""
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=yaml_src)
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/partial",
        "inputs": {"title": "Engineer"},
    })
    with pytest.raises(PlatformError) as exc_info:
        await run_workflow._tool_definition.func(args, ctx)
    msg = str(exc_info.value)

    # Only 'url' is missing — 'title' is present.
    assert "'url'" in msg
    assert '"url": "<value from user request>"' in msg
    # The supplied inputs surface names the key the model already
    # sent so the retry doesn't drop it.
    assert "'title'" in msg


# ----------------------------------------------------------------------
# AC #2 — @tool description primes the protocol.
# ----------------------------------------------------------------------


def test_tool_description_mentions_protocol_and_example() -> None:
    """AC #2 — description primes model to read YAML + gives example call."""
    run_workflow = _load_run_workflow()
    description = run_workflow._tool_definition.description
    assert isinstance(description, str)

    # (a) Directs the model to read the workflow YAML for input names.
    lowered = description.lower()
    assert "workflow yaml" in lowered, (
        f"description doesn't mention workflow YAML: {description!r}"
    )
    # (b) Names the {{ inputs.X }} reference discipline.
    assert "{{ inputs." in description or "inputs.x" in lowered, (
        f"description doesn't mention {{{{ inputs.X }}}} references: "
        f"{description!r}"
    )
    # (c) A concrete run_workflow(ref=..., inputs={...}) example.
    assert "run_workflow(ref=" in description, (
        f"description missing concrete example call: {description!r}"
    )
    assert 'inputs={"url":' in description or "inputs={\"url\":" in description, (
        f"description missing example inputs shape: {description!r}"
    )
