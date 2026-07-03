"""Phase 3.8 Step 0 — ``kolsch workflows events`` CLI tests.

Covers Step 0 ACs:

- 0.6 ``--format text`` (default) prints an event_id / kind / ts /
  payload table that includes every JSONL event.
- 0.7 ``--format json`` returns a well-formed envelope whose
  ``data.events`` is a list, each entry carrying ``event_id`` /
  ``kind`` / ``ts`` / ``payload_preview``.
- 0.8 missing run exits 1 with a ``WorkflowRunNotFound`` envelope.
- empty events: a freshly-minted run whose ``events.jsonl`` is empty
  returns ``0`` cleanly (no events listed).
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

from cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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
    from _yamlio import dump

    (wf_dir / f"{workflow_name}.yaml").write_text(dump(workflow_yaml), encoding="utf-8")
    return bundle


def _run_cli(
    args: list[str], *, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _run_noop_workflow(workspace: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run a single-noop workflow and return its run id."""
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
    return out.strip().splitlines()[0].split()[1]


# ---------------------------------------------------------------------------
# AC 0.6 — text format prints the event table
# ---------------------------------------------------------------------------


class TestEventsText:
    def test_lists_every_event_id_from_jsonl(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        run_id = _run_noop_workflow(workspace, capsys)

        # Read the JSONL straight from disk so the test owns the
        # canonical event_id set; the CLI output must contain ALL of
        # them.
        jsonl = workspace / "runs" / run_id / "events.jsonl"
        on_disk = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert on_disk, "noop workflow produced no events"

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "events",
                run_id,
            ],
            capsys=capsys,
        )
        assert rc == 0
        # AC 0.6 — header row + one line per event.
        assert "event_id" in out
        assert "kind" in out
        assert "ts" in out
        assert "payload" in out
        for event in on_disk:
            assert event["event_id"] in out
            assert event["kind"] in out

    def test_payload_preview_is_length_bounded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # AC 0.6 reviewer note — the preview must truncate cleanly
        # so wide payloads don't make the row unscannable. Run a bash
        # node that prints a long stdout; assert no single line of
        # the events table exceeds a sane width.
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "loud": {
                        "type": "kolsch/nodes/bash",
                        "command": "printf 'x%.0s' {1..500}",
                    }
                }
            },
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
                "events",
                run_id,
            ],
            capsys=capsys,
        )
        assert rc == 0
        # Every event line should be under a sane terminal width.
        # The handler caps preview at 50 chars; each row also carries
        # the event_id (~40), kind (~30), ts (~30), and three tab
        # separators. 200 chars is a comfortable upper bound.
        for line in out.splitlines():
            assert len(line) < 200, (
                f"event row exceeded preview cap: {len(line)} chars — {line!r}"
            )

    def test_run_not_found_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "events",
                "wrk_does_not_exist",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "WorkflowRunNotFound" in err or "not found" in err

    def test_run_with_no_events_returns_empty_table(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A run that exists (meta.json present) but has no JSONL
        # entries is a freshly-minted-but-never-executed state — the
        # ``store.create`` surface produces exactly this shape.
        from workflows.run_store import WorkflowRunStore

        workspace = _make_workspace(tmp_path)
        store = WorkflowRunStore(workspace)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "events",
                str(run.run_id),
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "no events" in out


# ---------------------------------------------------------------------------
# AC 0.7 — JSON format
# ---------------------------------------------------------------------------


class TestEventsJson:
    def test_returns_well_formed_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        run_id = _run_noop_workflow(workspace, capsys)

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
        envelope = json.loads(out)
        assert envelope["command"] == "workflows events"
        assert envelope["status"] == "ok"
        data = envelope["data"]
        assert data["run_id"] == run_id
        events = data["events"]
        assert isinstance(events, list)
        assert events, "noop workflow produced no events"
        for row in events:
            # Each row carries the four documented fields.
            assert set(row) >= {
                "event_id",
                "kind",
                "ts",
                "payload_preview",
            }

    def test_run_not_found_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "events",
                "wrk_does_not_exist",
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        assert envelope["errors"][0]["class"] == "WorkflowRunNotFound"

    def test_empty_events_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from workflows.run_store import WorkflowRunStore

        workspace = _make_workspace(tmp_path)
        store = WorkflowRunStore(workspace)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})

        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "events",
                str(run.run_id),
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["status"] == "ok"
        assert envelope["data"]["events"] == []
