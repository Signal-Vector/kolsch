"""bug-159 — where a scoped legacy rule's refusal surfaces to the user.

A legacy `{who, what}` rule carrying `from:` (or a non-`*` `pattern:`)
is refused by `PermissionRule`. These tests pin how that refusal reaches
each real loader:

- agent `permissions:` → `ConfigInvalid` from the kernel pipeline,
  naming the field;
- the chat `/reload` agent rescan → its "invalid permission rule … Old
  rules still active" outcome (old rules kept: fail closed);
- a `bundle.yaml` carrying one → the catalog skips the whole bundle
  with its existing "malformed bundle.yaml" warning (spec S6).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import yaml

from assets import catalog as catalog_module
from cli.chat.services.authz_introspection import _reload_agents
from errors import ConfigInvalid
from kernel import resolve_and_validate

if TYPE_CHECKING:
    from pathlib import Path

A = "kolsch/agents/a"
_LEGACY_FROM = {"who": ".*", "what": r"tool\.bash", "from": A, "effect": "allow"}
_LEGACY_PATTERN = {"who": ".*", "what": "x", "pattern": "safe*", "effect": "allow"}


@pytest.mark.parametrize(
    ("rule", "field"),
    [(_LEGACY_FROM, "`from:`"), (_LEGACY_PATTERN, "`pattern: 'safe*'`")],
    ids=["legacy_from", "legacy_pattern"],
)
def test_agent_permissions_surface_config_invalid(
    rule: dict[str, str], field: str
) -> None:
    with pytest.raises(ConfigInvalid) as excinfo:
        resolve_and_validate([{"permissions": [rule]}], {})
    messages = " ".join(
        str(e.get("message", "")) for e in excinfo.value.detail["validation_errors"]
    )
    assert field in messages
    assert "canonical" in messages


@pytest.mark.parametrize(
    "rule", [_LEGACY_FROM, _LEGACY_PATTERN], ids=["legacy_from", "legacy_pattern"]
)
def test_reload_rescan_reports_invalid_rule_and_keeps_old(
    tmp_path: Path, rule: dict[str, str]
) -> None:
    agent_yaml = tmp_path / "a.yaml"
    agent_yaml.write_text(yaml.safe_dump({"permissions": [rule]}), encoding="utf-8")
    catalog = SimpleNamespace(
        assets={
            A: SimpleNamespace(
                asset_type="agent", path=str(agent_yaml), ref=A, bundle="kolsch"
            )
        }
    )
    outcome = _reload_agents(catalog)
    assert outcome.error is not None
    assert "invalid permission rule" in outcome.error
    assert "Old rules still active" in outcome.error
    assert "legacy" in outcome.error
    assert outcome.rules_by_id == {}


def test_bundle_with_legacy_from_rule_is_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundles = tmp_path / "bundles"
    for name, permissions in (("good", []), ("bad", [_LEGACY_FROM])):
        (bundles / name).mkdir(parents=True)
        (bundles / name / "bundle.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "version": "0.1.0",
                    "description": f"{name} bundle (test fixture)",
                    "permissions": permissions,
                }
            ),
            encoding="utf-8",
        )
    with caplog.at_level(logging.WARNING):
        catalog = catalog_module.scan(tmp_path)
    assert "good" in catalog.bundles
    assert "bad" not in catalog.bundles
    assert any(
        "Skipping bundle bad: missing or malformed bundle.yaml" in r.getMessage()
        for r in caplog.records
    )
