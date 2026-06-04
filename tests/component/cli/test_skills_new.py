"""``kolsch skills new`` — Step 5 SKILL.md scaffolder.

Six cases:

- Happy path: creates the file at the right path, frontmatter
  parses, the ref re-resolves on a fresh catalog scan.
- ``--description`` lands verbatim in the frontmatter.
- Default placeholder description when ``--description`` omitted.
- Refuses to overwrite an existing SKILL.md.
- Refuses a missing bundle.
- Rejects a name that doesn't match ``[a-z][a-z0-9_-]*``.

The "ref is resolvable in the catalog after a re-scan" assertion
constructs a fresh :class:`AssetManagement` against the same tmp
workspace — the catalog is built once at bootstrap (no hot reload
per the Step 5 escalation note), so a second instance is the right
proxy for "the next ``kolsch ...`` invocation will see it".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from assets import AssetManagement
from assets.skill_metadata import parse_skill_frontmatter
from cli import main

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Workspace fixture — platform.yaml + a kolsch bundle (the default).
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


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_skills_new_creates_skeleton_and_ref_resolves_on_rescan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: the SKILL.md lands at <root>/bundles/<bundle>/skills/<name>/,
    the frontmatter parses cleanly via the same parser the catalog
    uses, and a fresh AssetManagement scan finds the ref. The
    ``--bundle`` default ('kolsch') is exercised by omitting the flag.

    Sync test because ``main()`` calls ``asyncio.run`` internally —
    pytest-asyncio's auto-mode would put us inside a running loop,
    which ``asyncio.run`` rejects. The post-CLI catalog check is
    wrapped in a nested ``asyncio.run`` so the async ``get_asset``
    surface is still exercised.
    """
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "kolsch")

    exit_code = main(["--root", str(tmp_path), "skills", "new", "literature-review"])
    assert exit_code == 0

    skill_md = tmp_path / "bundles" / "kolsch" / "skills" / "literature-review" / "SKILL.md"
    assert skill_md.is_file()

    # Frontmatter parses via the SAME parser the catalog uses, so a
    # round-trip mismatch (template format vs parser expectations)
    # would surface here, not at a later catalog scan.
    fm = parse_skill_frontmatter(skill_md)
    assert fm["name"] == "literature-review"
    assert "description" in fm
    assert fm["allowed-tools"] == []
    assert fm["disable_model_invocation"] is False

    # The body carries the TODO marker so the user knows what to do
    # with the scaffold.
    body = skill_md.read_text(encoding="utf-8")
    assert "TODO" in body
    assert "# literature-review" in body

    # The ref the command printed.
    out = capsys.readouterr().out
    assert "ref: kolsch/skills/literature-review" in out
    # The user-facing hint about the catalog-not-hot-reloaded constraint
    # — the escalation note from Step 5.
    assert "next `kolsch ...` invocation" in out

    # The escalation-path assertion: a fresh AssetManagement (proxy
    # for the next CLI invocation) finds the ref.
    async def _resolve() -> tuple[str, dict]:
        am = AssetManagement(root=tmp_path)
        await am.initialize()
        entry = await am.get_asset("kolsch/skills/literature-review")
        return entry.asset_type, dict(entry.metadata)

    asset_type, metadata = asyncio.run(_resolve())
    assert asset_type == "skill"
    assert metadata["name"] == "literature-review"


def test_skills_new_description_flag_lands_in_frontmatter(
    tmp_path: Path,
) -> None:
    """``--description "..."`` writes the user-supplied text into the
    frontmatter (and the catalog reads it back). The default
    placeholder only kicks in when the flag is omitted."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "kolsch")

    custom = "Conduct a structured literature review on a given topic."
    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "new",
            "literature-review",
            "--description",
            custom,
        ],
    )
    assert exit_code == 0

    fm = parse_skill_frontmatter(
        tmp_path
        / "bundles"
        / "kolsch"
        / "skills"
        / "literature-review"
        / "SKILL.md"
    )
    assert fm["description"] == custom


def test_skills_new_default_description_is_a_placeholder(
    tmp_path: Path,
) -> None:
    """Without ``--description``, the frontmatter description is a
    TODO placeholder — surfaced as such so the user knows it needs
    editing before the skill is discoverable by the model (the
    description IS the model's discovery signal per spec §2.3)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "kolsch")

    exit_code = main(["--root", str(tmp_path), "skills", "new", "stub"])
    assert exit_code == 0

    fm = parse_skill_frontmatter(
        tmp_path / "bundles" / "kolsch" / "skills" / "stub" / "SKILL.md"
    )
    assert "TODO" in fm["description"]


# ---------------------------------------------------------------------------
# Refusals.
# ---------------------------------------------------------------------------


def test_skills_new_refuses_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An existing SKILL.md at the target path → exit 1, friendly
    error pointing at the existing file. Protects the user from
    accidentally clobbering an in-progress skill body via a typo'd
    re-run."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "kolsch")
    skill_dir = tmp_path / "bundles" / "kolsch" / "skills" / "existing"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    original_body = (
        "---\nname: existing\ndescription: Already here.\n---\n\n"
        "PRECIOUS body the user has been editing.\n"
    )
    skill_md.write_text(original_body, encoding="utf-8")

    exit_code = main(["--root", str(tmp_path), "skills", "new", "existing"])
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "already exists" in err
    assert "Refusing to overwrite" in err
    # The friendly hint points at `skills show` so the user can
    # inspect the existing file without leaving the CLI.
    assert "kolsch skills show kolsch/skills/existing" in err

    # The pre-existing file is untouched.
    assert skill_md.read_text(encoding="utf-8") == original_body


def test_skills_new_refuses_missing_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--bundle <nonexistent>`` → exit 1, friendly error. The
    command does NOT create bundles; that's an explicit design
    decision (bundles are larger units that bundle.yaml + scaffolded
    directory layout, not something `new` should auto-create)."""
    _platform_yaml(tmp_path)
    # Don't create any bundle — bundles/ is empty.

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "new",
            "lit-review",
            "--bundle",
            "kolsch",
        ],
    )
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "bundle 'kolsch' not found" in err
    # No traceback — friendly error only.
    assert "Traceback" not in err

    # Nothing landed on disk.
    assert not (tmp_path / "bundles" / "kolsch" / "skills" / "lit-review").exists()


def test_skills_new_rejects_invalid_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Names must match ``[a-z][a-z0-9_-]*`` per spec §2.3 — the
    catalog uses the directory name as the ref segment regardless of
    frontmatter, so a name with uppercase / spaces / leading digit
    would silently produce a confusing ref. Validate at write time."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "kolsch")

    exit_code = main(
        ["--root", str(tmp_path), "skills", "new", "Bad Name!"],
    )
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "invalid" in err
    assert "[a-z][a-z0-9_-]*" in err
    # Nothing got written.
    assert not (tmp_path / "bundles" / "kolsch" / "skills" / "Bad Name!").exists()
