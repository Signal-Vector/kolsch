"""Phase 3.0 Step 4 — workflow policy / authz integration tests.

Covers Step 4 ACs 4.1-4.17:

- ``allow()`` signature: ``layers: tuple[PermissionLayer, ...]`` +
  ``caller_chain: tuple[str, ...]`` (the per-layer ``agent_permissions``
  / ``bundle_permissions`` kwargs are gone).
- :class:`PermissionLayer` is ``@dataclass(frozen=True, slots=True)``.
- Three-segment refs for nodes / workflows / agents / tools validate
  via :func:`parse_three_segment` + :meth:`AssetCatalog.lookup`.
- Action ``node.<name>`` matches ``<bundle>/nodes/<name>`` refs via
  the action map.
- Two-segment tool refs (``kolsch/bash``) continue to work
  (migration shim — DO NOT touch).
- ``Rule.from_`` field with STRICT 3-seg + asset_type
  (``agents``/``workflows``) + catalog-existence validation
  (Round 4 item #13 HARD ERRORS — no warning fallback).
- ``from:`` matcher fires only when ANY ``caller_chain`` frame matches
  the glob (transitive); empty chain fails closed.
- ``audit_level: str`` config field defaults to ``"rule_matched"``;
  accepts arbitrary strings (typed ``str``, not ``Literal``).
- ``allow()`` docstring documents the engine-side layer-tuple
  construction contract + the ``policy.decision.made`` wire kind.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from providers.authz import (
    PermissionLayer,
    Rule,
    RuleBasedAuthorizationConfig,
    RuleBasedAuthorizationProvider,
)
from workflows.errors import (
    WorkflowFromAssetTypeError,
    WorkflowFromNotFound,
    WorkflowFromShapeError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _catalog_with_assets() -> AssetCatalog:
    """A small in-memory catalog covering the asset types this step
    references — tools (2-seg shim), nodes (3-seg), workflows
    (3-seg), agents (3-seg for ``from:`` targets)."""
    cat = AssetCatalog()
    for ref, asset_type, bundle in [
        ("kolsch/tools/bash", "tool", "kolsch"),
        ("kolsch/nodes/parse", "node", "kolsch"),
        ("kolsch/workflows/triage", "workflow", "kolsch"),
        ("kolsch/agents/triage", "agent", "kolsch"),
        ("kolsch/agents/helper", "agent", "kolsch"),
    ]:
        cat.assets[ref] = AssetEntry(
            ref=ref,
            asset_type=asset_type,
            bundle=bundle,
            path=f"/var/empty/{ref}",
            metadata={},
        )
    return cat


def _provider(
    rules: list[dict[str, Any]] | None = None,
    catalog: AssetCatalog | None = None,
) -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={"rules": rules or []},
        catalog=catalog,
    )


# ---------------------------------------------------------------------------
# AC 4.1 — PermissionLayer dataclass + layers tuple signature
# ---------------------------------------------------------------------------


def test_permission_layer_is_frozen_dataclass() -> None:
    layer = PermissionLayer(name="workflow", rules=())
    with pytest.raises(FrozenInstanceError):
        layer.name = "agent"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        layer.rules = (Rule(what="*", effect="deny"),)  # type: ignore[misc]


def test_permission_layer_has_slots() -> None:
    layer = PermissionLayer(name="workflow", rules=())
    # ``__slots__`` is locked by ``@dataclass(slots=True)`` — confirm via
    # the class attribute rather than runtime mutation (frozen+slots
    # interact with ``__setattr__`` such that the raised exception type
    # depends on the Python version).
    assert hasattr(PermissionLayer, "__slots__")
    assert set(layer.__slots__) == {"name", "rules"}


async def test_allow_uses_layers_tuple_not_per_layer_kwargs() -> None:
    """Round 4 item #7 — ``allow()`` accepts a single ordered
    ``layers`` tuple (the legacy ``agent_permissions=`` /
    ``bundle_permissions=`` kwargs are GONE)."""
    provider = _provider()
    workflow_layer = PermissionLayer(
        name="workflow",
        rules=(Rule(what="*", effect="allow"),),
    )
    result = await provider.allow("user.local", "tool.bash", layers=(workflow_layer,))
    assert result.allowed is True
    assert "who_chain=workflow" in result.reason


async def test_allow_walks_layers_in_order() -> None:
    """First-match-wins per layer; layer-tuple ordering is the
    caller's responsibility."""
    provider = _provider()
    workflow_layer = PermissionLayer(
        name="workflow",
        rules=(Rule(what="tool.bash", effect="allow"),),
    )
    agent_layer = PermissionLayer(
        name="agent",
        rules=(Rule(what="tool.bash", effect="deny"),),
    )
    # workflow FIRST → workflow's allow wins.
    result = await provider.allow(
        "user.local",
        "tool.bash",
        layers=(workflow_layer, agent_layer),
    )
    assert result.allowed is True
    assert "who_chain=workflow" in result.reason

    # agent FIRST → agent's deny wins.
    result2 = await provider.allow(
        "user.local",
        "tool.bash",
        layers=(agent_layer, workflow_layer),
    )
    assert result2.allowed is False
    assert "who_chain=agent" in result2.reason


async def test_omitting_workflow_layer_simulates_embedded_agent() -> None:
    """When the proximate caller is an embedded agent, the engine
    OMITS the ``workflow`` layer from the tuple entirely — there is
    no workflow-layer match path."""
    provider = _provider()
    agent_layer = PermissionLayer(
        name="agent",
        rules=(Rule(what="tool.bash", effect="allow"),),
    )
    result = await provider.allow("user.local", "tool.bash", layers=(agent_layer,))
    assert result.allowed is True
    assert "who_chain=agent" in result.reason
    assert "workflow" not in result.reason


# ---------------------------------------------------------------------------
# AC 4.3 / 4.4 / 4.7 — 3-seg refs for nodes / workflows / agents / tools
# ---------------------------------------------------------------------------


def test_3seg_node_ref_validates_against_catalog() -> None:
    """``what: kolsch/nodes/parse`` resolves when the node exists."""
    _provider(
        rules=[
            {"what": "kolsch/nodes/parse", "pattern": "*", "effect": "allow"},
        ],
        catalog=_catalog_with_assets(),
    )


def test_3seg_workflow_ref_validates_against_catalog() -> None:
    _provider(
        rules=[
            {
                "what": "kolsch/workflows/triage",
                "pattern": "*",
                "effect": "allow",
            },
        ],
        catalog=_catalog_with_assets(),
    )


def test_3seg_agent_ref_validates_against_catalog() -> None:
    _provider(
        rules=[
            {
                "what": "kolsch/agents/triage",
                "pattern": "*",
                "effect": "allow",
            },
        ],
        catalog=_catalog_with_assets(),
    )


def test_3seg_tool_ref_validates_against_catalog() -> None:
    """AC 4.7 — 3-seg tool refs are accepted (future-proofing)."""
    _provider(
        rules=[
            {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
        ],
        catalog=_catalog_with_assets(),
    )


def test_3seg_node_ref_unknown_raises() -> None:
    from errors import AuthzToolRefUnknown

    with pytest.raises(AuthzToolRefUnknown):
        _provider(
            rules=[
                {
                    "what": "kolsch/nodes/missing",
                    "pattern": "*",
                    "effect": "allow",
                },
            ],
            catalog=_catalog_with_assets(),
        )


# ---------------------------------------------------------------------------
# AC 4.5 — action node.<name> maps to <bundle>/nodes/<name>
# ---------------------------------------------------------------------------


async def test_node_action_matches_3seg_node_ref() -> None:
    """A rule with ``what: kolsch/nodes/parse`` fires on a
    ``node.parse`` action invocation."""
    provider = _provider(
        rules=[
            {"what": "kolsch/nodes/parse", "pattern": "*", "effect": "allow"},
        ],
        catalog=_catalog_with_assets(),
    )
    result = await provider.allow("user.local", "node.parse")
    assert result.allowed is True


# ---------------------------------------------------------------------------
# AC 4.6 — tool ref directly matches the 3-seg action
# (qol-action-namespace-bundle-scoped.md Step 2 — the bridge map for
# tools is deleted; action IS the ref now.)
# ---------------------------------------------------------------------------


async def test_3seg_tool_ref_matches_3seg_tool_action() -> None:
    """``what: kolsch/tools/bash`` matches ``kolsch/tools/bash`` action.

    qol-action-namespace-bundle-scoped.md Step 2 — supersedes the
    deleted 2-seg → 1-seg bridge. The action namespace and the
    rule.what share the same 3-seg grammar; matcher fires directly.
    """
    provider = _provider(
        rules=[
            {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
        ],
        catalog=_catalog_with_assets(),
    )
    result = await provider.allow("user.local", "kolsch/tools/bash")
    assert result.allowed is True


# ---------------------------------------------------------------------------
# AC 4.9 — Rule.from_ field (alias "from")
# ---------------------------------------------------------------------------


def test_rule_from_field_accepts_alias() -> None:
    """The YAML key ``from:`` populates the Python attribute ``from_``."""
    rule = Rule.model_validate(
        {"what": "*", "effect": "allow", "from": "kolsch/agents/triage"}
    )
    assert rule.from_ == "kolsch/agents/triage"


def test_rule_from_field_defaults_to_none() -> None:
    rule = Rule(what="*", effect="allow")
    assert rule.from_ is None


# ---------------------------------------------------------------------------
# AC 4.10 / 4.11 / 4.12 — caller_chain semantics
# ---------------------------------------------------------------------------


async def test_caller_chain_kwarg_default_is_empty_tuple() -> None:
    """``allow(...)`` without ``caller_chain=`` defaults to ``()``."""
    provider = _provider(
        rules=[{"what": "*", "pattern": "*", "effect": "allow"}],
    )
    # No from: rules so the default-empty chain has no effect.
    result = await provider.allow("user.local", "tool.bash")
    assert result.allowed is True


async def test_from_filter_fails_closed_on_empty_caller_chain() -> None:
    """AC 4.11 — rule with ``from:`` + empty chain → rule SKIPS
    (does not contribute allow/deny)."""
    provider = _provider(
        rules=[
            {
                "what": "*",
                "pattern": "*",
                "from": "kolsch/agents/triage",
                "effect": "allow",
            },
            {"what": "*", "pattern": "*", "effect": "deny"},
        ],
        catalog=_catalog_with_assets(),
    )
    # No caller_chain → from: rule SKIPS → deny tail fires.
    result = await provider.allow("user.local", "tool.bash")
    assert result.allowed is False


async def test_from_filter_fires_when_chain_has_matching_frame() -> None:
    provider = _provider(
        rules=[
            {
                "what": "*",
                "pattern": "*",
                "from": "kolsch/agents/triage",
                "effect": "allow",
            },
            {"what": "*", "pattern": "*", "effect": "deny"},
        ],
        catalog=_catalog_with_assets(),
    )
    result = await provider.allow(
        "user.local",
        "tool.bash",
        caller_chain=("kolsch/agents/triage",),
    )
    assert result.allowed is True


async def test_from_filter_is_transitive_across_chain_frames() -> None:
    """AC 4.12 — the rule fires when ANY frame in the chain matches,
    even a DISTAL one."""
    provider = _provider(
        rules=[
            {
                "what": "*",
                "pattern": "*",
                "from": "kolsch/agents/triage",
                "effect": "allow",
            },
            {"what": "*", "pattern": "*", "effect": "deny"},
        ],
        catalog=_catalog_with_assets(),
    )
    # triage is the DISTAL frame — should still fire.
    result = await provider.allow(
        "user.local",
        "tool.bash",
        caller_chain=(
            "kolsch/agents/helper",
            "kolsch/workflows/triage",
            "kolsch/agents/triage",
        ),
    )
    assert result.allowed is True


async def test_from_filter_glob_matches_pattern() -> None:
    """``from: kolsch/agents/*`` fires for any agent in the kolsch bundle."""
    provider = _provider(
        rules=[
            {
                "what": "*",
                "pattern": "*",
                "from": "kolsch/agents/triage",  # passes shape validation
                "effect": "allow",
            },
        ],
        catalog=_catalog_with_assets(),
    )
    # Build the same rule programmatically with a glob from_ value
    # (the catalog-existence check is satisfied by the YAML path; for
    # globs we use the programmatic path which still validates shape).
    rule = Rule(what="*", from_="kolsch/agents/*", effect="allow")
    result = await provider.allow(
        "user.local",
        "tool.bash",
        layers=(PermissionLayer(name="agent", rules=(rule,)),),
        caller_chain=("kolsch/agents/helper",),
    )
    assert result.allowed is True
    _ = provider  # silence unused-warning when the catalog-rule isn't exercised


async def test_from_filter_misses_when_no_chain_frame_matches() -> None:
    """``from: kolsch/agents/triage`` does NOT fire when the chain
    contains only other agents."""
    provider = _provider(
        rules=[
            {
                "what": "*",
                "pattern": "*",
                "from": "kolsch/agents/triage",
                "effect": "allow",
            },
            {"what": "*", "pattern": "*", "effect": "deny"},
        ],
        catalog=_catalog_with_assets(),
    )
    result = await provider.allow(
        "user.local",
        "tool.bash",
        caller_chain=("kolsch/agents/helper",),
    )
    assert result.allowed is False


# ---------------------------------------------------------------------------
# AC 4.13 — STRICT from: validation HARD ERRORS at compile time
# ---------------------------------------------------------------------------


def test_from_shape_error_for_non_3seg_ref() -> None:
    with pytest.raises(WorkflowFromShapeError):
        _provider(
            rules=[
                {
                    "what": "*",
                    "from": "random",
                    "effect": "allow",
                },
            ],
            catalog=_catalog_with_assets(),
        )


def test_from_asset_type_error_for_nodes_segment() -> None:
    with pytest.raises(WorkflowFromAssetTypeError):
        _provider(
            rules=[
                {
                    "what": "*",
                    "from": "kolsch/nodes/parse",  # 3-seg but wrong asset_type
                    "effect": "allow",
                },
            ],
            catalog=_catalog_with_assets(),
        )


def test_from_not_found_error_for_unknown_agent() -> None:
    with pytest.raises(WorkflowFromNotFound):
        _provider(
            rules=[
                {
                    "what": "*",
                    "from": "kolsch/agents/nonexistent",
                    "effect": "allow",
                },
            ],
            catalog=_catalog_with_assets(),
        )


def test_from_shape_warning_class_is_removed() -> None:
    """Round 4 item #13 — the warning fallback is GONE."""
    from workflows import errors as workflow_errors

    assert not hasattr(workflow_errors, "WorkflowFromShapeWarning")


# ---------------------------------------------------------------------------
# AC 4.14 — core/principals NOT added
# ---------------------------------------------------------------------------


def test_core_principals_not_referenced_in_src() -> None:
    """AC 4.14 — ``core/principals/*`` is NOT supported. The same
    invariant lives in :mod:`test_workflow_scaffolding` (Step 1
    item 1.16); replicate the src-tree grep here so authz-focused
    test sessions catch a regression without depending on the
    scaffolding suite."""
    import subprocess
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "src"
    result = subprocess.run(
        ["grep", "-rln", "core/principals", str(src)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", (
        f"unexpected core/principals references in src/:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# AC 4.15 — engine-side layer-tuple-construction contract documented
# ---------------------------------------------------------------------------


def test_allow_docstring_documents_layer_construction_contract() -> None:
    doc = RuleBasedAuthorizationProvider.allow.__doc__ or ""
    assert "workflow" in doc.lower()
    assert "proximate" in doc.lower()
    assert "embedded agent" in doc.lower()
    assert "omit" in doc.lower()


def test_allow_docstring_documents_policy_decision_made_wire_kind() -> None:
    doc = RuleBasedAuthorizationProvider.allow.__doc__ or ""
    assert "policy.decision.made" in doc


# ---------------------------------------------------------------------------
# AC 4.16 — audit_level config field
# ---------------------------------------------------------------------------


def test_audit_level_defaults_to_rule_matched() -> None:
    provider = RuleBasedAuthorizationProvider(config={"rules": []})
    assert provider.audit_level == "rule_matched"


def test_audit_level_accepts_all() -> None:
    provider = RuleBasedAuthorizationProvider(
        config={"rules": [], "audit_level": "all"}
    )
    assert provider.audit_level == "all"


def test_audit_level_typed_as_str_not_literal() -> None:
    """Round 4 item #9 — the field is typed ``str``, not
    ``Literal["all", "rule_matched"]``. Arbitrary strings are
    accepted; semantic interpretation lives at the emit site."""
    provider = RuleBasedAuthorizationProvider(
        config={"rules": [], "audit_level": "verbose"}
    )
    assert provider.audit_level == "verbose"


def test_audit_level_field_on_pydantic_config_model() -> None:
    """The :class:`RuleBasedAuthorizationConfig` model exposes the
    field with a default of ``"rule_matched"``."""
    cfg = RuleBasedAuthorizationConfig()
    assert cfg.audit_level == "rule_matched"
    cfg2 = RuleBasedAuthorizationConfig(audit_level="all")
    assert cfg2.audit_level == "all"
