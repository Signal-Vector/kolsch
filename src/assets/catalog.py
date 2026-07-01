"""Asset catalog — the in-memory record of every bundle and asset.

Read-only with respect to the filesystem: scans directories, parses
YAML, reads `SKILL.md` frontmatter; never imports tool Python or
executes asset code.

- [`AssetEntry`][assets.AssetEntry] — one row per asset.
- [`AssetCatalog`][assets.AssetCatalog] — the collection and lookup
  helpers.
- [`scan`][assets.catalog.scan] — build the catalog from a platform
  root.
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
)

_log = logging.getLogger("assets")

_ASSET_TYPES: frozenset[str] = frozenset(
    {"tool", "skill", "agent", "prompt", "workflow", "node"}
)


class AssetEntry(BaseModel):
    """One catalogued asset — bundle, ref, type, and on-disk path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    asset_type: str
    bundle: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetCatalog(BaseModel):
    """Built once at startup; immutable thereafter.

    `lookup()` is the chokepoint for
    **external** catalog reads. Direct `catalog.assets[ref]` subscript
    access is permitted ONLY inside this module; every other call
    site (`src/workflows/*`, `src/providers/authz.py`, `src/cli/*`,
    `src/assets/asset_manager.py`, and so on) routes through
    `catalog.lookup(ref, expected_type=...)`. A custom ruff rule is
    queued to mechanically enforce this; until then the discipline is
    reviewer-grep enforced.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    bundles: dict[str, BundleManifest] = Field(default_factory=dict)
    assets: dict[str, AssetEntry] = Field(default_factory=dict)

    def lookup(
        self, ref: str, expected_type: str | None = None
    ) -> AssetEntry | None:
        """Resolve a 3-segment ref to its [`AssetEntry`][assets.AssetEntry].

        The documented chokepoint for catalog access. Combines shape
        validation (via
        [`parse_three_segment`][assets.refs.parse_three_segment]) and
        dict-get into one well-typed call. `None` is returned in
        every "not found" scenario so callers can write
        `entry = catalog.lookup(ref, expected_type="agent")` and
        check truthiness without juggling exception classes:

        - `ref` is not a 3-seg string → `None` (shape miss).
        - `ref` shape is valid but the catalog has no entry → `None`.
        - `ref` resolves but its `asset_type` differs from
          `expected_type` → `None`.

        Args:
            ref: 3-segment catalog ref (`<bundle>/<plural>/<name>`).
            expected_type: Optional singular asset type guard
                (`tool` / `skill` / `agent` / `prompt` / `workflow` /
                `node`).

        Returns:
            The matching [`AssetEntry`][assets.AssetEntry], or `None`.
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
    """Walk `root/bundles/` and build the catalog.

    Bundles with missing or malformed `bundle.yaml` are skipped with
    a warning rather than aborting the scan; structural integrity
    failures across bundles (duplicate names, name/directory
    mismatches) abort with the matching `AssetsError` so the platform
    fails loud at startup.

    Args:
        root: The platform root (workspace directory containing
            `bundles/`).

    Returns:
        The fully built [`AssetCatalog`][assets.AssetCatalog]. An
        empty catalog when `bundles/` is absent.

    Raises:
        AssetsDuplicateBundleName: Two bundles share a manifest name.
            Detected BEFORE the directory-name match check so a
            same-named pair surfaces as a duplicate rather than the
            first-encountered mismatch.
        AssetsBundleNameMismatch: A manifest's `name` does not match
            its directory.
        AssetsDuplicateName: Two assets inside one bundle share a
            `<bundle>/<name>` pair across asset types.
    """
    bundles_dir = root / "bundles"
    if not bundles_dir.exists():
        return AssetCatalog()

    # --- Pass 1: load every well-formed manifest -----------------------
    raw: list[tuple[Path, BundleManifest]] = []
    for bundle_dir in sorted(bundles_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
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

    # --- Pass 2: duplicate-name detection ------------------------------
    by_name: dict[str, list[str]] = {}
    for _, manifest in raw:
        by_name.setdefault(manifest.name, []).append(manifest.path)
    duplicates = {n: p for n, p in by_name.items() if len(p) > 1}
    if duplicates:
        name = next(iter(duplicates))
        raise AssetsDuplicateBundleName(name=name, paths=duplicates[name])

    # --- Pass 3: name == directory match -------------------------------
    for bundle_dir, manifest in raw:
        if manifest.name != bundle_dir.name:
            raise AssetsBundleNameMismatch(
                directory=bundle_dir.name,
                manifest_name=manifest.name,
            )

    # --- Pass 4: build asset entries + uniqueness check ---------------
    # `<bundle>/<name>` must resolve to a unique catalog entry. A
    # bundle that ships both `agents/butler.yaml` and `tools/butler.py`
    # cannot disambiguate which one `<bundle>/butler` means; surface
    # the collision at catalog scan time so the failure is loud and
    # early. This check is independent of the existing full-ref
    # dedup (which keys on `<bundle>/<plural>/<name>` and won't catch
    # cross-type collisions within a bundle).
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

    # --- Pass 5: register virtual kolsch/nodes/<name> built-in slots ---
    _register_builtin_nodes(assets)

    return AssetCatalog(bundles=manifests, assets=assets)


def _register_builtin_nodes(assets: dict[str, AssetEntry]) -> None:
    """Inject the reserved `kolsch/nodes/<name>` virtual built-in entries.

    Registered unconditionally so
    `AssetCatalog.lookup` resolves
    `kolsch/nodes/bash` uniformly with authored Python-node refs.
    User-authored files at these slots already raised
    [`WorkflowBuiltinShadowed`][workflows.errors.WorkflowBuiltinShadowed]
    inside `discover_nodes` during Pass 4; reaching this point means
    there are no on-disk collisions to deconflict.
    """
    from workflows.catalog import builtin_node_entries  # noqa: PLC0415

    for entry in builtin_node_entries():
        assets[entry.ref] = entry


def _scan_bundle_assets(manifest: BundleManifest) -> list[AssetEntry]:
    """Walk a bundle's six asset subdirectories and produce entries.

    Local import of `workflows.catalog` (PLC0415-disabled at the call
    site) keeps the dependency one-way — the workflows package
    depends on `assets.catalog` for the
    [`AssetEntry`][assets.AssetEntry] shape; the reverse edge would
    create an import cycle.
    """
    from workflows.catalog import (  # noqa: PLC0415
        discover_nodes,
        discover_workflows,
    )

    bundle_dir = Path(manifest.path)
    entries: list[AssetEntry] = []
    entries.extend(_scan_tools(manifest.name, bundle_dir))
    entries.extend(_scan_skills(manifest.name, bundle_dir))
    entries.extend(_scan_agents(manifest.name, bundle_dir))
    entries.extend(_scan_prompts(manifest.name, bundle_dir))
    entries.extend(discover_workflows(manifest))
    entries.extend(discover_nodes(manifest))
    return entries


def _scan_tools(bundle: str, bundle_dir: Path) -> list[AssetEntry]:
    tools_dir = bundle_dir / "tools"
    if not tools_dir.exists():
        return []
    out: list[AssetEntry] = []
    for path in sorted(tools_dir.glob("*.py")):
        stem = path.stem
        # The catalog entry's ref segment is intended to be the
        # @tool decorator's `name`. Without importing the Python
        # file (catalog scan must not execute asset code) we cannot
        # read the decorator; v1 uses the filename stem as the ref
        # segment. A single file with multiple @tool decorators
        # surfaces a single catalog entry for the file; the Tools
        # subsystem (which DOES import at registration time)
        # expands per-decorator at that point.
        out.append(
            AssetEntry(
                ref=build_ref(bundle, "tool", stem),
                asset_type="tool",
                bundle=bundle,
                path=str(path.resolve()),
                metadata={},
            )
        )
    # Emit one diagnostic INFO line per bundle scanned so
    # ``kolsch run -v`` surfaces tool discovery. Empty tools/ dirs
    # stay silent to keep the log clean for bundles that don't ship
    # tools.
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
    """Return the set of catalogued asset-type strings."""
    return _ASSET_TYPES
