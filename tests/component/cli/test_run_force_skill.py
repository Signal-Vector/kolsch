"""``kolsch run --skill <ref>`` — Step 3 force-invoke surface.

Mirrors the chat surface tested in test_chat_force_skill.py but for
the one-shot CLI. Three cases per the build prompt:

- ``--skill <enabled>`` → trigger shape flips to
  ``user_message_with_force_skill``; the engine's pre-flight emits a
  synthetic ``tool.called`` for ``read`` on the SKILL.md path.
- ``--skill`` repeated → ``force_skills`` carries both refs; engine
  emits one synthetic ``read`` per ref in declaration order.
- ``--skill <not-enabled>`` → friendly error before
  ``execute_stream`` is called; exit code 2; no exchange started.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cli import main

# ---------------------------------------------------------------------------
# Workspace fixture — a bundle with two skills + an agent enabling both.
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
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
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
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

    bundle = tmp_path / "bundles" / "rs"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "rs", "version": "0.1", "description": "rs"}),
        encoding="utf-8",
    )
    for name, body in (("alpha", "Alpha body."), ("beta", "Beta body.")):
        skill_dir = bundle / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description.\n---\n\n{body}\n",
            encoding="utf-8",
        )
    (bundle / "agents" / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_rs",
                "name": "rs",
                "description": "run-skill demo",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "hi",
                "io": {"mode": "conversational"},
                "subsystems": {
                    "tools": {
                        "variant": "standard",
                        "config": {"builtin": ["read"]},
                    },
                    "skills": {
                        "variant": "standard",
                        "config": {
                            "enabled": [
                                {"ref": "rs/skills/alpha"},
                                {"ref": "rs/skills/beta"},
                            ],
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _synthetic_read_calls(stream_lines: list[str]) -> list[dict]:
    """Pick out the engine's synthetic tool.called events from a --stream run.

    System-actor tool.called events with tool_name=='read' are exactly
    the pre-flight force-invoke synthesis (engine.py:937-945).
    """
    out: list[dict] = []
    for line in stream_lines:
        if not line.strip():
            continue
        evt = json.loads(line)
        if (
            evt.get("type") == "tool.called"
            and evt.get("actor", {}).get("kind") == "system"
            and evt.get("data", {}).get("tool_name") == "read"
        ):
            out.append(evt)
    return out


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_run_skill_single_enabled_ref_emits_synthetic_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--skill <enabled>`` flips the trigger shape; engine emits
    one synthetic system tool.called for ``read`` on the SKILL.md
    absolute path BEFORE thread.exchange_started."""
    root = _make_workspace(tmp_path)

    exit_code = main(
        [
            "--root",
            str(root),
            "run",
            "rs/agents/agent",
            "--input",
            "content=hi",
            "--stream",
            "--skill",
            "rs/skills/alpha",
        ],
    )
    assert exit_code == 0

    lines = capsys.readouterr().out.splitlines()
    synthetic = _synthetic_read_calls(lines)
    assert len(synthetic) == 1
    expected_path = str(
        (root / "bundles" / "rs" / "skills" / "alpha" / "SKILL.md").resolve()
    )
    assert synthetic[0]["data"]["arguments"]["path"] == expected_path

    # Sanity: synthetic read precedes thread.exchange_started.
    types = [json.loads(line)["type"] for line in lines if line.strip()]
    synthetic_idx = next(
        i
        for i, line in enumerate(line for line in lines if line.strip())
        if json.loads(line).get("type") == "tool.called"
        and json.loads(line).get("actor", {}).get("kind") == "system"
    )
    started_idx = types.index("thread.exchange_started")
    assert synthetic_idx < started_idx


def test_run_skill_repeated_emits_one_synthetic_read_per_ref(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--skill alpha --skill beta`` → two synthetic reads, in the
    order the flags were passed (the trigger's ``force_skills`` list
    preserves order; the engine iterates it in step 9 of pre-flight)."""
    root = _make_workspace(tmp_path)

    exit_code = main(
        [
            "--root",
            str(root),
            "run",
            "rs/agents/agent",
            "--input",
            "content=hi",
            "--stream",
            "--skill",
            "rs/skills/alpha",
            "--skill",
            "rs/skills/beta",
        ],
    )
    assert exit_code == 0

    synthetic = _synthetic_read_calls(capsys.readouterr().out.splitlines())
    assert len(synthetic) == 2
    paths = [evt["data"]["arguments"]["path"] for evt in synthetic]
    assert paths[0].endswith("/rs/skills/alpha/SKILL.md")
    assert paths[1].endswith("/rs/skills/beta/SKILL.md")


def test_run_skill_unknown_ref_fails_before_execute(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--skill <not-enabled>`` exits 2 with a friendly stderr
    message before the engine is invoked. The session/thread are
    untouched — no events on the stream."""
    root = _make_workspace(tmp_path)

    exit_code = main(
        [
            "--root",
            str(root),
            "run",
            "rs/agents/agent",
            "--input",
            "content=hi",
            "--stream",
            "--skill",
            "rs/skills/does-not-exist",
        ],
    )
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "not in this agent's enabled list" in captured.err
    assert "rs/skills/does-not-exist" in captured.err
    # The enabled list is surfaced for context.
    assert "rs/skills/alpha" in captured.err

    # No exchange started — failure is pre-execute_stream.
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert stdout_lines == []
