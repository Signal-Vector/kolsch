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
from typing import TYPE_CHECKING

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


def _read_jsonl(path: Path) -> list[dict[str, object]]:
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
    rc = _run_cli(
        ["--root", str(workspace), "setup", "--non-interactive"]
    )
    assert rc == 0
    (workspace / "platform.yaml").write_text(
        _ALLOW_ALL_PLATFORM_YAML, encoding="utf-8"
    )
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
    run_completed = next(
        e for e in events if e["kind"] == "workflow.run.completed"
    )
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
    rc = _run_cli(
        ["--root", str(workspace), "setup", "--non-interactive"]
    )
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
