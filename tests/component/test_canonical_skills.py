"""TC-KSKILL-* — canonical kolsch skills shipped in the starter bundle.

Phase 2.4 Step 9 ships three skills under
``src/cli/_starter_bundle/kolsch/skills/`` that `kolsch setup` copies
into a fresh workspace's ``bundles/kolsch/skills/`` tree.

These tests pin the structural contract — frontmatter parses, body
is substantive, the catalog picks them up. The SKILL.md CONTENT
(whether the test-writing guidance is good) is reviewed at PR time
by reading the body; it's not testable in CI.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from assets import AssetManagement
from assets.skill_metadata import parse_skill_frontmatter
from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR

_CANONICAL_SKILLS: tuple[str, ...] = (
    "write-python-test",
    "review-pr-diff",
    "commit-from-staged",
)
"""The skills Phase 2.4 Step 9 ships. Pinning the tuple here means
adding a fourth canonical skill needs to update this list, which
forces a conscious "is this canonical?" decision rather than a
silent grow-the-set drift."""


# ---------------------------------------------------------------------------
# Per-skill structural assertions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _CANONICAL_SKILLS)
def test_canonical_skill_md_exists_in_starter_template(skill_name: str) -> None:
    """Every canonical skill ships in the packaged starter bundle.

    This is the "we didn't drop a file on `git add`" check — the
    packaged template is what `kolsch setup` copies, so a missing
    SKILL.md here means new workspaces don't get the skill."""
    skill_md = _STARTER_BUNDLE_TEMPLATE_DIR / "skills" / skill_name / "SKILL.md"
    assert skill_md.is_file(), (
        f"canonical skill {skill_name!r} missing at {skill_md}"
    )


@pytest.mark.parametrize("skill_name", _CANONICAL_SKILLS)
def test_canonical_skill_frontmatter_parses_with_required_fields(
    skill_name: str,
) -> None:
    """Frontmatter parses via the same parser the catalog uses, has
    ``name`` (matching the directory name) + ``description``,
    declares an ``allowed-tools`` list (informational per Sk-I3 but
    still shape-checked by ``skills validate``), and declares
    ``disable-model-invocation`` explicitly.

    The disable-model-invocation field is functionally equivalent to
    ``false`` when absent (the parser defaults via
    ``bool(metadata.get(..., False))``), but the convention shipped
    by the Step 5 scaffolder is to write it explicitly. Canonical
    skills should follow the convention they ship to users."""
    skill_md = _STARTER_BUNDLE_TEMPLATE_DIR / "skills" / skill_name / "SKILL.md"
    fm = parse_skill_frontmatter(skill_md)
    assert isinstance(fm, dict)
    assert fm
    assert fm["name"] == skill_name, (
        f"name field {fm.get('name')!r} must match directory name "
        f"{skill_name!r} — the catalog uses the dir name as the ref "
        f"segment, so a mismatch would silently produce a confusing ref."
    )
    assert isinstance(fm["description"], str) and fm["description"]
    assert isinstance(fm["allowed-tools"], list)
    # The parser canonicalises ``disable-model-invocation`` →
    # ``disable_model_invocation`` (skill_metadata.py:56-57), so the
    # snake_case key is the post-parse surface to assert on.
    assert "disable_model_invocation" in fm, (
        f"{skill_name} omits `disable-model-invocation:` — canonical "
        "skills should declare it explicitly to match the Step 5 "
        "scaffolder's convention (functionally `false` but explicit)."
    )
    assert isinstance(fm["disable_model_invocation"], bool)


@pytest.mark.parametrize("skill_name", _CANONICAL_SKILLS)
def test_canonical_skill_body_meets_substance_threshold(skill_name: str) -> None:
    """Each canonical skill's body (the prose after the frontmatter
    block) is >= 50 lines. The threshold catches drive-by skill
    drops — a skill should be substantive enough to justify its
    "available" listing in the system prompt.
    """
    skill_md = _STARTER_BUNDLE_TEMPLATE_DIR / "skills" / skill_name / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    # Trim frontmatter — body starts after the second `---`.
    lines = text.splitlines()
    delimiters = [i for i, line in enumerate(lines) if line.strip() == "---"]
    assert len(delimiters) >= 2, "SKILL.md missing the frontmatter delimiters"
    body_lines = lines[delimiters[1] + 1 :]
    nonempty = [line for line in body_lines if line.strip()]
    assert len(nonempty) >= 50, (
        f"{skill_name} body has only {len(nonempty)} non-empty lines; "
        "canonical skills should be substantive enough to justify their "
        "system-prompt listing (50-line floor)."
    )


# ---------------------------------------------------------------------------
# Catalog round-trip.
# ---------------------------------------------------------------------------


async def test_canonical_skills_appear_in_catalog_after_fresh_copy(
    tmp_path: Path,
) -> None:
    """Lay down the kolsch bundle from the packaged template, build a
    fresh AssetManagement against it, and assert each canonical
    skill's ref resolves — proving the dir name → ref translation
    (`kolsch/skills/<name>`) lands as expected.

    This is the production path for fresh workspaces: ``kolsch setup``
    copies the template into ``bundles/kolsch/``; the next catalog
    scan picks the skills up. The test bypasses the rest of the
    setup flow + copies the bundle directly so the assertion is
    about the catalog's reading of the template, not about the
    setup orchestrator."""
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
    shutil.copytree(
        _STARTER_BUNDLE_TEMPLATE_DIR,
        tmp_path / "bundles" / "kolsch",
    )

    am = AssetManagement(root=tmp_path)
    await am.initialize()

    catalogued_skill_refs = {a.ref for a in am.list_assets(asset_type="skill")}
    for skill_name in _CANONICAL_SKILLS:
        assert f"kolsch/skills/{skill_name}" in catalogued_skill_refs


# ---------------------------------------------------------------------------
# Negative — the helper agent stays clean.
# ---------------------------------------------------------------------------


def test_helper_agent_does_not_auto_enable_canonical_skills() -> None:
    """Sk-I6 (enablement is explicit) is load-bearing. The helper
    agent shipped in the starter bundle MUST NOT pre-enable the
    canonical skills — users opt in via ``subsystems.skills.config.enabled``
    or force-invoke with ``/skill <ref>`` at chat time. Auto-enabling
    them on helper would dilute the invariant.

    Post-E-BUG-MODELLINE-001 (a-prime): helper.yaml no longer carries
    the unresolved ``{{MODEL_LINE}}`` placeholder; the model is sourced
    from ``platform.yaml``'s ``defaults.model``. We can yaml.safe_load
    the helper directly."""
    helper_yaml = _STARTER_BUNDLE_TEMPLATE_DIR / "agents" / "helper.yaml"
    payload = yaml.safe_load(helper_yaml.read_text(encoding="utf-8"))
    skills_subsystem = (payload.get("subsystems") or {}).get("skills")
    if skills_subsystem is None:
        return  # Subsystem block absent — strongest form of "not enabled".
    enabled = (skills_subsystem.get("config") or {}).get("enabled") or []
    assert enabled == [], (
        "Helper agent has skills enabled in the packaged template — "
        "Phase 2.4 Step 9 §2 forbids this (Sk-I6 is the invariant). "
        f"Found: {enabled!r}"
    )


# ---------------------------------------------------------------------------
# Migration shim — re-run setup picks up missing skills.
# ---------------------------------------------------------------------------


def test_setup_migration_shim_copies_missing_skills_into_existing_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_maybe_install_missing_kolsch_skills` is the additive
    drift handler that picks up canonical skills missing from a
    user's existing kolsch bundle (an upgrade path — the user's
    workspace was created before Step 9 landed). Doesn't overwrite
    user-edited skills; just adds missing ones.

    This test simulates a pre-Step-9 workspace: kolsch bundle with
    bundle.yaml + agents/ + tools/ but NO skills/ dir. After the
    shim runs, every canonical skill is present.
    """
    import io as _io

    from cli._setup import (
        NonInteractivePrompter,
        _maybe_install_missing_kolsch_skills,
        _SetupIO,
    )

    # Pre-Step-9 kolsch bundle: copy the template, then nuke skills/
    # to simulate the older workspace shape.
    (tmp_path / "bundles").mkdir(parents=True)
    shutil.copytree(
        _STARTER_BUNDLE_TEMPLATE_DIR,
        tmp_path / "bundles" / "kolsch",
    )
    shutil.rmtree(tmp_path / "bundles" / "kolsch" / "skills")
    assert not (tmp_path / "bundles" / "kolsch" / "skills").exists()

    captured = _io.StringIO()
    setup_io = _SetupIO(
        root=tmp_path,
        out=captured,
        err=_io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=lambda _r, _p, _k: None,  # type: ignore[arg-type]
        run_handler=lambda _a: (0, None),  # type: ignore[arg-type]
    )
    _maybe_install_missing_kolsch_skills(setup_io)

    # Every canonical skill landed.
    for skill_name in _CANONICAL_SKILLS:
        assert (
            tmp_path / "bundles" / "kolsch" / "skills" / skill_name / "SKILL.md"
        ).is_file(), f"shim failed to install {skill_name}"

    # The shim announced what it did (count + names).
    rendered = captured.getvalue()
    assert "installed" in rendered
    assert "canonical skill" in rendered
    for skill_name in _CANONICAL_SKILLS:
        assert skill_name in rendered

    _ = capsys  # unused — _SetupIO captures via its own stream
