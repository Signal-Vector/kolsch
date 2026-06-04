"""Schema additions — Phase 2.2 Step 8 (ENV §8 / §A.11).

Pins the four envelope fields the schema now carries:

- ``extends: str | None`` on agent (with prefix grammar).
- ``permissions: list[PermissionRule]`` on agent.
- ``context_files: list[ContextFileSpec]`` on agent (with
  prefix grammar on ``path``).
- ``workspace_summary: str`` accepted on platform.yaml's section
  list.

Backward compatibility: a YAML without ANY of these fields still
loads and resolves through Layer 1 cleanly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kernel.partials import PartialAgentConfig
from kernel.validation import validate_partial, validate_resolved
from schemas import ContextFileSpec, PermissionRule


# ---------------------------------------------------------------------------
# ContextFileSpec — prefix grammar at construction time.
# ---------------------------------------------------------------------------


def test_context_file_spec_accepts_kolsch_prefix() -> None:
    spec = ContextFileSpec(path="kolsch:docs/index.md", primary=True)
    assert spec.path == "kolsch:docs/index.md"
    assert spec.primary is True
    assert spec.summary is None


def test_context_file_spec_accepts_workspace_prefix() -> None:
    spec = ContextFileSpec(
        path="workspace:CONVENTIONS.md",
        summary="Conventions",
    )
    assert spec.path == "workspace:CONVENTIONS.md"
    assert spec.summary == "Conventions"
    assert spec.primary is False


def test_context_file_spec_accepts_plain_path() -> None:
    """Plain paths are now accepted at the schema layer
    (qol-prefix-defaults-and-unified-refs.md Step 2 — implicit
    workspace: prefix). Resolution semantics live in
    ``templating.context_files.resolve_path``; the schema only
    requires a non-empty string."""
    spec = ContextFileSpec(path="plain.md")
    assert spec.path == "plain.md"


def test_context_file_spec_accepts_unknown_prefix_shape() -> None:
    """Schema layer is permissive — any non-empty string is
    accepted. URLs would fall through to plain-path workspace
    lookup at resolve time and surface ContextFileNotFound, not
    a schema error."""
    spec = ContextFileSpec(path="http://example.com/x.md")
    assert spec.path == "http://example.com/x.md"


def test_context_file_spec_rejects_empty_path() -> None:
    """The one remaining schema check — non-empty string."""
    with pytest.raises(ValidationError):
        ContextFileSpec(path="")


# ---------------------------------------------------------------------------
# PermissionRule — effect literal.
# ---------------------------------------------------------------------------


def test_permission_rule_accepts_three_effect_values() -> None:
    for effect in ("allow", "deny", "prompt"):
        rule = PermissionRule(who=".*", what=r"tool\.bash", effect=effect)
        assert rule.effect == effect


def test_permission_rule_rejects_unknown_effect() -> None:
    with pytest.raises(ValidationError):
        PermissionRule(who=".*", what=r"tool\.bash", effect="wrong")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PartialAgentConfig — full envelope load + back-compat.
# ---------------------------------------------------------------------------


def test_partial_agent_with_all_envelope_fields_validates() -> None:
    """The bug doc's load-bearing path — an agent declaring
    ``extends``, ``permissions``, AND ``context_files`` parses
    through Layer 1 (no more ``extra_forbidden``)."""
    partial = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "agent_id": "ag_helper",
        "name": "helper",
        "description": "demo",
        "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
        "prompt": "hi",
        "io": {"mode": "conversational"},
        "extends": "kolsch:agents/_base.yaml",
        "permissions": [
            {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
        ],
        "context_files": [
            {"path": "kolsch:docs/index.md", "primary": True},
        ],
    }
    result = validate_partial(partial)
    assert result.ok, result.errors


def test_partial_agent_without_envelope_fields_still_validates() -> None:
    """Back-compat: Phase 1.x / 2.0 / 2.1 agent YAMLs continue
    to parse cleanly."""
    partial = {
        "schema_version": "0.1",
        "name": "legacy",
    }
    result = validate_partial(partial)
    assert result.ok


def test_partial_agent_with_unprefixed_context_file_path_passes_layer_1() -> None:
    """qol-prefix-defaults-and-unified-refs.md Step 2 — the
    schema layer accepts plain paths; the resolver applies the
    implicit ``workspace:`` interpretation."""
    partial = {
        "schema_version": "0.1",
        "context_files": [{"path": "plain.md"}],
    }
    result = validate_partial(partial)
    assert result.ok, result.errors


def test_partial_agent_with_unprefixed_extends_passes_layer_1() -> None:
    """qol-prefix-defaults-and-unified-refs.md Step 2 — the
    extends validator is now permissive. The four input forms
    (1 / 2 / 3-segment catalog refs + prefixed file paths) are
    disambiguated at resolve time."""
    partial = {
        "schema_version": "0.1",
        "extends": "plain.yaml",
    }
    result = validate_partial(partial)
    assert result.ok, result.errors


def test_partial_agent_with_invalid_permission_effect_fails_layer_1() -> None:
    """The Literal["allow", "deny", "prompt"] catches a typo at
    Layer 1."""
    partial = {
        "schema_version": "0.1",
        "permissions": [
            {"who": ".*", "what": "anything", "effect": "magic"},
        ],
    }
    result = validate_partial(partial)
    assert not result.ok


# ---------------------------------------------------------------------------
# Resolved-config Layer 1 — required fields enforced.
# ---------------------------------------------------------------------------


def test_resolved_agent_with_envelope_fields_validates() -> None:
    """The resolved config carries the new fields without
    breaking the existing required-field discipline."""
    resolved = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "agent_id": "ag_helper",
        "name": "helper",
        "description": "demo",
        "model": {"provider": "anthropic", "model": "claude-opus-4-7"},
        "prompt": "hi",
        "io": {"mode": "conversational"},
        "extends": "kolsch:agents/_base.yaml",
        "permissions": [
            {"who": ".*", "what": r"tool\.bash", "effect": "prompt"},
        ],
        "context_files": [
            {"path": "kolsch:docs/index.md", "primary": True},
        ],
    }
    result = validate_resolved(resolved)
    assert result.ok, result.errors


def test_partial_agent_config_pydantic_round_trip() -> None:
    """``PartialAgentConfig`` can absorb the new fields directly
    too — useful for Step 13's inheritance resolver that
    constructs partials in memory."""
    p = PartialAgentConfig.model_validate(
        {
            "extends": "kolsch:agents/_base.yaml",
            "permissions": [
                {"who": ".*", "what": "tool.bash", "effect": "allow"},
            ],
            "context_files": [
                {"path": "workspace:CONVENTIONS.md"},
            ],
        }
    )
    assert p.extends == "kolsch:agents/_base.yaml"
    assert p.permissions is not None and len(p.permissions) == 1
    assert p.context_files is not None and len(p.context_files) == 1
