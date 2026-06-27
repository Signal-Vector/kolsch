"""Phase 3.4 Step 2 — ``kolsch workflows resume`` CLI + orphan sweep tests.

Covers Step 2 ACs:

- 2.1 — ``kolsch workflows resume <run_id> --input k=v`` parses +
  dispatches; reuses ``_parse_input_pairs``.
- 2.2 — successful resume exits 0; rejected / invalid resume exits 1
  with the right error class in the envelope.
- 2.3 — ``--format text`` (default) + ``--format json`` both work
  for success and error paths.
- 2.4 — PID sidecar atomically refreshed on resume.
- 2.5 — orphan sweep skips ``WAITING_FOR_INPUT`` runs even when
  the recorded PID is dead.
- 2.6 — verified separately in :mod:`tests.component.test_workflow_cli`
  (the help test there asserts ``resume`` appears alongside the rest).
- 2.7 — full-suite regression check runs at CI; this file's own
  tests don't regress anything by construction.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from cli.main import main

# ---------------------------------------------------------------------------
# Workspace fixture — mirrors tests/component/test_workflow_cli.py's pattern
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
    from _yamlio import dump  # noqa: PLC0415

    (wf_dir / f"{workflow_name}.yaml").write_text(
        dump(workflow_yaml), encoding="utf-8"
    )
    return bundle


def _run_cli(
    args: list[str], *, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _pause_workflow_via_cli(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    workflow_ref: str = "demo/workflows/hello",
) -> str:
    """Run the workflow + parse the run id from stdout.

    Pause-able workflows produce ``run <wrk_id> waiting_for_input``
    as the first stdout line; the helper splits the token out.
    """
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
    assert rc == 0, f"expected the initial pause to exit 0; got rc={rc}"
    first_line = out.strip().splitlines()[0]
    # first_line: "run wrk_<uuid> waiting_for_input"
    parts = first_line.split()
    assert parts[0] == "run"
    assert parts[2] == "waiting_for_input"
    return parts[1]


def _paused_workflow_yaml() -> dict[str, Any]:
    return {
        "nodes": {
            "ask": {
                "type": "kolsch/nodes/user_input",
                "prompt": "Tell me something",
            }
        }
    }


def _paused_then_bash_workflow_yaml() -> dict[str, Any]:
    """Workflow that pauses, then runs a downstream bash node so the
    resume's full-pipeline rendering (final outputs) is exercised."""
    return {
        "nodes": {
            "ask": {
                "type": "kolsch/nodes/user_input",
                "prompt": "Say something",
            },
            "echo": {
                "type": "kolsch/nodes/bash",
                "command": "echo got={{ inputs.r }}",
            },
        },
        "graph": {
            "edges": [
                {
                    "from": "ask",
                    "to": "echo",
                    "binds": {"r": "{{ nodes.ask.response }}"},
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# AC 2.1 + 2.2 (success path) — happy path: pause via CLI, resume via CLI
# ---------------------------------------------------------------------------


class TestResumeCliHappyPath:
    def test_resume_completes_workflow_exit_zero_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace, "demo", _paused_then_bash_workflow_yaml()
        )
        run_id = _pause_workflow_via_cli(workspace, capsys)

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "response=hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        # Default text-format output names the final status + outputs.
        assert "succeeded" in out
        # The downstream bash node ran with the resumed response.
        assert "got=hello" in out

    def test_resume_completes_workflow_exit_zero_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace, "demo", _paused_then_bash_workflow_yaml()
        )
        run_id = _pause_workflow_via_cli(workspace, capsys)

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "resume",
                run_id,
                "--input",
                "response=world",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "workflows resume"
        assert envelope["status"] == "ok"
        assert envelope["data"]["run_id"] == run_id
        assert envelope["data"]["status"] == "succeeded"
        # Echo node's stdout lands under outputs["echo"]["stdout"].
        outputs = envelope["data"]["outputs"]
        assert "echo" in outputs
        assert "world" in outputs["echo"]["stdout"]


# ---------------------------------------------------------------------------
# AC 2.2 (error path) — wrong status rejects
# ---------------------------------------------------------------------------


class TestResumeWrongStatus:
    def test_resume_on_missing_run_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _paused_workflow_yaml())
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                "wrk_does_not_exist",
                "--input",
                "response=x",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "not found" in err

    def test_resume_on_succeeded_run_rejects_with_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        # A workflow that completes immediately — no pause path.
        _write_workflow_bundle(
            workspace,
            "demo",
            {"nodes": {"n": {"type": "kolsch/nodes/noop"}}},
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        # Pick the run id from stdout (line starts "run wrk_...
        # succeeded").
        run_id = out.strip().splitlines()[0].split()[1]

        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "response=x",
            ],
            capsys=capsys,
        )
        assert rc == 1
        # The error envelope names the typed exception.
        assert "WorkflowResumeRejected" in err

    def test_resume_on_succeeded_run_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {"nodes": {"n": {"type": "kolsch/nodes/noop"}}},
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        run_id = out.strip().splitlines()[0].split()[1]

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "resume",
                run_id,
                "--input",
                "response=x",
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        assert envelope["errors"][0]["class"] == "WorkflowResumeRejected"


# ---------------------------------------------------------------------------
# AC 2.2 — bad input shape rejects
# ---------------------------------------------------------------------------


class TestResumeBadInput:
    def test_resume_missing_response_key_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _paused_workflow_yaml())
        run_id = _pause_workflow_via_cli(workspace, capsys)

        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "wrong_key=x",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "WorkflowInputInvalid" in err

    def test_resume_missing_response_preserves_paused_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _paused_workflow_yaml())
        run_id = _pause_workflow_via_cli(workspace, capsys)

        # Bad attempt.
        rc, _, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "nope=x",
            ],
            capsys=capsys,
        )
        assert rc == 1

        # Retry with correct input — should succeed.
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "response=hi",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "succeeded" in out


# ---------------------------------------------------------------------------
# AC 2.4 — PID sidecar atomically refreshed on resume
# ---------------------------------------------------------------------------


class TestPidRefresh:
    def test_resume_writes_current_pid_to_sidecar(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo", _paused_workflow_yaml())
        run_id = _pause_workflow_via_cli(workspace, capsys)

        # The pre-pause PID file points at the test runner's PID (the
        # pause ran in-process). Overwrite it with a known sentinel so
        # we can see the resume's refresh.
        pid_path = workspace / "runs" / run_id / "pid"
        pid_path.write_text("99999", encoding="utf-8")

        rc, _, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "response=x",
            ],
            capsys=capsys,
        )
        assert rc == 0

        # After resume the PID file holds the current process's PID
        # (the test runner) — i.e., the sentinel is gone.
        written = pid_path.read_text(encoding="utf-8").strip()
        assert written == str(os.getpid())
        # No tmp leftover from the atomic write.
        assert not pid_path.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# AC 2.5 — orphan sweep skips waiting_for_input runs
# ---------------------------------------------------------------------------


class TestOrphanSweepSkipsPaused:
    def test_paused_run_with_dead_pid_is_not_reconciled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Pause a workflow via the CLI so meta.json + waiting_at +
        # JSONL are all coherent.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace, "demo", _paused_workflow_yaml()
        )
        run_id = _pause_workflow_via_cli(workspace, capsys)

        # Plant a guaranteed-dead PID in the sidecar so the orphan
        # sweep's _pid_alive probe returns False; if the sweep's
        # status gate is broken, it would reconcile this run to
        # ``failed``.
        run_dir = workspace / "runs" / run_id
        (run_dir / "pid").write_text("99999", encoding="utf-8")

        from workflows.orphan_sweep import sweep  # noqa: PLC0415
        from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

        store = WorkflowRunStore(workspace)
        reconciled = sweep(store)
        assert reconciled == []

        # Run still reads as WAITING_FOR_INPUT.
        from workflows.ids import WorkflowRunId  # noqa: PLC0415
        from workflows.run import WorkflowRunStatus  # noqa: PLC0415

        loaded = store.read(WorkflowRunId(run_id))
        assert loaded.status == WorkflowRunStatus.WAITING_FOR_INPUT

    def test_non_paused_orphaned_run_still_reconciled(
        self, tmp_path: Path
    ) -> None:
        # Regression guard: the paused-skip check must not absolve
        # NON-paused orphans. A run with ``workflow.run.started`` +
        # no terminal + a dead PID + status != waiting_for_input
        # should still be reconciled to ``failed``.
        from workflows.ids import TraceId, WorkflowRunId  # noqa: PLC0415
        from workflows.orphan_sweep import sweep  # noqa: PLC0415
        from workflows.run import WorkflowRun, WorkflowRunStatus  # noqa: PLC0415
        from workflows.run_store import WorkflowRunStore  # noqa: PLC0415
        import uuid  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415

        workspace = tmp_path
        store = WorkflowRunStore(workspace)
        run_id = WorkflowRunId(f"wrk_{uuid.uuid4()}")
        run_dir = store.directory(run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        run = WorkflowRun(
            run_id=run_id,
            trace_id=TraceId(f"tr_{uuid.uuid4()}"),
            workflow_ref="demo/workflows/x",
            status=WorkflowRunStatus.RUNNING,
            created_at=datetime.now(UTC),
        )
        run_dir.meta_path.write_text(
            run.model_dump_json(), encoding="utf-8"
        )
        store.append_event(
            run_id,
            {"kind": "workflow.run.started", "payload": {}},
        )
        # Dead PID.
        run_dir.pid_path.write_text("99999", encoding="utf-8")

        reconciled = sweep(store)
        assert reconciled == [run_id]
