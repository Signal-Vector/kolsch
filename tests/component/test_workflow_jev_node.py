"""spec-136 M3 — ``kolsch/nodes/jev`` runner + multi-way branching (component).

Tests 5 and 6. Every workflow runs through the real
``IterativeExecutor`` with the real ``JevRunner``; the ``jev_decide``
tool is the module the runtime itself loads
(``tools.builtins.load_jev_decide_module``), pointed at a local fake
Jev server. No network, no real key.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from files import PathPolicy
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tests.component.test_jev_decide import _FakeJevServer, _ok
from tools.builtins import load_jev_decide_module, make_jev_decide
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore
from workflows.validator import validate_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.slow

_KEY = "sk-or-m3-test-dummy"


@pytest.fixture
def fake_jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeJevServer]:
    server = _FakeJevServer()
    server.start()
    module = load_jev_decide_module()
    monkeypatch.setitem(
        module._PROFILES,
        "openrouter",
        dataclasses.replace(module._PROFILES["openrouter"], base_url=server.base_url),
    )
    try:
        yield server
    finally:
        server.stop()


def _authz(effect: str = "allow") -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": effect},
                {"what": "*", "pattern": "*", "effect": "allow"},
            ]
        }
    )


def _registry(
    tmp_path: Path, *, env: str = f"OPENROUTER_API_KEY={_KEY}\n", effect: str = "allow"
) -> RunnerRegistry:
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    defn = get_tool_definition(make_jev_decide(policy=PathPolicy(), root=tmp_path))
    defn.home_bundle = "kolsch"
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=defn,  # required kwarg; bash is never dispatched here
        authz=_authz(effect),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        jev_decide_defn=defn,
    )
    return registry


async def _run(
    tmp_path: Path,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
    *,
    validate: bool = True,
) -> tuple[WorkflowRunStatus, list[dict[str, Any]]]:
    # `validate=False` mimics resume / fork, which reload YAML without
    # running the validator.
    if validate:
        result = validate_workflow(workflow, AssetCatalog(), file="wf.yaml")
        assert result.is_valid, [str(e) for e in result.errors]
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/jev", inputs=inputs or {})
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    status = await executor.run()
    return status, [dict(e) for e in store.read_events(run.run_id)]


def _kind(event: dict[str, Any]) -> str:
    return str(event.get("kind"))


def _by_kind(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if _kind(e) == kind]


def _node(events: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (event,) = [e for e in _by_kind(events, kind) if e.get("node_name") == name]
    return event


_TRIAGE_QUESTION = {
    "team": {
        "type": "choice",
        "instructions": "Which team should handle this ticket?",
        "criteria": {"billing": None, "outage": None, "feature": None},
    }
}


def _triage_workflow(**route_extra: Any) -> Workflow:
    route: dict[str, Any] = {
        "type": "kolsch/nodes/jev",
        "state": "{{ inputs.ticket_text }}",
        "questions": _TRIAGE_QUESTION,
        "route_on": "team",
        **route_extra,
    }
    return Workflow.model_validate(
        {
            "nodes": {
                "route": route,
                "billing_handler": {"type": "kolsch/nodes/compute", "value": "B"},
                "outage_handler": {"type": "kolsch/nodes/compute", "value": "O"},
                "feature_handler": {"type": "kolsch/nodes/compute", "value": "F"},
            },
            "graph": {
                "edges": [
                    {"from": "route", "to": "billing_handler", "branch": "billing"},
                    {"from": "route", "to": "outage_handler", "branch": "outage"},
                    {"from": "route", "to": "feature_handler", "branch": "feature"},
                ]
            },
        }
    )


def _choice_reply(choice: str, confidence: float = 0.97) -> Any:
    others = [o for o in ("billing", "outage", "feature") if o != choice]
    probabilities = {choice: confidence, **dict.fromkeys(others, (1 - confidence) / 2)}
    return _ok(
        {
            "team": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": probabilities,
            }
        }
    )


# ---------------------------------------------------------------------------
# Test 5 — runner capture, dispatch path, failure outcome
# ---------------------------------------------------------------------------


async def test_jev_node_dispatches_via_dispatch_tool(
    tmp_path: Path, fake_jev: _FakeJevServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.dispatch as dispatch_mod

    calls: list[str] = []
    real = dispatch_mod.dispatch_tool

    async def _spy(**kwargs: Any) -> Any:
        calls.append(kwargs["defn"].name)
        return await real(**kwargs)

    monkeypatch.setattr(dispatch_mod, "dispatch_tool", _spy)
    fake_jev.replies = [_choice_reply("outage")]
    status, _ = await _run(
        tmp_path,
        _triage_workflow(),
        _registry(tmp_path),
        {"ticket_text": "the site is returning 502s"},
    )
    assert status == WorkflowRunStatus.SUCCEEDED
    assert calls == ["jev_decide"]
    sent = json.loads(fake_jev.seen[0].body)
    assert sent["state"] == "the site is returning 502s"  # Jinja-rendered
    assert sent["questions"] == _TRIAGE_QUESTION
    assert sent["model"] == "typesafe/jev-1.14"
    assert fake_jev.seen[0].headers["authorization"] == f"Bearer {_KEY}"


async def test_jev_node_choice_capture_and_three_way_route(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    """Test 6: only the chosen arm fires; the other two are pruned with a
    str ``branch_value``."""
    fake_jev.replies = [_choice_reply("outage")]
    status, events = await _run(
        tmp_path,
        _triage_workflow(),
        _registry(tmp_path),
        {"ticket_text": "the site is returning 502s"},
    )
    assert status == WorkflowRunStatus.SUCCEEDED
    completed = _node(events, "workflow.node.completed", "route")
    output = completed["payload"]["output"]
    assert output["value"] == "outage"
    assert set(output) == {"answers", "model", "usage", "value"}
    assert output["answers"]["team"]["choice"] == "outage"
    assert completed["payload"]["branch_value"] == "outage"

    _node(events, "workflow.node.completed", "outage_handler")
    pruned = _by_kind(events, "workflow.node.pruned")
    assert sorted(e["node_name"] for e in pruned) == [
        "billing_handler",
        "feature_handler",
    ]
    for event in pruned:
        assert event["payload"]["branch_value"] == "outage"
        assert isinstance(event["payload"]["branch_value"], str)
        assert event["payload"]["upstream_source"] == "route"
        assert event["payload"]["reason"] == "branch_not_taken"


@pytest.mark.parametrize(
    ("noul", "threshold", "fired", "pruned"),
    [(0.8, 0.9, "no_arm", "yes_arm"), (0.8, 0.5, "yes_arm", "no_arm")],
)
async def test_jev_node_noul_route_uses_custom_threshold(
    tmp_path: Path,
    fake_jev: _FakeJevServer,
    noul: float,
    threshold: float,
    fired: str,
    pruned: str,
) -> None:
    fake_jev.replies = [_ok({"billing": {"type": "noul", "noul": noul}})]
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "route": {
                    "type": "kolsch/nodes/jev",
                    "state": "I was charged twice",
                    "questions": {
                        "billing": {"type": "noul", "instructions": "Billing?"}
                    },
                    "route_on": "billing",
                    "noul_threshold": threshold,
                },
                "yes_arm": {"type": "kolsch/nodes/noop"},
                "no_arm": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "route", "to": "yes_arm", "branch": True},
                    {"from": "route", "to": "no_arm", "branch": False},
                ]
            },
        }
    )
    status, events = await _run(tmp_path, workflow, _registry(tmp_path))
    assert status == WorkflowRunStatus.SUCCEEDED
    completed = _node(events, "workflow.node.completed", "route")
    assert completed["payload"]["output"]["value"] is (fired == "yes_arm")
    assert completed["payload"]["branch_value"] is (fired == "yes_arm")
    _node(events, "workflow.node.completed", fired)
    assert [e["node_name"] for e in _by_kind(events, "workflow.node.pruned")] == [
        pruned
    ]


async def test_jev_node_without_route_on_captures_no_value(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    fake_jev.replies = [_choice_reply("billing")]
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "ask": {
                    "type": "kolsch/nodes/jev",
                    "state": {
                        "subject": "{{ inputs.s }}",
                        "tags": ["{{ inputs.t }}", 3],
                    },
                    "questions": _TRIAGE_QUESTION,
                },
                "after": {"type": "kolsch/nodes/noop"},
            },
            "graph": {"edges": [{"from": "ask", "to": "after"}]},
        }
    )
    status, events = await _run(
        tmp_path, workflow, _registry(tmp_path), {"s": "refund", "t": "vip"}
    )
    assert status == WorkflowRunStatus.SUCCEEDED
    completed = _node(events, "workflow.node.completed", "ask")
    assert set(completed["payload"]["output"]) == {"answers", "model", "usage"}
    assert "branch_value" not in completed["payload"]
    _node(events, "workflow.node.completed", "after")
    # Nested strings inside objects / arrays were rendered too.
    sent = json.loads(fake_jev.seen[0].body)
    assert sent["state"] == {"subject": "refund", "tags": ["vip", 3]}


async def test_jev_node_threshold_breach_fails_the_node(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    fake_jev.replies = [_choice_reply("feature", confidence=0.4)]
    question = {"team": {**_TRIAGE_QUESTION["team"], "fail_below": 0.6}}
    status, events = await _run(
        tmp_path,
        _triage_workflow(questions=question),
        _registry(tmp_path),
        {"ticket_text": "maybe add dark mode?"},
    )
    assert status == WorkflowRunStatus.FAILED
    failed = _node(events, "workflow.node.failed", "route")
    nested = failed["payload"]["nested_error"]
    assert nested["type"] == "tool.jev_below_threshold"
    assert nested["detail"]["failures"] == [
        {"question": "team", "metric": "confidence", "value": 0.4, "threshold": 0.6}
    ]
    assert failed["payload"]["error_class"] == "WorkflowNodeFailed"
    assert _by_kind(events, "workflow.run.failed")
    # No arm fired.
    assert not [
        e
        for e in _by_kind(events, "workflow.node.completed")
        if e.get("node_name", "").endswith("_handler")
    ]


async def test_jev_node_missing_key_fails_with_connect_hint(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    status, events = await _run(
        tmp_path,
        _triage_workflow(),
        _registry(tmp_path, env=""),
        {"ticket_text": "x"},
    )
    assert status == WorkflowRunStatus.FAILED
    nested = _node(events, "workflow.node.failed", "route")["payload"]["nested_error"]
    assert nested["type"] == "secret.not_found"
    assert nested["recovery_hint"] == "Run `kolsch connect openrouter`."
    assert fake_jev.seen == []


async def test_jev_node_unauthorized_fails_the_node(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    status, events = await _run(
        tmp_path,
        _triage_workflow(),
        _registry(tmp_path, effect="deny"),
        {"ticket_text": "x"},
    )
    assert status == WorkflowRunStatus.FAILED
    nested = _node(events, "workflow.node.failed", "route")["payload"]["nested_error"]
    assert nested["type"] == "tool.unauthorized"
    assert fake_jev.seen == []


# ---------------------------------------------------------------------------
# Test 6 — validator rules (beyond the golden fixtures)
# ---------------------------------------------------------------------------


def _errors(workflow: Workflow) -> list[str]:
    result = validate_workflow(workflow, AssetCatalog(), file="wf.yaml")
    return [type(e).__name__ for e in result.errors]


def _jev(route_on: str | None, questions: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "kolsch/nodes/jev",
        "state": "x",
        "questions": questions,
    }
    if route_on is not None:
        node["route_on"] = route_on
    return node


_NOUL = {"refund": {"type": "noul", "instructions": "Refund?"}}


def _two_arm(source: dict[str, Any], arms: list[Any]) -> Workflow:
    nodes: dict[str, Any] = {"src": source}
    edges = []
    for i, arm in enumerate(arms):
        nodes[f"n{i}"] = {"type": "kolsch/nodes/noop"}
        edges.append({"from": "src", "to": f"n{i}", "branch": arm})
    return Workflow.model_validate({"nodes": nodes, "graph": {"edges": edges}})


def test_validator_jev_without_route_on_is_not_branchable() -> None:
    wf = _two_arm(_jev(None, _TRIAGE_QUESTION), ["billing"])
    assert _errors(wf) == ["WorkflowBranchOnNonBranchable"]


def test_validator_jev_route_on_unknown_question() -> None:
    wf = _two_arm(_jev("nope", _TRIAGE_QUESTION), [])
    assert _errors(wf) == ["WorkflowJevInvalidRouteOn"]


def test_validator_choice_route_rejects_bool_arm() -> None:
    wf = _two_arm(
        _jev("team", _TRIAGE_QUESTION), ["billing", "outage", "feature", True]
    )
    assert _errors(wf) == ["WorkflowBranchTypeMismatch"]


def test_validator_noul_route_needs_bool_arms_and_both_of_them() -> None:
    assert _errors(_two_arm(_jev("refund", _NOUL), [True, False])) == []
    assert _errors(_two_arm(_jev("refund", _NOUL), [True, "no"])) == [
        "WorkflowBranchTypeMismatch",
        "WorkflowBranchMissingArm",
    ]
    assert _errors(_two_arm(_jev("refund", _NOUL), [True])) == [
        "WorkflowBranchMissingArm"
    ]


def test_validator_str_arm_on_bash_source_is_type_mismatch() -> None:
    wf = _two_arm({"type": "kolsch/nodes/bash", "command": "true"}, [True, "on"])
    assert _errors(wf) == ["WorkflowBranchTypeMismatch"]


def test_validator_missing_choice_arm_names_the_option() -> None:
    wf = _two_arm(_jev("team", _TRIAGE_QUESTION), ["billing", "outage"])
    result = validate_workflow(wf, AssetCatalog(), file="wf.yaml")
    (error,) = result.errors
    assert type(error).__name__ == "WorkflowBranchMissingArm"
    assert error.missing_arm == "feature"


# ---------------------------------------------------------------------------
# Runtime guards — unvalidated (resume / fork) workflows fail loudly
# ---------------------------------------------------------------------------


def _skip_run_start_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mimic resume / fork: they reload YAML and re-enter the worklist
    without the validator. Here, ``run()``'s run-start gate is disabled
    so the same worklist sees an unvalidated graph."""
    import workflows.validator as validator_mod

    monkeypatch.setattr(
        validator_mod, "validate_workflow_or_raise", lambda *a, **k: None
    )


async def test_unvalidated_str_arm_on_bool_source_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-spec-136 ``branch: yes`` (now a str) on a boolean source must
    not silently prune both arms."""
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "gate": {"type": "kolsch/nodes/branch", "condition": True},
                "yes_arm": {"type": "kolsch/nodes/noop"},
                "no_arm": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "gate", "to": "yes_arm", "branch": "yes"},
                    {"from": "gate", "to": "no_arm", "branch": "no"},
                ]
            },
        }
    )
    _skip_run_start_gate(monkeypatch)
    status, events = await _run(tmp_path, workflow, _registry(tmp_path), validate=False)
    assert status == WorkflowRunStatus.FAILED
    failed = _node(events, "workflow.node.failed", "gate")
    assert "different type" in failed["payload"]["message"]
    assert not _by_kind(events, "workflow.node.pruned")
    assert not [
        e
        for e in _by_kind(events, "workflow.node.completed")
        if "arm" in str(e.get("node_name"))
    ]


async def test_jev_choice_with_unwired_option_fails_loudly(
    tmp_path: Path, fake_jev: _FakeJevServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jev returning an option no arm names must not prune every arm."""
    fake_jev.replies = [_choice_reply("outage")]
    workflow = Workflow.model_validate(
        {
            "nodes": {
                "route": {
                    "type": "kolsch/nodes/jev",
                    "state": "x",
                    "questions": _TRIAGE_QUESTION,
                    "route_on": "team",
                },
                "billing_handler": {"type": "kolsch/nodes/noop"},
                "feature_handler": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "route", "to": "billing_handler", "branch": "billing"},
                    {"from": "route", "to": "feature_handler", "branch": "feature"},
                ]
            },
        }
    )
    _skip_run_start_gate(monkeypatch)
    status, events = await _run(tmp_path, workflow, _registry(tmp_path), validate=False)
    assert status == WorkflowRunStatus.FAILED
    failed = _node(events, "workflow.node.failed", "route")
    assert "unknown option 'outage'" in failed["payload"]["message"]
    assert not _by_kind(events, "workflow.node.pruned")
