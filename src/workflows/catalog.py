"""Workflow and node asset-catalog discovery.

Mirrors the existing `tools/` / `agents/` / `skills/` auto-discovery
in `assets.catalog` for two new asset types:

- `workflows/` — YAML workflow definitions (runnable workflows and
  node libraries). Discovery PARSES each file via
  [`load_workflow`][workflows.parser.load_workflow] so the resulting
  catalog entry metadata carries the inferred `kind` (`"workflow"` vs
  `"node_library"`) and the resolved `extends` list. Malformed YAMLs
  surface as warnings and skip.
- `nodes/` — authored Python node files (one `@node`-decorated `run`
  per file). Discovery does NOT import the module; introspection
  happens at the run-start gate. The file's stem becomes the final
  segment of the 3-seg ref.

Every catalog entry's `ref` is built via
[`build_ref`][assets.refs.build_ref] — no inline f-strings.

**Virtual `kolsch` built-in nodes.** The 16 reserved
`kolsch/nodes/<name>` slots from `BUILTIN_TYPE_NAMES` are registered
unconditionally via `builtin_node_entries`. A user authoring a file at
one of these reserved slots (`bundles/kolsch/nodes/bash.py`) trips
[`WorkflowBuiltinShadowed`][workflows.errors.WorkflowBuiltinShadowed]
at scan time. A bundle merely NAMED `bash`
(`bundles/bash/nodes/parse.py` → `bash/nodes/parse`) is fine — the
collision is per-slot, not per-bundle-name.

The `bundle.yaml` `exclude:` block (a list of paths relative to the
bundle root — see
[`BundleManifest`][assets.manifest.BundleManifest]) suppresses listed
files from both asset types so authors can keep in-progress drafts on
disk without registering them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from assets.catalog import AssetEntry
from assets.refs import build_ref
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.errors import WorkflowBuiltinShadowed, WorkflowError
from workflows.parser import load_workflow

if TYPE_CHECKING:
    from assets.manifest import BundleManifest

__all__ = [
    "RESERVED_BUILTIN_NODE_REFS",
    "builtin_node_entries",
    "discover_nodes",
    "discover_workflows",
]


_log = logging.getLogger("workflows.catalog")


# The 16 reserved built-in catalog slots — `BUILTIN_TYPE_NAMES`
# composed with the `kolsch/nodes/<name>` template via `build_ref`
# (no inline f-strings).
RESERVED_BUILTIN_NODE_REFS: frozenset[str] = frozenset(
    build_ref("kolsch", "node", name) for name in BUILTIN_TYPE_NAMES
)


def discover_workflows(manifest: BundleManifest) -> list[AssetEntry]:
    """Scan `<bundle_root>/workflows/*.yaml` and emit catalog entries.

    Each entry's `ref` is built via `build_ref`
    (`<bundle>/workflows/<stem>`). Metadata carries the inferred `kind`
    and the resolved `extends` list from the parsed
    [`Workflow`][workflows.parser.Workflow].

    Malformed YAML (raises
    [`WorkflowError`][workflows.errors.WorkflowError] from the parser)
    is skipped with a warning so a single broken file does not block
    catalog construction. The validator re-surfaces these on the
    explicit `kolsch workflows validate` path.

    Files listed in `manifest.exclude` (paths relative to the bundle
    root) are silently skipped. The list pattern matches the existing
    `bundle.yaml` opt-out convention.

    Args:
        manifest: The bundle's parsed manifest.

    Returns:
        Catalog entries for every non-excluded workflow file in the
        bundle.
    """
    bundle_dir = Path(manifest.path)
    workflows_dir = bundle_dir / "workflows"
    if not workflows_dir.exists():
        return []
    excluded = set(manifest.exclude)
    out: list[AssetEntry] = []
    for path in sorted(workflows_dir.glob("*.yaml")):
        rel = str(path.relative_to(bundle_dir))
        if rel in excluded:
            continue
        try:
            wf = load_workflow(path)
        except WorkflowError as exc:
            _log.warning("Skipping malformed workflow %s: %s", path, exc)
            continue
        out.append(
            AssetEntry(
                ref=build_ref(manifest.name, "workflow", path.stem),
                asset_type="workflow",
                bundle=manifest.name,
                path=str(path.resolve()),
                metadata={
                    "kind": wf.kind,
                    "extends": list(wf.extends),
                },
            )
        )
    return out


def discover_nodes(manifest: BundleManifest) -> list[AssetEntry]:
    """Scan `<bundle_root>/nodes/*.py` and emit catalog entries.

    Does NOT import the module — Python execution is reserved for the
    run-start gate. Only the filename stem is read here to construct
    the `<bundle>/nodes/<name>` ref.

    A user-authored file whose ref collides with one of the 14
    `RESERVED_BUILTIN_NODE_REFS` (e.g. `bundles/kolsch/nodes/bash.py`
    → `kolsch/nodes/bash`) raises `WorkflowBuiltinShadowed`. A bundle
    merely NAMED `bash` is fine: `bundles/bash/nodes/parse.py`
    produces `bash/nodes/parse`, which is not a reserved slot.

    Files listed in `manifest.exclude` are silently skipped.

    Args:
        manifest: The bundle's parsed manifest.

    Returns:
        Catalog entries for every non-excluded node file in the bundle.

    Raises:
        WorkflowBuiltinShadowed: A user-authored file's ref collides
            with one of the reserved built-in slots.
    """
    bundle_dir = Path(manifest.path)
    nodes_dir = bundle_dir / "nodes"
    if not nodes_dir.exists():
        return []
    excluded = set(manifest.exclude)
    out: list[AssetEntry] = []
    for path in sorted(nodes_dir.glob("*.py")):
        rel = str(path.relative_to(bundle_dir))
        if rel in excluded:
            continue
        ref = build_ref(manifest.name, "node", path.stem)
        if ref in RESERVED_BUILTIN_NODE_REFS:
            raise WorkflowBuiltinShadowed(file=str(path), line=1, ref=ref)
        out.append(
            AssetEntry(
                ref=ref,
                asset_type="node",
                bundle=manifest.name,
                path=str(path.resolve()),
                metadata={},
            )
        )
    return out


def builtin_node_entries() -> list[AssetEntry]:
    """Emit the 14 virtual `kolsch/nodes/<name>` built-in entries.

    These slots are reserved (see `RESERVED_BUILTIN_NODE_REFS`); their
    implementation lives in `workflows.parser` as Python Pydantic
    models rather than on disk. The catalog still surfaces them as
    [`AssetEntry`][assets.catalog.AssetEntry] records so the
    chokepoint
    [`AssetCatalog.lookup`][assets.catalog.AssetCatalog.lookup]
    resolves `kolsch/nodes/bash` uniformly with authored-Python-node
    refs.

    `path` is the empty string — a marker that no on-disk file backs
    the entry. `metadata["builtin"] = True` lets downstream consumers
    (validator, executor) branch on the kind.

    Returns:
        One entry per reserved built-in node slot.
    """
    return [
        AssetEntry(
            ref=build_ref("kolsch", "node", name),
            asset_type="node",
            bundle="kolsch",
            path="",
            metadata={"builtin": True},
        )
        for name in sorted(BUILTIN_TYPE_NAMES)
    ]
