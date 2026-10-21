"""spec-136 M3 Test 7 — the shipped Jev triage starter workflow, offline.

Loads ``src/cli/_starter_bundle/kolsch/workflows/triage-tickets-jev.yaml``
exactly as shipped, validates it, and runs it through the real
``IterativeExecutor`` with the builtin runners from
``register_builtins`` (real ``JevRunner`` → real ``dispatch_tool`` →
real ``jev_decide`` tool). The tool is pointed at a local fake Jev
server that answers ``choice: "billing"``; no network, dummy key.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING

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
from workflows.parser import load_workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore
from workflows.validator import validate_workflow_or_raise

if TYPE_CHECKING:
    from collections.abc import Iterator

_STARTER = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "workflows"
    / "triage-tickets-jev.yaml"
)


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


async def test_jev_triage_starter_routes_billing_offline(
    tmp_path: Path, fake_jev: _FakeJevServer
) -> None:
    fake_jev.replies = [
        _ok(
            {
                "team": {
                    "type": "choice",
                    "choice": "billing",
                    "confidence": 0.95,
                    "probabilities": {"billing": 0.95, "outage": 0.03, "feature": 0.02},
                }
            }
        )
    ]
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-dummy\n", encoding="utf-8")
    workflow = load_workflow(_STARTER)
    validate_workflow_or_raise(workflow, AssetCatalog(), file=str(_STARTER))

    defn = get_tool_definition(make_jev_decide(policy=PathPolicy(), root=tmp_path))
    defn.home_bundle = "kolsch"
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=defn,  # required kwarg; the workflow has no bash node
        authz=RuleBasedAuthorizationProvider(
            config={
                "rules": [
                    {
                        "what": "kolsch/tools/jev_decide",
                        "pattern": "*",
                        "effect": "allow",
                    },
                    {"what": "*", "pattern": "*", "effect": "deny"},
                ]
            }
        ),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        jev_decide_defn=defn,
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="kolsch/workflows/triage-tickets-jev",
        inputs={"ticket_text": "I was charged twice"},
    )
    status = await IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    ).run()

    events = [dict(e) for e in store.read_events(run.run_id)]
    assert status == WorkflowRunStatus.SUCCEEDED, events
    kinds = [(e["kind"], e.get("node_name")) for e in events]
    assert ("workflow.node.completed", "billing_handler") in kinds
    pruned = sorted(str(n) for k, n in kinds if k == "workflow.node.pruned")
    assert pruned == ["feature_handler", "outage_handler"]
    route_done = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed" and e.get("node_name") == "route"
    )
    assert route_done["payload"]["branch_value"] == "billing"
    billing_done = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e.get("node_name") == "billing_handler"
    )
    assert billing_done["payload"]["output"] == {
        "value": "billing team (confidence 0.95)"
    }
    assert json.loads(fake_jev.seen[0].body)["state"] == "I was charged twice"
    assert events[-1]["kind"] == "workflow.run.completed"
