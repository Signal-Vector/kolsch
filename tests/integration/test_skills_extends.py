"""TC-SKILL-COMP-* — extends: composition for skills.config.enabled.

Pins the Phase 2.2 §8.2 "wholesale per field" rule for the skills
subsystem against the production assembly path
(:meth:`AssetManagement.build_resolved_config`). The Step 1 audit of
docs/explanation/skills-subsystem.md called these out as the
locked-decision behaviour worth a regression test even though the
underlying RFC 7386 merge is exercised elsewhere — the integration
between extends-chain resolution + the kernel resolver + the skills
Layer 3 validator is what this file holds the line on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine import LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime


# ---------------------------------------------------------------------------
# Workspace fixture — two skills + a parent agent + two child agents.
# ---------------------------------------------------------------------------


def _write_workspace(root: Path) -> None:
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
    (root / ".env").write_text("", encoding="utf-8")

    bundle = root / "bundles" / "myskills"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump(
            {"name": "myskills", "version": "0.1", "description": "skills demo"}
        ),
        encoding="utf-8",
    )

    for name, description in (
        ("skill-a", "Skill A — enabled by the parent agent."),
        ("skill-b", "Skill B — enabled by the overriding child."),
    ):
        skill_dir = bundle / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
            encoding="utf-8",
        )


def _agent_yaml(extras: dict[str, Any]) -> str:
    base: dict[str, Any] = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "name": "ext",
        "description": "extends regression agent",
        "model": {"provider": "fake", "model": "fake-model"},
        "prompt": "hi",
        "io": {"mode": "conversational"},
    }
    base.update(extras)
    return yaml.safe_dump(base)


async def _stub_llm(_request: LLMRequest) -> LLMResponse:
    return LLMResponse(content="ok")


# ---------------------------------------------------------------------------
# TC-SKILL-COMP-01 — child wholesale-replaces parent's enabled list.
# ---------------------------------------------------------------------------


async def test_tc_skill_comp_01_extends_replaces_skills_enabled_list(
    tmp_path: Path,
) -> None:
    """Per Phase 2.2 §8.2: a child that specifies
    subsystems.skills.config.enabled REPLACES the parent's list
    wholesale (RFC 7386 — arrays are atomic). Pinned at the
    production assembly path so this can't drift even if the
    kernel resolver does."""
    _write_workspace(tmp_path)
    bundle = tmp_path / "bundles" / "myskills" / "agents"

    bundle.joinpath("parent.yaml").write_text(
        _agent_yaml(
            {
                "agent_id": "ag_parent",
                "subsystems": {
                    "skills": {
                        "variant": "standard",
                        "config": {
                            "enabled": [{"ref": "myskills/skill-a"}],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bundle.joinpath("child.yaml").write_text(
        _agent_yaml(
            {
                "agent_id": "ag_child",
                "extends": "bundles/myskills/agents/parent.yaml",
                "subsystems": {
                    "skills": {
                        "variant": "standard",
                        "config": {
                            "enabled": [{"ref": "myskills/skill-b"}],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    resolved = await runtime.build_resolved_config("myskills/child")

    skills_block = resolved.subsystems["skills"].config
    assert skills_block["enabled"] == [{"ref": "myskills/skill-b"}], (
        "Child's enabled list must replace parent's wholesale (Phase 2.2 §8.2). "
        f"Got: {skills_block['enabled']!r}"
    )


# ---------------------------------------------------------------------------
# TC-SKILL-COMP-02 — child silent on subsystems inherits parent's enabled.
# ---------------------------------------------------------------------------


async def test_tc_skill_comp_02_extends_inherits_skills_when_child_omits(
    tmp_path: Path,
) -> None:
    """Per Phase 2.2 §8.2 "Silence vs explicit-empty" table: a child
    that does not declare subsystems at all defers to the parent's
    value. The parent's skills.config.enabled flows through to the
    resolved config unchanged."""
    _write_workspace(tmp_path)
    bundle = tmp_path / "bundles" / "myskills" / "agents"

    bundle.joinpath("parent.yaml").write_text(
        _agent_yaml(
            {
                "agent_id": "ag_parent",
                "subsystems": {
                    "skills": {
                        "variant": "standard",
                        "config": {
                            "enabled": [{"ref": "myskills/skill-a"}],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    # Child carries identity overrides but is silent on subsystems —
    # the parent's whole subsystems block (including skills) survives.
    bundle.joinpath("child_silent.yaml").write_text(
        _agent_yaml(
            {
                "agent_id": "ag_child_silent",
                "name": "child-silent",
                "extends": "bundles/myskills/agents/parent.yaml",
            }
        ),
        encoding="utf-8",
    )

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    resolved = await runtime.build_resolved_config("myskills/child_silent")

    skills_block = resolved.subsystems["skills"].config
    assert skills_block["enabled"] == [{"ref": "myskills/skill-a"}], (
        "Silent child must inherit parent's enabled list (Phase 2.2 §8.2). "
        f"Got: {skills_block['enabled']!r}"
    )
    # Sanity: the child's own identity overrides still landed.
    assert str(resolved.agent_id) == "ag_child_silent"
    assert resolved.name == "child-silent"
