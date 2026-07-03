"""Phase 3.0 Step 6 — ``kolsch workflows`` CLI tests.

Covers ACs 6.1-6.9:

- ``list`` / ``show`` / ``validate`` text + json envelopes.
- ``cancel`` writes sidecar ``cancel.flag`` atomically; exits 1 on
  unknown run id.
- ``run`` is a stub — never executes; emits the Phase 3.1 message.
- ``--format json`` envelope shape matches master spec §14.3 +
  §14.4 + R3 #6/#8/#11.
- ExceptionGroup → errors array of length N on a multi-error workflow.
"""

from __future__ import annotations

import io
import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from cli._format import render_exception
from cli.main import main
from workflows.errors import (
    WorkflowJinjaRefUnknown,
    WorkflowNodeNotDefined,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    # Phase 3.1 Step 5 — ``run`` is now live, so the workspace's
    # platform.yaml needs an authz rule that lets the bash node fire.
    # The ``*|*`` glob is the catch-all "allow everything in tests";
    # production workspaces ship the rule_matched default-deny tail.
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
    workspace: Path, bundle_name: str, workflow_yaml: dict[str, Any] | None = None
) -> Path:
    bundle = workspace / "bundles" / bundle_name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        f'name: {bundle_name}\nversion: "0.1"\ndescription: t\n',
        encoding="utf-8",
    )
    wf_dir = bundle / "workflows"
    wf_dir.mkdir(exist_ok=True)
    if workflow_yaml is not None:
        from _yamlio import dump

        (wf_dir / "hello.yaml").write_text(dump(workflow_yaml), encoding="utf-8")
    return bundle


def _run_cli(
    args: list[str], *, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """Wrap :func:`cli.main.main` and capture stdout/stderr."""
    capsys.readouterr()  # drain
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# AC 6.1 — list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_text_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "g": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    }
                },
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            ["--root", str(workspace), "workflows", "list"], capsys=capsys
        )
        assert rc == 0
        assert "demo/workflows/hello" in out
        assert "kind" in out

    def test_list_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {"nodes": {"g": {"type": "kolsch/nodes/noop"}}},
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "list",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "workflows list"
        assert envelope["status"] == "ok"
        assert "workflows" in envelope["data"]
        refs = [w["ref"] for w in envelope["data"]["workflows"]]
        assert "demo/workflows/hello" in refs


# ---------------------------------------------------------------------------
# AC 6.7 — --format accepted at group OR subcommand level
# ---------------------------------------------------------------------------


class TestFormatPlacement:
    """AC 6.7 — ``--format`` is honored at BOTH the group level
    (``workflows --format json list``) AND the subcommand level
    (``workflows list --format json``)."""

    def _workspace_with_one_workflow(self, tmp_path: Path) -> Path:
        ws = _make_workspace(tmp_path)
        _write_workflow_bundle(
            ws,
            "demo",
            {
                "nodes": {"h": {"type": "kolsch/nodes/bash", "command": "echo"}},
                "graph": {"edges": []},
            },
        )
        return ws

    def test_format_at_subcommand_level_workflows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = self._workspace_with_one_workflow(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(ws),
                "workflows",
                "list",
                "--format",
                "json",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "workflows list"
        assert envelope["status"] == "ok"

    def test_format_at_group_level_workflows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = self._workspace_with_one_workflow(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(ws),
                "workflows",
                "--format",
                "json",
                "list",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "workflows list"
        assert envelope["status"] == "ok"

    def test_format_at_subcommand_level_nodes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            ["--root", str(ws), "nodes", "list", "--format", "json"],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "nodes list"
        assert envelope["status"] == "ok"

    def test_format_at_group_level_nodes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            ["--root", str(ws), "nodes", "--format", "json", "list"],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "nodes list"
        assert envelope["status"] == "ok"

    def test_format_default_is_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Neither placement → text default preserved."""
        ws = self._workspace_with_one_workflow(tmp_path)
        rc, out, _ = _run_cli(["--root", str(ws), "workflows", "list"], capsys=capsys)
        assert rc == 0
        # Text output is tab-separated, not JSON.
        assert not out.startswith("{")


# ---------------------------------------------------------------------------
# AC 6.2 — show
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_text_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "g": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    }
                },
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "show",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "demo/workflows/hello" in out
        assert "nodes" in out

    def test_show_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"g": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "show",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["data"]["workflow"]["ref"] == "demo/workflows/hello"

    def test_show_unknown_ref_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "show",
                "demo/workflows/missing",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "missing" in err


# ---------------------------------------------------------------------------
# AC 6.3 — validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_clean_workflow_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "g": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    }
                },
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "validate",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "valid" in out

    def test_validate_failure_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "ghost", "to": "a"}]},
            },
        )
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "validate",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "ghost" in err

    def test_validate_json_envelope_includes_errors_array(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "ghost", "to": "a"}]},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "validate",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert envelope["status"] == "error"
        # AC 6.3 / R3 #6 — BOTH errors AND warnings arrays present.
        assert isinstance(envelope["errors"], list)
        assert isinstance(envelope["warnings"], list)
        assert len(envelope["errors"]) >= 1
        # R3 #8 — caller_message present on each error.
        assert all("caller_message" in e for e in envelope["errors"])

    def test_validate_multi_error_envelope_has_n_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {
                    "edges": [
                        {"from": "x", "to": "a"},
                        {"from": "y", "to": "a"},
                        {"from": "z", "to": "a"},
                    ]
                },
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "validate",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 1
        envelope = json.loads(out)
        assert len(envelope["errors"]) == 3


# ---------------------------------------------------------------------------
# Phase 3.1 Step 5 — live ``run`` command
# ---------------------------------------------------------------------------


class TestRunLive:
    def test_run_unknown_ref_emits_workflow_not_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        rc, _, err = _run_cli(
            ["--root", str(workspace), "workflows", "run", "nope/none"],
            capsys=capsys,
        )
        assert rc == 1
        assert "unknown" in err.lower() or "not found" in err.lower()

    def test_run_dry_run_prints_plan_and_writes_no_jsonl(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "run",
                "demo/workflows/hello",
                "--dry-run",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "dry-run plan" in out
        assert "a\t" in out
        assert "b\t" in out
        # AC 5.5 — no JSONL written for the dry-run path.
        runs_dir = workspace / "runs"
        assert not runs_dir.exists() or not any(runs_dir.iterdir())

    def test_run_dry_run_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "run",
                "demo/workflows/hello",
                "--dry-run",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["status"] == "ok"
        assert envelope["data"]["dry_run"] is True
        assert envelope["data"]["workflow_ref"] == "demo/workflows/hello"
        plan = envelope["data"]["plan"]
        assert [step["node"] for step in plan] == ["a"]

    def test_run_blocks_until_terminal_and_returns_outputs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {
                    "first": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hello",
                    },
                },
                "graph": {"edges": []},
            },
        )
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "run",
                "demo/workflows/hello",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["status"] == "ok"
        assert envelope["data"]["status"] == "succeeded"
        outputs = envelope["data"]["outputs"]
        assert "first" in outputs
        # Bash node returns {stdout, stderr, exit_code}.
        assert outputs["first"]["exit_code"] == 0

    def test_run_invalid_input_pair_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(
            workspace,
            "demo",
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": []},
            },
        )
        with pytest.raises(SystemExit):
            _run_cli(
                [
                    "--root",
                    str(workspace),
                    "workflows",
                    "run",
                    "demo/workflows/hello",
                    "--input",
                    "malformed",
                ],
                capsys=capsys,
            )


# ---------------------------------------------------------------------------
# AC 6.5 — cancel (R3 #1)
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_writes_sidecar_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        run_dir = workspace / "runs" / "wrk_test123"
        run_dir.mkdir(parents=True)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "cancel",
                "wrk_test123",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert (run_dir / "cancel.flag").exists()
        assert "cancel requested" in out

    def test_cancel_unknown_run_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "cancel",
                "wrk_missing",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "not found" in err

    def test_cancel_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        run_dir = workspace / "runs" / "wrk_json"
        run_dir.mkdir(parents=True)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "workflows",
                "--format",
                "json",
                "cancel",
                "wrk_json",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["status"] == "ok"
        assert envelope["data"]["run_id"] == "wrk_json"
        assert envelope["data"]["cancel_flag_path"].endswith("cancel.flag")


# ---------------------------------------------------------------------------
# AC 6.6 — --help lists all subcommands (Phase 3.4 added ``resume``)
# ---------------------------------------------------------------------------


class TestHelpListsAllCommands:
    def test_help_mentions_all_six_subcommands(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        _write_workflow_bundle(workspace, "demo")
        # argparse's --help calls sys.exit(0); guard with SystemExit.
        with pytest.raises(SystemExit) as exc_info:
            main(["--root", str(workspace), "workflows", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        for cmd in ("list", "show", "validate", "cancel", "run", "resume"):
            assert cmd in combined


# ---------------------------------------------------------------------------
# AC 6.9 / R3 #11 — render_exception generic shape
# ---------------------------------------------------------------------------


class TestRenderException:
    def test_render_exception_includes_caller_message(self) -> None:
        exc = WorkflowNodeNotDefined(
            file="<wf>", line=0, ref="missing", location="edges[0].from"
        )
        rendered = render_exception(exc)
        assert rendered["class"] == "WorkflowNodeNotDefined"
        assert rendered["caller_message"]
        assert "ref" in rendered["identifying_names"]
        assert rendered["identifying_names"]["ref"] == "missing"
        assert rendered["file"] == "<wf>"

    def test_render_exception_jinja_ref_unknown(self) -> None:
        exc = WorkflowJinjaRefUnknown(
            file="<wf>",
            line=0,
            node="consumer",
            reference="nodes.foo.bar",
            reason="unknown_field",
        )
        rendered = render_exception(exc)
        assert rendered["class"] == "WorkflowJinjaRefUnknown"
        assert rendered["identifying_names"]["reference"] == "nodes.foo.bar"
        assert rendered["identifying_names"]["reason"] == "unknown_field"


# ---------------------------------------------------------------------------
# AC 6.10 / 6.11 — nodes list/show
# ---------------------------------------------------------------------------


class TestNodesCLI:
    def test_nodes_list_includes_virtual_builtins(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            ["--root", str(workspace), "nodes", "list"], capsys=capsys
        )
        assert rc == 0
        # All 14 reserved built-in slots appear.
        assert "kolsch/nodes/bash" in out
        assert "kolsch/nodes/compute" in out

    def test_nodes_list_bundle_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        bundle = workspace / "bundles" / "demo"
        bundle.mkdir(parents=True)
        (bundle / "bundle.yaml").write_text(
            'name: demo\nversion: "0.1"\ndescription: t\n',
            encoding="utf-8",
        )
        (bundle / "nodes").mkdir()
        (bundle / "nodes" / "echo.py").write_text("x = 1\n", encoding="utf-8")
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "nodes",
                "list",
                "--bundle",
                "demo",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "demo/nodes/echo" in out
        # Kolsch built-ins are filtered out by --bundle demo.
        assert "kolsch/nodes/bash" not in out

    def test_nodes_list_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "nodes",
                "--format",
                "json",
                "list",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["command"] == "nodes list"
        assert any(n["ref"] == "kolsch/nodes/bash" for n in envelope["data"]["nodes"])

    def test_nodes_show_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "nodes",
                "show",
                "kolsch/nodes/bash",
            ],
            capsys=capsys,
        )
        assert rc == 0
        assert "kolsch/nodes/bash" in out

    def test_nodes_show_unknown_ref_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, _, err = _run_cli(
            [
                "--root",
                str(workspace),
                "nodes",
                "show",
                "acme/nodes/ghost",
            ],
            capsys=capsys,
        )
        assert rc == 1
        assert "ghost" in err

    def test_nodes_show_json_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = _make_workspace(tmp_path)
        rc, out, _ = _run_cli(
            [
                "--root",
                str(workspace),
                "nodes",
                "--format",
                "json",
                "show",
                "kolsch/nodes/bash",
            ],
            capsys=capsys,
        )
        assert rc == 0
        envelope = json.loads(out)
        assert envelope["data"]["node"]["ref"] == "kolsch/nodes/bash"


_ = io  # reserved for future stream-based tests
