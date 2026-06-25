"""Component tests for src/assets — TC-ASM-01..12.

Spec: docs/explanation/asset-management-contract.md +
Module API Reference §2. The asset manager bootstraps the platform
(loads platform.yaml, builds the catalog) and resolves references
into content for the kernel pipeline.

These are component tests per handbook §8.1: the asset manager runs
against a real on-disk bundles directory and a real platform.yaml
in a tmp_path-rooted layout.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from assets import AssetManagement
from errors import (
    AssetsBundleNameMismatch,
    AssetsDuplicateBundleName,
    AssetsNotFound,
    ConfigInvalid,
)
from schemas import ResolvedAgentConfig

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Bundle/platform-root fixtures
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path) -> Path:
    """Create the canonical platform-root subdirectories under tmp_path."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _write_platform_yaml(root: Path, payload: dict[str, Any] | None = None) -> None:
    """Write platform.yaml; default is a sensible minimal config."""
    payload = payload or _default_platform_payload()
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _default_platform_payload() -> dict[str, Any]:
    return {
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
        "defaults": {
            "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
        },
    }


def _write_bundle(
    root: Path,
    *,
    dir_name: str,
    manifest_name: str | None = None,
    version: str = "0.1.0",
    description: str = "test bundle",
    tools: dict[str, str] | None = None,
    skills: dict[str, tuple[dict[str, Any], str]] | None = None,
    agents: dict[str, dict[str, Any]] | None = None,
    prompts: dict[str, str] | None = None,
    skip_manifest: bool = False,
) -> Path:
    """Author a bundle on disk and return its directory path.

    - ``tools``: ``{filename_stem: python_source}``
    - ``skills``: ``{skill_dir_name: (frontmatter_dict, body_text)}``
    - ``agents``: ``{agent_name: agent_yaml_payload}``
    - ``prompts``: ``{prompt_name: text}``
    """
    bundle = root / "bundles" / dir_name
    bundle.mkdir(parents=True, exist_ok=True)
    if not skip_manifest:
        manifest = {
            "name": manifest_name or dir_name,
            "version": version,
            "description": description,
        }
        (bundle / "bundle.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    if tools:
        (bundle / "tools").mkdir(exist_ok=True)
        for stem, source in tools.items():
            (bundle / "tools" / f"{stem}.py").write_text(source, encoding="utf-8")

    if skills:
        (bundle / "skills").mkdir(exist_ok=True)
        for name, (frontmatter, body) in skills.items():
            skill_dir = bundle / "skills" / name
            skill_dir.mkdir(exist_ok=True)
            fm_yaml = yaml.safe_dump(frontmatter)
            content = f"---\n{fm_yaml}---\n\n{body}\n"
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    if agents:
        (bundle / "agents").mkdir(exist_ok=True)
        for name, payload in agents.items():
            (bundle / "agents" / f"{name}.yaml").write_text(
                yaml.safe_dump(payload), encoding="utf-8"
            )

    if prompts:
        (bundle / "prompts").mkdir(exist_ok=True)
        for name, text in prompts.items():
            (bundle / "prompts" / f"{name}.md").write_text(text, encoding="utf-8")

    return bundle


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    return _make_root(tmp_path)


# ---------------------------------------------------------------------------
# TC-ASM-01 — catalog build registers expected assets
# ---------------------------------------------------------------------------


async def test_catalog_build_registers_expected_assets(
    platform_root: Path,
) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        tools={"web_search": "def main(): pass\n"},
        skills={
            "literature-review": (
                {"name": "literature-review", "description": "review papers"},
                "Body.",
            )
        },
        agents={"researcher": {"name": "researcher"}},
        prompts={"role": "you are a researcher"},
    )
    _write_bundle(
        platform_root,
        dir_name="personal",
        prompts={"context": "project context"},
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    # Phase 3.0 Step 3 — the catalog also registers 14 virtual
    # ``kolsch/nodes/<name>`` built-in node entries unconditionally.
    # Filter them out so the assertion stays focused on the
    # user-authored assets under test.
    refs = {
        a.ref
        for a in am.list_assets()
        if not a.metadata.get("builtin")
    }
    assert refs == {
        "acme/tools/web_search",
        "acme/skills/literature-review",
        "acme/agents/researcher",
        "acme/prompts/role",
        "personal/prompts/context",
    }


# ---------------------------------------------------------------------------
# TC-ASM-02 — duplicate bundle name is fatal
# ---------------------------------------------------------------------------


async def test_duplicate_bundle_name_is_fatal(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    # Two distinct directories declaring the same manifest name.
    _write_bundle(platform_root, dir_name="alpha", manifest_name="dup")
    _write_bundle(platform_root, dir_name="beta", manifest_name="dup")

    am = AssetManagement(root=platform_root)
    with pytest.raises(AssetsDuplicateBundleName) as exc_info:
        await am.initialize()
    # The error names the conflict and BOTH paths.
    assert exc_info.value.detail["name"] == "dup"
    assert len(exc_info.value.detail["paths"]) == 2


# ---------------------------------------------------------------------------
# TC-ASM-03 — bundle name / directory mismatch is fatal
# ---------------------------------------------------------------------------


async def test_bundle_name_mismatch_is_fatal(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(platform_root, dir_name="foo", manifest_name="bar")

    am = AssetManagement(root=platform_root)
    with pytest.raises(AssetsBundleNameMismatch):
        await am.initialize()


# ---------------------------------------------------------------------------
# TC-ASM-04 — `core` is NOT a reserved bundle name (Phase 3.0 R3 #5)
# The platform-shipped bundle is `kolsch`; `core` was speculative for the
# dropped `core/principals/*` design and carries no special meaning.
# ---------------------------------------------------------------------------


async def test_bundle_name_core_is_accepted(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(platform_root, dir_name="core")

    am = AssetManagement(root=platform_root)
    await am.initialize()
    # Catalog initializes cleanly — no reserved-name exception fires.
    assert any(
        m.name == "core" for m in am._catalog.bundles.values()  # noqa: SLF001
    )


# ---------------------------------------------------------------------------
# TC-ASM-05 — resolve_ref returns the 3-segment catalog key
# (Phase 3.0 Step 1B — shorthand resolution removed; ref grammar
# is strict 3-seg only)
# ---------------------------------------------------------------------------


async def test_resolve_ref_returns_catalog_key(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        tools={"web_search": "def main(): pass\n"},
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    resolved = await am.resolve_ref("acme/tools/web_search", asset_type="tool")
    assert resolved == "acme/tools/web_search"


async def test_resolve_ref_unknown_raises_not_found(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        tools={"web_search": "def main(): pass\n"},
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    with pytest.raises(AssetsNotFound):
        await am.resolve_ref("acme/tools/missing", asset_type="tool")


# ---------------------------------------------------------------------------
# TC-ASM-07 — prompt refs pre-resolved, tool/skill refs not
# ---------------------------------------------------------------------------


async def test_prompts_pre_resolved_tools_and_skills_pass_through(
    platform_root: Path,
) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        prompts={"role": "You are an agent."},
        skills={
            "lit-review": (
                {"name": "lit-review", "description": "literature review"},
                "Body",
            )
        },
        tools={"web_search": "def main(): pass\n"},
        agents={
            "researcher": {
                "schema_version": "0.1",
                "assembly_version": "0.1",
                "agent_id": "ag_researcher",
                "name": "researcher",
                "description": "a researcher",
                "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
                "prompt": ["acme/prompts/role"],
                "io": {"mode": "conversational"},
                "subsystems": {
                    "tools": {
                        "variant": "default",
                        "config": {"declared": [{"ref": "acme/tools/web_search"}]},
                    },
                    "skills": {
                        "variant": "default",
                        "config": {"enabled": ["acme/skills/lit-review"]},
                    },
                },
            }
        },
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    # The engine supplies subsystem validators; for this Am-I4 / Am-I5
    # test we don't care about per-variant body validation — accept all.
    from kernel import ValidationResult

    def _accept_anything(_body: dict[str, Any]) -> ValidationResult:
        return ValidationResult(ok=True)

    cfg = await am.build_resolved_config(
        "acme/agents/researcher",
        subsystem_validators={
            "tools": {"default": _accept_anything},
            "skills": {"default": _accept_anything},
        },
    )
    assert isinstance(cfg, ResolvedAgentConfig)

    # Am-I4 — prompt refs pre-resolved to actual content.
    assert cfg.prompt == ["You are an agent."]

    # Am-I5 — tool and skill refs pass through unchanged.
    tools_cfg = cfg.subsystems["tools"].config
    assert tools_cfg["declared"][0]["ref"] == "acme/tools/web_search"
    skills_cfg = cfg.subsystems["skills"].config
    assert skills_cfg["enabled"] == ["acme/skills/lit-review"]


# ---------------------------------------------------------------------------
# TC-ASM-08 — platform.yaml sections dispatch correctly
# ---------------------------------------------------------------------------


async def test_platform_config_sections_dispatch_distinctly(
    platform_root: Path,
) -> None:
    payload = _default_platform_payload()
    # Tag each section with a distinct marker so we can verify dispatch.
    payload["secrets"]["config"]["MARKER"] = "secrets-marker"
    payload["sandbox"]["config"]["MARKER"] = "sandbox-marker"
    payload["authorization"]["config"]["MARKER"] = "authz-marker"
    payload["storage"]["MARKER"] = "storage-marker"
    payload["event_bus"]["MARKER"] = "bus-marker"
    payload["file_access"]["MARKER"] = "fil-marker"
    payload["defaults"]["MARKER"] = "defaults-marker"
    _write_platform_yaml(platform_root, payload)

    am = AssetManagement(root=platform_root)
    await am.initialize()

    assert am.get_platform_config("secrets")["config"]["MARKER"] == "secrets-marker"
    assert am.get_platform_config("sandbox")["config"]["MARKER"] == "sandbox-marker"
    assert am.get_platform_config("authorization")["config"]["MARKER"] == "authz-marker"
    assert am.get_platform_config("storage")["MARKER"] == "storage-marker"
    assert am.get_platform_config("event_bus")["MARKER"] == "bus-marker"
    assert am.get_platform_config("file_access")["MARKER"] == "fil-marker"
    assert am.get_platform_config("defaults")["MARKER"] == "defaults-marker"


# ---------------------------------------------------------------------------
# TC-ASM-09 — missing bundle.yaml skipped with warning
# ---------------------------------------------------------------------------


async def test_missing_bundle_yaml_skipped_with_warning(
    platform_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="good",
        prompts={"role": "ok"},
    )
    # Create an invalid bundle directory (no bundle.yaml).
    (platform_root / "bundles" / "broken").mkdir()

    am = AssetManagement(root=platform_root)
    import logging

    with caplog.at_level(logging.WARNING, logger="assets"):
        await am.initialize()

    bundles = {b.name for b in am.list_bundles()}
    assert bundles == {"good"}  # `broken` was skipped, not registered
    assert any("broken" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TC-ASM-10 — skill metadata captured (body NOT loaded)
# ---------------------------------------------------------------------------


async def test_skill_metadata_captured_in_catalog(
    platform_root: Path,
) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        skills={
            "lit-review": (
                {
                    "name": "lit-review",
                    "description": "review papers",
                    "license": "MIT",
                    "compatibility": "claude-4",
                    "metadata": {"category": "research"},
                    "allowed_tools": ["web_search", "read"],
                    "disable-model-invocation": False,
                },
                "Skill body text that should NOT be loaded at catalog build.",
            )
        },
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    entry = await am.get_asset("acme/skills/lit-review")
    md = entry.metadata
    assert md["name"] == "lit-review"
    assert md["description"] == "review papers"
    assert md["license"] == "MIT"
    assert md["compatibility"] == "claude-4"
    assert md["metadata"] == {"category": "research"}
    assert md["allowed_tools"] == ["web_search", "read"]
    # YAML frontmatter key is kebab-case; we surface it verbatim.
    assert md["disable_model_invocation"] is False
    # The body is NOT loaded into the catalog entry.
    assert "body" not in md


# ---------------------------------------------------------------------------
# TC-ASM-11 — malformed platform.yaml is fatal
# ---------------------------------------------------------------------------


async def test_malformed_platform_yaml_is_fatal(platform_root: Path) -> None:
    # Top-level structure is a list, not a mapping → invalid.
    (platform_root / "platform.yaml").write_text("- not\n- a\n- mapping\n")
    am = AssetManagement(root=platform_root)
    with pytest.raises(ConfigInvalid):
        await am.initialize()


async def test_unparseable_platform_yaml_is_fatal(platform_root: Path) -> None:
    (platform_root / "platform.yaml").write_text("{:::: invalid")
    am = AssetManagement(root=platform_root)
    with pytest.raises(ConfigInvalid):
        await am.initialize()


# ---------------------------------------------------------------------------
# TC-ASM-12 — first-run generates default platform.yaml
# ---------------------------------------------------------------------------


async def test_first_run_generates_default_platform_yaml(
    platform_root: Path,
) -> None:
    # No platform.yaml on disk.
    assert not (platform_root / "platform.yaml").exists()

    am = AssetManagement(root=platform_root)
    await am.initialize()

    assert (platform_root / "platform.yaml").exists()
    # Default sections are present and usable.
    storage = am.get_platform_config("storage")
    assert storage["entity_backend"] == "sqlite"
    bus_cfg = am.get_platform_config("event_bus")
    assert bus_cfg["backend"] == "in_process"
    file_access = am.get_platform_config("file_access")
    assert "allowed_paths" in file_access and "denied_paths" in file_access


# ---------------------------------------------------------------------------
# Spec invariants — additional coverage
# ---------------------------------------------------------------------------


async def test_get_asset_unknown_ref_raises(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    am = AssetManagement(root=platform_root)
    await am.initialize()
    with pytest.raises(AssetsNotFound):
        await am.get_asset("acme/tools/missing")


async def test_invalid_ref_raises(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    am = AssetManagement(root=platform_root)
    await am.initialize()
    # Phase 3.0 Step 1B — non-3-seg input fails the catalog
    # shape-check inside ``AssetCatalog.lookup`` and surfaces as
    # :class:`AssetsNotFound` (no shorthand fallback to attempt).
    with pytest.raises(AssetsNotFound):
        await am.get_asset("no-slashes")


async def test_catalog_is_read_only_no_python_imported(
    platform_root: Path,
) -> None:
    """Am-I10 — catalog build MUST NOT execute Python from tools/."""
    # If the tool file is imported, this side-effect would land in a
    # local marker file. The test asserts no such side effect occurs.
    marker = platform_root / "side_effect.marker"
    source = textwrap.dedent(
        f"""
        from pathlib import Path
        Path({str(marker)!r}).write_text("imported!")
        """
    )
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        tools={"side_effect": source},
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()

    assert not marker.exists(), (
        "Asset Management MUST NOT import tool Python at catalog build (spec Am-I10)"
    )
    # The tool was still registered (path-only).
    entry = await am.get_asset("acme/tools/side_effect")
    assert entry.path.endswith("side_effect.py")


async def test_non_mapping_agent_yaml_raises_config_invalid(
    platform_root: Path,
) -> None:
    """A non-mapping agent .yaml file MUST surface as a typed
    PlatformError (ConfigInvalid), not a raw TypeError. Handbook
    hard rule: no non-PlatformError escapes a public method."""
    _write_platform_yaml(platform_root)
    bundle = _write_bundle(platform_root, dir_name="acme")
    (bundle / "agents").mkdir()
    # YAML list — not a mapping.
    (bundle / "agents" / "broken.yaml").write_text(
        "- not\n- a\n- mapping\n", encoding="utf-8"
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()
    with pytest.raises(ConfigInvalid):
        await am.build_resolved_config("acme/agents/broken")


async def test_prompt_list_with_inline_url_is_not_treated_as_ref(
    platform_root: Path,
) -> None:
    """An inline string in a list-shape PromptSpec that happens to
    contain a slash (e.g. a URL) MUST NOT be treated as a prompt ref.
    Only well-shaped <bundle>/prompts/<name> values trigger lookup."""
    _write_platform_yaml(platform_root)
    inline = "See https://example.com for context."
    _write_bundle(
        platform_root,
        dir_name="acme",
        agents={
            "researcher": {
                "schema_version": "0.1",
                "assembly_version": "0.1",
                "agent_id": "ag_researcher",
                "name": "researcher",
                "description": "a researcher",
                "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
                "prompt": [inline],
                "io": {"mode": "conversational"},
            }
        },
    )

    am = AssetManagement(root=platform_root)
    await am.initialize()
    cfg = await am.build_resolved_config("acme/agents/researcher")
    # The URL passed through verbatim — no AssetsPromptLoadFailed.
    assert cfg.prompt == [inline]


async def test_list_assets_filtered_by_type(platform_root: Path) -> None:
    _write_platform_yaml(platform_root)
    _write_bundle(
        platform_root,
        dir_name="acme",
        tools={"a": "x = 1\n", "b": "y = 2\n"},
        prompts={"p1": "hi"},
    )
    am = AssetManagement(root=platform_root)
    await am.initialize()
    assert {a.ref for a in am.list_assets(asset_type="tool")} == {
        "acme/tools/a",
        "acme/tools/b",
    }
    assert {a.ref for a in am.list_assets(asset_type="prompt")} == {"acme/prompts/p1"}
