"""bug-024 Test 3 (AC #9) — the shipped helper's `run_workflow` reaches the LLM.

This is the Layer 1 + Layer 2 composition test. Drives the real
`ToolsSubsystem.register_for_agent` + `list_registered_tools` path
using the shipped starter helper.yaml's `declared:` list, then
asserts the tool schema that `MinimalFramework.run_exchange` would
emit (a) contains `run_workflow`, (b) carries its rich description
(not the `"Tool: <name>"` placeholder), (c) survives the
`_tool_to_openai_spec` normalisation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from engine._crewai_adapter import _tool_to_openai_spec
from engine.observers import _ObservedToolHandle
from files import PathPolicy
from providers import (
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
)
from schemas.identifiers import AgentId
from tools.subsystem import ToolsSubsystem


_STARTER_BUNDLE = (
    Path(__file__).resolve().parents[2] / "src" / "cli" / "_starter_bundle" / "kolsch"
)
_HELPER_YAML = _STARTER_BUNDLE / "agents" / "helper.yaml"
_TOOLS_DIR = _STARTER_BUNDLE / "tools"


async def _emit(*_: Any, **__: Any) -> None:  # pragma: no cover — unused
    return None


@pytest.fixture
def declared_paths() -> list[Path]:
    """Resolve every `declared:` ref in the shipped helper.yaml to a Path.

    Mirrors the engine's `_resolve_declared_tool_paths` logic in
    miniature — the ref shape is `kolsch/tools/<name>` and every
    file lives in `_starter_bundle/kolsch/tools/`.
    """
    yaml = YAML(typ="safe")
    payload = yaml.load(_HELPER_YAML.read_text(encoding="utf-8"))
    subsystems = payload.get("subsystems") or {}
    tools_cfg = (subsystems.get("tools") or {}).get("config") or {}
    declared = tools_cfg.get("declared") or []
    paths: list[Path] = []
    for entry in declared:
        ref = entry.get("ref")
        assert isinstance(ref, str) and ref.startswith("kolsch/tools/"), ref
        name = ref.split("/", 2)[2]
        paths.append(_TOOLS_DIR / f"{name}.py")
    return paths


@pytest.fixture
async def tools_subsystem(tmp_path: Path) -> ToolsSubsystem:
    """Real `ToolsSubsystem` bound to a scratch workspace."""
    (tmp_path / ".env").touch()
    (tmp_path / "data").mkdir(exist_ok=True)
    secrets = DotenvSecretsProvider(root=tmp_path, config={})
    sandbox = NativeSandboxProvider(config={})
    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
    )
    path_policy = PathPolicy(allowed_paths=["data/"], denied_paths=[".env"])
    return ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=tmp_path,
    )


async def test_shipped_helper_registers_run_workflow(
    tools_subsystem: ToolsSubsystem,
    declared_paths: list[Path],
) -> None:
    """`run_workflow` appears in `list_registered_tools` after registration."""
    agent_id = AgentId("ag_helper")
    owner_map = {p: "kolsch" for p in declared_paths}
    await tools_subsystem.register_for_agent(
        agent_id=agent_id,
        declared_paths=declared_paths,
        declared_owner_bundles=owner_map,
        home_bundle="kolsch",
    )
    registered = tools_subsystem.list_registered_tools(agent_id)
    names = {t.name for t in registered}
    assert "run_workflow" in names, (
        f"run_workflow missing from registered tools; got: {sorted(names)}. "
        "The starter helper.yaml's declared: list is out of sync with "
        "the shipped tool files."
    )
    # Sanity: the other two workflow tools are declared too (AC #2).
    assert "workflow_status" in names
    assert "cancel_workflow" in names


async def test_shipped_run_workflow_description_survives_pipeline(
    tools_subsystem: ToolsSubsystem,
    declared_paths: list[Path],
) -> None:
    """The rich `@tool(description=...)` copy reaches the LLM-facing schema.

    Traces the full pipeline: ToolDefinition.description →
    _ObservedToolHandle.description → MinimalFramework's
    `[{name, description, schema}]` list → `_tool_to_openai_spec`'s
    `function.description`. The regression this pins is that every
    tool used to ship as `"Tool: <name>"` — Kimi K2.6 then defaulted
    to bash.
    """
    agent_id = AgentId("ag_helper")
    owner_map = {p: "kolsch" for p in declared_paths}
    await tools_subsystem.register_for_agent(
        agent_id=agent_id,
        declared_paths=declared_paths,
        declared_owner_bundles=owner_map,
        home_bundle="kolsch",
    )
    registered = tools_subsystem.list_registered_tools(agent_id)

    # Locate the ToolDefinition for run_workflow via the same
    # accessor `_ObservedToolHandle` uses at engine.py:1265.
    defn = tools_subsystem._registry.get_tool(agent_id, "run_workflow")
    assert defn is not None
    handle = _ObservedToolHandle(
        emit=_emit,
        agent_id=str(agent_id),
        session_id="ses_test",
        thread_id="th_test",
        tools_subsystem=tools_subsystem,
        defn=defn,
    )

    # (a) The handle carries a real description.
    assert handle.description == defn.description
    assert handle.description != f"Tool: {handle.name}"
    assert "prefer this over" in handle.description.lower()

    # (b) The internal shape MinimalFramework builds includes it.
    engine_tool = {
        "name": handle.name,
        "description": handle.description,
        "schema": handle.schema,
    }

    # (c) The OpenAI-spec adapter surfaces the same description.
    openai_spec = _tool_to_openai_spec(engine_tool)
    assert openai_spec["type"] == "function"
    fn = openai_spec["function"]
    assert fn["name"] == "run_workflow"
    assert fn["description"] == handle.description
    assert fn["description"] != "Tool: run_workflow"
    # AC #8 check: the description explicitly disambiguates from bash.
    assert "bash" in fn["description"].lower()

    # And the same trace for the other two workflow tools (AC #8
    # says "same shape" — this test proves the pipeline works
    # identically for them).
    for other_name in ("workflow_status", "cancel_workflow"):
        other_defn = tools_subsystem._registry.get_tool(agent_id, other_name)
        assert other_defn is not None
        assert other_defn.description != f"Tool: {other_name}"
        assert "bash" in other_defn.description.lower()

    # Sanity check: the returned schema list also uses ToolInfo, not
    # `"Tool: <name>"`. This is the surface `list_registered_tools`
    # exposes to the engine at engine.py:1211-1214.
    for info in registered:
        if info.name == "run_workflow":
            assert info.description == handle.description
            break
    else:  # pragma: no cover — sanity fallthrough
        raise AssertionError("run_workflow ToolInfo missing after register")
