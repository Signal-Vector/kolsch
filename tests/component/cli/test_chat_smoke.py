"""CLI smoke tests for ``kolsch chat`` — Phase 2.1 Step 24a.

Per the AGENTS.md hard rule (bug-sessions-namespace-attr-error.md):
every new CLI subcommand requires a smoke test that invokes the
CLI in a fresh subprocess. These two complement
``tests/component/test_cli_smoke.py``'s coverage-gate test —
adding ``chat`` to ``_TOP_LEVEL_SUBCOMMANDS`` made the
parametrized ``--help`` smoke run for it automatically; this
file adds the bug-ticket's prescribed companion case (a
minimal-args invocation that feeds EOF on stdin).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


_TIMEOUT_SECONDS = 30


def _run(
    *cli_args: str,
    workspace: Path | None = None,
    stdin: str = "",
    timeout: int = _TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m cli <args>`` in a fresh subprocess.

    Mirrors the helper in ``test_cli_smoke.py``; duplicated here so
    the chat smokes can pass ``stdin=...`` without thrashing the
    main smoke module's signature.
    """
    env = dict(os.environ)
    env.pop("KOLSCH_PROVIDER", None)
    env.pop("KOLSCH_PROVIDER_KEY", None)
    if workspace is not None:
        env["KOLSCH_WORKSPACE"] = str(workspace)
    else:
        env.pop("KOLSCH_WORKSPACE", None)
    return subprocess.run(
        [sys.executable, "-m", "cli", *cli_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
        check=False,
    )


@pytest.fixture(scope="session")
def initialized_workspace_for_chat(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """A workspace pre-initialized via ``kolsch setup --non-interactive``.

    Separate fixture from ``initialized_workspace`` in
    ``test_cli_smoke.py`` because pytest sessions don't share
    fixtures across files when each is session-scoped; each file
    pays the setup cost once. Setup is ~1s with ``-n auto`` so
    the duplication is acceptable.
    """
    root = tmp_path_factory.mktemp("chat_smoke_workspace")
    r = _run("setup", "--non-interactive", workspace=root, timeout=60)
    if r.returncode != 0:
        msg = (
            f"setup failed: rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )
        raise RuntimeError(msg)
    return root


# ===========================================================================
# TC-CLI-SMOKE-CHAT-01 — kolsch chat --help (covered by the parametrized
# `--help` test in test_cli_smoke.py via _TOP_LEVEL_SUBCOMMANDS; this is the
# explicit standalone smoke that the catalog entry references)
# ===========================================================================


def test_tc_cli_smoke_chat_01_chat_help_exits_zero() -> None:
    """``kolsch chat --help`` exits 0 with non-empty stdout; no
    traceback. Doubles up with the coverage-gate-driven parametrized
    test in ``test_cli_smoke.py`` for explicit visibility."""
    result = _run("chat", "--help")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert "Traceback" not in result.stderr
    assert "agent_ref" in result.stdout  # the positional arg name
    assert "--session" in result.stdout
    assert "--model" in result.stdout


# ===========================================================================
# TC-CLI-SMOKE-CHAT-02 — minimal-args invocation feeds EOF on stdin
# ===========================================================================


def test_tc_cli_smoke_chat_02_minimal_args_exits_on_eof(
    initialized_workspace_for_chat: Path,
) -> None:
    """``kolsch chat <agent>`` against an initialized workspace with
    EOF (empty stdin) on the input prompt exits 0 cleanly.

    24a stub: each submitted line echoes; EOF on stdin terminates
    the input loop via ``EOFError``. The test verifies the
    subprocess exits cleanly (no traceback, no hung process)
    without the full slash-command surface (which lands in 24b).
    """
    result = _run(
        "chat",
        "kolsch/agents/helper",
        workspace=initialized_workspace_for_chat,
        stdin="",  # empty stdin → EOFError on the first prompt → exit 0
    )
    assert result.returncode == 0, (
        f"chat exit was {result.returncode}; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr
    # The banner should have rendered before EOF.
    assert "kolsch chat" in result.stdout
    assert "helper" in result.stdout
    assert "ses_" in result.stdout
