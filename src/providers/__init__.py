"""Providers — memory, secrets, sandbox, authorization.

Per Module API Reference §6-§8 +
docs/explanation/secrets-sandbox-authz-contract.md. These three are
**platform-level** Provider Modules: configured in ``platform.yaml``,
applied uniformly to every agent. Memory is agent-level and lives
elsewhere (Step 8).

Each provider exposes a Protocol (the contract), one v1 variant, and
a small registry seam so future variants slot in without core changes.
"""

from providers.authz import (
    DEFAULT_DENY_RULE_ID,
    KOLSCH_TEST_BUILD_ENV_VAR,
    AllowAllTestAuthorizationProvider,
    AuthorizationProvider,
    AuthorizationResult,
    AuthzEffect,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_rule_id,
    make_authorization_provider,
    register_authorization_variant,
)
from providers.memory import (
    CompactionResult,
    FilterExpression,
    MemoryContext,
    MemoryItem,
    RecentBufferMemoryContext,
    RecentBufferMemoryProvider,
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
    make_memory_provider,
    register_memory_variant,
    validate_recent_buffer_config,
)
from providers.sandbox import (
    NativeSandboxProvider,
    SandboxProvider,
    make_sandbox_provider,
    register_sandbox_variant,
)
from providers.secrets import (
    DotenvSecretsProvider,
    SecretsProvider,
    make_secrets_provider,
    register_secrets_variant,
)

__all__ = [
    "DEFAULT_DENY_RULE_ID",
    "KOLSCH_TEST_BUILD_ENV_VAR",
    "AllowAllTestAuthorizationProvider",
    "AuthorizationProvider",
    "AuthorizationResult",
    "AuthzEffect",
    "CompactionResult",
    "DotenvSecretsProvider",
    "FilterExpression",
    "MemoryContext",
    "MemoryItem",
    "NativeSandboxProvider",
    "RecentBufferMemoryContext",
    "RecentBufferMemoryProvider",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedItem",
    "Rule",
    "RuleBasedAuthorizationProvider",
    "SandboxProvider",
    "SecretsProvider",
    "derive_rule_id",
    "make_authorization_provider",
    "make_memory_provider",
    "make_sandbox_provider",
    "make_secrets_provider",
    "register_authorization_variant",
    "register_memory_variant",
    "register_sandbox_variant",
    "register_secrets_variant",
    "validate_recent_buffer_config",
]
