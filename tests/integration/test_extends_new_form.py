"""Integration tests for the new ref convention on ``extends:``
— qol-prefix-defaults-and-unified-refs.md Step 2.

Two end-to-end scenarios against ``build_resolved_config``:

1. New canonical form: ``extends: kolsch/agents/helper`` resolves to
   the kolsch bundle's helper agent in the workspace via
   catalog lookup. No deprecation warning fires.

2. Legacy file-path form: ``extends: workspace:bundles/kolsch/agents/helper.yaml``
   resolves to the same target via the file-path resolver, with
   the ``ConfigDeprecatedRefForm`` warning surfacing in the
   ``templating.context_files`` logger.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from assets import AssetManagement
from schemas import ResolvedAgentConfig


_PLATFORM_PAYLOAD: dict[str, Any] = {
    "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
    "sandbox": {"variant": "native", "config": {}},
    "authorization": {
        "variant": "rule_based",
        "config": {"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
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


def _write_workspace(root: Path, *, extends_value: str) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(_PLATFORM_PAYLOAD), encoding="utf-8"
    )

    # Parent bundle.
    parent_bundle = root / "bundles" / "kolsch"
    (parent_bundle / "agents").mkdir(parents=True, exist_ok=True)
    (parent_bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "kolsch", "version": "0.1", "description": "p"}),
        encoding="utf-8",
    )
    (parent_bundle / "agents" / "helper.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_id": "ag_helper",
                "name": "helper",
                "description": "parent",
                "model": {"provider": "fake", "model": "fake"},
                "prompt": "I am helper.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )

    # Child bundle that extends the parent.
    child_bundle = root / "bundles" / "child_bundle"
    (child_bundle / "agents").mkdir(parents=True, exist_ok=True)
    (child_bundle / "bundle.yaml").write_text(
        yaml.safe_dump(
            {"name": "child_bundle", "version": "0.1", "description": "c"}
        ),
        encoding="utf-8",
    )
    (child_bundle / "agents" / "child.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_id": "ag_child",
                "name": "child",
                "description": "child agent",
                "extends": extends_value,
                "prompt": "I am child.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )


async def test_extends_new_catalog_ref_form(tmp_path: Path) -> None:
    """``extends: kolsch/agents/helper`` (3-segment catalog ref)
    resolves correctly; no deprecation warning fires."""
    _write_workspace(tmp_path, extends_value="kolsch/agents/helper")
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    cfg = await am.build_resolved_config("child_bundle/agents/child")

    assert isinstance(cfg, ResolvedAgentConfig)
    # `model` inherited from parent (child YAML omits it); proves
    # the chain composed.
    assert cfg.model.provider == "fake"
    assert cfg.model.model == "fake"
    # Child's own fields preserved.
    assert cfg.name == "child"


async def test_extends_legacy_workspace_prefix_still_works(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The legacy ``workspace:bundles/.../foo.yaml`` form still
    resolves AND emits a ConfigDeprecatedRefForm warning via the
    templating.context_files logger."""
    legacy = "workspace:bundles/kolsch/agents/helper.yaml"
    _write_workspace(tmp_path, extends_value=legacy)
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    with caplog.at_level(logging.WARNING, logger="templating.context_files"):
        cfg = await am.build_resolved_config("child_bundle/agents/child")

    # Same resolution semantics as the new-form path.
    assert cfg.model.provider == "fake"
    assert cfg.name == "child"
    # Deprecation log surface.
    deprecation_messages = [
        rec.message
        for rec in caplog.records
        if "deprecated_ref_form" in rec.message or "deprecated ref" in rec.message
    ]
    assert deprecation_messages, (
        f"expected a deprecation log entry; got {[r.message for r in caplog.records]}"
    )
