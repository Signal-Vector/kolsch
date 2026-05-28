"""Asset catalog — spec §4.

Builds and holds the in-memory record of every bundle + every asset
on disk. Read-only with respect to the filesystem (Am-I10): scans
directories, parses YAML, reads SKILL.md frontmatter; never imports
tool Python or executes asset code.

Public surface:

- :class:`AssetEntry` — one row per asset.
- :class:`AssetCatalog` — the collection + lookup helpers.
- :func:`scan` — build the catalog from a platform root.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assets.manifest import (
    BundleManifest,
    is_valid_bundle_name,
    load_manifest,
)
from assets.skill_metadata import parse_skill_frontmatter
from errors import (
    AssetsBundleNameMismatch,
    AssetsDuplicateBundleName,
    AssetsReservedBundleName,
)

_log = logging.getLogger("assets")

_RESERVED_BUNDLE_NAMES: frozenset[str] = frozenset({"core"})
_ASSET_TYPES: frozenset[str] = frozenset({"tool", "skill", "agent", "prompt"})


class AssetEntry(BaseModel):
    """One catalogued asset — spec §4.2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    asset_type: str
    bundle: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetCatalog(BaseModel):
    """Built once at startup; immutable thereafter (Am-I7)."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    bundles: dict[str, BundleManifest] = Field(default_factory=dict)
    assets: dict[str, AssetEntry] = Field(default_factory=dict)


def scan(root: Path) -> AssetCatalog:
    """Walk ``root/bundles/`` and build the catalog.

    Raises:
        AssetsReservedBundleName: a bundle uses the reserved ``core``
            name (Am-I3).
        AssetsDuplicateBundleName: two bundles share a manifest name
            (Am-I2). Detected BEFORE the directory-name match check
            so a same-named pair surfaces as a duplicate rather than
            the first-encountered mismatch.
        AssetsBundleNameMismatch: a manifest's ``name`` does not
            match its directory (Am-I1).

    Bundles with missing/malformed ``bundle.yaml`` are skipped with
    a warning (spec §4.1 step 2).
    """
    bundles_dir = root / "bundles"
    if not bundles_dir.exists():
        return AssetCatalog()

    # --- Pass 1: load every well-formed manifest -----------------------
    raw: list[tuple[Path, BundleManifest]] = []
    for bundle_dir in sorted(bundles_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
        if bundle_dir.name in _RESERVED_BUNDLE_NAMES:
            raise AssetsReservedBundleName(name=bundle_dir.name)
        if not is_valid_bundle_name(bundle_dir.name):
            _log.warning(
                "Skipping bundle directory %s: invalid name "
                "(must be lowercase alphanumeric with - and _)",
                bundle_dir.name,
            )
            continue
        manifest = load_manifest(bundle_dir)
        if manifest is None:
            _log.warning(
                "Skipping bundle %s: missing or malformed bundle.yaml",
                bundle_dir.name,
            )
            continue
        raw.append((bundle_dir, manifest))

    # --- Pass 2: duplicate-name detection (Am-I2) ----------------------
    by_name: dict[str, list[str]] = {}
    for _, manifest in raw:
        by_name.setdefault(manifest.name, []).append(manifest.path)
    duplicates = {n: p for n, p in by_name.items() if len(p) > 1}
    if duplicates:
        name = next(iter(duplicates))
        raise AssetsDuplicateBundleName(name=name, paths=duplicates[name])

    # --- Pass 3: name == directory match (Am-I1) -----------------------
    for bundle_dir, manifest in raw:
        if manifest.name != bundle_dir.name:
            raise AssetsBundleNameMismatch(
                directory=bundle_dir.name,
                manifest_name=manifest.name,
            )

    # --- Pass 4: build asset entries -----------------------------------
    manifests: dict[str, BundleManifest] = {m.name: m for _, m in raw}
    assets: dict[str, AssetEntry] = {}
    for manifest in manifests.values():
        for entry in _scan_bundle_assets(manifest):
            assets[entry.ref] = entry

    return AssetCatalog(bundles=manifests, assets=assets)


def _scan_bundle_assets(manifest: BundleManifest) -> list[AssetEntry]:
    """Walk a bundle's four asset subdirectories and produce entries."""
    bundle_dir = Path(manifest.path)
    entries: list[AssetEntry] = []
    entries.extend(_scan_tools(manifest.name, bundle_dir))
    entries.extend(_scan_skills(manifest.name, bundle_dir))
    entries.extend(_scan_agents(manifest.name, bundle_dir))
    entries.extend(_scan_prompts(manifest.name, bundle_dir))
    return entries


def _scan_tools(bundle: str, bundle_dir: Path) -> list[AssetEntry]:
    tools_dir = bundle_dir / "tools"
    if not tools_dir.exists():
        return []
    out: list[AssetEntry] = []
    for path in sorted(tools_dir.glob("*.py")):
        stem = path.stem
        # Spec §4.3: the catalog entry's ref segment is intended to
        # be the @tool decorator's `name`. Without importing the
        # Python file (Am-I10) we cannot read the decorator; v1 uses
        # the filename stem as the ref segment. A single file with
        # multiple @tool decorators surfaces a single catalog entry
        # for the file; the Tools subsystem (which DOES import at
        # registration time) expands per-decorator at that point.
        out.append(
            AssetEntry(
                ref=f"{bundle}/tools/{stem}",
                asset_type="tool",
                bundle=bundle,
                path=str(path.resolve()),
                metadata={},
            )
        )
    return out


def _scan_skills(bundle: str, bundle_dir: Path) -> list[AssetEntry]:
    skills_dir = bundle_dir / "skills"
    if not skills_dir.exists():
        return []
    out: list[AssetEntry] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            _log.warning(
                "Skipping skill %s/%s: SKILL.md missing",
                bundle,
                skill_dir.name,
            )
            continue
        metadata = parse_skill_frontmatter(skill_md)
        out.append(
            AssetEntry(
                ref=f"{bundle}/skills/{skill_dir.name}",
                asset_type="skill",
                bundle=bundle,
                path=str(skill_md.resolve()),
                metadata=metadata,
            )
        )
    return out


def _scan_agents(bundle: str, bundle_dir: Path) -> list[AssetEntry]:
    agents_dir = bundle_dir / "agents"
    if not agents_dir.exists():
        return []
    out: list[AssetEntry] = []
    for path in sorted(agents_dir.glob("*.yaml")):
        out.append(
            AssetEntry(
                ref=f"{bundle}/agents/{path.stem}",
                asset_type="agent",
                bundle=bundle,
                path=str(path.resolve()),
                metadata={},
            )
        )
    return out


def _scan_prompts(bundle: str, bundle_dir: Path) -> list[AssetEntry]:
    prompts_dir = bundle_dir / "prompts"
    if not prompts_dir.exists():
        return []
    out: list[AssetEntry] = []
    for path in sorted(prompts_dir.iterdir()):
        if not path.is_file():
            continue
        out.append(
            AssetEntry(
                ref=f"{bundle}/prompts/{path.stem}",
                asset_type="prompt",
                bundle=bundle,
                path=str(path.resolve()),
                metadata={},
            )
        )
    return out


def known_asset_types() -> frozenset[str]:
    """Spec §4.3 — the four catalogued asset types."""
    return _ASSET_TYPES
