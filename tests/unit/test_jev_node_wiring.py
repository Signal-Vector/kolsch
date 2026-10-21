"""spec-136 M3 Test 5 (unit) — ``kolsch/nodes/jev`` registration wiring.

Guards every link of the built-in chain (catalog name → parser class →
union → phase map → runner → ``register_builtins`` → the three CLI call
sites) plus the YAML 1.2 boolean/str behaviour multi-way branching
depends on.
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from kolsch import _yamlio
from workflows.builtins import JevRunner
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.errors import WorkflowValidationError
from workflows.executor import BUILTIN_PHASE_MAP
from workflows.parser import (
    _BUILTIN_NODE_CLASSES,
    EdgeDef,
    JevNodeDef,
    NodeDefUnion,
    Workflow,
)

_CHOICE = {
    "team": {
        "type": "choice",
        "instructions": "Route it",
        "criteria": {"billing": None, "outage": None, "feature": None},
    }
}


def test_jev_is_a_reserved_builtin_slot() -> None:
    assert "jev" in BUILTIN_TYPE_NAMES
    assert len(BUILTIN_TYPE_NAMES) == 16


def test_jev_parser_wiring_registered_in_both_places() -> None:
    assert _BUILTIN_NODE_CLASSES["jev"] is JevNodeDef
    assert JevNodeDef in typing.get_args(NodeDefUnion)
    assert "jev" in BUILTIN_PHASE_MAP


def test_jev_node_parses_into_jev_node_def_with_defaults() -> None:
    wf = Workflow.model_validate(
        {
            "nodes": {
                "route": {
                    "type": "kolsch/nodes/jev",
                    "state": "{{ inputs.ticket_text }}",
                    "questions": _CHOICE,
                    "route_on": "team",
                }
            },
            "graph": {"edges": []},
        }
    )
    node = wf.nodes["route"]
    assert isinstance(node, JevNodeDef)
    assert node.model == "typesafe/jev-1.14"
    assert node.noul_threshold == 0.5
    assert node.route_on == "team"


@pytest.mark.parametrize(
    "questions",
    [
        {"q": {"type": "box", "instructions": "x"}},
        {},
        {"q": {"type": "choice", "instructions": "x", "criteria": {}}},
        {"q": {"type": "noul", "instructions": "x", "fail_below": 3}},
    ],
    ids=["box", "empty", "no-options", "fail_below-range"],
)
def test_jev_node_questions_validated_by_the_tool_models(
    questions: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        JevNodeDef.model_validate(
            {"type": "kolsch/nodes/jev", "state": "x", "questions": questions}
        )


def test_jev_node_noul_threshold_range() -> None:
    with pytest.raises(ValidationError):
        JevNodeDef.model_validate(
            {
                "type": "kolsch/nodes/jev",
                "state": "x",
                "questions": {"q": {"type": "noul", "instructions": "x"}},
                "noul_threshold": 1.5,
            }
        )


def test_unquoted_boolean_option_key_rejected_with_rename_hint(tmp_path: Path) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text(
        "nodes:\n"
        "  route:\n"
        "    type: kolsch/nodes/jev\n"
        "    state: x\n"
        "    questions:\n"
        "      q:\n"
        "        type: choice\n"
        "        instructions: pick\n"
        "        criteria:\n"
        "          true: yes it is\n"
        "          other: no\n",
        encoding="utf-8",
    )
    from workflows.parser import load_workflow

    with pytest.raises(WorkflowValidationError) as exc_info:
        load_workflow(path)
    message = str(exc_info.value)
    assert str(path) in message  # the real file, not a placeholder
    assert "loaded as a boolean" in message
    assert "rename" in message


def test_yaml_12_branch_values_round_trip() -> None:
    """``branch: yes`` stays the str ``"yes"``; ``branch: true`` is a bool."""
    data = _yamlio.load(
        "edges:\n"
        "  - {from: a, to: b, branch: yes}\n"
        "  - {from: a, to: c, branch: true}\n"
        "  - {from: a, to: d, branch: billing}\n"
        "  - {from: a, to: e, branch: 'false'}\n"
    )
    edges = [EdgeDef.model_validate(e) for e in data["edges"]]
    assert [e.branch for e in edges] == ["yes", True, "billing", "false"]
    assert isinstance(edges[1].branch, bool)
    assert isinstance(edges[0].branch, str)


def test_jev_runner_registered_by_register_builtins() -> None:
    """With ``jev_decide_defn`` the slot resolves to a ``JevRunner``;
    without it the slot keeps raising ``NodeTypeNotImplemented``."""
    from files import PathPolicy
    from providers.authz import RuleBasedAuthorizationProvider
    from providers.sandbox import NativeSandboxProvider
    from tools.builtins import make_jev_decide
    from tools.decorator import get_tool_definition
    from workflows.builtins import register_builtins
    from workflows.errors import NodeTypeNotImplemented
    from workflows.executor import RunnerRegistry

    assert "jev_decide_defn" in inspect.signature(register_builtins).parameters
    defn = get_tool_definition(make_jev_decide(policy=PathPolicy(), root=Path()))
    common: dict[str, typing.Any] = {
        "bash_defn": defn,
        "authz": RuleBasedAuthorizationProvider(config={"rules": []}),
        "secrets": typing.cast("typing.Any", object()),
        "sandbox": NativeSandboxProvider(config={}),
    }

    wired = RunnerRegistry()
    register_builtins(wired, jev_decide_defn=defn, **common)
    runner = wired.get("kolsch/nodes/jev", node_name="route")
    assert isinstance(runner, JevRunner)
    assert runner._defn is defn

    bare = RunnerRegistry()
    register_builtins(bare, **common)
    with pytest.raises(NodeTypeNotImplemented):
        bare.get("kolsch/nodes/jev", node_name="route")


def test_cli_paths_wire_the_jev_runner_through_the_shared_builder() -> None:
    """``build_workflow_executor`` builds and passes ``jev_decide_defn``;
    since bug-141 run, resume and fork all go through it, so
    ``cli.workflows`` builds no tool definitions of its own (the
    behavioural check is
    ``test_builtin_tool_defns_stamped_on_all_paths``)."""
    src = Path(__file__).resolve().parents[2] / "src" / "cli"
    runtime_src = (src / "_workflow_runtime.py").read_text(encoding="utf-8")
    workflows_src = (src / "workflows.py").read_text(encoding="utf-8")
    assert runtime_src.count("jev_decide_defn=jev_decide_defn") == 1
    assert "make_jev_decide(" in runtime_src
    assert "make_jev_decide(" not in workflows_src
    assert "register_builtins(" not in workflows_src
    assert workflows_src.count("build_workflow_executor(") == 3
