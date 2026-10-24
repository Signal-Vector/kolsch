"""bug-163 — a workflow `write_file` node is authorized on the canonical path.

Runs a one-node `kolsch/nodes/write_file` workflow through the real
`IterativeExecutor` → builtin runner → `dispatch_tool` path, with the
workflow's own `policy:` allowing `bundles/*` under a platform
`* * deny`. Asserts the run outcome, the filesystem, and the
`policy.decision.made` audit payload (`target` canonical, `raw_target`
only when the spelling differed).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog, AssetEntry
from files import PathPolicy
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tools.builtins import make_bash, make_write_file
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

_WF = "kolsch/workflows/writer"
_WRITE = "kolsch/tools/write_file"


async def _run_write(
    tmp_path: Path, path: str
) -> tuple[str, Path, list[dict[str, Any]]]:
    """Run the writer workflow with `path`; return status, root and events."""
    catalog = AssetCatalog()
    for ref, asset_type in ((_WF, "workflow"), (_WRITE, "tool")):
        catalog.assets[ref] = AssetEntry(
            ref=ref,
            asset_type=asset_type,
            bundle="kolsch",
            path=f"/var/empty/{ref}",
            metadata={},
        )
    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"what": "*", "pattern": "*", "effect": "deny"}]},
        catalog=catalog,
    )
    root = (tmp_path / "ws").resolve()
    (root / "bundles").mkdir(parents=True)
    (root / "data").mkdir()
    policy = PathPolicy(allowed_paths=["."])
    write_defn = get_tool_definition(make_write_file(policy=policy, root=root))
    write_defn.home_bundle = "kolsch"
    bash_defn = get_tool_definition(make_bash(policy=policy, root=root))
    bash_defn.home_bundle = "kolsch"
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=bash_defn,
        write_file_defn=write_defn,
        authz=authz,
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
    )
    workflow = Workflow.model_validate(
        {
            "policy": [{"what": _WRITE, "pattern": "bundles/*", "effect": "allow"}],
            "nodes": {
                "write": {
                    "type": "kolsch/nodes/write_file",
                    "path": path,
                    "content": "hello",
                }
            },
        }
    )
    store = WorkflowRunStore(root)
    run = store.create(workflow_ref=_WF, inputs={})
    status = await IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    ).run()
    events = [
        json.loads(line)
        for line in store.directory(run.run_id)
        .events_path.read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    return status.value, root, events


def _decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in events
        if e["kind"] == "policy.decision.made" and e["payload"]["action"] == _WRITE
    ]


async def test_traversal_out_of_bundles_fails_the_node(tmp_path: Path) -> None:
    status, root, events = await _run_write(tmp_path, "bundles/../data/x.txt")

    assert status == "failed"
    assert not (root / "data" / "x.txt").exists()
    decisions = _decisions(events)
    assert len(decisions) == 1
    assert decisions[0]["effect"] == "deny"
    assert decisions[0]["target"] == "data/x.txt"
    assert decisions[0]["raw_target"] == "bundles/../data/x.txt"


async def test_canonical_path_emits_no_raw_target(tmp_path: Path) -> None:
    status, root, events = await _run_write(tmp_path, "out.txt")

    assert status == "failed"
    assert not (root / "out.txt").exists()
    decisions = _decisions(events)
    assert [d["target"] for d in decisions] == ["out.txt"]
    assert "raw_target" not in decisions[0]


async def test_allowed_path_writes_and_audits_canonically(tmp_path: Path) -> None:
    status, root, events = await _run_write(tmp_path, "./bundles//ok.txt")

    assert status == "succeeded"
    assert (root / "bundles" / "ok.txt").read_text(encoding="utf-8") == "hello"
    decisions = _decisions(events)
    assert [(d["effect"], d["target"], d["raw_target"]) for d in decisions] == [
        ("allow", "bundles/ok.txt", "./bundles//ok.txt")
    ]
    assert [e["kind"] for e in events] == [
        "workflow.run.started",
        "workflow.node.started",
        "policy.decision.made",
        "workflow.node.completed",
        "workflow.run.completed",
    ]
