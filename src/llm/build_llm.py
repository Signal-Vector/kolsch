"""The `build_llm` helper — the bridge from `ModelSpec` to `crewai.LLM`.

Consults the provider registry, resolves the API key via the Secrets
provider, and dispatches to the matched provider's `build_llm` method.
Adding a new provider does not change this function — provider-specific
concerns live entirely inside the provider class.

[`call_error_classifier`][llm.build_llm.call_error_classifier] is its
call-time sibling (bug-145): it binds the same connector's
`classify_call_error` hook so the engine's LLM call seam can turn a raw
provider exception into a typed `model.*` error.

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
    from collections.abc import Callable

    import crewai

    from llm.protocol import LLMCallContext, ProviderConfig
    from providers import SecretsProvider
    from schemas import ModelSpec


async def build_llm(
    spec: ModelSpec,
    provider_config: ProviderConfig | None,
    secrets: SecretsProvider,
    *,
    configured_providers: list[str] | None = None,
    context: LLMCallContext | None = None,
) -> crewai.LLM:
    """Construct a configured `crewai.LLM` for the given model spec.

    Expects `spec` in canonical `{provider, model}` form — alias
    expansion and string normalisation have already run in the
    resolver. `provider_config` is the entry from `platform.yaml`'s
    `llm_providers[spec.provider]`, or `None` when the provider is
    absent (which raises `LlmProviderNotConfigured`). `secrets`
    resolves the API key when the provider needs one. `context` is
    forwarded verbatim to the connector's `build_llm`.

    Args:
        spec: Canonical model spec — `{provider, model}` already
            normalised.
        provider_config: The platform.yaml entry for `spec.provider`,
            or `None` when the provider isn't configured.
        secrets: Secrets provider used to resolve `api_key_secret`
            when set.
        configured_providers: Sibling provider names for the
            `LlmProviderNotConfigured` error's suggestion list.
        context: The [`LLMCallContext`][llm.protocol.LLMCallContext]
            for the owning session. Production callers always pass
            one; `None` is for test doubles only.

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
    # Thread `provider_name` so the error names the offending
    # `llm_providers.<name>` entry and points at `platform.yaml`.
    connector = registry.get(
        provider_config.type,
        provider_name=provider_name,
    )

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
            parameters=spec.parameters or None,
            context=context,
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


def call_error_classifier(
    spec: ModelSpec,
    provider_config: ProviderConfig,
) -> Callable[[BaseException], PlatformError | None]:
    """Bind the spec's connector `classify_call_error` hook (bug-145).

    Resolves the connector exactly as
    [`build_llm`][llm.build_llm.build_llm] does
    (`registry.get(provider_config.type, provider_name=spec.provider)`)
    and returns a one-argument classifier with `provider_config` and
    `spec.model` bound. The engine's LLM call seam
    (`engine._crewai_adapter.make_llm_callable`) invokes it on any
    non-`PlatformError` the `crewai.LLM` raises.

    Call it only after `build_llm` succeeded for the same arguments:
    the `None`-config check is not repeated here.

    Args:
        spec: The canonical model spec the LLM was built for.
        provider_config: The same platform.yaml entry passed to
            `build_llm`.

    Returns:
        A pure classifier returning a typed `model.*` error, or `None`
        when the connector doesn't recognise the exception.

    Raises:
        LlmConnectorUnknown: `provider_config.type` is not in the
            registry.
    """
    connector = registry.get(provider_config.type, provider_name=spec.provider)
    model_id = spec.model

    def classify(exc: BaseException) -> PlatformError | None:
        return connector.classify_call_error(
            exc, provider_config=provider_config, model_id=model_id
        )

    return classify
