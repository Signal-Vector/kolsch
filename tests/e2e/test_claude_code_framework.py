"""spec-121 Test 8 — real-`claude` end-to-end through the tool bridge.

A `claude_code` agent that DECLARES a real kolsch tool (`read_file`),
run via `kolsch run`, drives a live `claude -p` agentic loop that
calls the tool THROUGH the T2 bridge (`--mcp-config` → `kolsch mcp
bridge` proxy → Unix socket → `invoke_observed`). Asserts the three
signals:

1. `tool.called` for `read_file` lands in the session JSONL (emitted
   by the bridge sink — ground truth, NOT the live stream).
2. `tool.result` carrying the seeded file content lands next to it.
3. The final `agent.message` reflects the tool result (the marker
   phrase only reachable by actually reading the file).

Module-level `pytestmark = pytest.mark.e2e` so the CI gate's
`-m "not e2e"` deselects it (mirrors spec-105/111/118). Requires a
logged-in `claude` (subscription auth); skips when the binary is
absent. Run manually:

    uv run pytest tests/e2e/test_claude_code_framework.py -m e2e -s
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

_CLAUDE_BINARY_ENV = "KOLSCH_MCP_E2E_CLAUDE_BINARY"

# A phrase that exists ONLY inside the seeded file — the model cannot
# produce it without the tool round-trip.
_MARKER = "the bridge fortune is PLUM-KOLSCH-77"


def _claude_binary() -> str | None:
    envp = os.environ.get(_CLAUDE_BINARY_ENV)
    if envp:
        return envp
    return shutil.which("claude")


def _setup_workspace(ws: Path) -> None:
    """`kolsch setup --non-interactive` + claude_code provider + agent."""
    setup = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env={**os.environ, "KOLSCH_WORKSPACE": str(ws)},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if setup.returncode != 0:
        pytest.skip(
            f"kolsch setup --non-interactive failed (rc={setup.returncode}); "
            f"stderr:\n{setup.stderr}"
        )

    # Connect the claude_code provider (keyless, subscription auth) —
    # the YAML-edit equivalent of `kolsch connect claude_code`.
    platform_yaml = ws / "platform.yaml"
    payload = yaml.safe_load(platform_yaml.read_text(encoding="utf-8"))
    payload.setdefault("llm_providers", {})["claude_code"] = {"type": "claude_code"}
    platform_yaml.write_text(yaml.safe_dump(payload), encoding="utf-8")

    # The agent: claude_code model + ONE real declared tool. read_file
    # is `allow` under setup's default rules (bash/write_file are
    # `prompt`, which structurally collapses to deny over the bridge).
    agent = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "agent_id": "ag_bridge_smoke",
        "name": "bridge_smoke",
        "description": "spec-121 e2e agent — claude_code with a real tool.",
        "model": {"provider": "claude_code", "model": "sonnet"},
        "io": {"mode": "conversational"},
        "prompt": (
            "You are a test agent. You have a read_file tool. When asked "
            "about the fortune file, you MUST call the read_file tool to "
            "read it and then repeat the exact phrase it contains."
        ),
        "subsystems": {
            "tools": {
                "variant": "standard",
                "config": {"declared": [{"ref": "kolsch/tools/read_file"}]},
            }
        },
    }
    agents_dir = ws / "bundles" / "kolsch" / "agents"
    (agents_dir / "bridge_smoke.yaml").write_text(
        yaml.safe_dump(agent), encoding="utf-8"
    )

    # Seed the file the tool must read.
    (ws / "data").mkdir(exist_ok=True)
    (ws / "data" / "fortune.txt").write_text(_MARKER + "\n", encoding="utf-8")


def _read_all_events(ws: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for jsonl in sorted((ws / "sessions").rglob("*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_real_claude_agent_calls_kolsch_tool_through_bridge(
    tmp_path: Path,
) -> None:
    """The T3 loop, live: `kolsch run` → engine AUTO-selects the
    subprocess framework → `claude -p --mcp-config` → `kolsch mcp
    bridge` → socket → `invoke_observed` → `read_file` executes →
    events in JSONL → final message carries the file's marker."""
    if not _claude_binary():
        pytest.skip(
            f"`claude` binary not on PATH — set {_CLAUDE_BINARY_ENV}=... "
            "to override, or install Claude Code to run the loop."
        )

    ws = tmp_path
    _setup_workspace(ws)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "--root",
            str(ws),
            "run",
            "kolsch/agents/bridge_smoke",
            "--input",
            (
                "content=Use your read_file tool to read data/fortune.txt "
                "and repeat the exact phrase it contains."
            ),
        ],
        env={**os.environ, "KOLSCH_WORKSPACE": str(ws)},
        capture_output=True,
        text=True,
        check=False,
        timeout=420,
    )
    assert proc.returncode == 0, (
        f"kolsch run failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    # (3) The final agent.message reflects the tool result.
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    final_text = payload["content"]["text"]
    assert "PLUM-KOLSCH-77" in final_text, (
        f"final message does not reflect the file content: {final_text!r}"
    )

    # (1) + (2) tool.called / tool.result in the JSONL ground truth.
    events = _read_all_events(ws)
    called = [
        e
        for e in events
        if e.get("type") == "tool.called"
        and e.get("data", {}).get("tool_name") == "read_file"
    ]
    results = [e for e in events if e.get("type") == "tool.result"]
    assert called, "no tool.called for read_file in the session JSONL"
    assert any("PLUM-KOLSCH-77" in json.dumps(e.get("data", {})) for e in results), (
        "no tool.result carrying the file content in the session JSONL"
    )

    # Evidence paste for the Complete report (run with -s).
    print("\n--- spec-121 e2e evidence ---")  # noqa: T201
    for e in events:
        if e.get("type") in {"tool.called", "tool.result"}:
            print(json.dumps(e))  # noqa: T201
    print(f"final agent.message text: {final_text!r}")  # noqa: T201
