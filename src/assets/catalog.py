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
from assets.refs import build_ref, parse_three_segment
from assets.skill_metadata import parse_skill_frontmatter
from errors import (
    AssetsBundleNameMismatch,
    AssetsDuplicateBundleName,
    AssetsDuplicateName,
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
    """Built once at startup; immutable thereafter (Am-I7).

    Phase 3.0 Step 1 (Round 5 H3) — :meth:`lookup` is the chokepoint
    for **external** catalog reads. Direct ``catalog.assets[ref]``
    subscript access is permitted ONLY inside this module; all
    other call sites (``src/workflows/*``, ``src/providers/authz.py``,
    ``src/cli/*``, ``src/assets/asset_manager.py``, etc.) route
    through ``catalog.lookup(ref, expected_type=...)``. A custom
    ruff lint rule (queued, see ``process/qol-refs-1seg-removal.md``)
    will enforce this on future commits; for now the discipline is
    documentary + reviewer-grep enforced.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    bundles: dict[str, BundleManifest] = Field(default_factory=dict)
    assets: dict[str, AssetEntry] = Field(default_factory=dict)

    def lookup(
        self, ref: str, expected_type: str | None = None
    ) -> AssetEntry | None:
        """Resolve a 3-segment ref to its :class:`AssetEntry`, or ``None``.

        Phase 3.0 Step 1 (Round 5 H3) — the documented chokepoint for
        catalog access. Combines shape validation (via
        :func:`assets.refs.parse_three_segment`) and dict-get into one
        well-typed call. ``None`` is returned in every "not found"
        scenario so callers can write
        ``entry = catalog.lookup(ref, expected_type="agent")``
        and check truthiness without juggling multiple exception
        classes:

        - ``ref`` is not a 3-seg string → ``None`` (shape miss).
        - ``ref`` shape is valid but the catalog has no entry → ``None``.
        - ``ref`` resolves but its ``asset_type`` differs from
          ``expected_type`` → ``None``.

        Args:
            ref: 3-segment catalog ref (``<bundle>/<plural>/<name>``).
            expected_type: optional singular asset type guard
                (``tool``/``skill``/``agent``/``prompt``/``workflow``/
                ``node``).

        Returns:
            The matching :class:`AssetEntry` or ``None``.
        """
        try:
            parse_three_segment(ref)
        except ValueError:
            return None
        entry = self.assets.get(ref)
        if entry is None:
            return None
        if expected_type is not None and entry.asset_type != expected_type:
            return None
        return entry


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

    # --- Pass 4: build asset entries + uniqueness check ---------------
    # Under the new 2-segment ref convention
    # (qol-prefix-defaults-and-unified-refs.md), `<bundle>/<name>`
    # must resolve to a unique catalog entry. A bundle that ships
    # both `agents/butler.yaml` and `tools/butler.py` cannot
    # disambiguate which one `<bundle>/butler` means; surface the
    # collision at catalog scan time so the failure is loud and
    # early. This check is independent of the existing full-ref
    # dedup (which keys on `<bundle>/<plural>/<name>` and won't
    # catch cross-type collisions within a bundle).
    manifests: dict[str, BundleManifest] = {m.name: m for _, m in raw}
    assets: dict[str, AssetEntry] = {}
    by_bundle_name: dict[tuple[str, str], list[str]] = {}
    for manifest in manifests.values():
        for entry in _scan_bundle_assets(manifest):
            assets[entry.ref] = entry
            name = entry.ref.rsplit("/", 1)[-1]
            by_bundle_name.setdefault((entry.bundle, name), []).append(entry.ref)

    for (bundle, name), refs in by_bundle_name.items():
        if len(refs) > 1:
            raise AssetsDuplicateName(bundle=bundle, name=name, refs=refs)

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
                ref=build_ref(bundle, "tool", stem),
                asset_type="tool",
                bundle=bundle,
                path=str(path.resolve()),
                metadata={},
            )
        )
    # Bug-engine-tools-wiring-gap.md Step B Task 3 — emit one
    # diagnostic INFO line per bundle scanned so ``kolsch run -v``
    # surfaces tool discovery (the absence of this line in the
    # user's repro was Check #2 of the ground-truth investigation).
    # Empty tools/ dirs stay silent to keep the log clean for
    # bundles that don't ship tools.
    if out:
        _log.info(
            "catalog: registered %d tools from bundles/%s/tools/",
            len(out),
            bundle,
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
                ref=build_ref(bundle, "skill", skill_dir.name),
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
                ref=build_ref(bundle, "agent", path.stem),
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
                ref=build_ref(bundle, "prompt", path.stem),
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
