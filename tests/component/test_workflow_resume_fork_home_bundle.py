"""bug-141 — built-in tools keep their canonical action on resume / fork.

`kolsch workflows run` stamps every built-in tool definition with
`home_bundle="kolsch"`, so authz sees `kolsch/tools/<name>`. The resume
and fork CLI paths used to build their own unstamped copies, so the
same node was authorised as the legacy `tool.<name>`, matched no
`kolsch/tools/*` rule, and fell through to the default-deny tail.

The workspace here allows ONLY `kolsch/tools/read_file` above the
protected default-deny tail — no catch-all allow — so a wrong action
string fails the node with `tool.unauthorized`.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.main import main

if TYPE_CHECKING:
    import pytest

_STARTER_BUNDLE = (
    Path(__file__).resolve().parents[2] / "src" / "cli" / "_starter_bundle" / "kolsch"
)

_RULES = """\
secrets: {variant: dotenv, config: {path: .env}}
sandbox: {variant: native, config: {}}
authorization:
  variant: rule_based
  config:
    rules:
      - {what: "kolsch/tools/read_file", pattern: "*", effect: "allow"}
      - {what: "*", pattern: "*", effect: "deny"}
storage: {entity_backend: sqlite, entity_path: .platform/entities.db}
event_bus: {backend: in_process}
file_access: {allowed_paths: ["data/"], denied_paths: [".env"]}
defaults: {}
"""


def _workspace(tmp_path: Path, workflow: dict[str, Any]) -> Path:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(textwrap.dedent(_RULES), encoding="utf-8")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "data" / "note.txt").write_text("hello from data", encoding="utf-8")
    # The kolsch bundle, as `kolsch setup` materialises it, so the
    # `kolsch/tools/read_file` rule resolves against the catalog.
    shutil.copytree(_STARTER_BUNDLE, tmp_path / "bundles" / "kolsch")
    bundle = tmp_path / "bundles" / "demo"
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: demo\nversion: "0.1"\ndescription: t\n', encoding="utf-8"
    )
    from kolsch._yamlio import dump

    (bundle / "workflows" / "wf.yaml").write_text(dump(workflow), encoding="utf-8")
    return tmp_path


def _cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    rc = main(args)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _events(root: Path, run_id: str) -> list[dict[str, Any]]:
    path = root / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _run_id(out: str) -> str:
    # "run wrk_<uuid> <status>" or "forked run wrk_<uuid> <status>"
    first = out.strip().splitlines()[0].split()
    return next(tok for tok in first if tok.startswith("wrk_"))


def _assert_read_node_authorised_canonically(events: list[dict[str, Any]]) -> None:
    decisions = [
        e["payload"]
        for e in events
        if e["kind"] == "policy.decision.made"
        and "read_file" in str(e["payload"].get("action"))
    ]
    assert decisions, "no policy decision recorded for the read_file node"
    assert [d["action"] for d in decisions] == ["kolsch/tools/read_file"]
    assert decisions[0]["effect"] == "allow"
    completed = [
        e
        for e in events
        if e["kind"] == "workflow.node.completed" and e.get("node_name") == "read"
    ]
    assert completed, [e["kind"] for e in events]
    assert "hello from data" in completed[0]["payload"]["output"]["content"]
    assert not [e for e in events if e["kind"] == "workflow.node.failed"]


_PAUSE_THEN_READ = {
    "nodes": {
        "ask": {"type": "kolsch/nodes/user_input", "prompt": "go?"},
        "read": {"type": "kolsch/nodes/read_file", "path": "data/note.txt"},
    },
    "graph": {"edges": [{"from": "ask", "to": "read"}]},
}

_NOOP_THEN_READ = {
    "nodes": {
        "start": {"type": "kolsch/nodes/noop"},
        "read": {"type": "kolsch/nodes/read_file", "path": "data/note.txt"},
    },
    "graph": {"edges": [{"from": "start", "to": "read"}]},
}


def test_resume_home_bundle_canonical_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path, _PAUSE_THEN_READ)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 0
    run_id = _run_id(out)
    assert "waiting_for_input" in out

    rc, out = _cli(
        [
            "--root",
            str(root),
            "workflows",
            "resume",
            run_id,
            "--input",
            "response=yes",
        ],
        capsys,
    )
    _assert_read_node_authorised_canonically(_events(root, run_id))
    assert rc == 0, out


def test_fork_home_bundle_canonical_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path, _NOOP_THEN_READ)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 0, out
    parent_id = _run_id(out)
    fork_at = next(
        e["event_id"]
        for e in _events(root, parent_id)
        if e["kind"] == "workflow.node.completed" and e.get("node_name") == "start"
    )

    rc, out = _cli(
        ["--root", str(root), "workflows", "fork", parent_id, "--at", fork_at],
        capsys,
    )
    forked = next(p.name for p in (root / "runs").iterdir() if p.name != parent_id)
    _assert_read_node_authorised_canonically(_events(root, forked))
    assert rc == 0, out


def test_builtin_tool_defns_stamped_on_all_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tool-definition kwarg `register_builtins` receives on run,
    resume and fork is stamped `kolsch`, and the three sets are equal."""
    import cli._workflow_runtime as runtime_mod
    import workflows.builtins as builtins_mod

    calls: list[dict[str, Any]] = []
    real = builtins_mod.register_builtins

    def _spy(registry: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        real(registry, **kwargs)

    monkeypatch.setattr(builtins_mod, "register_builtins", _spy)
    monkeypatch.setattr(runtime_mod, "register_builtins", _spy)

    root = _workspace(tmp_path, _PAUSE_THEN_READ)
    rc, out = _cli(
        ["--root", str(root), "workflows", "run", "demo/workflows/wf"], capsys
    )
    assert rc == 0
    run_id = _run_id(out)
    rc, _ = _cli(
        ["--root", str(root), "workflows", "resume", run_id, "--input", "response=y"],
        capsys,
    )
    assert rc == 0
    fork_at = next(
        e["event_id"]
        for e in _events(root, run_id)
        if e["kind"] == "workflow.run.started"
    )
    rc, out = _cli(
        ["--root", str(root), "workflows", "fork", run_id, "--at", fork_at],
        capsys,
    )
    assert rc == 0, out

    assert len(calls) == 3, "expected one register_builtins call per path"
    defn_sets = []
    for path_name, kwargs in zip(("run", "resume", "fork"), calls, strict=True):
        defns = {k: v for k, v in kwargs.items() if k.endswith("_defn")}
        assert "jev_decide_defn" in defns, path_name
        unstamped = sorted(k for k, d in defns.items() if d.home_bundle != "kolsch")
        assert unstamped == [], f"{path_name}: unstamped {unstamped}"
        defn_sets.append(set(defns))
    assert defn_sets[0] == defn_sets[1] == defn_sets[2]
