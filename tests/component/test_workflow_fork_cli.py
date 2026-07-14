"""Phase 3.8 Step 2 — ``kolsch workflows fork`` CLI tests.

Covers Step 2 ACs 2.1-2.10 of ``process/phase3.8-fork-build.md``:

- 2.1 ``fork`` subcommand registered + reachable via ``--help``.
- 2.2 ``--input k=v`` parsed via existing ``_parse_input_pairs``.
- 2.3 Success path exits 0 with the
  ``{new_run_id, parent_run_id, fork_event_id, status, outputs}``
  JSON envelope (spec §14.3).
- 2.4 Failure paths exit 1 with the appropriate error class:
  ``WorkflowRunNotFound`` for missing parent / deleted JSONL;
  ``WorkflowRuntimeError`` for unknown event_id.
- 2.5 Sub-workflow fork AFTER the workflow-call node REUSES the
  parent's captured child output — no new child wrk_id directory
  appears under ``runs/`` on the fork.
- 2.6 Sub-workflow fork BEFORE the workflow-call node spawns a
  FRESH child run (new wrk_id directory).
- 2.7 Forked run can be paused on a downstream user_input and
  resumed via Phase 3.4 machinery; the resume completes the
  FORKED run, not the original parent.
- 2.8 Re-fork chains correctly: ``forked_from.run_id`` on a re-fork
  points at the IMMEDIATE parent (the previous fork), not the
  original root.
- 2.9 ``--help`` text includes the side-effect-discipline note from
  spec §9.2.
- 2.10 No regressions in adjacent CLI commands.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture — mirrors tests/component/test_workflow_resume_cli.py
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        textwrap.dedent(
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
        ),
        encoding="utf-8",
    )
    return tmp_path


def _write_workflow_bundle(
    workspace: Path,
    bundle_name: str,
    workflow_yaml: dict[str, Any],
    *,
    workflow_name: str = "hello",
) -> Path:
    bundle = workspace / "bundles" / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        f'name: {bundle_name}\nversion: "0.1"\ndescription: t\n',
        encoding="utf-8",
    )
    wf_dir = bundle / "workflows"
    wf_dir.mkdir(exist_ok=True)
    from kolsch._yamlio import dump

    (wf_dir / f"{workflow_name}.yaml").write_text(dump(workflow_yaml), encoding="utf-8")
    return bundle


def _run_cli(
    args: list[str], *, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _pipeline_workflow() -> dict[str, Any]:
    """A → B → C using compute. B doubles A; C echoes B."""
    return {
        "nodes": {
            "A": {"type": "kolsch/nodes/compute", "value": 10},
            "B": {
                "type": "kolsch/nodes/compute",
                "value": "{{ inputs.upstream * 2 }}",
            },
            "C": {
                "type": "kolsch/nodes/compute",
                "value": "{{ inputs.v }}",
            },
        },
        "graph": {
            "edges": [
                {
                    "from": "A",
                    "to": "B",
                    "binds": {"upstream": "{{ nodes.A.value }}"},
                },
                {
                    "from": "B",
                    "to": "C",
                    "binds": {"v": "{{ nodes.B.value }}"},
                },
            ]
        },
    }


def _run_pipeline_via_cli(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    workflow_ref: str = "demo/workflows/hello",
) -> str:
    """Run the pipeline and return its run id."""
    rc, out, _ = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            workflow_ref,
        ],
        capsys=capsys,
    )
    assert rc == 0, f"parent run failed: rc={rc}"
    return out.strip().splitlines()[0].split()[1]


def _events_via_cli(
    workspace: Path,
    run_id: str,
    capsys: pytest.CaptureFixture[str],
) -> list[dict[str, Any]]:
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
    events: list[dict[str, Any]] = json.loads(out)["data"]["events"]
    return events


def _find_event_id(
    events: list[dict[str, Any]],
    *,
    kind: str,
    preview_contains: str | None = None,
) -> str:
    for event in events:
        if event["kind"] != kind:
            continue
        if preview_contains is not None and preview_contains not in event.get(
            "payload_preview", ""
        ):
            continue
        event_id: str = event["event_id"]
        return event_id
    msg = f"no event matching kind={kind!r} preview_contains={preview_contains!r}"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# AC 2.1 + 2.9 — subcommand registered + --help wording
# ---------------------------------------------------------------------------


class TestForkCliHelp:
    def test_fork_appears_in_workflows_help(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        # argparse exits via SystemExit(0) after printing --help; the
        # other workflows --help tests use the same pattern.
        with pytest.raises(SystemExit) as exc_info:
            main(["--root", str(workspace), "workflows", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "fork" in combined

    def test_fork_help_mentions_side_effect_discipline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["--root", str(workspace), "workflows", "fork", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        combined = (captured.out + captured.err).lower()
        # AC 2.9 — the --help text must guide operators about
        # side-effect discipline per spec §9.2.
        assert "side effect" in combined or "side-effect" in combined
        assert (
            "not undone" in combined
            or "not be undone" in combined
            or "stays as it was" in combined
        )


# ---------------------------------------------------------------------------
# AC 2.1 + 2.3 — happy path: fork at completed, text + JSON formats
# ---------------------------------------------------------------------------


class TestForkCliHappyPath:
    def test_fork_at_completed_text_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        events = _events_via_cli(workspace, parent_id, capsys)
        b_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
        )

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "fork",
                parent_id,
                "--at",
                b_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        # First line names the new run id + status.
        first = out.strip().splitlines()[0]
        assert "forked run wrk_" in first
        assert "succeeded" in first
        # Second line correlates the fork back to the parent.
        assert f"forked from: {parent_id} at {b_completed}" in out

    def test_fork_at_completed_json_envelope_has_required_fields(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        events = _events_via_cli(workspace, parent_id, capsys)
        b_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
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
                b_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "workflows fork"
        assert envelope["status"] == "ok"
        data = envelope["data"]
        # AC 2.3 — required spec §14.3 fields.
        assert data["new_run_id"].startswith("wrk_")
        assert data["new_run_id"] != parent_id
        assert data["parent_run_id"] == parent_id
        assert data["fork_event_id"] == b_completed
        assert data["status"] == "succeeded"
        # The forked run's outputs match the parent's (C echoes B = 20).
        assert "C" in data["outputs"]
        assert data["outputs"]["C"]["value"] == 20

    def test_fork_persists_forked_from_pointer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Round-trip via WorkflowRunStore: meta.json carries the
        # forked_from pointer the CLI minted at fork time.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        events = _events_via_cli(workspace, parent_id, capsys)
        b_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
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
                b_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        forked_id = json.loads(out)["data"]["new_run_id"]

        from workflows.ids import WorkflowRunId
        from workflows.run_store import WorkflowRunStore

        store = WorkflowRunStore(workspace)
        run = store.read(WorkflowRunId(forked_id))
        assert run.forked_from is not None
        assert str(run.forked_from.run_id) == parent_id
        assert str(run.forked_from.fork_event_id) == b_completed
        # And the forked run inherits the parent's trace_id.
        parent = store.read(WorkflowRunId(parent_id))
        assert run.trace_id == parent.trace_id


# ---------------------------------------------------------------------------
# AC 2.2 — --input override flows through
# ---------------------------------------------------------------------------


class TestForkCliInputOverride:
    def test_input_override_patches_fork_point_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        events = _events_via_cli(workspace, parent_id, capsys)
        b_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
        )
        # Override B.value → 99. Downstream C sees 99 (vs parent's 20).
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
                b_completed,
                "--input",
                "value=99",
            ],
            capsys=capsys,
        )
        assert rc == 0
        outputs = json.loads(out)["data"]["outputs"]
        # ``_parse_input_pairs`` returns strings; the override drops
        # the string "99" into ``_captured["B"]["value"]``. C's bind
        # `{{ nodes.B.value }}` renders via Jinja's NativeEnvironment
        # which coerces the bare-string expression to a native int
        # (same behaviour the compute happy-path test relies on for
        # ``{{ 1 + 1 }}`` returning ``int 2``). Either way, the
        # override correctly REPLACED the parent's value (which was
        # 20); the comparison below is intentionally loose about the
        # numeric type since this test owns the CLI surface, not the
        # Jinja coercion rules.
        assert outputs["C"]["value"] in (99, "99")
        # And critically: it is NOT the parent's pre-override value.
        assert outputs["C"]["value"] != 20


# ---------------------------------------------------------------------------
# AC 2.4 — error paths
# ---------------------------------------------------------------------------


class TestForkCliErrorPaths:
    def test_missing_parent_run_exits_one_with_run_not_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                "wrk_99999999-9999-4999-8999-999999999999",
                "--at",
                "ev_99999999-9999-4999-8999-999999999999",
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        assert envelope["errors"][0]["class"] == "WorkflowRunNotFound"

    def test_invalid_event_id_exits_one_with_runtime_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)

        bogus_event_id = "ev_99999999-9999-4999-8999-999999999999"
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
                bogus_event_id,
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        assert envelope["errors"][0]["class"] == "WorkflowRuntimeError"
        # The summary surfaces the operator-guidance message.
        summary = envelope["errors"][0]["summary"]
        assert bogus_event_id in summary
        assert "session" in summary.lower() or "agent" in summary.lower()

    def test_parent_jsonl_deleted_exits_one_with_run_not_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        events = _events_via_cli(workspace, parent_id, capsys)
        b_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
        )
        # Delete the parent's events.jsonl.
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
                b_completed,
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["errors"][0]["class"] == "WorkflowRunNotFound"


# ---------------------------------------------------------------------------
# AC 2.5 + 2.6 — sub-workflow fork semantics
# ---------------------------------------------------------------------------


def _child_workflow_yaml() -> dict[str, Any]:
    """A trivial single-node child workflow."""
    return {
        "nodes": {
            "child_node": {
                "type": "kolsch/nodes/compute",
                "value": "child-value",
            },
        }
    }


def _parent_with_subworkflow_yaml() -> dict[str, Any]:
    """Three-node parent: prep → sub (sub-workflow) → wrap."""
    return {
        "nodes": {
            "prep": {
                "type": "kolsch/nodes/compute",
                "value": "prep-done",
            },
            "sub": {
                "type": "kolsch/nodes/workflow",
                "ref": "demo/workflows/child",
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


def _count_runs(workspace: Path) -> int:
    """Count the number of wrk_* directories under runs/."""
    runs_dir = workspace / "runs"
    if not runs_dir.exists():
        return 0
    return sum(
        1
        for child in runs_dir.iterdir()
        if child.is_dir() and child.name.startswith("wrk_")
    )


class TestForkCliSubWorkflow:
    def test_fork_after_subworkflow_reuses_captured_child_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # AC 2.5 — fork at the workflow-call node's COMPLETED event
        # rehydrates the child output from the parent's captured
        # payload. NO new child wrk_id directory appears on the fork.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            _parent_with_subworkflow_yaml(),
            workflow_name="hello",
        )
        _write_workflow_bundle(
            workspace, "demo", _child_workflow_yaml(), workflow_name="child"
        )
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        # Parent + child both have run directories now.
        runs_before_fork = _count_runs(workspace)
        assert runs_before_fork == 2  # parent + child

        events = _events_via_cli(workspace, parent_id, capsys)
        sub_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=sub",
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
                sub_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        outputs = json.loads(out)["data"]["outputs"]
        # The fork only ran ``wrap`` downstream of the sub-workflow
        # node; both ``prep`` and ``sub`` were replayed.
        assert outputs["wrap"]["value"] == "wrap-done"

        # AC 2.5 reviewer note — the runs/ directory grew by ONE
        # (the forked run), not by two. No fresh child was spawned.
        runs_after_fork = _count_runs(workspace)
        assert runs_after_fork == runs_before_fork + 1

    def test_fork_before_subworkflow_spawns_fresh_child(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # AC 2.6 — fork at the workflow-call node's STARTED event
        # (before the call has fired) causes the forked run's
        # worklist to fire the workflow node fresh, spawning a NEW
        # child run.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            _parent_with_subworkflow_yaml(),
            workflow_name="hello",
        )
        _write_workflow_bundle(
            workspace, "demo", _child_workflow_yaml(), workflow_name="child"
        )
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        runs_before_fork = _count_runs(workspace)
        assert runs_before_fork == 2

        events = _events_via_cli(workspace, parent_id, capsys)
        # ``prep``'s completed event is the latest workflow-level event
        # whose downstream IS the sub-workflow node. Fork there: the
        # forked run replays prep, then fires sub (new child) + wrap.
        prep_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=prep",
        )

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
        # AC 2.6 — the runs/ directory grew by TWO: the forked run
        # AND the freshly-spawned child of the forked run.
        runs_after_fork = _count_runs(workspace)
        assert runs_after_fork == runs_before_fork + 2


# ---------------------------------------------------------------------------
# AC 2.7 — forked run can pause + resume via Phase 3.4 machinery
# ---------------------------------------------------------------------------


class TestForkPauseResume:
    def test_forked_run_pauses_and_resumes_correctly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Workflow: A (compute) → pause (user_input) → echo (compute).
        # Run parent to user_input pause + resume to completion.
        # Then fork at A's completion; the forked run reaches the
        # same user_input pause and can be resumed via the existing
        # Phase 3.4 resume CLI. The resume completes the FORKED run,
        # not the original parent.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "A": {"type": "kolsch/nodes/compute", "value": "seed"},
                    "pause": {
                        "type": "kolsch/nodes/user_input",
                        "prompt": "say something",
                    },
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.said }}",
                    },
                },
                "graph": {
                    "edges": [
                        {"from": "A", "to": "pause"},
                        {
                            "from": "pause",
                            "to": "echo",
                            "binds": {"said": "{{ nodes.pause.response }}"},
                        },
                    ]
                },
            },
        )
        # Run parent: pauses at user_input.
        parent_id = _run_pipeline_via_cli(workspace, capsys)
        rc, _, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                parent_id,
                "--input",
                "response=parent-said",
            ],
            capsys=capsys,
        )
        assert rc == 0

        # Now fork at A's completed event; the forked run also
        # reaches the user_input pause.
        events = _events_via_cli(workspace, parent_id, capsys)
        a_completed = _find_event_id(
            events,
            kind="workflow.node.completed",
            preview_contains="node_name=A",
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
                a_completed,
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        # The fork paused mid-run; status is waiting_for_input.
        assert envelope["data"]["status"] == "waiting_for_input"
        forked_id = envelope["data"]["new_run_id"]

        # AC 2.7 — resume the FORKED run via the Phase 3.4 CLI; the
        # downstream node fires on the FORK, not on the parent.
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "resume",
                forked_id,
                "--input",
                "response=fork-said",
            ],
            capsys=capsys,
        )
        assert rc == 0
        resume_envelope = json.loads(out)
        assert resume_envelope["data"]["status"] == "succeeded"
        assert resume_envelope["data"]["run_id"] == forked_id
        assert resume_envelope["data"]["outputs"]["echo"]["value"] == "fork-said"

        # AC 2.7 reviewer note — the parent's terminal output is
        # untouched. Read the parent's JSONL directly (the events
        # CLI's payload_preview truncates at 50 chars and drops the
        # full output dict) and verify the parent's echo node
        # captured "parent-said", not "fork-said".
        import json as _json

        parent_jsonl = workspace / "runs" / parent_id / "events.jsonl"
        parent_events_after = [
            _json.loads(line)
            for line in parent_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        parent_echo_completion = next(
            e
            for e in parent_events_after
            if e.get("kind") == "workflow.node.completed"
            and (
                e.get("node_name") == "echo"
                or e.get("payload", {}).get("node_name") == "echo"
            )
        )
        assert parent_echo_completion["payload"]["output"]["value"] == "parent-said"


# ---------------------------------------------------------------------------
# AC 2.8 — re-fork chains correctly
# ---------------------------------------------------------------------------


class TestReForkChains:
    def test_refork_points_at_immediate_parent_not_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Fork once → forkA; fork forkA → forkB.
        # forkB.forked_from.run_id MUST equal forkA.run_id, NOT the
        # original root's id. Operator-visible via meta.json.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _pipeline_workflow())
        root_id = _run_pipeline_via_cli(workspace, capsys)
        root_events = _events_via_cli(workspace, root_id, capsys)
        root_b = _find_event_id(
            root_events,
            kind="workflow.node.completed",
            preview_contains="node_name=B",
        )

        # First fork.
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                root_id,
                "--at",
                root_b,
            ],
            capsys=capsys,
        )
        assert rc == 0
        fork_a_id = json.loads(out)["data"]["new_run_id"]

        # Second fork — uses fork_a as parent.
        fork_a_events = _events_via_cli(workspace, fork_a_id, capsys)
        fork_a_c = _find_event_id(
            fork_a_events,
            kind="workflow.node.completed",
            preview_contains="node_name=C",
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "fork",
                fork_a_id,
                "--at",
                fork_a_c,
            ],
            capsys=capsys,
        )
        assert rc == 0
        fork_b_id = json.loads(out)["data"]["new_run_id"]
        envelope_b = json.loads(out)
        # The fork envelope itself confirms parent_run_id is fork_a.
        assert envelope_b["data"]["parent_run_id"] == fork_a_id

        # AC 2.8 — meta.json on fork_b carries forked_from = fork_a,
        # not root.
        from workflows.ids import WorkflowRunId
        from workflows.run_store import WorkflowRunStore

        store = WorkflowRunStore(workspace)
        fork_b = store.read(WorkflowRunId(fork_b_id))
        assert fork_b.forked_from is not None
        assert str(fork_b.forked_from.run_id) == fork_a_id
        assert str(fork_b.forked_from.run_id) != root_id
