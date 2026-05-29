"""The `build_llm` helper — Module API Reference §11.5.1.

Bridge from `ModelSpec` to a usable `crewai.LLM`. Consults the
connector registry, resolves the API key via Secrets, and dispatches
to the connector's `build_llm` method.

About 30 lines of substance. Adding a provider does not change this
function — provider-specific concerns live entirely in the
connector's `build_llm` method.

Engine pre-flight step 6 calls this per exchange (see
`docs/explanation/agent-engine-contract.md` §2.1).
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
    """Construct a configured `crewai.LLM`.

    Preconditions:
    - `spec` is canonical-form `{provider, model}` (alias expansion
      and string-form normalization already happened in the resolver
      — see Schemas §A.4.1).
    - `provider_config` is the entry from platform.yaml's
      `llm_providers[spec.provider]`, OR ``None`` when the provider
      is absent (in which case we raise
      :class:`LlmProviderNotConfigured`).
    - `secrets` provides the resolved API key when the connector
      needs one.

    Postconditions:
    - Returns a configured `crewai.LLM` the engine can hold for the
      exchange's lifetime.

    Raises:
    - :class:`LlmProviderNotConfigured` (Fatal) — `provider_config`
      is ``None``.
    - :class:`LlmSecretMissing` (Fatal) — `api_key_secret` configured
      but not resolvable; `SecretNotFound` chained as ``__cause__``.
    - :class:`LlmClientInitFailed` (Fatal) — connector's `build_llm`
      raised during construction.
    - :class:`LlmConnectorUnknown` (Fatal) — `provider_config.type`
      not in the registry.
    """
    provider_name = spec.provider

    if provider_config is None:
        raise LlmProviderNotConfigured(
            provider=provider_name,
            configured_providers=configured_providers or [],
        )

    # registry.get raises LlmConnectorUnknown if the type is missing.
    connector = registry.get(provider_config.type)

    # Resolve the API key when the connector wants one. Null-key
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
        # Already typed (e.g., connector raises a domain-specific
        # error). Bubble up unchanged.
        raise
    except Exception as exc:
        raise LlmClientInitFailed(
            provider=provider_name,
            model=spec.model,
            reason=f"{type(exc).__name__}: {exc}",
        ) from exc
