"""Phase 3.1 Step 6 — end-to-end integration tests.

Two phase-level smoke tests that exercise the full executor stack
via the live CLI:

- **Phase Test 1**: ``setup --non-interactive`` a fresh workspace,
  then ``kolsch workflows run kolsch/workflows/hello``. Asserts the
  starter workflow exits 0 and the JSONL records the expected event
  sequence in topological order.
- **Phase Test 2**: cancel-mid-run. Schedules a workflow whose first
  node takes long enough for the harness to drop ``cancel.flag``;
  the executor aborts at the next node boundary. The JSONL ends
  with ``workflow.run.aborted``.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
import time
from typing import TYPE_CHECKING

from cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _run_cli(argv: list[str]) -> int:
    """Invoke ``cli.main.main`` directly so the test stays in-process."""
    return main(argv)


# ---------------------------------------------------------------------------
# Phase Test 1 — starter workflow end-to-end
# ---------------------------------------------------------------------------


def test_setup_then_run_starter_workflow_succeeds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6.3 — the starter workflow runs end-to-end.

    Set up a fresh workspace via the non-interactive scaffold, then
    invoke ``kolsch workflows run kolsch/workflows/hello``. Validate
    the JSONL records ``workflow.run.started`` first, paired
    ``workflow.node.{started,completed}`` for each node in topological
    order, and ``workflow.run.completed`` last.
    """
    workspace = tmp_path / "ws"
    rc = _run_cli(["--root", str(workspace), "setup", "--non-interactive"])
    assert rc == 0
    capsys.readouterr()  # drain setup output

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
    assert len(runs) == 1, f"expected exactly one run dir; got {[r.name for r in runs]}"
    events = _read_jsonl(runs[0] / "events.jsonl")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "workflow.run.started"
    assert kinds[-1] == "workflow.run.completed"
    # The starter's two nodes — ``ready`` (noop) then ``greet``
    # (authored echo) — each emit started + completed in topo order.
    per_node = [
        (e["payload"].get("node_name"), e["kind"])
        for e in events
        if e["kind"].startswith("workflow.node.")
    ]
    assert per_node == [
        ("ready", "workflow.node.started"),
        ("ready", "workflow.node.completed"),
        ("greet", "workflow.node.started"),
        ("greet", "workflow.node.completed"),
    ]


# ---------------------------------------------------------------------------
# Phase Test 2 — cancel-mid-run
# ---------------------------------------------------------------------------


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


_SLEEP_WORKFLOW_YAML = textwrap.dedent(
    """\
    description: "Long-running bash node — for cancel-mid-run test."
    nodes:
      slow:
        type: kolsch/nodes/bash
        command: "sleep 1.5"
      after:
        type: kolsch/nodes/bash
        command: "echo never reached"
    graph:
      edges:
        - from: slow
          to: after
    """
)


def _make_cancel_workspace(tmp_path: Path) -> Path:
    """Scaffold a minimal workspace with an allow-all bash rule.

    The starter's ``platform.yaml`` maps ``kolsch/bash`` to ``prompt``
    which collapses to ``deny`` on the headless CLI. Phase Test 2
    needs the bash node to actually fire so the cancel-flag poll can
    interrupt it at the next node boundary, so we ship a permissive
    rule here.
    """
    workspace = tmp_path / "ws"
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    (workspace / "platform.yaml").write_text(_ALLOW_ALL_PLATFORM_YAML, encoding="utf-8")
    bundle = workspace / "bundles" / "demo"
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: demo\nversion: "0.1"\ndescription: t\n', encoding="utf-8"
    )
    (bundle / "workflows" / "sleep.yaml").write_text(
        _SLEEP_WORKFLOW_YAML, encoding="utf-8"
    )
    return workspace


def test_cancel_mid_run_aborts_at_node_boundary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6.4 — start a long-running bash workflow, drop the cancel
    flag mid-run, assert the executor aborts at the next node
    boundary.

    The flag is dropped from a watchdog thread that polls the run
    directory and triggers as soon as the first event lands in
    ``events.jsonl``. The first node finishes its sleep, the
    executor emits ``workflow.node.completed``, observes the flag,
    and emits ``workflow.run.aborted`` — the second node never fires.
    """
    workspace = _make_cancel_workspace(tmp_path)

    cancel_signaled = asyncio.Event()

    def _watchdog() -> None:
        runs_dir = workspace / "runs"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if runs_dir.exists():
                children = list(runs_dir.iterdir())
                if children:
                    events_path = children[0] / "events.jsonl"
                    if events_path.exists() and events_path.stat().st_size > 0:
                        (children[0] / "cancel.flag").write_text("", encoding="utf-8")
                        cancel_signaled.set()
                        return
            time.sleep(0.05)
        cancel_signaled.set()  # avoid hanging on test timeout

    import threading

    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

    rc = _run_cli(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "demo/workflows/sleep",
        ]
    )
    t.join(timeout=10.0)
    captured = capsys.readouterr()
    # Non-success terminal — exit code 1 per ``_run_blocking``.
    assert rc == 1, f"expected non-zero exit; got {rc}. stderr: {captured.err}"
    runs = list((workspace / "runs").iterdir())
    assert len(runs) == 1
    events = _read_jsonl(runs[0] / "events.jsonl")
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "workflow.run.aborted"
    assert events[-1]["payload"]["reason"] == "user_cancel"
    # ``after`` must NEVER have fired.
    assert not any(
        e["kind"].startswith("workflow.node.")
        and e["payload"].get("node_name") == "after"
        for e in events
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    """Parse ``events.jsonl`` into a list of event dicts."""
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            rows.append(json.loads(stripped))
    return rows
