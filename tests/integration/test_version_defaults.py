"""QoL — version fields optional at the resolved layer.

Covers spec ``qol-yaml-version-fields-optional.md`` Step 2 acceptance:
a YAML omitting ``schema_version`` and ``assembly_version`` resolves
cleanly with the layer defaults applied (``"0.1"`` / ``"1"``), and an
explicit value still wins over the default.

The capability-check regression (``assembly_version: "999"`` rejected
at engine pre-flight) lives in ``tests/component/test_engine.py``
alongside the existing engine wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from assets import AssetManagement
from schemas import ResolvedAgentConfig

if TYPE_CHECKING:
    from pathlib import Path

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


def _agent_payload(
    *,
    include_schema_version: bool = False,
    include_assembly_version: bool = False,
    schema_version: str = "0.1",
    assembly_version: str = "1",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent_id": "ag_demo",
        "name": "demo",
        "description": "version-defaults regression fixture.",
        "model": {"provider": "mock", "model": "mock-model"},
        "prompt": "You are a demo agent.",
        "io": {"mode": "conversational"},
    }
    if include_schema_version:
        payload["schema_version"] = schema_version
    if include_assembly_version:
        payload["assembly_version"] = assembly_version
    return payload


def _write_workspace(root: Path, agent_payload: dict[str, Any]) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(_PLATFORM_PAYLOAD), encoding="utf-8"
    )
    bundle = root / "bundles" / "demo"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(agent_payload), encoding="utf-8"
    )


async def test_build_resolved_config_applies_version_defaults_when_omitted(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path, _agent_payload())
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    cfg = await am.build_resolved_config("demo/agents/demo")

    assert isinstance(cfg, ResolvedAgentConfig)
    assert cfg.schema_version == "0.1"
    assert cfg.assembly_version == "1"


async def test_build_resolved_config_preserves_explicit_versions(
    tmp_path: Path,
) -> None:
    _write_workspace(
        tmp_path,
        _agent_payload(
            include_schema_version=True,
            include_assembly_version=True,
            schema_version="0.1",
            assembly_version="1",
        ),
    )
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    cfg = await am.build_resolved_config("demo/agents/demo")

    assert cfg.schema_version == "0.1"
    assert cfg.assembly_version == "1"
