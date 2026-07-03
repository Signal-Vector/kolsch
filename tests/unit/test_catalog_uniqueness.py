"""Unit tests for the per-bundle name-uniqueness check —
qol-prefix-defaults-and-unified-refs.md Step 2.

Under the new 2-segment ref convention, ``<bundle>/<name>`` must
resolve to a unique catalog entry. A bundle that ships both
``tools/butler.py`` and ``agents/butler.yaml`` cannot
disambiguate which one ``<bundle>/butler`` means; the catalog
must reject this at scan time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from assets.catalog import scan
from errors import AssetsDuplicateName

if TYPE_CHECKING:
    from pathlib import Path


def _write_bundle(
    root: Path,
    *,
    bundle: str,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    agents: list[str] | None = None,
    prompts: list[str] | None = None,
) -> None:
    bundle_dir = root / "bundles" / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.safe_dump({"name": bundle, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    if tools:
        (bundle_dir / "tools").mkdir(exist_ok=True)
        for name in tools:
            (bundle_dir / "tools" / f"{name}.py").write_text(
                "# stub tool\n", encoding="utf-8"
            )
    if skills:
        (bundle_dir / "skills").mkdir(exist_ok=True)
        for name in skills:
            sk = bundle_dir / "skills" / name
            sk.mkdir(exist_ok=True)
            (sk / "SKILL.md").write_text(
                "---\nname: " + name + "\ndescription: x\n---\n", encoding="utf-8"
            )
    if agents:
        (bundle_dir / "agents").mkdir(exist_ok=True)
        for name in agents:
            (bundle_dir / "agents" / f"{name}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "agent_id": f"ag_{name}",
                        "name": name,
                        "description": "x",
                        "model": {"provider": "fake", "model": "fake"},
                        "prompt": "x",
                        "io": {"mode": "conversational"},
                    }
                ),
                encoding="utf-8",
            )
    if prompts:
        (bundle_dir / "prompts").mkdir(exist_ok=True)
        for name in prompts:
            (bundle_dir / "prompts" / f"{name}.md").write_text(
                "stub prompt\n", encoding="utf-8"
            )


def test_scan_rejects_agent_and_tool_sharing_name_in_same_bundle(
    tmp_path: Path,
) -> None:
    _write_bundle(tmp_path, bundle="acme", agents=["butler"], tools=["butler"])
    with pytest.raises(AssetsDuplicateName) as excinfo:
        scan(tmp_path)
    assert excinfo.value.detail["bundle"] == "acme"
    assert excinfo.value.detail["name"] == "butler"
    assert set(excinfo.value.detail["refs"]) == {
        "acme/agents/butler",
        "acme/tools/butler",
    }


def test_scan_rejects_tool_and_skill_sharing_name_in_same_bundle(
    tmp_path: Path,
) -> None:
    _write_bundle(tmp_path, bundle="acme", tools=["butler"], skills=["butler"])
    with pytest.raises(AssetsDuplicateName):
        scan(tmp_path)


def test_scan_allows_same_name_across_different_bundles(tmp_path: Path) -> None:
    """`acme/butler` and `kolsch/butler` are distinct refs even
    if both name their asset 'butler'."""
    _write_bundle(tmp_path, bundle="acme", agents=["butler"])
    _write_bundle(tmp_path, bundle="kolsch", agents=["butler"])
    catalog = scan(tmp_path)
    assert "acme/agents/butler" in catalog.assets
    assert "kolsch/agents/butler" in catalog.assets


def test_scan_allows_different_names_in_same_bundle(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        bundle="acme",
        agents=["helper"],
        tools=["bash"],
        skills=["review"],
    )
    catalog = scan(tmp_path)
    # Phase 3.0 Step 3 — also drops the 14 virtual ``kolsch/nodes/<name>``
    # built-in node entries into ``catalog.assets``. Filter on the
    # ``builtin`` metadata marker so this test stays focused on the
    # on-disk acme bundle.
    on_disk = [a for a in catalog.assets.values() if not a.metadata.get("builtin")]
    assert {a.bundle for a in on_disk} == {"acme"}
    assert {a.ref.rsplit("/", 1)[-1] for a in on_disk} == {
        "helper",
        "bash",
        "review",
    }
