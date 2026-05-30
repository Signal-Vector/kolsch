"""Shared fixtures for the chat tests — Phase 2.1 Step 24c.

The chat tests need a real workspace + runtime to exercise the
engine-wired input loop (``_run_input_loop`` now takes runtime,
config, and thread_id). Scaffolding a workspace via ``kolsch
setup --non-interactive`` is ~1s; doing it once per test session
keeps the suite under the gate target.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="session")
def chat_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace initialised via ``kolsch setup --non-interactive``.

    Session-scoped so the cost is paid once for the whole chat
    test module. Subprocess invocation is the canonical way to
    drive the setup runner (mirrors the smoke-test pattern).
    """
    root = tmp_path_factory.mktemp("chat_workspace")
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"chat_workspace setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    return root
