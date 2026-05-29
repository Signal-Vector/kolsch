"""TC-CLI-01..05 — component tests for the ``pi-platform`` CLI.

Each test runs the CLI in-process via :func:`cli.main` against a
tmp-path platform root + a hand-authored agent bundle. No subprocess
spawning — keeps the suite fast and avoids needing the wheel to be
re-installed between edits.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
import yaml

from cli import main

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_platform_root(tmp_path: Path) -> Path:
    """Lay down platform.yaml + a minimal bundle with one agent."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {"who": ".*", "what": ".*", "effect": "allow"},
                        ],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    bundle = tmp_path / "bundles" / "acme"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    agents_dir = bundle / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "Demo agent.",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    return _make_platform_root(tmp_path)


# ===========================================================================
# TC-CLI-01 — pi-platform run end-to-end with EchoLLM
# ===========================================================================


def test_tc_cli_01_run_end_to_end(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default ``pi-platform run`` works with no external services.

    Uses the v1 default tier (SQLite + in-process bus + EchoLLM via
    FakeFramework). Asserts the agent_message text from the engine
    lands on stdout (`Echo: hello world`)."""
    exit_code = main(
        [
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=hello world",
        ],
    )
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["content"]["text"] == "Echo: hello world"


# ===========================================================================
# TC-CLI-02 — streaming yields events in order
# ===========================================================================


def test_tc_cli_02_run_stream_yields_events(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=stream me",
            "--stream",
        ],
    )
    assert exit_code == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    types = [json.loads(line)["type"] for line in lines]

    # The engine's golden sequence for a simple text exchange. The
    # stream surfaces them in order (Engine I6).
    assert "thread.exchange_started" in types
    assert "user.message" in types
    assert "model.request" in types
    assert "model.response" in types
    assert "agent.message" in types
    assert "thread.exchange_completed" in types
    started_idx = types.index("thread.exchange_started")
    completed_idx = types.index("thread.exchange_completed")
    assert started_idx < completed_idx


# ===========================================================================
# TC-CLI-03 — sessions list reflects the engine's runs
# ===========================================================================


def test_tc_cli_03_sessions_list(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Run once to create a session.
    main(
        [
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=hello",
        ],
    )
    capsys.readouterr()  # discard run output

    exit_code = main(
        [
            "--root",
            str(platform_root),
            "sessions",
            "list",
        ],
    )
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert "ag_demo" in out
    assert "cli:acme/agents/demo" in out


# ===========================================================================
# TC-CLI-04 — reconstruct round-trips
# ===========================================================================


def test_tc_cli_04_reconstruct_after_db_wipe(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(
        [
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=hello",
        ],
    )
    capsys.readouterr()

    # Wipe the entity DB; the JSONL session file survives.
    db = platform_root / ".platform" / "entities.db"
    db.unlink()

    exit_code = main(
        [
            "--root",
            str(platform_root),
            "reconstruct",
        ],
    )
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["sessions_reconstructed"] >= 1
    assert result["threads_reconstructed"] >= 1


# ===========================================================================
# TC-CLI-05 — sessions events prints the per-session log
# ===========================================================================


def test_tc_cli_05_sessions_events(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(
        [
            "--root",
            str(platform_root),
            "run",
            "acme/agents/demo",
            "--input",
            "content=hi",
        ],
    )
    capsys.readouterr()

    # Discover the session id via `sessions list`.
    main(["--root", str(platform_root), "sessions", "list"])
    list_out = capsys.readouterr().out.strip().splitlines()
    assert list_out, "expected at least one session"
    session_id = list_out[0].split()[0]
    assert session_id.startswith("ses_")

    exit_code = main(
        [
            "--root",
            str(platform_root),
            "sessions",
            "events",
            session_id,
        ],
    )
    assert exit_code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    types = [json.loads(line)["type"] for line in lines]
    assert "session.opened" in types
    assert "agent.message" in types


# ===========================================================================
# TC-CLI-06 — help exit code 1 when no command
# ===========================================================================


def test_tc_cli_06_no_command_prints_help_and_exits_nonzero(
    platform_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--root", str(platform_root)])
    assert exit_code == 1
    captured = capsys.readouterr()
    # argparse prints usage to stdout when print_help() is called.
    assert "usage:" in captured.out.lower()


# Suppress an unused import the linter spots when the file is read
# without _StringIO usage — StringIO is imported for clarity in
# future tests that need to capture sys.stderr explicitly.
_ = StringIO
