"""Phase 3.8 — Phase Tests 1-9 wired end-to-end against the starter.

These tests exercise the ``kolsch/workflows/fork-demo`` starter
workflow through the public CLI surface (``cli.main.main``), so
they cover the operator-visible round-trip:

  run → events → fork → resume (transitive)

Mapping to the build doc's Phase Tests:

- Phase Test 1: events command lists every JSONL event.
- Phase Test 2: fork at a ``workflow.node.completed`` event.
- Phase Test 3: fork at ``workflow.run.started`` re-runs everything.
- Phase Test 4: fork at an unknown event_id raises WorkflowRuntimeError.
- Phase Test 5: fork at a spawned-session event_id is rejected via
  the same code path (the event isn't in the parent's
  events.jsonl). The CLI tests don't run a real agent — the
  executor test
  :mod:`tests.component.test_workflow_fork_executor.TestForkErrorPaths`
  covers the equivalent "event not in parent's JSONL" rejection. We
  duplicate the smoke here against a bogus id so the operator-facing
  message wording is locked.
- Phase Test 6: fork against a parent whose JSONL was deleted raises
  WorkflowRunNotFound.
- Phase Test 7: ``--input`` override correctly modifies the
  fork-point node's captured output.
- Phase Test 8: sub-workflow fork — reuse vs re-execute.
- Phase Test 9: no regressions — implicitly verified by the full
  pytest suite green.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture — `kolsch setup --non-interactive` materialises the
# starter bundle including ``kolsch/workflows/fork-demo`` (shipped in
# Step 3 Step 1). We invoke ``setup`` once per test so each test gets
# a clean workspace with the starter assets in place.
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    rc = main(["--root", str(tmp_path), "setup", "--non-interactive"])
    assert rc == 0
    return tmp_path


def _run_cli(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _run_fork_demo(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    seed: str = "urgent issue",
) -> str:
    rc, out, _ = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "kolsch/workflows/fork-demo",
            "--input",
            f"seed={seed}",
        ],
        capsys=capsys,
    )
    assert rc == 0
    # First stdout line: "run wrk_<id> succeeded"
    return out.strip().splitlines()[0].split()[1]


def _events_json(
    workspace: Path,
    run_id: str,
    capsys: pytest.CaptureFixture[str],
) -> list[dict]:
    rc, out, _ = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "--format",
            "json",
            "events",
            run_id,
        ],
        capsys=capsys,
    )
    assert rc == 0
    return json.loads(out)["data"]["events"]


def _find_completed_event_id(
    events: list[dict], node_name: str
) -> str:
    for event in events:
        if event["kind"] != "workflow.node.completed":
            continue
        if f"node_name={node_name}" in event.get("payload_preview", ""):
            return event["event_id"]
    msg = f"no completion event for node {node_name!r}"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Phase Test 1 — events command lists every JSONL event
# ---------------------------------------------------------------------------


class TestPhaseTest1Events:
    def test_events_command_lists_run_events(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_id = _run_fork_demo(workspace, capsys)
        # Read the JSONL directly so the test owns the canonical
        # event id set; the CLI output must contain ALL of them.
        on_disk = [
            json.loads(line)
            for line in (
                workspace / "runs" / run_id / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert on_disk, "fork-demo produced no events"

        events = _events_json(workspace, run_id, capsys)
        cli_event_ids = {row["event_id"] for row in events}
        for raw in on_disk:
            assert raw["event_id"] in cli_event_ids
        # Each row has the four documented fields.
        for row in events:
            assert set(row) >= {
                "event_id",
                "kind",
                "ts",
                "payload_preview",
            }


# ---------------------------------------------------------------------------
# Phase Test 2 — fork at a workflow.node.completed event
# ---------------------------------------------------------------------------


class TestPhaseTest2ForkAtCompleted:
    def test_fork_at_classify_completed_succeeds(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent_id = _run_fork_demo(workspace, capsys)
        events = _events_json(workspace, parent_id, capsys)
        classify_id = _find_completed_event_id(events, "classify")

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                classify_id,
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["data"]["status"] == "succeeded"
        assert envelope["data"]["parent_run_id"] == parent_id
        assert envelope["data"]["fork_event_id"] == classify_id
        # Forked outputs match the parent's urgent-path outputs since
        # we didn't override anything.
        outputs = envelope["data"]["outputs"]
        assert outputs["handle_urgent"]["value"] == "URGENT: urgent issue"
        assert outputs["handle_normal"] is None


# ---------------------------------------------------------------------------
# Phase Test 3 — fork at workflow.run.started re-runs everything
# ---------------------------------------------------------------------------


class TestPhaseTest3ForkAtStarted:
    def test_fork_at_started_re_runs_workflow(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent_id = _run_fork_demo(workspace, capsys, seed="urgent issue")
        events = _events_json(workspace, parent_id, capsys)
        started_id = next(
            e["event_id"] for e in events if e["kind"] == "workflow.run.started"
        )

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                started_id,
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["data"]["status"] == "succeeded"
        # Read the forked run's JSONL: every fork-demo node should
        # have emitted a node.started event (full re-run).
        forked_id = envelope["data"]["new_run_id"]
        fork_events = _events_json(workspace, forked_id, capsys)
        started_names = [
            e["payload_preview"].split(",")[0].replace("node_name=", "")
            for e in fork_events
            if e["kind"] == "workflow.node.started"
        ]
        # ``handle_normal`` is pruned (urgent path) so it's not in
        # node.started; the other three nodes are.
        assert "classify" in started_names
        assert "route" in started_names
        assert "handle_urgent" in started_names


# ---------------------------------------------------------------------------
# Phase Test 4 — invalid event_id raises WorkflowRuntimeError
# ---------------------------------------------------------------------------


class TestPhaseTest4InvalidEventId:
    def test_unknown_event_id_exits_one_with_runtime_error(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent_id = _run_fork_demo(workspace, capsys)
        bogus = "ev_99999999-9999-4999-8999-999999999999"
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                bogus,
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        assert envelope["errors"][0]["class"] == "WorkflowRuntimeError"
        assert bogus in envelope["errors"][0]["summary"]


# ---------------------------------------------------------------------------
# Phase Test 5 — spawned-session event_id rejected
#
# The CLI never reaches into a spawned-session JSONL, so this test
# uses a bogus event_id that LOOKS like a session-side event to lock
# the operator-facing guidance in the error message. The structural
# guarantee (events not in parent's events.jsonl are rejected) is
# covered by Phase Test 4; this test pins the WORDING that guides
# operators away from chat-capsule / agent-session ids.
# ---------------------------------------------------------------------------


class TestPhaseTest5SpawnedSessionGuidance:
    def test_error_message_guides_user_to_workflow_level_event(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent_id = _run_fork_demo(workspace, capsys)
        # A syntactically-valid event id that doesn't exist in the
        # parent's JSONL — the same rejection arm that would fire
        # for a real spawned-session id (which by spec §9.5 cannot
        # appear in the parent's workflow-level events.jsonl).
        bogus = "ev_88888888-8888-4888-8888-888888888888"
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "fork",
                parent_id,
                "--at",
                bogus,
            ],
            capsys=capsys,
        )
        assert rc == 1
        # The error message in text mode goes to stderr per
        # ``_emit_error_envelope``. It should mention session / agent
        # so the operator knows where they went wrong.
        assert (
            "session" in err.lower() or "agent" in err.lower()
        ), err


# ---------------------------------------------------------------------------
# Phase Test 6 — parent JSONL deleted raises WorkflowRunNotFound
# ---------------------------------------------------------------------------


class TestPhaseTest6ParentDeleted:
    def test_parent_jsonl_deleted_exits_one_with_run_not_found(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent_id = _run_fork_demo(workspace, capsys)
        events = _events_json(workspace, parent_id, capsys)
        classify_id = _find_completed_event_id(events, "classify")
        # Delete the parent's events.jsonl out from under us.
        (workspace / "runs" / parent_id / "events.jsonl").unlink()

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                classify_id,
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["errors"][0]["class"] == "WorkflowRunNotFound"

    def test_parent_meta_and_jsonl_both_deleted(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Belt-and-braces: nuking the whole run directory should
        # also produce WorkflowRunNotFound (not a different error
        # class), so operators get one consistent error envelope
        # regardless of which sidecar disappeared.
        parent_id = _run_fork_demo(workspace, capsys)
        events = _events_json(workspace, parent_id, capsys)
        classify_id = _find_completed_event_id(events, "classify")
        shutil.rmtree(workspace / "runs" / parent_id)

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                classify_id,
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["errors"][0]["class"] == "WorkflowRunNotFound"


# ---------------------------------------------------------------------------
# Phase Test 7 — --input override modifies fork-point node's output
# ---------------------------------------------------------------------------


class TestPhaseTest7InputOverride:
    def test_classify_value_override_flips_branch(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Parent classifies as 'urgent' → routes to handle_urgent.
        # Fork at classify's completion with --input value=normal:
        # the override flips the classification, route sees label=normal,
        # branch is false, handle_normal fires instead.
        parent_id = _run_fork_demo(workspace, capsys, seed="urgent issue")
        # Sanity — parent took the urgent path.
        parent_events = _events_json(workspace, parent_id, capsys)
        classify_id = _find_completed_event_id(parent_events, "classify")

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                parent_id,
                "--at",
                classify_id,
                "--input",
                "value=normal",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["data"]["status"] == "succeeded"
        outputs = envelope["data"]["outputs"]
        # Routing flipped — handle_normal fired (with the ORIGINAL
        # seed, since the override only patched classify's output).
        assert outputs["handle_normal"]["value"] == "normal: urgent issue"
        assert outputs["handle_urgent"] is None


# ---------------------------------------------------------------------------
# Phase Test 8 — sub-workflow fork: reuse vs re-execute
# ---------------------------------------------------------------------------


def _write_inline_subworkflow_bundle(workspace: Path) -> None:
    """Materialise a tiny parent+child sub-workflow bundle.

    The starter bundle's ``nested-greeting`` workflow needs an agent
    runtime that the headless test environment doesn't have wired up;
    a compute-only inline bundle exercises the SAME spec §9.3
    fork-with-subworkflow path without the agent dependency.
    """
    from _yamlio import dump

    bundle = workspace / "bundles" / "subdemo"
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: subdemo\nversion: "0.1"\ndescription: test\n',
        encoding="utf-8",
    )
    parent_yaml = {
        "nodes": {
            "prep": {
                "type": "kolsch/nodes/compute",
                "value": "prep-done",
            },
            "sub": {
                "type": "kolsch/nodes/workflow",
                "ref": "subdemo/workflows/child",
            },
            "wrap": {
                "type": "kolsch/nodes/compute",
                "value": "wrap-done",
            },
        },
        "graph": {
            "edges": [
                {"from": "prep", "to": "sub"},
                {"from": "sub", "to": "wrap"},
            ]
        },
    }
    child_yaml = {
        "nodes": {
            "only": {
                "type": "kolsch/nodes/compute",
                "value": "leaf-output",
            },
        }
    }
    (bundle / "workflows" / "parent.yaml").write_text(
        dump(parent_yaml), encoding="utf-8"
    )
    (bundle / "workflows" / "child.yaml").write_text(
        dump(child_yaml), encoding="utf-8"
    )


def _count_runs(workspace: Path) -> int:
    runs_dir = workspace / "runs"
    if not runs_dir.exists():
        return 0
    return sum(
        1
        for child in runs_dir.iterdir()
        if child.is_dir() and child.name.startswith("wrk_")
    )


class TestPhaseTest8SubWorkflowFork:
    def test_fork_after_workflow_call_reuses_child_output(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_inline_subworkflow_bundle(workspace)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "subdemo/workflows/parent",
            ],
            capsys=capsys,
        )
        assert rc == 0
        parent_id = out.strip().splitlines()[0].split()[1]
        runs_before = _count_runs(workspace)
        # parent + child = 2 runs.
        assert runs_before == 2

        events = _events_json(workspace, parent_id, capsys)
        sub_completed = _find_completed_event_id(events, "sub")

        rc, _, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "fork",
                parent_id,
                "--at",
                sub_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        # Fork added EXACTLY 1 run dir (the forked run itself); the
        # child output was rehydrated from the parent's captured
        # payload — no fresh child spawn.
        assert _count_runs(workspace) == runs_before + 1

    def test_fork_before_workflow_call_spawns_fresh_child(
        self, workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_inline_subworkflow_bundle(workspace)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "subdemo/workflows/parent",
            ],
            capsys=capsys,
        )
        assert rc == 0
        parent_id = out.strip().splitlines()[0].split()[1]
        runs_before = _count_runs(workspace)
        assert runs_before == 2

        events = _events_json(workspace, parent_id, capsys)
        prep_completed = _find_completed_event_id(events, "prep")
        rc, _, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "fork",
                parent_id,
                "--at",
                prep_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        # Fork added 2 run dirs: the forked run + a fresh child
        # spawned by the workflow-call node firing on the fork.
        assert _count_runs(workspace) == runs_before + 2
