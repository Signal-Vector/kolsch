"""Bug-engine-tools-wiring-gap.md Step B Task 4 — catalog-scan test.

Pins the property that the asset-management catalog scanner
walks ``<workspace_root>/bundles/*/tools/*.py`` at build time
and produces an :class:`AssetEntry` per file with the canonical
ref ``<bundle>/tools/<stem>``.

Also pins the new diagnostic INFO log line that Step B Task 3
added so ``kolsch run -v`` surfaces tool discovery. The absence
of this line in the user's repro was Check #2 of the
ground-truth investigation.

Scope per coordinator resolution to Step B (verify-only +
small enhancements; not architecture rework): assert the
EXISTING scanner produces the right entries + the new log
line lands. Don't build a parallel ``scan_bundles`` import
abstraction — that would violate Am-I10
("asset discovery is read-only of Python").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml

from assets.catalog import AssetCatalog, scan

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_bundle(
    root: Path,
    bundle_name: str,
    *,
    tools: dict[str, str],
) -> None:
    """Scaffold a synthetic bundle at ``<root>/bundles/<name>``.

    ``tools`` maps stem → source code for each tool file. The
    files don't need to be valid `@tool`-decorated Python at
    catalog-scan time (Am-I10 — the catalog never imports
    them); a stub string is enough to make the file exist on
    disk and surface in ``glob("*.py")``.
    """
    bundle_dir = root / "bundles" / bundle_name
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": bundle_name,
                "version": "0.1",
                "description": f"synthetic bundle for {bundle_name}",
            },
        ),
        encoding="utf-8",
    )
    tools_dir = bundle_dir / "tools"
    tools_dir.mkdir()
    for stem, body in tools.items():
        (tools_dir / f"{stem}.py").write_text(body, encoding="utf-8")


# ===========================================================================
# Step B Task 4 — catalog scan produces tool entries + emits INFO line
# ===========================================================================


def test_catalog_scan_finds_both_tool_files_in_synthetic_bundle(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step B Task 4 (happy path).

    Create a tmp workspace with one bundle containing two tool
    files; bootstrap the catalog; assert both tool refs are in
    the catalog AND the INFO log line was emitted.
    """
    _write_bundle(
        tmp_path,
        "acme",
        tools={
            "foo": "def foo(): pass\n",
            "bar": "def bar(): pass\n",
        },
    )

    with caplog.at_level(logging.INFO, logger="assets"):
        catalog = scan(tmp_path)

    # Both refs landed in the catalog.
    assert isinstance(catalog, AssetCatalog)
    assert "acme/tools/foo" in catalog.assets
    assert "acme/tools/bar" in catalog.assets
    # And nothing extra showed up in the tool slot for this bundle.
    tool_refs = sorted(
        ref
        for ref, entry in catalog.assets.items()
        if entry.asset_type == "tool" and entry.bundle == "acme"
    )
    assert tool_refs == ["acme/tools/bar", "acme/tools/foo"]

    # Step B Task 3 — the INFO log line surfaces tool discovery.
    info_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO and rec.name == "assets"
    ]
    matching = [
        line for line in info_lines if "registered 2 tools" in line and "acme" in line
    ]
    assert matching, (
        f"expected 'catalog: registered 2 tools from bundles/acme/tools/' "
        f"INFO log line; got assets-level INFO records: {info_lines!r}"
    )


def test_catalog_scan_silent_for_bundle_with_no_tools_dir(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bundle that ships no ``tools/`` directory produces no
    catalog entries AND no log noise — keeping ``-v`` output
    clean for bundles that legitimately have no tools.
    """
    bundle_dir = tmp_path / "bundles" / "skills-only"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "skills-only",
                "version": "0.1",
                "description": "no tools",
            },
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.INFO, logger="assets"):
        catalog = scan(tmp_path)

    tool_refs_for_bundle = [
        ref
        for ref, entry in catalog.assets.items()
        if entry.asset_type == "tool" and entry.bundle == "skills-only"
    ]
    assert tool_refs_for_bundle == []
    # No "registered N tools" line for this bundle.
    tool_info = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and rec.name == "assets"
        and "registered" in rec.getMessage()
        and "tools" in rec.getMessage()
    ]
    assert tool_info == []


def test_catalog_scan_silent_for_empty_tools_dir(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bundle with an EMPTY ``tools/`` dir (rare but legal)
    also stays silent — the count would be zero and a "registered
    0 tools" line is noise.
    """
    _write_bundle(tmp_path, "empty", tools={})

    with caplog.at_level(logging.INFO, logger="assets"):
        scan(tmp_path)

    tool_info = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and "registered" in rec.getMessage()
        and "empty" in rec.getMessage()
    ]
    assert tool_info == []


def test_catalog_scan_emits_one_line_per_bundle(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two bundles each shipping tools → two INFO lines (one per
    bundle), not one combined line. Makes the log scannable
    bundle-by-bundle.
    """
    _write_bundle(tmp_path, "alpha", tools={"a": "def a(): pass\n"})
    _write_bundle(
        tmp_path,
        "beta",
        tools={"b": "def b(): pass\n", "c": "def c(): pass\n"},
    )

    with caplog.at_level(logging.INFO, logger="assets"):
        scan(tmp_path)

    alpha_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and "alpha" in rec.getMessage()
        and "registered 1 tools" in rec.getMessage()
    ]
    beta_lines = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and "beta" in rec.getMessage()
        and "registered 2 tools" in rec.getMessage()
    ]
    assert len(alpha_lines) == 1, f"expected one alpha line; got {alpha_lines}"
    assert len(beta_lines) == 1, f"expected one beta line; got {beta_lines}"
