"""Phase 3.0 Step 3 — workflow + node asset-catalog discovery tests.

Covers Step 3 ACs 3.1-3.13:

- ``workflows/*.yaml`` and ``nodes/*.py`` auto-discovered.
- Refs constructed via ``build_ref`` (no inline f-strings).
- Workflow metadata carries ``kind`` (workflow vs node_library) and
  ``extends``.
- ``bundle.yaml`` ``exclude:`` suppresses listed files.
- Node files NOT imported during discovery (Am-I10).
- Catalog ``lookup`` resolves hit / miss / wrong-type correctly.
- Virtual ``kolsch/nodes/<name>`` built-in slots resolve for all 14.
- ``WorkflowBuiltinShadowed`` fires when a user authors a file at one
  of the 14 reserved slots.
- A bundle merely NAMED ``bash`` does NOT collide with the
  ``kolsch/nodes/bash`` slot.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from assets.catalog import scan
from assets.refs import build_ref
from workflows.catalog import (
    RESERVED_BUILTIN_NODE_REFS,
    builtin_node_entries,
    discover_nodes,
    discover_workflows,
)
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.errors import WorkflowBuiltinShadowed

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path) -> Path:
    """Create the canonical platform-root subdirectories under tmp_path."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _write_bundle_yaml(
    bundle_dir: Path, name: str, *, exclude: list[str] | None = None
) -> None:
    body = (
        f'name: {name}\nversion: "0.1"\ndescription: "test bundle"\n'
    )
    if exclude:
        body += "exclude:\n"
        for entry in exclude:
            body += f"  - {entry}\n"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(body, encoding="utf-8")


def _write_workflow(
    bundle_dir: Path, name: str, *, runnable: bool = True, extends: list[str] | None = None
) -> Path:
    wf_dir = bundle_dir / "workflows"
    wf_dir.mkdir(exist_ok=True)
    path = wf_dir / f"{name}.yaml"
    body = ""
    if extends:
        body += "extends:\n" + "\n".join(f"  - {e}" for e in extends) + "\n"
    body += textwrap.dedent(
        """\
        nodes:
          greet:
            type: kolsch/nodes/bash
            command: echo hi
        """
    )
    if runnable:
        body += "graph:\n  edges: []\n"
    path.write_text(body, encoding="utf-8")
    return path


def _write_node_py(bundle_dir: Path, name: str, *, body: str = "x = 1\n") -> Path:
    nodes_dir = bundle_dir / "nodes"
    nodes_dir.mkdir(exist_ok=True)
    path = nodes_dir / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC 3.1 / 3.3 — workflow discovery
# ---------------------------------------------------------------------------


def test_workflow_yaml_auto_discovered(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "hello")

    cat = scan(root)
    assert "demo/workflows/hello" in cat.assets
    entry = cat.assets["demo/workflows/hello"]
    assert entry.asset_type == "workflow"
    assert entry.bundle == "demo"


def test_workflow_ref_built_via_build_ref(tmp_path: Path) -> None:
    """AC 3.3 — refs match :func:`build_ref` output exactly."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "hello")

    cat = scan(root)
    assert cat.assets["demo/workflows/hello"].ref == build_ref(
        "demo", "workflow", "hello"
    )


# ---------------------------------------------------------------------------
# AC 3.2 / 3.4 — node discovery
# ---------------------------------------------------------------------------


def test_node_py_auto_discovered(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_node_py(bundle, "echo")

    cat = scan(root)
    assert "demo/nodes/echo" in cat.assets
    entry = cat.assets["demo/nodes/echo"]
    assert entry.asset_type == "node"
    assert entry.ref == build_ref("demo", "node", "echo")


def test_node_discovery_does_not_import_module(tmp_path: Path) -> None:
    """AC 3.7 — node files must NOT be imported during discovery
    (Am-I10). A file with an import-time side effect proves it."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    marker = root / "imported.marker"
    body = textwrap.dedent(
        f"""\
        from pathlib import Path
        Path({str(marker)!r}).write_text("imported")
        """
    )
    _write_node_py(bundle, "side_effect", body=body)

    cat = scan(root)
    assert "demo/nodes/side_effect" in cat.assets
    assert not marker.exists()


# ---------------------------------------------------------------------------
# AC 3.5 — workflow metadata: kind + extends
# ---------------------------------------------------------------------------


def test_workflow_kind_inference_runnable(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "runnable", runnable=True)

    cat = scan(root)
    assert cat.assets["demo/workflows/runnable"].metadata["kind"] == "workflow"


def test_workflow_kind_inference_node_library(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "library", runnable=False)

    cat = scan(root)
    assert (
        cat.assets["demo/workflows/library"].metadata["kind"] == "node_library"
    )


def test_workflow_empty_edges_still_runnable(tmp_path: Path) -> None:
    """Polish-pass Decision P3 — ``graph: { edges: [] }`` resolves to
    ``kind="workflow"``."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "single", runnable=True)

    cat = scan(root)
    assert cat.assets["demo/workflows/single"].metadata["kind"] == "workflow"


def test_workflow_extends_metadata_populated(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    _write_workflow(bundle, "child", extends=["demo/workflows/parent"])

    cat = scan(root)
    extends = cat.assets["demo/workflows/child"].metadata["extends"]
    assert extends == ["demo/workflows/parent"]


# ---------------------------------------------------------------------------
# AC 3.6 — bundle.yaml exclude:
# ---------------------------------------------------------------------------


def test_exclude_suppresses_workflow_files(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo", exclude=["workflows/wip.yaml"])
    _write_workflow(bundle, "ready")
    _write_workflow(bundle, "wip")

    cat = scan(root)
    assert "demo/workflows/ready" in cat.assets
    assert "demo/workflows/wip" not in cat.assets


def test_exclude_suppresses_node_files(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo", exclude=["nodes/draft.py"])
    _write_node_py(bundle, "ready")
    _write_node_py(bundle, "draft")

    cat = scan(root)
    assert "demo/nodes/ready" in cat.assets
    assert "demo/nodes/draft" not in cat.assets


# ---------------------------------------------------------------------------
# AC 3.8 — existing asset types untouched
# ---------------------------------------------------------------------------


def test_existing_tools_agents_still_discovered(tmp_path: Path) -> None:
    """A bundle with tools/ + agents/ + workflows/ + nodes/ surfaces
    entries of all four kinds."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")

    (bundle / "tools").mkdir()
    (bundle / "tools" / "echo.py").write_text("def main(): pass\n", encoding="utf-8")

    (bundle / "agents").mkdir()
    (bundle / "agents" / "bot.yaml").write_text(
        "name: bot\n", encoding="utf-8"
    )

    _write_workflow(bundle, "hello")
    _write_node_py(bundle, "passthru")

    cat = scan(root)
    types = {e.asset_type for e in cat.assets.values() if e.bundle == "demo"}
    assert types == {"tool", "agent", "workflow", "node"}


# ---------------------------------------------------------------------------
# AC 3.9 — no inline f-string ref construction
# ---------------------------------------------------------------------------


def test_no_inline_ref_construction_in_workflow_catalog() -> None:
    """AC 3.9 — every ref comes through :func:`build_ref`."""
    src_root = Path(__file__).resolve().parent.parent.parent / "src"
    target = src_root / "workflows" / "catalog.py"
    result = subprocess.run(
        ["grep", "-Pn", r'f"\{[^}]*\}/[a-z_]*/\{[^}]*\}"', str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", (
        f"{target}: inline 3-seg f-string still present:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# AC 3.10 — AssetCatalog.lookup hit / miss / wrong-type
# ---------------------------------------------------------------------------


def test_lookup_resolves_workflow_entry(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "acme"
    _write_bundle_yaml(bundle, "acme")
    _write_workflow(bundle, "triage")

    cat = scan(root)
    entry = cat.lookup("acme/workflows/triage")
    assert entry is not None
    assert entry.asset_type == "workflow"


def test_lookup_returns_none_for_unknown_ref(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "acme"
    _write_bundle_yaml(bundle, "acme")
    _write_workflow(bundle, "triage")

    cat = scan(root)
    assert cat.lookup("acme/workflows/missing") is None


def test_lookup_wrong_expected_type_returns_none(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "acme"
    _write_bundle_yaml(bundle, "acme")
    _write_workflow(bundle, "triage")

    cat = scan(root)
    assert cat.lookup("acme/workflows/triage", expected_type="node") is None


# ---------------------------------------------------------------------------
# AC 3.11 — WorkflowBuiltinShadowed at reserved slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved_name", sorted(BUILTIN_TYPE_NAMES))
def test_user_authored_reserved_slot_raises_shadowed(
    tmp_path: Path, reserved_name: str
) -> None:
    """Authoring ``bundles/kolsch/nodes/<reserved>.py`` raises
    :class:`WorkflowBuiltinShadowed`."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "kolsch"
    _write_bundle_yaml(bundle, "kolsch")
    _write_node_py(bundle, reserved_name)

    with pytest.raises(WorkflowBuiltinShadowed) as exc_info:
        scan(root)
    assert reserved_name in str(exc_info.value)


def test_bundle_named_bash_does_not_collide(tmp_path: Path) -> None:
    """R3 #5 — a bundle merely NAMED ``bash`` is fine; only the specific
    ``kolsch/nodes/<reserved>`` slots are protected."""
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "bash"
    _write_bundle_yaml(bundle, "bash")
    _write_node_py(bundle, "parse")

    cat = scan(root)
    assert cat.assets["bash/nodes/parse"].asset_type == "node"


# ---------------------------------------------------------------------------
# AC 3.12 — virtual kolsch built-in nodes
# ---------------------------------------------------------------------------


def test_all_14_virtual_builtins_resolve(tmp_path: Path) -> None:
    """``catalog.lookup("kolsch/nodes/<name>", expected_type="node")``
    resolves for every reserved slot, even when no ``kolsch`` bundle
    exists on disk."""
    root = _make_root(tmp_path)
    cat = scan(root)
    for name in BUILTIN_TYPE_NAMES:
        ref = build_ref("kolsch", "node", name)
        entry = cat.lookup(ref, expected_type="node")
        assert entry is not None, ref
        assert entry.metadata.get("builtin") is True
        assert entry.path == ""


def test_reserved_builtin_refs_constant_matches_builtin_set() -> None:
    assert {
        build_ref("kolsch", "node", n) for n in BUILTIN_TYPE_NAMES
    } == RESERVED_BUILTIN_NODE_REFS


def test_builtin_node_entries_emits_15_entries() -> None:
    # 15 reserved built-in nodes since PR #1 added `browse_url` as
    # the 15th slot (was 14; parity guard for BUILTIN_TYPE_NAMES).
    entries = builtin_node_entries()
    assert len(entries) == 15
    types = {e.asset_type for e in entries}
    assert types == {"node"}
    bundles = {e.bundle for e in entries}
    assert bundles == {"kolsch"}
    refs = {e.ref for e in entries}
    assert refs == RESERVED_BUILTIN_NODE_REFS


# ---------------------------------------------------------------------------
# Discovery helpers exercised directly
# ---------------------------------------------------------------------------


def test_discover_workflows_skips_missing_dir(tmp_path: Path) -> None:
    """No ``workflows/`` subdir → empty list (not an error)."""
    from assets.manifest import BundleManifest

    bundle = tmp_path / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    manifest = BundleManifest(
        name="demo", version="0.1", description="t", path=str(bundle)
    )
    assert discover_workflows(manifest) == []


def test_discover_nodes_skips_missing_dir(tmp_path: Path) -> None:
    from assets.manifest import BundleManifest

    bundle = tmp_path / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    manifest = BundleManifest(
        name="demo", version="0.1", description="t", path=str(bundle)
    )
    assert discover_nodes(manifest) == []


def test_discover_workflows_skips_malformed_yaml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken workflow YAML is skipped with a warning, not fatal."""
    import logging

    from assets.manifest import BundleManifest

    bundle = tmp_path / "bundles" / "demo"
    _write_bundle_yaml(bundle, "demo")
    (bundle / "workflows").mkdir()
    # Missing 'type:' → parser raises WorkflowValidationError.
    (bundle / "workflows" / "broken.yaml").write_text(
        "nodes:\n  n: {}\n", encoding="utf-8"
    )

    manifest = BundleManifest(
        name="demo", version="0.1", description="t", path=str(bundle)
    )
    with caplog.at_level(logging.WARNING, logger="workflows.catalog"):
        out = discover_workflows(manifest)
    assert out == []
    assert any("broken.yaml" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Mixed-bundles smoke test
# ---------------------------------------------------------------------------


def test_mixed_bundles_workflows_only_nodes_only_both(tmp_path: Path) -> None:
    root = _make_root(tmp_path)

    wf_only = root / "bundles" / "wfonly"
    _write_bundle_yaml(wf_only, "wfonly")
    _write_workflow(wf_only, "wf1")

    nd_only = root / "bundles" / "ndonly"
    _write_bundle_yaml(nd_only, "ndonly")
    _write_node_py(nd_only, "nd1")

    both = root / "bundles" / "both"
    _write_bundle_yaml(both, "both")
    _write_workflow(both, "wf1")
    _write_node_py(both, "nd1")

    cat = scan(root)
    assert "wfonly/workflows/wf1" in cat.assets
    assert "ndonly/nodes/nd1" in cat.assets
    assert "both/workflows/wf1" in cat.assets
    assert "both/nodes/nd1" in cat.assets


# ---------------------------------------------------------------------------
# Catalog typing — make sure AssetCatalog accepts the new asset types
# ---------------------------------------------------------------------------


def test_catalog_keeps_known_asset_types_set_extended() -> None:
    """AC: extended :data:`_ASSET_TYPES` includes ``workflow`` + ``node``."""
    from assets.catalog import known_asset_types

    types = known_asset_types()
    assert {"workflow", "node"}.issubset(types)


def test_lookup_returns_assetentry_instance(tmp_path: Path) -> None:
    """Sanity — :meth:`AssetCatalog.lookup` returns an :class:`AssetEntry`
    (not a raw dict) for both real and virtual entries."""
    root = _make_root(tmp_path)
    cat = scan(root)
    entry = cat.lookup("kolsch/nodes/bash")
    assert entry is not None
    assert hasattr(entry, "ref") and hasattr(entry, "asset_type")


# ---------------------------------------------------------------------------
# Reachability of the imports (no removed-feature survivors)
# ---------------------------------------------------------------------------


def test_workflows_catalog_uses_build_ref_import() -> None:
    """Source-level grep confirming the build_ref import is the
    construction surface — no f-string rewrites slipped in."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "workflows"
        / "catalog.py"
    )
    text = src.read_text(encoding="utf-8")
    assert "from assets.refs import build_ref" in text
    assert sys.version_info >= (3, 12)  # sanity: typing-related syntax
