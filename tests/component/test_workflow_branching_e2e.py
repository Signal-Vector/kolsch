"""Phase 3.3 Step 3 — end-to-end integration tests.

Drives the starter ``branching-demo`` workflow via the live CLI on a
fresh ``setup --non-interactive`` workspace and asserts:

- Phase Test 1: bash exit-code routing — non-zero exit fires the
  ``branch: false`` arm and prunes ``branch: true``; the JSONL records
  ``branch_value`` on the source's completion event and emits one
  ``workflow.node.pruned`` event per pruned descendant (master spec
  §11.1 extension).
- Phase Test 6: no regressions — the existing ``hello`` starter
  workflow continues to produce its Phase 3.1 JSONL shape (no
  ``branch_value`` field, no ``workflow.node.pruned`` events) when
  there's no branching at play.

Phase Tests 2-5 (authored Python bool branching, multi-input
``inputs.*`` namespace, strict-mode bind mismatch, pruning cascade
with partial fan-in) are covered by
:mod:`tests.component.test_workflow_branching` against the executor
directly — the CLI e2e here verifies the surfacing-on-disk shape of
the JSONL the documented forensics tooling will read.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

from cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_ALLOW_ALL_PLATFORM_YAML = textwrap.dedent(
    """\
    secrets: {variant: dotenv, config: {path: .env}}
    sandbox: {variant: native, config: {}}
    authorization:
      variant: rule_based
      config:
        rules:
          - {what: "*", pattern: "*", effect: "allow"}
    storage: {entity_backend: sqlite, entity_path: .platform/entities.db}
    event_bus: {backend: in_process}
    file_access: {allowed_paths: []}
    defaults: {}
    """
)


def _run_cli(argv: list[str]) -> int:
    return main(argv)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _make_workspace_with_allow_all(tmp_path: Path) -> Path:
    """``kolsch setup --non-interactive`` lands a ``platform.yaml``
    whose ``kolsch/bash`` rule prompts (collapsing to deny on the
    headless CLI). Branching-demo's bash nodes must actually run, so
    we overwrite ``platform.yaml`` with an allow-all variant after
    the scaffold completes — same pattern Phase 3.1 Phase Test 2
    uses for the cancel workflow.
    """
    workspace = tmp_path / "ws"
    rc = _run_cli(["--root", str(workspace), "setup", "--non-interactive"])
    assert rc == 0
    (workspace / "platform.yaml").write_text(_ALLOW_ALL_PLATFORM_YAML, encoding="utf-8")
    return workspace


# ---------------------------------------------------------------------------
# Phase Test 1 — bash exit-code branching demo
# ---------------------------------------------------------------------------


def test_branching_demo_false_path_prunes_success_arm(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``check_cmd=false`` exits non-zero → the ``branch: false``
    arm (``on_failure``) fires and ``branch: true`` (``on_success``)
    is pruned. The JSONL records ``branch_value=false`` on
    ``check``'s completion + a ``workflow.node.pruned`` event for
    ``on_success`` chained to that completion.
    """
    workspace = _make_workspace_with_allow_all(tmp_path)
    capsys.readouterr()  # drain setup output

    rc = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "kolsch/workflows/branching-demo",
            "--input",
            "check_cmd=false",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"workflow run failed: {captured.err}"

    runs = list((workspace / "runs").iterdir())
    assert len(runs) == 1
    events = _read_jsonl(runs[0] / "events.jsonl")

    kinds = [e["kind"] for e in events]
    assert kinds[0] == "workflow.run.started"
    assert kinds[-1] == "workflow.run.completed"

    # ``check``'s completion carries branch_value=false.
    check_completed = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"]["node_name"] == "check"
    )
    assert check_completed["payload"]["branch_value"] is False

    # One pruned event for ``on_success``, chained to ``check``'s
    # completion id.
    pruned = [e for e in events if e["kind"] == "workflow.node.pruned"]
    assert len(pruned) == 1
    p = pruned[0]
    assert p["payload"] == {
        "node_name": "on_success",
        "reason": "branch_not_taken",
        "upstream_source": "check",
        "branch_value": False,
    }
    assert p["parent_event_id"] == check_completed["event_id"]

    # ``on_failure`` fires; ``on_success`` emits no started/completed.
    per_node = [
        (e["payload"].get("node_name"), e["kind"])
        for e in events
        if e["kind"].startswith("workflow.node.")
    ]
    assert ("on_failure", "workflow.node.started") in per_node
    assert ("on_failure", "workflow.node.completed") in per_node
    assert ("on_success", "workflow.node.started") not in per_node
    assert ("on_success", "workflow.node.completed") not in per_node

    # Inferred outputs carry ``null`` for the pruned terminal.
    run_completed = next(e for e in events if e["kind"] == "workflow.run.completed")
    outputs = run_completed["payload"]["outputs"]
    assert outputs["on_success"] is None
    assert outputs["on_failure"]["exit_code"] == 0


def test_branching_demo_true_path_prunes_failure_arm(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Symmetric counterpart — ``check_cmd=true`` exits 0 → the
    ``branch: true`` arm fires and ``branch: false`` is pruned."""
    workspace = _make_workspace_with_allow_all(tmp_path)
    capsys.readouterr()

    rc = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "kolsch/workflows/branching-demo",
            "--input",
            "check_cmd=true",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"workflow run failed: {captured.err}"

    runs = list((workspace / "runs").iterdir())
    assert len(runs) == 1
    events = _read_jsonl(runs[0] / "events.jsonl")

    check_completed = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"]["node_name"] == "check"
    )
    assert check_completed["payload"]["branch_value"] is True

    pruned = [e for e in events if e["kind"] == "workflow.node.pruned"]
    assert len(pruned) == 1
    assert pruned[0]["payload"]["node_name"] == "on_failure"
    assert pruned[0]["payload"]["branch_value"] is True


# ---------------------------------------------------------------------------
# Phase Test 6 — non-branching workflows unaffected
# ---------------------------------------------------------------------------


def test_non_branching_starter_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The existing ``hello`` starter workflow (no ``branch:`` edges)
    produces no ``branch_value`` payload fields and no
    ``workflow.node.pruned`` events — the Phase 3.3 changes leave
    pure linear workflows byte-for-byte unaffected (modulo the
    timestamp + id canonicalisation already applied to goldens).
    """
    workspace = tmp_path / "ws"
    rc = _run_cli(["--root", str(workspace), "setup", "--non-interactive"])
    assert rc == 0
    capsys.readouterr()

    rc = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "kolsch/workflows/hello",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"workflow run failed: {captured.err}"

    runs = list((workspace / "runs").iterdir())
    events = _read_jsonl(runs[0] / "events.jsonl")

    for ev in events:
        assert ev["kind"] != "workflow.node.pruned"
        if ev["kind"] == "workflow.node.completed":
            assert "branch_value" not in ev["payload"], (
                f"non-branching node {ev['payload'].get('node_name')!r} "
                f"leaked a branch_value payload field"
            )


# ---------------------------------------------------------------------------
# Phase 3.6 Step 2 — triage walkthrough end-to-end
#
# Drives ``kolsch/workflows/triage-tickets`` against a real engine
# + scripted LLM. The classifier returns "urgent" or "normal" per
# scenario; the Phase 3.6 ``branch`` runner captures
# ``{value: bool}``; Phase 3.3's edge dispatcher routes to the
# matching arm; the un-fired arm emits ``workflow.node.pruned``.
# Cross-JSONL trace_id linkage (Phase 3.2 §11.6.4 carryover) is
# verified separately to confirm branching doesn't break the
# spawned-session correlation.
#
# The static-runner counterpart at
# ``tests/golden/runs/triage-{urgent,normal}.jsonl`` pins the exact
# JSONL byte shape; this suite covers the dynamic case where the
# real agent runner spawns sessions through the engine.
# ---------------------------------------------------------------------------


import shutil  # noqa: E402

from tests.component.test_workflow_agent_runner_e2e import (  # noqa: E402
    _PERMISSIVE_PLATFORM_YAML,
    _build_engine_with_llm,
    _ScriptedLLM,
)
from workflows.builtins import register_builtins  # noqa: E402
from workflows.executor import IterativeExecutor, RunnerRegistry  # noqa: E402
from workflows.parser import Workflow  # noqa: E402
from workflows.run import WorkflowRunStatus  # noqa: E402
from workflows.run_store import WorkflowRunStore  # noqa: E402


def _scaffold_triage_workspace(root: Path) -> None:
    """Lay down a workspace seeded with the actual starter ``kolsch``
    bundle (so the e2e test exercises the same YAMLs the operator
    runs). Mirrors the pattern in
    ``test_workflow_user_input_e2e._make_workspace_with_starter``.
    """
    for sub in (
        "bundles",
        "sessions",
        "content",
        "logs",
        "data",
        ".platform",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "platform.yaml").write_text(_PERMISSIVE_PLATFORM_YAML, encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[2]
    starter_src = repo_root / "src" / "cli" / "_starter_bundle" / "kolsch"
    shutil.copytree(starter_src, root / "bundles" / "kolsch")
    # The starter bundle ships without ``bundle.yaml`` (laid down by
    # ``kolsch setup`` separately); add it so AssetManagement's
    # discovery accepts the bundle.
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        'name: kolsch\nversion: "0.1"\ndescription: phase3.6-e2e\n',
        encoding="utf-8",
    )


async def _drive_triage(
    root: Path,
    *,
    classify_reply: str,
    handler_reply: str,
) -> tuple[WorkflowRunStatus, Path, object]:
    """Drive the triage workflow once with a scripted LLM.

    Returns ``(status, events_path, session_store)``. The scripted
    LLM returns ``classify_reply`` first (→ ``{text: classify_reply}``
    captured by the classifier node → workflow's ``label`` bind →
    branch condition) then ``handler_reply`` (consumed by whichever
    responder agent fires).
    """
    import yaml

    from files import PathPolicy
    from tools.builtins import (
        make_bash,
        make_list_files,
        make_read_file,
        make_write_file,
    )
    from tools.decorator import get_tool_definition

    _scaffold_triage_workspace(root)

    llm = _ScriptedLLM(texts=[classify_reply, handler_reply])
    engine, store, am = await _build_engine_with_llm(root, llm=llm)
    catalog = am.catalog()

    workflow_text = (
        root / "bundles" / "kolsch" / "workflows" / "triage-tickets.yaml"
    ).read_text(encoding="utf-8")
    workflow = Workflow.model_validate(yaml.safe_load(workflow_text))

    run_store = WorkflowRunStore(root)
    run = run_store.create(
        workflow_ref="kolsch/workflows/triage-tickets",
        inputs={"ticket_text": "any"},
    )

    policy = PathPolicy(allowed_paths=["data/", "bundles/"], denied_paths=[".env"])
    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=get_tool_definition(make_bash(policy=policy, root=root)),
        # Reuse the engine's already-built providers — bypassing
        # ``bootstrap_runtime`` keeps the test lean and matches the
        # in-process construction agent-runner e2e uses.
        authz=engine._authz,
        secrets=engine._secrets,
        sandbox=engine._sandbox,
        catalog=catalog,
        engine=engine,
        session_store=store,
        config_builder=am.build_resolved_config,
        read_file_defn=get_tool_definition(make_read_file(policy=policy, root=root)),
        write_file_defn=get_tool_definition(make_write_file(policy=policy, root=root)),
        list_files_defn=get_tool_definition(make_list_files(policy=policy, root=root)),
    )
    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=run_store,
        registry=registry,
        catalog=catalog,
    )
    status = await executor.run()
    events_path = run_store.directory(run.run_id).events_path
    return status, events_path, store


def _branch_value_of(events: list[dict[str, Any]], *, node_name: str) -> object:
    completed = [
        e
        for e in events
        if e["kind"] == "workflow.node.completed" and e.get("node_name") == node_name
    ]
    assert len(completed) == 1, f"expected one completion for {node_name!r}"
    return completed[0]["payload"].get("branch_value")


def _pruned_nodes(events: list[dict[str, Any]]) -> set[str]:
    return {str(e["node_name"]) for e in events if e["kind"] == "workflow.node.pruned"}


def _fired_nodes(events: list[dict[str, Any]]) -> set[str]:
    return {str(e["node_name"]) for e in events if e["kind"] == "workflow.node.started"}


class TestPhase36TriageBothArms:
    async def test_urgent_arm_fires_normal_pruned(self, tmp_path: Path) -> None:
        status, events_path, _store = await _drive_triage(
            tmp_path,
            classify_reply="urgent",
            handler_reply="Acked urgent ticket",
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        events = _read_jsonl(events_path)
        assert _branch_value_of(events, node_name="is_urgent") is True
        assert "urgent_handler" in _fired_nodes(events)
        assert _pruned_nodes(events) == {"normal_handler"}

    async def test_normal_arm_fires_urgent_pruned(self, tmp_path: Path) -> None:
        status, events_path, _store = await _drive_triage(
            tmp_path,
            classify_reply="normal",
            handler_reply="Acked normal ticket",
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        events = _read_jsonl(events_path)
        assert _branch_value_of(events, node_name="is_urgent") is False
        assert "normal_handler" in _fired_nodes(events)
        assert _pruned_nodes(events) == {"urgent_handler"}


class TestPhase36TriageCrossJsonlLinkage:
    async def test_spawned_sessions_share_workflow_trace_id(
        self, tmp_path: Path
    ) -> None:
        """Spawned agent sessions inherit the workflow's ``trace_id``
        unchanged — Phase 3.2 §11.6.4. The branching context shouldn't
        break the linkage; both the classifier and the firing responder
        produce sessions whose events carry the same ``tr_*`` id as
        the workflow JSONL. The pruned handler does NOT spawn a
        session (the cascade short-circuits before agent runner
        construction).
        """
        status, events_path, _store = await _drive_triage(
            tmp_path,
            classify_reply="urgent",
            handler_reply="Acked urgent ticket",
        )
        assert status == WorkflowRunStatus.SUCCEEDED
        workflow_events = _read_jsonl(events_path)
        workflow_trace_id = workflow_events[0]["trace_id"]
        assert isinstance(workflow_trace_id, str)
        assert workflow_trace_id.startswith("tr_")

        session_paths = sorted((tmp_path / "sessions").glob("ses_*.jsonl"))
        assert len(session_paths) == 2  # classifier + urgent_responder
        for sp in session_paths:
            for event in _read_jsonl(sp):
                assert event.get("trace_id") == workflow_trace_id, (
                    f"trace_id mismatch in {sp.name}: "
                    f"event={event.get('event_id')} "
                    f"trace={event.get('trace_id')}"
                )
