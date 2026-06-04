"""``kolsch skills {list, show, which}`` — Step 4 read-only CLI verbs.

Mirrors test_chat_force_skill.py's "minimal in-process workspace +
real catalog scan" pattern. Each test sets up a tmp workspace by
hand (no ``kolsch setup`` subprocess — these tests don't need a
real provider wiring), then invokes ``cli.main([...])`` and
inspects stdout / stderr / exit code.

Per the build prompt: at least one test per op + the no-skills
case for ``list`` (must print cleanly, NOT crash).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from cli import main

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Workspace fixtures.
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


def _write_bundle_with_skills(root: Path, bundle: str, skills: list[dict]) -> None:
    bundle_dir = root / "bundles" / bundle
    (bundle_dir / "agents").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump({"name": bundle, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    for spec in skills:
        skill_dir = bundle_dir / "skills" / spec["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        fm: dict = {"name": spec["name"], "description": spec["description"]}
        if spec.get("disable_model_invocation"):
            fm["disable-model-invocation"] = True
        if spec.get("license"):
            fm["license"] = spec["license"]
        if spec.get("allowed_tools"):
            fm["allowed-tools"] = spec["allowed_tools"]
        body = spec.get("body", f"# {spec['name']}\n\nBody for {spec['name']}.\n")
        (skill_dir / "SKILL.md").write_text(
            f"---\n{yaml.safe_dump(fm)}---\n\n{body}",
            encoding="utf-8",
        )


def _write_agent(
    root: Path, bundle: str, name: str, enabled_skills: list[str]
) -> None:
    agent_path = root / "bundles" / bundle / "agents" / f"{name}.yaml"
    agent_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": f"ag_{name}",
                "name": name,
                "description": f"{name} agent",
                "model": {"provider": "fake", "model": "fake-model"},
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


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_skills_list_prints_table_across_bundles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """list prints one row per catalogued skill, across bundles, in
    deterministic ref order. Disabled-model-invocation skills carry
    the [hidden] marker so users see at a glance which need
    force-invoke."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [
            {"name": "alpha", "description": "Alpha description."},
            {
                "name": "hidden",
                "description": "Hidden description.",
                "disable_model_invocation": True,
            },
        ],
    )
    _write_bundle_with_skills(
        tmp_path,
        "beta",
        [{"name": "gamma", "description": "Gamma description."}],
    )

    exit_code = main(["--root", str(tmp_path), "skills", "list"])
    assert exit_code == 0

    out = capsys.readouterr().out
    # Header.
    assert "REF" in out and "NAME" in out and "DESCRIPTION" in out
    # All three skills listed.
    assert "acme/skills/alpha" in out
    assert "acme/skills/hidden" in out
    assert "beta/skills/gamma" in out
    # Hidden marker on the disabled skill only.
    hidden_line = next(line for line in out.splitlines() if "acme/skills/hidden" in line)
    alpha_line = next(line for line in out.splitlines() if "acme/skills/alpha" in line)
    assert "[hidden]" in hidden_line
    assert "[hidden]" not in alpha_line


def test_skills_list_bundle_filter_narrows_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--bundle <name>`` restricts the listing to one bundle."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )
    _write_bundle_with_skills(
        tmp_path,
        "beta",
        [{"name": "gamma", "description": "Gamma."}],
    )

    exit_code = main(["--root", str(tmp_path), "skills", "list", "--bundle", "acme"])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "acme/skills/alpha" in out
    assert "beta/skills/gamma" not in out


def test_skills_list_json_emits_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` emits a JSON list with one object per row carrying
    the structural fields. Useful for scripted introspection
    (`kolsch skills list --json | jq ...`)."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha description."}],
    )

    exit_code = main(["--root", str(tmp_path), "skills", "list", "--json"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert row["ref"] == "acme/skills/alpha"
    assert row["bundle"] == "acme"
    assert row["name"] == "alpha"
    assert row["disable_model_invocation"] is False


def test_skills_list_no_skills_prints_friendly_line_exit_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The no-skills case must print cleanly and exit 0 — a fresh
    workspace has no skills catalogued, and that's a valid state,
    not an error."""
    _platform_yaml(tmp_path)
    # Make a bundle with no skills/ directory at all.
    bundle = tmp_path / "bundles" / "empty"
    bundle.mkdir(parents=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "empty", "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )

    exit_code = main(["--root", str(tmp_path), "skills", "list"])
    assert exit_code == 0

    out = capsys.readouterr().out.strip()
    assert out == "(no skills catalogued)"


def test_skills_list_bundle_filter_no_match_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--bundle <name>`` against an unknown / empty bundle still
    exits 0 with a friendly message — the filter is a search, not
    an existence assertion."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )

    exit_code = main(
        ["--root", str(tmp_path), "skills", "list", "--bundle", "unknown"],
    )
    assert exit_code == 0
    assert "no skills in bundle 'unknown'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_skills_show_prints_frontmatter_and_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """show prints each frontmatter field on its own labeled line,
    then a body marker, then the SKILL.md body."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [
            {
                "name": "literature-review",
                "description": "Conduct a structured literature review.",
                "license": "MIT",
                "allowed_tools": ["read", "write"],
                "body": "# Literature Review\n\nStep 1: identify search terms.\n",
            }
        ],
    )

    exit_code = main(
        ["--root", str(tmp_path), "skills", "show", "acme/skills/literature-review"],
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    # Frontmatter surface.
    assert "ref:         acme/skills/literature-review" in out
    assert "name:        literature-review" in out
    assert "Conduct a structured literature review." in out
    assert "license:     MIT" in out
    assert "allowed-tools: read, write" in out
    # Body section after the marker.
    assert "--- body ---" in out
    assert "# Literature Review" in out
    assert "Step 1: identify search terms." in out


def test_skills_show_unknown_ref_friendly_error_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown ref prints a friendly stderr error (no traceback)
    and exits 1 — the ref might be a typo. The hint mentions the
    list verb so the user knows where to go next."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )

    exit_code = main(
        ["--root", str(tmp_path), "skills", "show", "acme/skills/does-not-exist"],
    )
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "not in catalog" in captured.err
    assert "kolsch skills list" in captured.err
    # No Python traceback noise.
    assert "Traceback" not in captured.err


def test_skills_show_non_skill_ref_friendly_error_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ref that resolves to an agent (or any non-skill asset)
    fails with a friendly error naming the actual asset_type."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )
    _write_agent(tmp_path, "acme", "researcher", enabled_skills=[])

    exit_code = main(
        ["--root", str(tmp_path), "skills", "show", "acme/agents/researcher"],
    )
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "is a agent" in err or "is an agent" in err
    assert "not a skill" in err


# ---------------------------------------------------------------------------
# which
# ---------------------------------------------------------------------------


def test_skills_which_lists_enabled_with_status_marker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """which prints each enabled ref with a ✓ when catalogued,
    ✗ when the agent's config references a skill the catalog can't
    resolve. A missing ref forces the non-zero exit because the
    next force-invoke against it would fail."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha description."}],
    )
    # Agent enables one catalogued skill + one orphan ref.
    _write_agent(
        tmp_path,
        "acme",
        "researcher",
        enabled_skills=["acme/skills/alpha", "acme/skills/missing"],
    )

    exit_code = main(
        ["--root", str(tmp_path), "skills", "which", "acme/agents/researcher"],
    )
    # Missing ref → exit 2 (actionable state).
    assert exit_code == 2

    out = capsys.readouterr().out
    # Both refs surface.
    assert "acme/skills/alpha" in out
    assert "acme/skills/missing" in out
    # Status markers — one good, one bad.
    alpha_line = next(line for line in out.splitlines() if "alpha" in line)
    missing_line = next(line for line in out.splitlines() if "missing" in line)
    assert "✓" in alpha_line
    assert "✗" in missing_line


def test_skills_which_agent_with_no_skills_prints_friendly_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An agent that doesn't enable any skills prints a clean
    "no skills enabled" line and exits 0 — that's a valid agent
    shape (skills are optional)."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )
    _write_agent(tmp_path, "acme", "skinny", enabled_skills=[])

    exit_code = main(
        ["--root", str(tmp_path), "skills", "which", "acme/agents/skinny"],
    )
    assert exit_code == 0
    assert "no skills enabled" in capsys.readouterr().out


def test_skills_which_json_returns_structured_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` returns a structured payload — useful for
    pre-flight scripts that want to assert all enabled refs are
    catalogued before kicking off a run."""
    _platform_yaml(tmp_path)
    _write_bundle_with_skills(
        tmp_path,
        "acme",
        [{"name": "alpha", "description": "Alpha."}],
    )
    _write_agent(
        tmp_path,
        "acme",
        "researcher",
        enabled_skills=["acme/skills/alpha", "acme/skills/missing"],
    )

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "which",
            "acme/agents/researcher",
            "--json",
        ],
    )
    assert exit_code == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "acme/agents/researcher"
    rows = {row["ref"]: row for row in payload["enabled"]}
    assert rows["acme/skills/alpha"]["available"] is True
    assert rows["acme/skills/alpha"]["name"] == "alpha"
    assert rows["acme/skills/missing"]["available"] is False
    assert rows["acme/skills/missing"]["name"] is None


# ---------------------------------------------------------------------------
# Dispatcher — missing op prints help, no crash.
# ---------------------------------------------------------------------------


def test_skills_without_op_prints_help_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``kolsch skills`` mirrors permissions/sessions: print the
    subparser help to stderr, exit 1, no traceback."""
    _platform_yaml(tmp_path)

    exit_code = main(["--root", str(tmp_path), "skills"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "list" in err and "show" in err and "which" in err
    assert "Traceback" not in err
