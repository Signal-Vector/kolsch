"""``kolsch skills remove <ref>`` — Step 8 delete-with-dependent-walk.

All tests use ``--yes`` to bypass the trust prompt (pytest's
non-TTY stdin would otherwise refuse, which a dedicated test
proves). The dependent-agent walk + the last-skill-in-bundle
cleanup branch are pinned by name.

Six cases:

- Happy path: ref resolves → skill dir is gone, bundle.yaml stays.
- Affected agents are surfaced in stdout BEFORE the prompt (the
  user gets the warning even when ``--yes`` short-circuits the
  prompt itself).
- ``--yes`` bypasses; no stdin needed.
- Last skill in a bundle: bundle-cleanup branch fires. With
  ``--yes`` both prompts auto-confirm and the bundle directory
  is gone too.
- Unknown ref → friendly stderr, exit 1.
- Non-TTY without ``--yes`` is refused (and the skill dir is
  untouched).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Workspace fixture helpers.
# ---------------------------------------------------------------------------


def _platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
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
    (root / ".env").write_text("", encoding="utf-8")


def _make_bundle(root: Path, name: str) -> Path:
    bundle = root / "bundles" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    return bundle


def _write_skill(root: Path, bundle: str, name: str) -> Path:
    skill_dir = root / "bundles" / bundle / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill.\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_agent(
    root: Path,
    bundle: str,
    name: str,
    enabled_skills: list[str],
) -> Path:
    agents_dir = root / "bundles" / bundle / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_path = agents_dir / f"{name}.yaml"
    agent_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": f"ag_{name}",
                "name": name,
                "description": f"{name} agent",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "hi",
                "io": {"mode": "conversational"},
                "subsystems": {
                    "skills": {
                        "variant": "standard",
                        "config": {
                            "enabled": [{"ref": r} for r in enabled_skills],
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return agent_path


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_remove_happy_path_deletes_skill_dir_keeps_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing a skill deletes the skill directory but leaves the
    bundle (bundle.yaml + sibling skills if any) intact."""
    _platform_yaml(tmp_path)
    bundle = _make_bundle(tmp_path, "acme")
    skill_dir = _write_skill(tmp_path, "acme", "literature-review")
    _write_skill(tmp_path, "acme", "other-skill")  # sibling, keeps bundle non-empty

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "acme/skills/literature-review",
            "--yes",
        ],
    )
    assert exit_code == 0, capsys.readouterr().err
    assert not skill_dir.exists()
    # Bundle + manifest + sibling skill survive.
    assert (bundle / "bundle.yaml").is_file()
    assert (bundle / "skills" / "other-skill" / "SKILL.md").is_file()

    out = capsys.readouterr().out
    assert "deleted" in out
    assert "literature-review" in out
    # No bundle-cleanup branch fired (the sibling kept the bundle non-empty).
    assert "empty of skills" not in out


# ---------------------------------------------------------------------------
# Dependent-agent walk.
# ---------------------------------------------------------------------------


def test_remove_surfaces_affected_agents_in_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agents whose YAML enables the to-be-deleted skill are listed
    in the warning. The warning fires regardless of ``--yes`` —
    audit trail stays honest even on auto-accept."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill(tmp_path, "acme", "lit-review")
    # Agent that enables the skill — should appear in the warning.
    _write_agent(
        tmp_path,
        "acme",
        "researcher",
        enabled_skills=["acme/skills/lit-review"],
    )
    # Agent that doesn't — should NOT appear.
    _write_agent(tmp_path, "acme", "unrelated", enabled_skills=[])

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "acme/skills/lit-review",
            "--yes",
        ],
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Affected agents" in out
    assert "acme/agents/researcher" in out
    assert "acme/skills/unrelated" not in out
    # The actionable hint about not auto-editing the agent YAMLs.
    assert "intentionally does NOT auto-edit" in out


def test_remove_with_no_affected_agents_says_so(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no agent enables the skill, the warning is a clear
    positive note rather than an empty list."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill(tmp_path, "acme", "orphan")

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "acme/skills/orphan",
            "--yes",
        ],
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No agents in the catalog enable this skill." in out


# ---------------------------------------------------------------------------
# --yes bypass + non-TTY refusal.
# ---------------------------------------------------------------------------


def test_remove_yes_skips_prompt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--yes`` runs to completion with no stdin read (pytest's
    captured stdin would otherwise refuse the prompt)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    skill_dir = _write_skill(tmp_path, "acme", "stub")

    exit_code = main(
        ["--root", str(tmp_path), "skills", "remove", "acme/skills/stub", "--yes"],
    )
    assert exit_code == 0
    assert not skill_dir.exists()
    # The y/n prompt line itself is absent.
    out = capsys.readouterr().out
    assert "[y/N]" not in out and "[Y/n]" not in out


def test_remove_non_tty_without_yes_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pytest stdin is non-TTY. Without ``--yes`` the command
    refuses, prints a friendly error, and leaves the skill dir
    untouched."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    skill_dir = _write_skill(tmp_path, "acme", "safe")

    exit_code = main(
        ["--root", str(tmp_path), "skills", "remove", "acme/skills/safe"],
    )
    assert exit_code == 1
    assert skill_dir.exists()  # untouched

    err = capsys.readouterr().err
    assert "TTY" in err
    assert "--yes" in err


# ---------------------------------------------------------------------------
# Last-skill-in-bundle cleanup branch.
# ---------------------------------------------------------------------------


def test_remove_last_skill_offers_bundle_cleanup_with_yes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the removed skill is the last asset of any kind in its
    bundle, the cleanup branch fires. ``--yes`` auto-confirms both
    the skill delete AND the bundle delete — the whole bundle
    directory is gone."""
    _platform_yaml(tmp_path)
    bundle = _make_bundle(tmp_path, "single-skill")
    skill_dir = _write_skill(tmp_path, "single-skill", "only")

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "single-skill/only",
            "--yes",
        ],
    )
    assert exit_code == 0
    assert not skill_dir.exists()
    assert not bundle.exists(), (
        "with --yes the auto-empty bundle MUST be removed too "
        "(both confirmation prompts auto-accept)"
    )

    out = capsys.readouterr().out
    assert "empty of skills" in out
    assert "deleted bundle" in out


def test_remove_last_skill_bundle_cleanup_does_not_fire_when_bundle_has_other_assets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bundle that has agents/prompts is NOT cleaned up
    even when its last skill is removed — those other assets are
    the user's intent. Cleanup is for the auto-created
    single-skill-bundle pattern (``skills install`` without
    ``--bundle``)."""
    _platform_yaml(tmp_path)
    bundle = _make_bundle(tmp_path, "multi")
    skill_dir = _write_skill(tmp_path, "multi", "only-skill")
    # An agent in the same bundle — should keep the bundle alive.
    _write_agent(tmp_path, "multi", "helper", enabled_skills=[])

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "multi/only-skill",
            "--yes",
        ],
    )
    assert exit_code == 0
    assert not skill_dir.exists()
    # Bundle survives — the cleanup branch never fired.
    assert (bundle / "bundle.yaml").is_file()
    assert (bundle / "agents" / "helper.yaml").is_file()

    out = capsys.readouterr().out
    assert "empty of skills" not in out
    assert "deleted bundle" not in out


# ---------------------------------------------------------------------------
# Unknown ref.
# ---------------------------------------------------------------------------


def test_remove_unknown_ref_friendly_error_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ref not in the catalog exits 1 with a friendly stderr
    (no traceback)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "acme/skills/does-not-exist",
            "--yes",
        ],
    )
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "not in catalog" in err
    assert "Traceback" not in err


def test_remove_non_skill_ref_friendly_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ref that resolves to an agent (or tool) exits 1 with a
    friendly stderr naming the actual asset_type — same shape as
    ``skills show`` on a non-skill ref."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill(tmp_path, "acme", "keep")  # so the catalog has at least one skill
    _write_agent(tmp_path, "acme", "researcher", enabled_skills=[])

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "remove",
            "acme/agents/researcher",
            "--yes",
        ],
    )
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "is a agent" in err or "is an agent" in err
    assert "not a skill" in err
    # The agent YAML wasn't touched.
    assert (tmp_path / "bundles" / "acme" / "agents" / "researcher.yaml").is_file()
