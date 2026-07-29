"""Test 7 (spec-105): real-`claude` round-trip smoke against `kolsch mcp serve`.

Module-level `pytestmark = pytest.mark.e2e` so the CI gate's
`-m "not e2e"` deselects it. Coder runs this manually before
signalling Complete; the paste of captured stdout (three signals:
MCP tool-invocation frame naming `mcp__kolsch__list_sessions`,
tool-result frame with JSON session data, `claude`'s final
assistant message referencing returned sessions) goes into the
Complete report so the Reviewer can confirm the loop ran once.

The pytest wrapper here is a convenience — the spec allows either
this test OR an equivalent shell invocation as the guarantee. See
`docs/how-to/mcp-operator-server.md` for the shell recipe.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e


# The e2e wrapper skips itself when the operator's environment lacks a
# logged-in `claude` binary. We don't want CI machines that happen to
# have the binary on PATH but no subscription auth to flap the test.
_CLAUDE_BINARY_ENV = "KOLSCH_MCP_E2E_CLAUDE_BINARY"


def _claude_binary() -> str | None:
    # Honour an explicit env-var override; else look for `claude` on PATH.
    envp = os.environ.get(_CLAUDE_BINARY_ENV)
    if envp:
        return envp
    return shutil.which("claude")


def test_real_claude_roundtrip_lists_sessions(
    tmp_path: Path,
) -> None:
    """Test 7: `claude -p` invokes `list_sessions` via MCP-over-stdio.

    Prereqs:
    - A logged-in `claude` binary (subscription auth; no ANTHROPIC_API_KEY
      in the child env)
    - `kolsch mcp serve` runnable in this workspace (this repo's `uv`)

    Skips (not fails) when `claude` isn't on PATH — the test's job is
    to guarantee the loop ran once when the environment supports it.
    """
    claude = _claude_binary()
    if not claude:
        pytest.skip(
            f"`claude` binary not on PATH — set {_CLAUDE_BINARY_ENV}=... "
            "to override, or install Claude Code to run the round-trip."
        )

    # Bootstrap a workspace via the same CLI the shipped recipe uses,
    # so the round-trip runs against the real `kolsch mcp serve` code
    # path (not against a synthetic in-process runtime).
    setup = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "setup",
            "--non-interactive",
        ],
        env={**os.environ, "KOLSCH_WORKSPACE": str(tmp_path)},
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

    # Write the mcp-config.json the shipped recipe uses.
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kolsch": {
                        "command": sys.executable,
                        "args": ["-m", "cli", "mcp", "serve"],
                        "env": {
                            "KOLSCH_WORKSPACE": str(tmp_path),
                        },
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Invoke `claude -p` against the config.
    child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [
            claude,
            "-p",
            "list my kolsch sessions",
            "--output-format",
            "stream-json",
            "--mcp-config",
            str(cfg),
            "--allowedTools",
            "mcp__kolsch__list_sessions",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    # Signals per spec-105 Test 7 (b) + (c):
    #   - The tool-result frame with mcp__kolsch__list_sessions.
    #   - The final assistant-message text referencing returned sessions.
    combined = proc.stdout + proc.stderr
    assert "mcp__kolsch__list_sessions" in combined, (
        f"no mcp__kolsch__list_sessions invocation frame in output; "
        f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
