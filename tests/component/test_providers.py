"""Component tests for src/providers — TC-SEC-*, TC-SBX-*, TC-AUZ-*.

Spec: docs/explanation/secrets-sandbox-authz-contract.md + Module API
Reference §6 (Secrets), §7 (Sandbox), §8 (Authorization).

These are component tests per handbook §8.1 — each provider runs
against real backing (a real .env file on disk for Secrets, real
Python execution for Sandbox, real regex compilation for Authz).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from errors import (
    AuthzMalformedRule,
    AuthzRegexInvalid,
    ConfigInvalid,
    SecretNotFound,
    SecretReadFailed,
)
from providers import (
    AuthorizationResult,
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
    make_authorization_provider,
    make_sandbox_provider,
    make_secrets_provider,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env(path: Path, payload: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in payload.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===========================================================================
# SECRETS — TC-SEC-*
# ===========================================================================


# --- TC-SEC-01 — resolve returns value -------------------------------------


async def test_dotenv_resolve_returns_value(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", {"KEY": "value"})
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    assert await provider.resolve("KEY") == "value"


async def test_dotenv_exists_correct(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", {"PRESENT": "x"})
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    assert await provider.exists("PRESENT") is True
    assert await provider.exists("ABSENT") is False


async def test_dotenv_list_names_returns_keys_not_values(
    tmp_path: Path,
) -> None:
    _write_env(tmp_path / ".env", {"A": "alpha", "B": "beta"})
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    names = await provider.list_names()
    assert set(names) == {"A", "B"}
    # Spec S-I1 reinforcement — the value strings MUST NOT be in the
    # return.
    assert "alpha" not in names
    assert "beta" not in names


async def test_dotenv_parses_quotes_and_comments(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        textwrap_dedent(
            """
            # comment
            PLAIN=plain_value
            QUOTED_DOUBLE="double quoted"
            QUOTED_SINGLE='single quoted'
            EMPTY=
            """
        ),
        encoding="utf-8",
    )
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    assert await provider.resolve("PLAIN") == "plain_value"
    assert await provider.resolve("QUOTED_DOUBLE") == "double quoted"
    assert await provider.resolve("QUOTED_SINGLE") == "single quoted"
    # Spec §1.4.4 — empty strings could be legitimate secret values;
    # accept them, don't filter.
    assert await provider.resolve("EMPTY") == ""


async def test_dotenv_missing_file_yields_empty_dict(tmp_path: Path) -> None:
    """No .env on disk: provider initializes with zero secrets rather
    than crashing — the user may add the file later (next restart)."""
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    assert await provider.list_names() == []


async def test_dotenv_unreadable_file_raises_secret_read_failed(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text("KEY=v\n", encoding="utf-8")
    env.chmod(0o000)
    try:
        with pytest.raises(SecretReadFailed):
            DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    finally:
        env.chmod(0o644)


# --- TC-SEC-02 — secret value never appears in logs ------------------------


async def test_secret_value_never_appears_in_provider_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec S-I1 — the Secrets provider MUST NOT log resolved values.

    Engine-side leakage (events) is covered by Engine I10 in a later
    step; this test pins the provider's own logging discipline.
    """
    distinctive_value = "VERY-DISTINCTIVE-SECRET-VALUE-9z8x7c6v5b4n3m2"
    _write_env(tmp_path / ".env", {"DISTINCTIVE_KEY": distinctive_value})

    with caplog.at_level(logging.DEBUG):
        provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
        _ = await provider.resolve("DISTINCTIVE_KEY")
        _ = await provider.exists("DISTINCTIVE_KEY")
        _ = await provider.list_names()
        # Also try a missing lookup — the error path must not leak
        # other secrets either.
        with pytest.raises(SecretNotFound):
            await provider.resolve("NOT_PRESENT")

    for record in caplog.records:
        rendered = record.getMessage()
        assert distinctive_value not in rendered, (
            f"secret value leaked in log record: {record!r}"
        )


# --- TC-SEC-03 — missing secret raises -------------------------------------


async def test_resolve_missing_secret_raises(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", {"KNOWN": "x"})
    provider = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    with pytest.raises(SecretNotFound) as exc_info:
        await provider.resolve("ABSENT")
    assert exc_info.value.detail["name"] == "ABSENT"


# ===========================================================================
# SANDBOX — TC-SBX-*
# ===========================================================================


# --- TC-SBX-01 — native sandbox passes result through ----------------------


async def test_native_sandbox_passes_result_through() -> None:
    sandbox = NativeSandboxProvider()

    async def work() -> dict[str, int]:
        return {"answer": 42}

    direct = await work()
    sandboxed = await sandbox.execute(work, policy={})
    assert sandboxed == direct == {"answer": 42}


async def test_native_sandbox_returns_caller_supplied_value() -> None:
    sandbox = NativeSandboxProvider()
    sentinel = object()

    async def work() -> object:
        return sentinel

    result = await sandbox.execute(work, policy={})
    assert result is sentinel


# --- TC-SBX-02 — native sandbox passes exceptions through ------------------


async def test_native_sandbox_passes_exceptions_through_unchanged() -> None:
    sandbox = NativeSandboxProvider()

    class _Boom(RuntimeError):
        pass

    async def work() -> None:
        msg = "kaboom"
        raise _Boom(msg)

    with pytest.raises(_Boom) as direct:
        await sandbox.execute(work, policy={})
    assert str(direct.value) == "kaboom"


async def test_native_sandbox_accepts_arbitrary_policy_dict() -> None:
    """Native variant ignores `policy` content per spec §2.3.1; any
    dict is acceptable. Real sandbox variants will validate."""
    sandbox = NativeSandboxProvider()

    async def work() -> int:
        return 7

    result = await sandbox.execute(
        work,
        policy={"timeout": 30, "memory_mb": 512, "ignored": True},
    )
    assert result == 7


# ===========================================================================
# AUTHORIZATION — TC-AUZ-*
# ===========================================================================


def _authz(rules: list[dict[str, str]]) -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(config={"rules": rules})


# --- TC-AUZ-01 — default allow-all permits ---------------------------------


async def test_default_allow_all_permits() -> None:
    provider = _authz([{"who": ".*", "what": ".*", "effect": "allow"}])
    result = await provider.allow("user.local", "agent.execute.x")
    assert isinstance(result, AuthorizationResult)
    assert result.allowed is True
    assert result.matched_rule is not None
    assert result.matched_rule.effect == "allow"


# --- TC-AUZ-02 — first-match-wins; deny before allow ----------------------


async def test_first_match_wins_deny_before_allow() -> None:
    provider = _authz(
        [
            {"who": ".*", "what": r"tool\.bash", "effect": "deny"},
            {"who": ".*", "what": ".*", "effect": "allow"},
        ]
    )
    result = await provider.allow("agent.x", "tool.bash")
    assert result.allowed is False
    assert result.matched_rule is not None
    assert result.matched_rule.effect == "deny"


async def test_first_match_wins_allow_before_deny() -> None:
    # Reverse order — allow fires first, deny never reached.
    provider = _authz(
        [
            {"who": ".*", "what": ".*", "effect": "allow"},
            {"who": ".*", "what": r"tool\.bash", "effect": "deny"},
        ]
    )
    result = await provider.allow("agent.x", "tool.bash")
    assert result.allowed is True


# --- TC-AUZ-03 — default deny when no rule matches ------------------------


async def test_default_deny_when_no_rule_matches() -> None:
    provider = _authz([{"who": ".*", "what": r"tool\.read", "effect": "allow"}])
    result = await provider.allow("agent.x", "tool.bash")
    assert result.allowed is False
    assert result.matched_rule is None
    assert "default deny" in result.reason.lower()


async def test_default_deny_with_empty_rule_set() -> None:
    provider = _authz([])
    result = await provider.allow("user.local", "agent.execute.x")
    assert result.allowed is False
    assert result.matched_rule is None


# --- TC-AUZ-04 — regex is FULLMATCH ----------------------------------------


async def test_regex_is_fullmatch_not_prefix_or_search() -> None:
    """Spec A-I1 — re.fullmatch semantics. `tool.b` MUST NOT match
    `tool.bash` (it's only a prefix substring), so the deny rule
    should NOT fire and the rule set falls through to default deny."""
    provider = _authz([{"who": ".*", "what": "tool.b", "effect": "deny"}])
    result = await provider.allow("agent.x", "tool.bash")
    assert result.allowed is False
    # Default deny fired, NOT the rule.
    assert result.matched_rule is None


async def test_principal_is_also_fullmatched() -> None:
    """The same fullmatch rule applies to `who`. `user.l` MUST NOT
    match `user.local`."""
    provider = _authz([{"who": "user.l", "what": ".*", "effect": "allow"}])
    result = await provider.allow("user.local", "x.y")
    assert result.allowed is False


# --- TC-AUZ-05 — malformed regex fails at init -----------------------------


async def test_invalid_regex_in_what_raises_at_init() -> None:
    with pytest.raises(AuthzRegexInvalid) as exc_info:
        _authz([{"who": ".*", "what": "[", "effect": "allow"}])
    assert exc_info.value.detail["field"] == "what"
    assert exc_info.value.detail["regex"] == "["


async def test_invalid_regex_in_who_raises_at_init() -> None:
    with pytest.raises(AuthzRegexInvalid) as exc_info:
        _authz([{"who": "(unclosed", "what": ".*", "effect": "allow"}])
    assert exc_info.value.detail["field"] == "who"


async def test_malformed_rule_missing_field_raises_at_init() -> None:
    with pytest.raises(AuthzMalformedRule):
        _authz([{"who": ".*", "what": ".*"}])  # missing effect


async def test_malformed_rule_unknown_effect_raises_at_init() -> None:
    with pytest.raises(AuthzMalformedRule):
        _authz([{"who": ".*", "what": ".*", "effect": "maybe"}])


async def test_list_rules_returns_compiled_rules_read_only() -> None:
    """Spec §3.3 — list_rules is for diagnostic purposes."""
    rules_in = [
        {"who": ".*", "what": r"tool\.bash", "effect": "deny"},
        {"who": ".*", "what": ".*", "effect": "allow"},
    ]
    provider = _authz(rules_in)
    listed = await provider.list_rules()
    assert len(listed) == 2
    assert listed[0].effect == "deny"
    assert listed[1].effect == "allow"


# ===========================================================================
# TC-AUZ-PERMDISC-* — qol-tool-permission-discovery-uniform-with-stacking
# ===========================================================================


async def test_allow_no_rule_no_callback_raises_default_deny() -> None:
    """Regression guard for the non-interactive path: an empty rule
    set with no callback registered preserves the Phase 2.0 default
    deny behaviour (the dispatcher raises ToolUnauthorized off this
    result)."""
    from providers.authz import AuthzEffect

    provider = _authz([])
    result = await provider.allow("agent.x", "tool.bash")
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY
    assert result.matched_rule is None
    assert "default deny" in result.reason.lower()


async def test_allow_no_rule_with_callback_fires_with_rule_id_none() -> None:
    """qol-tool-permission-discovery Step 2 — when no rule matches
    AND a prompt callback has been registered (chat mode), the
    callback fires with ``rule_id=None`` so the modal can render
    the no-covering-rule variant of the panel."""
    from providers.authz import AuthzEffect

    provider = _authz([])
    seen: dict[str, object] = {}

    async def _cb(
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
    ) -> AuthzEffect:
        seen["rule_id"] = rule_id
        seen["action"] = action
        seen["target"] = target
        return AuthzEffect.ALLOW

    provider.register_prompt_callback(_cb)  # type: ignore[arg-type]
    result = await provider.allow(
        "agent.x", "tool.bash", target="echo hi",
    )
    assert seen["rule_id"] is None
    assert seen["action"] == "tool.bash"
    assert seen["target"] == "echo hi"
    assert result.allowed is True
    assert result.effect is AuthzEffect.ALLOW
    assert "who_chain=no_rule" in result.reason


async def test_allow_no_rule_with_callback_persists_rule_and_fires_hook() -> (
    None
):
    """The callback's ``(effect, Rule)`` tuple lands the new rule
    above the default-deny tail AND fires the ``on_rule_added``
    hook so the chat's ``cli.permissions._write_back`` persists it
    to ``platform.yaml``."""
    from providers.authz import AuthzEffect, Rule

    provider = _authz([])
    hook_called_with: list[list[Rule]] = []

    async def _cb(
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
    ) -> tuple[AuthzEffect, Rule]:
        _ = rule_id, target
        return (
            AuthzEffect.ALLOW,
            Rule(who=".*", what=action, effect="allow"),
        )

    def _on_rule_added(rules: list[Rule]) -> None:
        hook_called_with.append(list(rules))

    provider.register_prompt_callback(_cb, on_rule_added=_on_rule_added)  # type: ignore[arg-type]
    first = await provider.allow("agent.x", "tool.bash")
    assert first.allowed is True

    # Second invocation hits the persisted rule directly — callback
    # is not fired again. If it were, _cb would record a new entry.
    second = await provider.allow("agent.x", "tool.bash")
    assert second.allowed is True
    assert second.matched_rule is not None
    assert second.matched_rule.effect == "allow"

    # The hook fired exactly once (after the first persist).
    assert len(hook_called_with) == 1
    persisted = hook_called_with[0]
    assert any(r.what == "tool.bash" and r.effect == "allow" for r in persisted)


async def test_priority_agent_layer_wins_over_bundle_layer() -> None:
    """Four-level stack: agent's allow rule wins over the bundle's
    deny rule for the same action."""
    from providers.authz import PermissionLayer, Rule

    provider = _authz([])
    agent_rules = (Rule(who=".*", what=r"tool\.bash", effect="allow"),)
    bundle_rules = (Rule(who=".*", what=r"tool\.bash", effect="deny"),)
    result = await provider.allow(
        "agent.x",
        "tool.bash",
        layers=(
            PermissionLayer(name="agent", rules=agent_rules),
            PermissionLayer(name="bundle", rules=bundle_rules),
        ),
    )
    assert result.allowed is True
    assert "who_chain=agent" in result.reason


async def test_priority_bundle_layer_wins_over_platform_layer() -> None:
    """Four-level stack: a bundle's allow rule wins over a platform
    deny rule for the same action."""
    provider = _authz([{"who": ".*", "what": r"tool\.bash", "effect": "deny"}])
    from providers.authz import PermissionLayer, Rule

    bundle_rules = (Rule(who=".*", what=r"tool\.bash", effect="allow"),)
    result = await provider.allow(
        "agent.x",
        "tool.bash",
        layers=(PermissionLayer(name="bundle", rules=bundle_rules),),
    )
    assert result.allowed is True
    assert "who_chain=bundle" in result.reason


# ===========================================================================
# Registry seam — make_*_provider dispatches by variant name
# ===========================================================================


async def test_make_secrets_provider_dispatches_by_variant(
    tmp_path: Path,
) -> None:
    _write_env(tmp_path / ".env", {"K": "v"})
    provider = make_secrets_provider(
        root=tmp_path,
        config={"variant": "dotenv", "config": {"path": ".env"}},
    )
    assert await provider.resolve("K") == "v"


async def test_make_sandbox_provider_dispatches_by_variant() -> None:
    sandbox = make_sandbox_provider(config={"variant": "native", "config": {}})

    async def work() -> int:
        return 1

    assert await sandbox.execute(work, policy={}) == 1


async def test_make_authorization_provider_dispatches_by_variant() -> None:
    provider = make_authorization_provider(
        config={
            "variant": "rule_based",
            "config": {"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
        }
    )
    result = await provider.allow("user.local", "tool.read")
    assert result.allowed is True


# --- Unknown-variant failure path: typed PlatformError at bootstrap --------
# Spec §1.4.1 / §2.3.1 / §3.4.1 — variants declared in platform.yaml
# that aren't registered MUST fail fast at bootstrap rather than
# producing AttributeError later. The factory raises ConfigInvalid
# with a `unknown_variant` finding code.


async def test_make_secrets_provider_unknown_variant_raises_config_invalid(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        make_secrets_provider(
            root=tmp_path,
            config={"variant": "vault", "config": {}},
        )
    codes = [e["code"] for e in exc_info.value.detail["validation_errors"]]
    assert "unknown_variant" in codes


async def test_make_sandbox_provider_unknown_variant_raises_config_invalid() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        make_sandbox_provider(config={"variant": "docker", "config": {}})
    codes = [e["code"] for e in exc_info.value.detail["validation_errors"]]
    assert "unknown_variant" in codes


async def test_make_authz_provider_unknown_variant_raises_config_invalid() -> None:
    with pytest.raises(ConfigInvalid) as exc_info:
        make_authorization_provider(config={"variant": "policy_engine", "config": {}})
    codes = [e["code"] for e in exc_info.value.detail["validation_errors"]]
    assert "unknown_variant" in codes


# ---------------------------------------------------------------------------
# Small helper to avoid importing textwrap into the file's __all__-style
# visibility (keeps the test top-of-file imports tight).
# ---------------------------------------------------------------------------


def textwrap_dedent(text: str) -> str:
    import textwrap as _tw

    return _tw.dedent(text)
