"""LLM provider configuration and the bridge to `crewai.LLM`.

Implements the LLM Providers subsystem — the seam between an agent's
`ModelSpec` and a configured `crewai.LLM`. Two reference providers ship
today (OpenCode + OpenRouter); additional providers (Anthropic, OpenAI,
Bedrock, Llama) plug in through the same Protocol.

The public surface re-exports:

- [`build_llm`][llm.build_llm.build_llm] — async helper that turns a
  `ModelSpec` plus provider config plus a secrets provider into a
  configured `crewai.LLM`. Called by the engine's pre-flight per
  exchange.
- [`ProviderConnector`][llm.protocol.ProviderConnector] and
  [`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector] —
  the Protocols every provider implements.
- The supporting dataclasses
  ([`AuthPromptSpec`][llm.protocol.AuthPromptSpec],
  [`PromptField`][llm.protocol.PromptField],
  [`Credentials`][llm.protocol.Credentials],
  [`ValidationResult`][llm.protocol.ValidationResult],
  [`ModelEntry`][llm.protocol.ModelEntry],
  [`ModelMetadata`][llm.protocol.ModelMetadata],
  [`ModelPricing`][llm.protocol.ModelPricing],
  [`ProviderConfig`][llm.protocol.ProviderConfig],
  [`LLMCallContext`][llm.protocol.LLMCallContext],
  [`RequestPolicyConfig`][llm.protocol.RequestPolicyConfig]).
- [`provider_config_from_yaml`][llm.protocol.provider_config_from_yaml] —
  the one factory that builds and validates a `ProviderConfig` from a
  `platform.yaml` `llm_providers` entry.
- `llm.registry` — explicit registration and lookup. Providers
  register themselves in `llm.providers` at import time.

For the full subsystem contract, see the
[LLM providers explanation](../explanation/llm-providers.md).
"""

from llm.build_llm import build_llm
from llm.protocol import (
    AuthPromptSpec,
    Credentials,
    LLMCallContext,
    MetadataSourceConnector,
    ModelEntry,
    ModelMetadata,
    ModelPricing,
    PromptField,
    ProviderConfig,
    ProviderConnector,
    RequestPolicyConfig,
    ValidationResult,
    provider_config_from_yaml,
)
from llm.registry import (
    connected_metadata_sources,
    get,
    known_types,
    register,
)

__all__ = [
    "AuthPromptSpec",
    "Credentials",
    "LLMCallContext",
    "MetadataSourceConnector",
    "ModelEntry",
    "ModelMetadata",
    "ModelPricing",
    "PromptField",
    "ProviderConfig",
    "ProviderConnector",
    "RequestPolicyConfig",
    "ValidationResult",
    "build_llm",
    "connected_metadata_sources",
    "get",
    "known_types",
    "provider_config_from_yaml",
    "register",
]
