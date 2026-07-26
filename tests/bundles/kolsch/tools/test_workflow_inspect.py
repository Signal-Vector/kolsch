"""bug-027 Test — ``workflow_inspect`` smoke tests (AC #3, #7, #8).

Covers:

- Valid ref returns the ``{ref, description, required_inputs,
  optional_inputs, example_call}`` shape (AC #7).
- Required vs optional distinction is correct: a
  ``{{ inputs.a }}`` reference is required; a
  ``{{ inputs.b | default(...) }}`` reference is optional (AC #8).
- Unknown ref raises ``WrappedError`` with the available refs
  surfaced when the impl exposes the parallel
  ``list_workflow_refs`` seam.
- ``example_call`` is a valid ``run_workflow(...)`` call template
  the model can copy — smoke check that the string parses as
  ``run_workflow(ref="...", inputs={...})``.

Exercises the tool function directly against a mock ``Workflows``
service that exposes ``resolve_workflow_yaml`` + optional
``list_workflow_refs`` — no ChatApp / Pilot needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from errors import PlatformError
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext

# ----------------------------------------------------------------------
# Load the tool function from the bundle.
# ----------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO / "bundles" / "kolsch" / "tools" / "workflow_inspect.py"


def _load_workflow_inspect() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.workflow_inspect_probe",
        _TOOL_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.workflow_inspect


# ----------------------------------------------------------------------
# Test doubles for the workflows service.
# ----------------------------------------------------------------------


class _MockWorkflows:
    """Duck-typed impl of ``Workflows`` for the introspection tool.

    Only exposes what the tool actually reads: ``resolve_workflow_yaml``
    (bug-025 seam) + ``list_workflow_refs`` (bug-027 seam). The
    Protocol's other methods (``start``, ``status``, ``cancel``,
    ``aclose``) are irrelevant to ``workflow_inspect``.
    """

    def __init__(
        self,
        *,
        yaml_by_ref: dict[str, str],
        list_refs: list[str] | None = None,
    ) -> None:
        self._yaml_by_ref = yaml_by_ref
        self._list_refs = list_refs

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)

    def list_workflow_refs(self) -> list[str]:
        if self._list_refs is None:
            return sorted(self._yaml_by_ref)
        return list(self._list_refs)


class _MinimalWorkflows:
    """Duck-typed impl without ``list_workflow_refs`` — legacy fixtures."""

    def __init__(self, *, yaml_by_ref: dict[str, str]) -> None:
        self._yaml_by_ref = yaml_by_ref

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)


def _mk_ctx(workflows: Any) -> ToolContext:
    return ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        workflows=workflows,
    )


# ----------------------------------------------------------------------
# AC #7 — valid ref returns the correct shape.
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
async def test_valid_ref_returns_expected_shape() -> None:
    """Returns the full 5-key dict and correctly classifies inputs."""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(
        yaml_by_ref={"andre/workflows/job_description_review": _JD_YAML},
    )
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate(
        {
            "ref": "andre/workflows/job_description_review",
        }
    )
    result = await workflow_inspect._tool_definition.func(args, ctx)

    assert isinstance(result, dict)
    assert set(result.keys()) == {
        "ref",
        "description",
        "required_inputs",
        "optional_inputs",
        "required_inputs_including_children",
        "example_call",
    }
    assert result["ref"] == "andre/workflows/job_description_review"
    assert result["description"] == ("job description review — requires inputs.url")
    assert result["required_inputs"] == ["url"]
    assert result["optional_inputs"] == []


# ----------------------------------------------------------------------
# AC #8 — required vs optional distinction.
# ----------------------------------------------------------------------


_MIXED_YAML = """
description: "mixed required + optional inputs"
nodes:
  ready:
    type: kolsch/nodes/noop
  greet:
    type: kolsch/nodes/echo
graph:
  edges:
    - from: ready
      to: greet
      binds:
        # `a` is unguarded — required. `b` has a default guard —
        # optional. `c` also has a default — optional.
        required_field: "{{ inputs.a }}"
        message: "{{ inputs.b | default('hi') }}"
        greeting: "{{ inputs.c | default('hello') }}"
"""


@pytest.mark.asyncio
async def test_required_and_optional_correctly_partitioned() -> None:
    """`{{ inputs.a }}` → required; `{{ inputs.b | default(...) }}` → optional."""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(yaml_by_ref={"andre/workflows/mixed": _MIXED_YAML})
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "andre/workflows/mixed"})
    result = await workflow_inspect._tool_definition.func(args, ctx)

    assert result["required_inputs"] == ["a"]
    assert result["optional_inputs"] == ["b", "c"]


# ----------------------------------------------------------------------
# `example_call` shape check.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_example_call_is_valid_run_workflow_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`example_call` matches `run_workflow(ref="...", inputs={...})`.

    bug-028: this test asserts the LEGACY bug-027 example_call
    shape. bug-028 flipped the default to prefer the virtual-tool
    shape (``run_workflow_<bundle>_<workflow>(url="<value>")``); the
    original assertion still holds when the operator sets
    ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0``. See
    ``test_workflow_inspect_example_call.py`` for the new-default
    coverage of the virtual-name shape.
    """
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "0")
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(
        yaml_by_ref={"andre/workflows/job_description_review": _JD_YAML},
    )
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate(
        {
            "ref": "andre/workflows/job_description_review",
        }
    )
    result = await workflow_inspect._tool_definition.func(args, ctx)

    example = result["example_call"]
    assert isinstance(example, str)
    # Shape: run_workflow(ref="andre/workflows/job_description_review",
    # inputs={"url": "<value from user request>"}).
    assert example.startswith("run_workflow(")
    assert 'ref="andre/workflows/job_description_review"' in example
    assert "inputs={" in example
    assert '"url"' in example


@pytest.mark.asyncio
async def test_example_call_empty_inputs_when_no_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow with only optional inputs → example uses ``inputs={}``.

    bug-028: pinned to ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0`` so this
    test continues asserting the legacy bug-027 shape. See
    ``test_workflow_inspect_example_call.py`` for the new default.
    """
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "0")
    yaml_src = """
description: "optional-only inputs"
nodes:
  greet:
    type: kolsch/nodes/noop
graph:
  edges:
    - from: greet
      to: greet
      binds:
        message: "{{ inputs.message | default('hi') }}"
"""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(yaml_by_ref={"kolsch/workflows/hello": yaml_src})
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "kolsch/workflows/hello"})
    result = await workflow_inspect._tool_definition.func(args, ctx)

    assert result["required_inputs"] == []
    assert result["optional_inputs"] == ["message"]
    assert result["example_call"] == (
        'run_workflow(ref="kolsch/workflows/hello", inputs={})'
    )


# ----------------------------------------------------------------------
# Unknown ref — raises WrappedError with helpful message.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_ref_raises_with_available_refs() -> None:
    """Unknown ref → ``WrappedError`` naming the available refs."""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(
        yaml_by_ref={"kolsch/workflows/hello": "nodes: {}\n"},
        list_refs=["kolsch/workflows/hello", "andre/workflows/foo"],
    )
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "kolsch/workflows/does_not_exist"})

    with pytest.raises(PlatformError) as exc_info:
        await workflow_inspect._tool_definition.func(args, ctx)
    err = exc_info.value
    assert "kolsch/workflows/does_not_exist" in str(err)
    assert "not found" in str(err)
    assert "kolsch/workflows/hello" in str(err)
    assert "andre/workflows/foo" in str(err)


@pytest.mark.asyncio
async def test_unknown_ref_without_lister_seam_still_raises() -> None:
    """Impl without ``list_workflow_refs`` still raises a legible error."""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MinimalWorkflows(yaml_by_ref={})
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "kolsch/workflows/nope"})

    with pytest.raises(PlatformError) as exc_info:
        await workflow_inspect._tool_definition.func(args, ctx)
    err = exc_info.value
    assert "kolsch/workflows/nope" in str(err)
    assert "not found" in str(err)


# ----------------------------------------------------------------------
# qol-097 — `required_inputs_including_children` (promoted set).
# ----------------------------------------------------------------------


class _MockWorkflowsWithPromoted:
    """Duck-typed impl exposing qol-097's ``resolve_effective_required_inputs``.

    The parent's yaml has NO direct ``{{ inputs.X }}`` refs, so
    ``required_inputs`` (direct scan) is empty; the promoted seam
    returns ``["url"]`` (sub-workflow child requirement) so
    ``required_inputs_including_children`` reports it.
    """

    def __init__(
        self,
        *,
        yaml_by_ref: dict[str, str],
        promoted_by_ref: dict[str, list[str]],
    ) -> None:
        self._yaml_by_ref = yaml_by_ref
        self._promoted_by_ref = promoted_by_ref

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)

    def resolve_effective_required_inputs(self, ref: str) -> list[str] | None:
        return self._promoted_by_ref.get(ref)


_PARENT_ONLY_SUB_YAML = """
description: "parent that only calls a sub-workflow (child needs url)"
nodes:
  sub:
    type: kolsch/nodes/workflow
    ref: demo/workflows/child
graph:
  edges: []
"""


@pytest.mark.asyncio
async def test_inspect_reports_required_including_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qol-097: parent with no direct inputs but sub-workflow child needing ``url``.

    ``required_inputs`` (direct scan) is empty; the promoted seam
    returns ``["url"]`` so
    ``required_inputs_including_children == ["url"]`` and
    ``example_call`` is rebuilt from the promoted set (contains
    ``url``, matches bug-027's template shape).
    """
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "0")
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflowsWithPromoted(
        yaml_by_ref={"demo/workflows/parent": _PARENT_ONLY_SUB_YAML},
        promoted_by_ref={"demo/workflows/parent": ["url"]},
    )
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "demo/workflows/parent"})
    result = await workflow_inspect._tool_definition.func(args, ctx)

    assert result["required_inputs"] == []  # backwards-compat: direct scan only
    assert result["required_inputs_including_children"] == ["url"]
    example = result["example_call"]
    assert isinstance(example, str)
    # bug-027 template shape rebuilt from the promoted set.
    assert example.startswith("run_workflow(")
    assert 'ref="demo/workflows/parent"' in example
    assert "inputs={" in example
    assert '"url": "<value from user request>"' in example


@pytest.mark.asyncio
async def test_missing_description_yields_empty_string() -> None:
    """No ``description:`` field → empty-string description in the result."""
    yaml_src = """
nodes:
  greet:
    type: kolsch/nodes/noop
graph:
  edges: []
"""
    workflow_inspect = _load_workflow_inspect()
    workflows = _MockWorkflows(yaml_by_ref={"kolsch/workflows/nodesc": yaml_src})
    ctx = _mk_ctx(workflows)

    args_schema = workflow_inspect._tool_definition.args_schema
    args = args_schema.model_validate({"ref": "kolsch/workflows/nodesc"})
    result = await workflow_inspect._tool_definition.func(args, ctx)

    assert result["description"] == ""
