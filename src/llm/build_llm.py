"""The `build_llm` helper — the bridge from `ModelSpec` to `crewai.LLM`.

Consults the provider registry, resolves the API key via the Secrets
provider, and dispatches to the matched provider's `build_llm` method.
Adding a new provider does not change this function — provider-specific
concerns live entirely inside the provider class.

The engine's pre-flight calls this per exchange. See the
[agent engine contract](../explanation/agent-engine-contract.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from errors import (
    LlmClientInitFailed,
    LlmProviderNotConfigured,
    LlmSecretMissing,
    PlatformError,
    SecretNotFound,
)
from llm import registry

if TYPE_CHECKING:
    import crewai

    from llm.protocol import ProviderConfig
    from providers import SecretsProvider
    from schemas import ModelSpec


async def build_llm(
    spec: ModelSpec,
    provider_config: ProviderConfig | None,
    secrets: SecretsProvider,
    *,
    configured_providers: list[str] | None = None,
) -> crewai.LLM:
    """Construct a configured `crewai.LLM` for the given model spec.

    Expects `spec` in canonical `{provider, model}` form — alias
    expansion and string normalisation have already run in the
    resolver. `provider_config` is the entry from `platform.yaml`'s
    `llm_providers[spec.provider]`, or `None` when the provider is
    absent (which raises `LlmProviderNotConfigured`). `secrets`
    resolves the API key when the provider needs one.

    Args:
        spec: Canonical model spec — `{provider, model}` already
            normalised.
        provider_config: The platform.yaml entry for `spec.provider`,
            or `None` when the provider isn't configured.
        secrets: Secrets provider used to resolve `api_key_secret`
            when set.
        configured_providers: Sibling provider names for the
            `LlmProviderNotConfigured` error's suggestion list.

    Returns:
        A configured `crewai.LLM` the engine holds for the exchange's
        lifetime.

    Raises:
        LlmProviderNotConfigured: `provider_config` is `None`.
        LlmSecretMissing: `api_key_secret` configured but not
            resolvable; `SecretNotFound` chained as `__cause__`.
        LlmClientInitFailed: The provider's `build_llm` raised during
            construction.
        LlmConnectorUnknown: `provider_config.type` is not in the
            registry.
    """
    provider_name = spec.provider

    if provider_config is None:
        raise LlmProviderNotConfigured(
            provider=provider_name,
            configured_providers=configured_providers or [],
        )

    # registry.get raises LlmConnectorUnknown if the type is missing.
    connector = registry.get(provider_config.type)

    # Resolve the API key when the provider wants one. Null-key
    # providers (Llama-local) skip this entire branch.
    api_key: str | None = None
    if provider_config.api_key_secret:
        try:
            api_key = await secrets.resolve(provider_config.api_key_secret)
        except SecretNotFound as exc:
            raise LlmSecretMissing(
                provider=provider_name,
                secret_name=provider_config.api_key_secret,
            ) from exc

    try:
        return await connector.build_llm(
            spec.model,
            provider_config,
            api_key,
        )
    except PlatformError:
        # Already typed (e.g., provider raises a domain-specific
        # error). Bubble up unchanged.
        raise
    except Exception as exc:
        raise LlmClientInitFailed(
            provider=provider_name,
            model=spec.model,
            reason=f"{type(exc).__name__}: {exc}",
        ) from exc
