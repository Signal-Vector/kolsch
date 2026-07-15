"""bug-028 — ``ToolsSubsystem.register_for_agent`` virtual-tool registration.

Covers ACs #5, #6, #7 for the tool-list-assembly path:

1. When the declared set includes ``run_workflow`` AND
   ``chat_services.workflows`` exposes the discovery seams, the
   subsystem registers one virtual tool per catalogued workflow
   alongside the generic tool.
2. When ``chat_services.workflows`` is missing, no virtuals are
   registered — the generic tool alone is registered.
3. Setting ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0`` disables the virtuals
   without touching the generic tool.
4. The generic ``run_workflow`` is ALWAYS registered when it was
   declared — the virtuals never replace it.

The declared-refs channel isn't exercised via a mock YAML file on
disk; instead we go through the ``tools=[...]`` programmatic
channel with the shipped ``run_workflow`` function so the test
doesn't need to model the ``discover_tools_in_file`` import step.
The subsystem's virtual-registration logic runs regardless of the
channel used to register ``run_workflow`` — the ONLY guard is
``registration.tools["run_workflow"]`` being present.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.service_registry import ChatServiceRegistry
from providers import DotenvSecretsProvider, NativeSandboxProvider
from providers.authz import RuleBasedAuthorizationProvider
from schemas import AgentId
from tools.subsystem import ToolsSubsystem

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[2]
_RUN_WORKFLOW_PATH = _REPO / "bundles" / "kolsch" / "tools" / "run_workflow.py"


def _load_run_workflow() -> Any:
    """Load the shipped ``run_workflow`` tool from the bundle."""
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.run_workflow_probe_028",
        _RUN_WORKFLOW_PATH,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.run_workflow


class _MockWorkflows:
    """Duck-typed workflows impl exposing the discovery seams."""

    def __init__(self, *, yaml_by_ref: dict[str, str]) -> None:
        self._yaml_by_ref = yaml_by_ref

    def list_workflow_refs(self) -> list[str]:
        return sorted(self._yaml_by_ref)

    def resolve_workflow_yaml(self, ref: str) -> str | None:
        return self._yaml_by_ref.get(ref)


_JD_YAML = """\
description: 'Given a URL, review it.'
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: 'curl {{ inputs.url }}'
graph:
  edges: []
"""

_HELLO_YAML = """\
description: 'Hello workflow'
nodes:
  greet:
    type: kolsch/nodes/noop
graph:
  edges:
    - from: greet
      to: greet
      binds:
        message: '{{ inputs.message | default("hi") }}'
"""


@pytest.fixture
def _subsystem(tmp_path: Path) -> ToolsSubsystem:
    """Build a ``ToolsSubsystem`` with real providers rooted at tmp."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    authz = RuleBasedAuthorizationProvider(
        config={"rules": []},
        catalog=None,
    )
    return ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        root=tmp_path,
    )


def _fresh_agent_id() -> AgentId:
    return AgentId(f"ag_{uuid4()}")


# ---------------------------------------------------------------------------
# Test 1 — AC #5: virtuals registered when declared + chat_services set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_for_agent_emits_virtual_tools_when_workflows_declared(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #5 — virtuals emerge alongside the generic ``run_workflow``."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    workflows = _MockWorkflows(
        yaml_by_ref={
            "andre/workflows/job_description_review": _JD_YAML,
            "kolsch/workflows/hello": _HELLO_YAML,
        },
    )
    _subsystem.set_chat_services(ChatServiceRegistry(workflows=workflows))
    agent_id = _fresh_agent_id()

    infos = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    names = {info.name for info in infos}

    # Generic still present.
    assert "run_workflow" in names
    # One virtual per catalogued workflow.
    assert "run_workflow_andre_workflows_job_description_review" in names
    assert "run_workflow_kolsch_workflows_hello" in names


# ---------------------------------------------------------------------------
# Test 2 — AC #5 negative: no virtuals when workflows impl missing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_virtuals_when_workflows_impl_missing(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``chat_services.workflows`` → generic only, no virtuals."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    # Registry with workflows=None (headless mode).
    _subsystem.set_chat_services(ChatServiceRegistry(workflows=None))
    agent_id = _fresh_agent_id()

    infos = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    names = {info.name for info in infos}

    assert "run_workflow" in names
    assert not any(n.startswith("run_workflow_") and n != "run_workflow" for n in names)


@pytest.mark.asyncio
async def test_no_virtuals_when_registry_absent(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``ChatServiceRegistry`` at all (headless caller) → no virtuals."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    # Never call set_chat_services — the sidecar stays None.
    agent_id = _fresh_agent_id()

    infos = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    names = {info.name for info in infos}

    assert "run_workflow" in names
    assert names == {"run_workflow"}


# ---------------------------------------------------------------------------
# Test 3 — AC #6: feature flag off disables virtuals.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_flag_disables_virtuals(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #6 — ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0`` skips virtual registration."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "0")
    workflows = _MockWorkflows(
        yaml_by_ref={
            "andre/workflows/job_description_review": _JD_YAML,
        },
    )
    _subsystem.set_chat_services(ChatServiceRegistry(workflows=workflows))
    agent_id = _fresh_agent_id()

    infos = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    names = {info.name for info in infos}
    assert "run_workflow" in names
    # No virtuals when flag disabled.
    assert "run_workflow_andre_workflows_job_description_review" not in names


# ---------------------------------------------------------------------------
# Test 4 — AC #7: generic ``run_workflow`` stays alongside virtuals.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_run_workflow_still_registered_alongside_virtuals(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #7 — the model sees BOTH the generic and each virtual."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    workflows = _MockWorkflows(
        yaml_by_ref={
            "andre/workflows/job_description_review": _JD_YAML,
        },
    )
    _subsystem.set_chat_services(ChatServiceRegistry(workflows=workflows))
    agent_id = _fresh_agent_id()

    infos = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    names = {info.name for info in infos}
    # Both present.
    assert "run_workflow" in names
    assert "run_workflow_andre_workflows_job_description_review" in names

    # Virtual carries the same home_bundle as the generic (authz
    # rules for `kolsch/tools/run_workflow` apply symmetrically —
    # the virtual's action string is
    # `kolsch/tools/run_workflow_<...>`, same bundle).
    listed = _subsystem.list_registered_tools(agent_id)
    generic_defn = next(i for i in listed if i.name == "run_workflow")
    virtual_defn = next(
        i
        for i in listed
        if i.name == "run_workflow_andre_workflows_job_description_review"
    )
    # Different args schemas — the generic has opaque `inputs: dict`,
    # the virtual has a typed `url` field.
    assert "inputs" in generic_defn.args_schema.get("properties", {})
    assert "url" in virtual_defn.args_schema.get("properties", {})
    assert "url" in virtual_defn.args_schema.get("required", [])


# ---------------------------------------------------------------------------
# Bonus — idempotence check: re-registering overwrites cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_for_agent_is_idempotent(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #5 — re-registration produces the same tool set."""
    monkeypatch.setenv("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    workflows = _MockWorkflows(
        yaml_by_ref={"kolsch/workflows/hello": _HELLO_YAML},
    )
    _subsystem.set_chat_services(ChatServiceRegistry(workflows=workflows))
    agent_id = _fresh_agent_id()

    first = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    second = await _subsystem.register_for_agent(
        agent_id=agent_id,
        tools=[_load_run_workflow()],
        home_bundle="kolsch",
    )
    assert {i.name for i in first} == {i.name for i in second}
