"""Component tests for src/skills — TC-SKL-01..05.

Spec: docs/explanation/skills-subsystem.md + Module API Reference §10.

Skills resolve agent-config refs against the asset catalog, project
metadata into a system-prompt block (omitting
``disable-model-invocation: true`` skills per Sk-I2), and prepare
force-invoke handoffs the engine drives through ``tool.read`` (Sk-I4).
Skills themselves emit no events (Sk-I5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from assets import AssetManagement
from errors import SkillForcedNotEnabled, SkillNotFound
from schemas import (
    AgentId,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SubsystemConfig,
)
from skills import (
    EnabledSkill,
    SkillInvocation,
    SkillsSubsystem,
    make_standard_validator,
)

# qol-skills-jinja-variable-symmetric-with-tools — SkillsVar replaces
# the pre-QoL ``format_skills_listing`` projection. Tests that used to
# assert on the post-append string assert on the Var's ``.list()`` /
# ``.summary()`` outputs instead.
from templating.vars import SkillItem, SkillsVar

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path) -> Path:
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
    return tmp_path


def _write_skill(
    root: Path,
    bundle: str,
    name: str,
    frontmatter: dict[str, Any],
    body: str = "Body content.",
) -> None:
    bundle_dir = root / "bundles" / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest = bundle_dir / "bundle.yaml"
    if not manifest.exists():
        manifest.write_text(
            yaml.safe_dump(
                {"name": bundle, "version": "0.1.0", "description": "test bundle"}
            ),
            encoding="utf-8",
        )
    skill_dir = bundle_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm}---\n\n{body}\n",
        encoding="utf-8",
    )


def _config(enabled_refs: list[str]) -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="0.1",
        agent_id=AgentId("ag_test"),
        name="test",
        description="t",
        model=ModelSpec(provider="anthropic", model="claude-opus-4-7"),
        prompt="hi",
        io=IOSpec(mode="single_shot"),
        subsystems={
            "skills": SubsystemConfig(
                variant="standard",
                config={"enabled": [{"ref": r} for r in enabled_refs]},
            ),
        },
    )


@pytest.fixture
async def am(tmp_path: Path) -> AssetManagement:
    root = _make_root(tmp_path)
    _write_skill(
        root,
        "acme",
        "literature-review",
        {
            "name": "literature-review",
            "description": "Conduct a structured literature review.",
            "license": "MIT",
        },
    )
    _write_skill(
        root,
        "acme",
        "dangerous-cleanup",
        {
            "name": "dangerous-cleanup",
            "description": "Wipe transient state.",
            "disable-model-invocation": True,
            "allowed-tools": ["bash", "write"],
        },
    )
    am_ = AssetManagement(root=root)
    await am_.initialize()
    return am_


# ===========================================================================
# TC-SKL-01 — Enabled skill in prompt listing
# ===========================================================================


async def test_tc_skl_01_enabled_skill_in_listing(am: AssetManagement) -> None:
    """qol-skills-jinja-variable-symmetric-with-tools — the
    ``format_skills_listing`` projection is gone; SkillsVar's
    ``.list()`` is the same per-skill rendering."""
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config(["acme/skills/literature-review"])

    skills = await sub.get_enabled_skills(cfg)
    var = SkillsVar(
        [SkillItem(name=s.name, description=s.description, ref=s.ref) for s in skills]
    )
    listing = var.list()

    assert [s.ref for s in skills] == ["acme/skills/literature-review"]
    assert skills[0].name == "literature-review"
    assert skills[0].license == "MIT"
    assert "literature-review" in listing
    assert "Conduct a structured literature review." in listing
    # Read path is the ref-derived logical path the model uses.
    assert "acme/skills/literature-review/SKILL.md" in listing
    # Sk-I1 negative half — bodies MUST NOT leak into the listing.
    # The fixture body is "Body content."; surfacing it would mean
    # someone started reading SKILL.md instead of catalog metadata.
    assert "Body content." not in listing


# ===========================================================================
# TC-SKL-02 — disable-model-invocation excluded from listing
# ===========================================================================


async def test_tc_skl_02_disable_excluded_from_listing(am: AssetManagement) -> None:
    """Sk-I2 — disabled skill is returned by ``get_enabled_skills``
    (so force-invoke can route to it) but the engine's pre-flight
    skips it when building the SkillsVar items.

    qol-skills-jinja-variable-symmetric-with-tools — the engine
    builds SkillsVar from non-disabled skills only (matching the
    pre-QoL behaviour of ``format_skills_listing`` filtering by
    ``disable_model_invocation``). This test mirrors that filter
    locally so the assertion stays a pure projection test.
    """
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config(
        [
            "acme/skills/literature-review",
            "acme/skills/dangerous-cleanup",
        ]
    )

    skills = await sub.get_enabled_skills(cfg)
    refs = {s.ref for s in skills}
    assert refs == {
        "acme/skills/literature-review",
        "acme/skills/dangerous-cleanup",
    }
    disabled = next(s for s in skills if s.ref.endswith("/dangerous-cleanup"))
    assert disabled.disable_model_invocation is True

    # The Var is built from non-disabled entries only — matching the
    # engine's pre-flight construction at engine.py:_render_prompt_if_needed.
    var = SkillsVar(
        [
            SkillItem(name=s.name, description=s.description, ref=s.ref)
            for s in skills
            if not s.disable_model_invocation
        ]
    )
    listing = var.list()
    assert "literature-review" in listing
    assert "dangerous-cleanup" not in listing


# ===========================================================================
# TC-SKL-03 — disabled-from-model skill still force-invocable
# ===========================================================================


async def test_tc_skl_03_disabled_still_force_invocable(
    am: AssetManagement,
) -> None:
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config(
        [
            "acme/skills/literature-review",
            "acme/skills/dangerous-cleanup",
        ]
    )

    invocation = await sub.force_invoke("acme/skills/dangerous-cleanup", cfg)

    # Sk-I4 — the engine passes this path to a synthetic `read` call;
    # the skills subsystem itself emits nothing (Sk-I5).
    assert isinstance(invocation, SkillInvocation)
    assert invocation.ref == "acme/skills/dangerous-cleanup"
    assert invocation.skill_md_path.endswith("/dangerous-cleanup/SKILL.md")


# ===========================================================================
# TC-SKL-04 — Force-invoke of non-enabled skill rejected
# ===========================================================================


async def test_tc_skl_04_force_invoke_of_non_enabled_rejected(
    am: AssetManagement,
) -> None:
    sub = SkillsSubsystem(asset_management=am)
    # dangerous-cleanup is in the catalog but NOT in this agent's enabled list.
    cfg = _config(["acme/skills/literature-review"])

    with pytest.raises(SkillForcedNotEnabled) as exc_info:
        await sub.force_invoke("acme/skills/dangerous-cleanup", cfg)
    assert exc_info.value.detail["ref"] == "acme/skills/dangerous-cleanup"
    assert exc_info.value.detail["agent_id"] == "ag_test"


# ===========================================================================
# TC-SKL-05 — Force-invoke uses tool.read authorization
# ===========================================================================


async def test_tc_skl_05_invocation_shape_pins_tool_read_only(
    am: AssetManagement,
) -> None:
    """The skills subsystem returns only the SKILL.md path; the engine
    then invokes ``read`` with regular ``tool.read`` authorization
    (Sk-I4 + spec §6.3). This test pins the contract at the skills
    layer: ``SkillInvocation`` carries no skill-specific authz action —
    only ``ref`` and ``skill_md_path``. The engine cannot route via a
    ``skill.*`` authz namespace because there isn't one to surface."""
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config(["acme/skills/literature-review"])

    invocation = await sub.force_invoke("acme/skills/literature-review", cfg)

    assert set(invocation.model_dump().keys()) == {"ref", "skill_md_path"}
    assert invocation.skill_md_path.endswith("/literature-review/SKILL.md")


# ===========================================================================
# Additional coverage — §10.9 success criteria + Layer 3 validator
# ===========================================================================


async def test_get_enabled_skills_unknown_ref_raises_skill_not_found(
    am: AssetManagement,
) -> None:
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config(["acme/skills/does-not-exist"])

    with pytest.raises(SkillNotFound) as exc_info:
        await sub.get_enabled_skills(cfg)
    assert exc_info.value.detail["ref"] == "acme/skills/does-not-exist"


async def test_get_enabled_skills_empty_config_returns_empty_list(
    am: AssetManagement,
) -> None:
    sub = SkillsSubsystem(asset_management=am)
    cfg = _config([])
    assert await sub.get_enabled_skills(cfg) == []


def test_skills_var_empty_renders_empty_string() -> None:
    """qol-skills-jinja-variable-symmetric-with-tools — empty
    SkillsVar renders to ``""`` so ``{{ skills }}`` against a
    skill-less agent contributes nothing (the
    ``agent.template_warning`` walker catches the
    referenced-but-empty case separately)."""
    assert SkillsVar([]).list() == ""
    assert str(SkillsVar([])) == ""
    assert SkillsVar([]).count == 0


def test_skills_var_engine_filter_excludes_disabled() -> None:
    """The engine's pre-flight filter (matches the pre-QoL
    ``format_skills_listing`` Sk-I2 filter) — engine.py:
    ``[SkillItem(...) for s in enabled_skills if not s.disable_model_invocation]``.

    This test simulates the filter at the EnabledSkill layer and
    asserts the resulting Var is empty when every skill is
    disabled — same outcome as the pre-QoL all-disabled case."""
    items = [
        EnabledSkill(
            ref="acme/skills/x",
            name="x",
            description="d",
            license=None,
            compatibility=None,
            metadata={},
            allowed_tools=[],
            disable_model_invocation=True,
        ),
    ]
    visible = [
        SkillItem(name=s.name, description=s.description, ref=s.ref)
        for s in items
        if not s.disable_model_invocation
    ]
    assert SkillsVar(visible).list() == ""


def test_layer3_validator_accepts_well_formed_enabled_list(
    am: AssetManagement,
) -> None:
    validator = make_standard_validator(am)
    result = validator({"enabled": [{"ref": "acme/skills/literature-review"}]})
    assert result.ok is True
    assert result.errors == []


def test_layer3_validator_rejects_unknown_ref(am: AssetManagement) -> None:
    validator = make_standard_validator(am)
    result = validator({"enabled": [{"ref": "acme/skills/does-not-exist"}]})
    assert result.ok is False
    assert all(e.source == "skills.standard" for e in result.errors)
    assert any(e.code == "ref_not_found" for e in result.errors)


def test_layer3_validator_rejects_duplicate_refs(am: AssetManagement) -> None:
    validator = make_standard_validator(am)
    ref = "acme/skills/literature-review"
    result = validator({"enabled": [{"ref": ref}, {"ref": ref}]})
    assert result.ok is False
    assert any(e.code == "duplicate_ref" for e in result.errors)


def test_layer3_validator_rejects_non_list_enabled(am: AssetManagement) -> None:
    validator = make_standard_validator(am)
    result = validator({"enabled": "nope"})
    assert result.ok is False
    assert any(e.code == "not_a_list" for e in result.errors)


def test_layer3_validator_rejects_malformed_entries(am: AssetManagement) -> None:
    validator = make_standard_validator(am)
    result = validator({"enabled": ["just-a-string"]})
    assert result.ok is False
    assert any(e.code == "malformed_entry" for e in result.errors)
