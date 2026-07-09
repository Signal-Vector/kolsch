"""bug-025 Test 4 — ``run_workflow`` validates inputs before enqueue.

Covers AC #8, #9. Two behavior-level tests:

1. Missing required input raises a diagnostic ``WrappedError``
   (Diagnostic level → engine observer emits ``TOOL_FAILED`` →
   framework wraps as ``is_error=true``).
2. Fully-supplied inputs proceed to ``services.workflows.start``
   normally.

Exercises the tool function directly against a fake ``Workflows``
service that exposes ``resolve_workflow_yaml`` (the duck-typed
seam). No ChatApp / Pilot needed — the validation lives at the
tool boundary, not the wire boundary.
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
# Load the tool function from the bundle (matches other tests).
# ----------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO / "bundles" / "kolsch" / "tools" / "run_workflow.py"


def _load_run_workflow() -> Any:
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.run_workflow_probe",
        _TOOL_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.run_workflow


# ----------------------------------------------------------------------
# Test doubles for the workflows + cards services.
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
# AC #8 / #9 — validation surfaces missing input.
# ----------------------------------------------------------------------


_JD_YAML = """
description: "job description review — requires inputs.url"
nodes:
  fetch_jd:
    type: kolsch/nodes/bash
    command: "python3 -c 'import requests; r = requests.get(\\"{{ inputs.url }}\\")'"
graph:
  edges: []
"""


@pytest.mark.asyncio
async def test_missing_required_input_returns_is_error_true() -> None:
    """No ``inputs.url`` supplied → the tool raises a diagnostic error.

    The engine observer at ``src/engine/observers.py:284`` catches
    ``PlatformError`` and emits ``TOOL_FAILED`` with the error
    message in the payload — the LLM sees ``is_error=true``.
    """
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=_JD_YAML)
    ctx = _mk_ctx(cards, workflows)

    # `@tool`-decorated function has an args_schema; call the wrapped
    # func with a constructed args object. Use `_tool_definition` if
    # exposed; otherwise call `run_workflow` with dict-shaped args
    # via the model_validate path.
    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
        "inputs": {},
    })
    with pytest.raises(PlatformError) as exc_info:
        # The @tool decorator wraps the coroutine; call the wrapped
        # tool function directly on args + ctx.
        await run_workflow._tool_definition.func(args, ctx)
    err = exc_info.value
    assert "url" in str(err), f"expected 'url' in error, got {err!s}"
    assert "requires input" in str(err)
    # Fire-and-forget MUST NOT have happened.
    assert workflows.starts == []
    # And the card MUST NOT have been opened either — validation runs
    # BEFORE `cards.open`.
    assert cards.opened == []


@pytest.mark.asyncio
async def test_valid_inputs_proceed_normally() -> None:
    """Supplied ``inputs.url`` → the run enqueues normally."""
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=_JD_YAML)
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
        "inputs": {"url": "https://example.com"},
    })
    result = await run_workflow._tool_definition.func(args, ctx)
    assert result["run_id"] == "wrk_probe"
    assert workflows.starts == [
        ("andre/workflows/job_description_review", {"url": "https://example.com"}),
    ]
    assert len(cards.opened) == 1


@pytest.mark.asyncio
async def test_defaulted_input_is_optional() -> None:
    """`{{ inputs.x | default(...) }}` doesn't count as required."""
    yaml_src = """
nodes:
  greet:
    type: kolsch/nodes/echo
graph:
  edges:
    - from: ready
      to: greet
      binds:
        message: "{{ inputs.message | default('hi') }}"
"""
    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _FakeWorkflows(yaml_source=yaml_src)
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "kolsch/workflows/hello",
        "inputs": {},
    })
    # Should NOT raise — defaulted input is optional.
    result = await run_workflow._tool_definition.func(args, ctx)
    assert result["run_id"] == "wrk_probe"
    assert workflows.starts == [("kolsch/workflows/hello", {})]


@pytest.mark.asyncio
async def test_resolver_absent_falls_back_to_pre_bug025_shape() -> None:
    """Fake workflows without ``resolve_workflow_yaml`` skip validation.

    Backward-compat: headless test fakes that predate bug-025 keep
    working — the validator's duck-typed probe returns ``None`` and
    the tool proceeds to enqueue as before.
    """
    class _MinimalWorkflows:
        def __init__(self) -> None:
            self.starts: list[tuple[str, dict[str, Any]]] = []

        async def start(
            self, ref: str, inputs: dict[str, Any], *, card: Any = None,
        ) -> str:
            _ = card
            self.starts.append((ref, inputs))
            return "wrk_headless"

    run_workflow = _load_run_workflow()
    cards = _FakeCards()
    workflows = _MinimalWorkflows()
    ctx = _mk_ctx(cards, workflows)

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
        "inputs": {},
    })
    # Should NOT raise — resolver is absent, so no validation.
    result = await run_workflow._tool_definition.func(args, ctx)
    assert result["run_id"] == "wrk_headless"
