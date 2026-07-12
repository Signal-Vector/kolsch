"""TC-AGENTID-01..04 — Step 3 regression suite for
bug-agent-id-derive-from-ref.md.

End-to-end coverage of the four behaviors Part 2's acceptance
criteria pin:

- TC-AGENTID-01 — YAML without ``agent_id`` resolves with the
  derived ``ag_<bundle>_<name>`` value (acceptance §1, §2).
- TC-AGENTID-02 — an explicit ``agent_id`` in YAML overrides
  the derived baseline (acceptance §3).
- TC-AGENTID-03 — a malformed explicit ``agent_id`` still
  raises ``ConfigInvalid`` with the AgentId prefix message
  (acceptance §4 — fix did NOT become a leniency layer).
- TC-AGENTID-04 — the starter bundle's
  ``kolsch/agents/helper.yaml`` resolves to ``ag_helper`` (NOT
  the derived ``ag_kolsch_helper``), proving the non-migration
  guarantee (acceptance §6 — historical event logs, authz
  rules, and memory bindings that pin ``ag_helper`` keep
  working).

Tests drive :meth:`AssetManagement.build_resolved_config`
against a tmp-path workspace — the same surface the CLI and
the engine pre-flight consume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from assets import AssetManagement
from errors import ConfigInvalid
from kernel import ValidationResult
from schemas import ResolvedAgentConfig

# ---------------------------------------------------------------------------
# Workspace harness — inline minimal version of test_assets.py's helpers.
# ---------------------------------------------------------------------------


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


def _agent_payload(*, agent_id: str | None) -> dict[str, Any]:
    """A minimal Layer 1 partial covering every required field. ``agent_id``
    is conditionally included so each TC can probe the three branches
    (omitted / explicit canonical / explicit malformed)."""
    payload: dict[str, Any] = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "name": "demo",
        "description": "TC-AGENTID regression fixture.",
        "model": {"provider": "mock", "model": "mock-model"},
        "prompt": "You are a demo agent.",
        "io": {"mode": "conversational"},
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _write_workspace(
    root: Path,
    *,
    bundle_name: str = "test",
    agent_filename: str = "demo",
    agent_id: str | None = None,
) -> None:
    """Lay down a workspace with one bundle + one agent file."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(_PLATFORM_PAYLOAD), encoding="utf-8"
    )
    bundle = root / "bundles" / bundle_name
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": bundle_name, "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents" / f"{agent_filename}.yaml").write_text(
        yaml.safe_dump(_agent_payload(agent_id=agent_id)), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# TC-AGENTID-01 — YAML without agent_id resolves with derived id.
# ---------------------------------------------------------------------------


async def test_tc_agentid_01_partial_without_agent_id_derives(tmp_path: Path) -> None:
    """A new agent in a new bundle whose YAML omits ``agent_id:`` resolves
    to ``ag_<bundle>_<name>`` from the asset ref."""
    _write_workspace(tmp_path, bundle_name="test", agent_filename="demo", agent_id=None)

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    cfg = await am.build_resolved_config("test/agents/demo")

    assert isinstance(cfg, ResolvedAgentConfig)
    assert cfg.agent_id == "ag_test_demo"
    # Other fields parse normally so we know the derived agent_id
    # didn't accidentally break the rest of the resolved shape.
    assert cfg.name == "demo"
    assert cfg.description == "TC-AGENTID regression fixture."
    assert cfg.model.provider == "mock"
    assert cfg.model.model == "mock-model"


# ---------------------------------------------------------------------------
# TC-AGENTID-02 — explicit agent_id wins over the derived baseline.
# ---------------------------------------------------------------------------


async def test_tc_agentid_02_explicit_agent_id_wins(tmp_path: Path) -> None:
    """When the YAML provides ``agent_id: ag_custom``, the resolved config
    carries ``ag_custom`` — NOT the derived ``ag_test_demo``."""
    _write_workspace(
        tmp_path, bundle_name="test", agent_filename="demo", agent_id="ag_custom"
    )

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    cfg = await am.build_resolved_config("test/agents/demo")

    assert cfg.agent_id == "ag_custom"
    # Cross-check: the derived form is what we WOULD see if explicit
    # didn't win. Pin both to catch a future regression in the merge
    # ordering that flipped priority.
    assert cfg.agent_id != "ag_test_demo"


# ---------------------------------------------------------------------------
# TC-AGENTID-03 — malformed explicit agent_id still raises.
# ---------------------------------------------------------------------------


async def test_tc_agentid_03_malformed_explicit_agent_id_rejected(
    tmp_path: Path,
) -> None:
    """``agent_id: butler`` (no canonical ``ag_`` prefix) raises
    :class:`ConfigInvalid`; ``validation_errors`` carry the AgentId
    prefix message. Proves the fix didn't accidentally become a
    leniency layer."""
    _write_workspace(
        tmp_path, bundle_name="test", agent_filename="demo", agent_id="butler"
    )

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    with pytest.raises(ConfigInvalid) as excinfo:
        await am.build_resolved_config("test/agents/demo")

    findings = excinfo.value.detail["validation_errors"]
    paths = [f["path"] for f in findings]
    messages = [f["message"] for f in findings]
    assert any("agent_id" in p for p in paths), findings
    assert any("ag_" in m and "prefix" in m for m in messages), findings


# ---------------------------------------------------------------------------
# TC-AGENTID-04 — starter helper.yaml still resolves to ag_helper.
# ---------------------------------------------------------------------------


_STARTER_HELPER = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "agents"
    / "helper.yaml"
)


def _accept_anything(_body: dict[str, Any]) -> ValidationResult:
    """Layer-3 stand-in so subsystem-variant dispatch succeeds without
    pulling in the real Tools subsystem. We're testing identity
    derivation, not variant validation."""
    return ValidationResult(ok=True)


async def test_tc_agentid_04_starter_helper_keeps_explicit_id(tmp_path: Path) -> None:
    """``kolsch/agents/helper`` resolves to ``ag_helper`` (the explicit
    YAML value), NOT the derived ``ag_kolsch_helper`` — protects every
    historical event log, authz rule, and memory binding that pins
    ``ag_helper``.

    The test reads the ACTUAL starter ``helper.yaml`` source verbatim
    so any future edit that drops the ``agent_id`` line trips this
    test. Post-E-BUG-MODELLINE-001 (a-prime): helper.yaml no longer
    carries a ``model:`` field; the model value flows in from
    ``platform.yaml``'s ``defaults.model`` via the kernel's defaults
    composition. The platform fixture seeds that value so resolution
    succeeds. Subsystem validators are accept-anything: we're testing
    identity, not the Tools variant body shape."""
    # Lay down the platform shell + a bundles/kolsch/agents/ directory.
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    platform_payload = {
        **_PLATFORM_PAYLOAD,
        "defaults": {"model": {"provider": "mock", "model": "mock-model"}},
    }
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(platform_payload), encoding="utf-8"
    )
    bundle = tmp_path / "bundles" / "kolsch"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "kolsch", "version": "0.1", "description": "starter"}),
        encoding="utf-8",
    )
    # Read the actual starter YAML verbatim — it has no model: field
    # after E-BUG-MODELLINE-001 (a-prime).
    (bundle / "agents" / "helper.yaml").write_text(
        _STARTER_HELPER.read_text(encoding="utf-8"), encoding="utf-8"
    )

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    cfg = await am.build_resolved_config(
        "kolsch/agents/helper",
        subsystem_validators={"tools": {"standard": _accept_anything}},
    )

    assert cfg.agent_id == "ag_helper"
    assert cfg.agent_id != "ag_kolsch_helper"
