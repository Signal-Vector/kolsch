"""``PlatformVar`` unit tests — Phase 2.2 Step 7 (spec §4.4 +
Step 7 reviewer's five checks).

Pins:

- All five top-level attributes (``providers`` / ``authorization``
  / ``workspace_summary`` / ``file_access`` / ``aliases``).
- ``platform.providers`` exposes the full standardized method
  family + four Pythonic protocols (mirrors :class:`ToolsVar` /
  :class:`DocsVar`).
- ``.get("nonexistent")`` returns ``None`` on each sub-object
  rather than raising.
- Module is named ``_platform.py`` (underscore prefix) to avoid
  the stdlib ``platform`` clash — reviewer's check 4.
- :class:`llm.protocol.ProviderConfig` objects expose their own
  attributes (``.type`` / ``.api_key_secret`` / ``.models``) —
  reviewer's check 5.
"""

from __future__ import annotations

import pytest

from llm.protocol import ProviderConfig
from providers.authz import Rule
from templating.vars._platform import (
    AliasesVar,
    AuthorizationVar,
    FileAccessVar,
    PlatformVar,
    ProvidersVar,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _make_provider(name: str, type_: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=type_,
        api_key_secret=f"{name.upper()}_API_KEY",
        base_url=f"https://{name}.example.com/v1",
        timeout_seconds=30,
        models=[{"id": "default-model"}],
    )


def _make_platform() -> PlatformVar:
    providers = ProvidersVar(
        [
            _make_provider("openrouter", "openrouter"),
            _make_provider("opencode", "opencode"),
        ]
    )
    authorization = AuthorizationVar(
        variant="rule_based",
        rules=[
            Rule(what="kolsch/tools/read_file", pattern="*", effect="allow"),
            Rule(what="kolsch/tools/bash", pattern="*", effect="prompt"),
        ],
    )
    file_access = FileAccessVar(
        allowed_paths=["data/", "bundles/"],
        denied_paths=[".env", ".platform/"],
    )
    aliases = AliasesVar({"opus": "anthropic/claude-opus-4-7"})
    return PlatformVar(
        providers=providers,
        authorization=authorization,
        workspace_summary="An active workspace.",
        file_access=file_access,
        aliases=aliases,
        _workspace_path="/tmp/ws",
    )


@pytest.fixture
def platform_var() -> PlatformVar:
    return _make_platform()


# ---------------------------------------------------------------------------
# All five attributes present.
# ---------------------------------------------------------------------------


def test_five_top_level_attributes(platform_var: PlatformVar) -> None:
    """Reviewer's check 1 — five top-level attributes."""
    assert isinstance(platform_var.providers, ProvidersVar)
    assert isinstance(platform_var.authorization, AuthorizationVar)
    assert platform_var.workspace_summary == "An active workspace."
    assert isinstance(platform_var.file_access, FileAccessVar)
    assert isinstance(platform_var.aliases, AliasesVar)


# ---------------------------------------------------------------------------
# ProvidersVar — full method family + Pythonic protocols.
# ---------------------------------------------------------------------------


def test_providers_names_comma_separated(platform_var: PlatformVar) -> None:
    """Spec §4.4: ``providers.names()`` → "openrouter, opencode"."""
    assert platform_var.providers.names() == "openrouter, opencode"


def test_providers_get_returns_provider_config(platform_var: PlatformVar) -> None:
    """``providers.get("openrouter")`` returns the typed
    :class:`ProviderConfig`. Reviewer's check 5 — the returned
    object exposes its own attributes."""
    pc = platform_var.providers.get("openrouter")
    assert pc is not None
    assert pc.name == "openrouter"
    assert pc.type == "openrouter"
    assert pc.api_key_secret == "OPENROUTER_API_KEY"
    assert pc.models == [{"id": "default-model"}]


def test_providers_get_returns_none_for_unknown(platform_var: PlatformVar) -> None:
    """Reviewer's check 3 — ``.get("nonexistent")`` returns
    ``None``, not raises."""
    assert platform_var.providers.get("ghost") is None


def test_providers_subscript_equals_get(platform_var: PlatformVar) -> None:
    """Spec §4.3 subscript-equivalence."""
    assert (
        platform_var.providers["openrouter"]
        is platform_var.providers.get("openrouter")
    )


def test_providers_count_and_len(platform_var: PlatformVar) -> None:
    """``.count`` attribute and ``__len__`` both return 2."""
    assert platform_var.providers.count == 2
    assert len(platform_var.providers) == 2


def test_providers_iteration_preserves_order(platform_var: PlatformVar) -> None:
    """Spec §4.3 — iteration over items in declaration order."""
    names = [p.name for p in platform_var.providers]
    assert names == ["openrouter", "opencode"]


def test_providers_contains(platform_var: PlatformVar) -> None:
    assert "openrouter" in platform_var.providers
    assert "ghost" not in platform_var.providers


# ---------------------------------------------------------------------------
# AuthorizationVar.
# ---------------------------------------------------------------------------


def test_authorization_exposes_variant_and_rules(platform_var: PlatformVar) -> None:
    """Spec §4.4: ``platform.authorization`` is a single-object
    with ``variant`` and ``rules``."""
    assert platform_var.authorization.variant == "rule_based"
    assert len(platform_var.authorization.rules) == 2
    assert all(isinstance(r, Rule) for r in platform_var.authorization.rules)


# ---------------------------------------------------------------------------
# FileAccessVar.
# ---------------------------------------------------------------------------


def test_file_access_exposes_allowed_and_denied_paths(
    platform_var: PlatformVar,
) -> None:
    """Spec §4.4: ``platform.file_access`` is a single-object
    with ``allowed_paths`` + ``denied_paths``."""
    assert platform_var.file_access.allowed_paths == ["data/", "bundles/"]
    assert platform_var.file_access.denied_paths == [".env", ".platform/"]


# ---------------------------------------------------------------------------
# AliasesVar — dict-shaped.
# ---------------------------------------------------------------------------


def test_aliases_get_returns_canonical_ref(platform_var: PlatformVar) -> None:
    """Spec §4.4: ``platform.aliases.get("opus")`` returns the
    canonical ref string."""
    assert platform_var.aliases.get("opus") == "anthropic/claude-opus-4-7"


def test_aliases_get_returns_none_for_unknown(platform_var: PlatformVar) -> None:
    """``.get("unknown")`` returns ``None``."""
    assert platform_var.aliases.get("unknown") is None


def test_aliases_subscript_equals_get(platform_var: PlatformVar) -> None:
    assert platform_var.aliases["opus"] == platform_var.aliases.get("opus")


def test_aliases_contains(platform_var: PlatformVar) -> None:
    assert "opus" in platform_var.aliases
    assert "ghost" not in platform_var.aliases


# ---------------------------------------------------------------------------
# Default rendering — spec §4.4.
# ---------------------------------------------------------------------------


def test_default_rendering_matches_spec(platform_var: PlatformVar) -> None:
    """Spec §4.4: ``Workspace: <path> · Providers: <names> ·
    <N> rules``. Uses U+00B7 (middle dot) per the spec example."""
    rendered = str(platform_var)
    assert "Workspace: /tmp/ws" in rendered
    assert "Providers: openrouter, opencode" in rendered
    assert "2 rules" in rendered
    assert "·" in rendered


# ---------------------------------------------------------------------------
# Module-name discipline — reviewer's check 4.
# ---------------------------------------------------------------------------


def test_module_uses_underscore_prefix_to_avoid_stdlib_clash() -> None:
    """Reviewer's check 4 — the module is named ``_platform.py``
    so ``import platform`` (the stdlib) still works alongside.
    Pinning the import path makes a future rename a deliberate
    edit."""
    import templating.vars._platform as plat_mod

    assert plat_mod.__name__ == "templating.vars._platform"
