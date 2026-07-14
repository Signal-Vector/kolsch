"""Phase 3.4 Step 3 — Phase Tests 1-6 wired end-to-end via the CLI.

The build doc's Phase-level Integration Tests section lists six
scenarios that must pass for Phase 3.4 to ship. Each test below
maps 1:1 to a numbered Phase Test from
``process/phase3.4-user-input-build.md``:

- Phase Test 1 — single pause + resume via CLI (uses
  ``hello-paused`` from the starter bundle).
- Phase Test 2 — sequential pauses (two ``user_input`` nodes).
- Phase Test 3 — resume rejected when run isn't ``waiting_for_input``.
- Phase Test 4 — replay reconstructs ``_captured`` (a downstream
  node consumes ``{{ nodes.<paused>.response }}`` correctly after
  resume).
- Phase Test 5 — bad resume input preserves the paused status so
  the operator's retry succeeds.
- Phase Test 6 — orphan sweep skips ``waiting_for_input`` runs
  even when the recorded PID is dead.

Phase Test 7 (full-suite regression) runs at CI; nothing to wire
here. The tests below intentionally exercise the CLI surface
(``cli.main.main``) so the cross-process pause/resume contract is
covered end-to-end (subparser → dispatcher → handler → executor →
JSONL → resume process → terminal envelope).
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from cli.main import main

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Workspace fixture — installs the starter bundle so the e2e tests
# can run the same workflows the operator does.
# ---------------------------------------------------------------------------


def _make_workspace_with_starter(tmp_path: Path) -> Path:
    """Build a workspace and seed the starter ``kolsch`` bundle.

    The starter bundle ships ``hello-paused.yaml`` +
    ``two-pauses.yaml`` + the ``kolsch/nodes/echo`` authored node
    these workflows depend on. Re-using the actual starter sources
    instead of inline YAML keeps the e2e tests testing the SAME
    artifacts the operator runs.
    """
    import shutil

    for sub in (
        "bundles",
        "sessions",
        "content",
        "logs",
        "data",
        ".platform",
    ):
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
    # Copy the starter bundle's ``kolsch/`` subtree into the
    # workspace's ``bundles/``. The starter source lives at
    # ``src/cli/_starter_bundle/kolsch``.
    repo_root = Path(__file__).resolve().parents[2]
    starter_src = repo_root / "src" / "cli" / "_starter_bundle" / "kolsch"
    dest = tmp_path / "bundles" / "kolsch"
    shutil.copytree(starter_src, dest)
    return tmp_path


def _run_cli(
    args: list[str], *, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _pause_via_cli(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    workflow_ref: str,
    inputs: list[str] | None = None,
) -> str:
    """Run a workflow expected to pause; return the run id."""
    args = ["--root", str(workspace), "workflows", "run", workflow_ref]
    for pair in inputs or []:
        args.extend(["--input", pair])
    rc, out, _ = _run_cli(args, capsys=capsys)
    assert rc == 0, f"initial pause should exit 0; got rc={rc}"
    first_line = out.strip().splitlines()[0]
    # "run wrk_<uuid> waiting_for_input"
    parts = first_line.split()
    assert parts[0] == "run"
    assert parts[2] == "waiting_for_input"
    return parts[1]


def _resume_via_cli(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    run_id: str,
    response: str,
    fmt: str = "text",
) -> tuple[int, str, str]:
    args = ["--root", str(workspace)]
    if fmt == "json":
        args.extend(["workflows", "--format", "json", "resume"])
    else:
        args.extend(["workflows", "resume"])
    args.extend([run_id, "--input", f"response={response}"])
    return _run_cli(args, capsys=capsys)


# ---------------------------------------------------------------------------
# Phase Test 1 — single pause + resume via the CLI
# ---------------------------------------------------------------------------


class TestPhaseTest1SinglePauseAndResume:
    def test_single_pause_and_resume_via_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace_with_starter(tmp_path)

        run_id = _pause_via_cli(
            workspace, capsys, workflow_ref="kolsch/workflows/hello-paused"
        )

        # Sidecar marks the open invocation; meta.json status is
        # ``waiting_for_input``.
        run_dir = workspace / "runs" / run_id
        assert (run_dir / "waiting_at").exists()

        rc, out, _ = _resume_via_cli(workspace, capsys, run_id=run_id, response="world")
        assert rc == 0
        assert "succeeded" in out
        # The downstream ``echo`` node's captured message lifts the
        # resumed response.
        assert "world" in out
        # ``waiting_at`` sidecar removed at terminal post-resume.
        assert not (run_dir / "waiting_at").exists()
        # JSONL is one continuous stream spanning both processes.
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        kinds = [
            json.loads(line)["kind"] for line in events.splitlines() if line.strip()
        ]
        assert "workflow.run.started" in kinds
        assert "workflow.run.resumed" in kinds
        assert kinds[-1] == "workflow.run.completed"


# ---------------------------------------------------------------------------
# Phase Test 2 — sequential pauses
# ---------------------------------------------------------------------------


class TestPhaseTest2SequentialPauses:
    def test_two_sequential_user_input_nodes_complete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace_with_starter(tmp_path)

        run_id = _pause_via_cli(
            workspace, capsys, workflow_ref="kolsch/workflows/two-pauses"
        )

        # First resume — workflow pauses AGAIN at ask2 (exit 0).
        rc, out, _ = _resume_via_cli(workspace, capsys, run_id=run_id, response="alpha")
        assert rc == 0
        assert "waiting_for_input" in out

        # Second resume — workflow reaches terminal.
        rc, out, _ = _resume_via_cli(workspace, capsys, run_id=run_id, response="beta")
        assert rc == 0
        assert "succeeded" in out
        # The final ``done`` node's captured message blends both
        # responses (per ``two-pauses.yaml``'s bind).
        assert "alpha+beta" in out


# ---------------------------------------------------------------------------
# Phase Test 3 — resume rejected when run isn't waiting
# ---------------------------------------------------------------------------


class TestPhaseTest3ResumeRejectedWhenNotWaiting:
    def test_resume_on_succeeded_run_rejects_with_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace_with_starter(tmp_path)
        # Run the non-paused starter ``hello`` workflow to terminal.
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "kolsch/workflows/hello",
                "--input",
                "message=hi",
            ],
            capsys=capsys,
        )
        assert rc == 0
        run_id = out.strip().splitlines()[0].split()[1]
        # Pre-resume JSONL line count.
        events_path = workspace / "runs" / run_id / "events.jsonl"
        pre_lines = events_path.read_text(encoding="utf-8").splitlines()

        rc, _, err = _resume_via_cli(workspace, capsys, run_id=run_id, response="x")
        assert rc == 1
        assert "WorkflowResumeRejected" in err

        # JSONL unchanged — no spurious resumed/completed events.
        post_lines = events_path.read_text(encoding="utf-8").splitlines()
        assert pre_lines == post_lines


# ---------------------------------------------------------------------------
# Phase Test 4 — replay reconstructs captured state
# ---------------------------------------------------------------------------


class TestPhaseTest4ReplayReconstructsState:
    def test_downstream_node_reads_pre_pause_captured_via_jinja(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The starter ``hello-paused`` workflow is ``ask → echo``
        # (two nodes only). For this phase test we need a workflow
        # where a node BEFORE the pause produces output a node
        # AFTER the pause consumes — proving replay of the pre-pause
        # ``workflow.node.completed`` rehydrates ``_captured``.
        #
        # We use ``kolsch/nodes/bash`` for the pre-pause node so its
        # output schema (``{stdout, stderr, exit_code}``) is
        # introspectable by the validator's jinja-refs layer.
        # Authored Python nodes (``CatalogNodeDef``) carry
        # ``not_declared`` for their cross-node references at v1
        # (see ``validator._check_value_for_refs``).
        workspace = _make_workspace_with_starter(tmp_path)
        wf_dir = workspace / "bundles" / "kolsch" / "workflows"
        from kolsch._yamlio import dump

        (wf_dir / "prep-pause-echo.yaml").write_text(
            dump(
                {
                    "description": "Phase Test 4 — prep → ask → echo.",
                    "nodes": {
                        "prep": {
                            "type": "kolsch/nodes/bash",
                            "command": "echo before",
                        },
                        "ask": {
                            "type": "kolsch/nodes/user_input",
                            "prompt": "ok?",
                        },
                        "echo": {"type": "kolsch/nodes/echo"},
                    },
                    "graph": {
                        "edges": [
                            {"from": "prep", "to": "ask"},
                            {
                                "from": "ask",
                                "to": "echo",
                                "binds": {
                                    "message": (
                                        "{{ nodes.prep.stdout | trim }}-"
                                        "{{ nodes.ask.response }}"
                                    ),
                                },
                            },
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        run_id = _pause_via_cli(
            workspace,
            capsys,
            workflow_ref="kolsch/workflows/prep-pause-echo",
        )

        rc, out, _ = _resume_via_cli(workspace, capsys, run_id=run_id, response="after")
        assert rc == 0
        # The echo node's captured message blends pre-pause (prep's
        # bash stdout) with post-resume (ask.response). Confirms
        # replay restored ``_captured["prep"]`` for the post-resume
        # Jinja render.
        assert "before-after" in out


# ---------------------------------------------------------------------------
# Phase Test 5 — bad resume input preserves paused status
# ---------------------------------------------------------------------------


class TestPhaseTest5BadInputPreservesStatus:
    def test_bad_then_good_resume_retry_succeeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace_with_starter(tmp_path)
        run_id = _pause_via_cli(
            workspace,
            capsys,
            workflow_ref="kolsch/workflows/hello-paused",
        )

        # Bad attempt — wrong key.
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "resume",
                run_id,
                "--input",
                "wrong=x",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "WorkflowInputInvalid" in err

        # Run still paused — sidecar present, meta.json status
        # unchanged.
        run_dir = workspace / "runs" / run_id
        assert (run_dir / "waiting_at").exists()

        # Retry with correct input — succeeds.
        rc, out, _ = _resume_via_cli(
            workspace, capsys, run_id=run_id, response="recovered"
        )
        assert rc == 0
        assert "succeeded" in out
        assert "recovered" in out


# ---------------------------------------------------------------------------
# Phase Test 6 — orphan sweep skips waiting_for_input runs
# ---------------------------------------------------------------------------


class TestPhaseTest6OrphanSweepSkipsPaused:
    def test_paused_run_with_dead_pid_not_reconciled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace_with_starter(tmp_path)
        run_id = _pause_via_cli(
            workspace,
            capsys,
            workflow_ref="kolsch/workflows/hello-paused",
        )
        # Plant a guaranteed-dead PID so the sweep's pid-aliveness
        # probe would otherwise reconcile this run.
        run_dir = workspace / "runs" / run_id
        (run_dir / "pid").write_text("99999", encoding="utf-8")

        from workflows.orphan_sweep import sweep
        from workflows.run_store import WorkflowRunStore

        store = WorkflowRunStore(workspace)
        reconciled = sweep(store)
        assert reconciled == []

        from workflows.ids import WorkflowRunId
        from workflows.run import WorkflowRunStatus

        loaded = store.read(WorkflowRunId(run_id))
        assert loaded.status == WorkflowRunStatus.WAITING_FOR_INPUT

    def test_resume_after_orphan_sweep_still_works(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Regression guard — after the sweep correctly leaves the
        # paused run alone, the operator's resume must still complete
        # the run. Confirms the sweep's "skip" doesn't damage state.
        workspace = _make_workspace_with_starter(tmp_path)
        run_id = _pause_via_cli(
            workspace,
            capsys,
            workflow_ref="kolsch/workflows/hello-paused",
        )
        (workspace / "runs" / run_id / "pid").write_text("99999", encoding="utf-8")

        # Sweep runs implicitly as part of the next CLI invocation
        # (the main entrypoint reconciles on startup). The resume
        # CLI call below triggers that path; the sweep skips the
        # paused run and the resume proceeds normally.
        rc, out, _ = _resume_via_cli(
            workspace, capsys, run_id=run_id, response="post-sweep"
        )
        assert rc == 0
        assert "succeeded" in out
        assert "post-sweep" in out
        # And the resume process's PID landed in the sidecar.
        pid_written = (
            (workspace / "runs" / run_id / "pid").read_text(encoding="utf-8").strip()
        )
        assert pid_written == str(os.getpid())
