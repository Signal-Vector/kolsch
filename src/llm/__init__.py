"""LLM Provider Configuration — Phase 1.1.

Per `docs/explanation/llm-providers.md` + Module API Reference §11.5.

The bridge between the agent's `ModelSpec` and a usable `crewai.LLM`.
v1.1 ships two reference connectors (OpenCode + OpenRouter); four
others (Anthropic, OpenAI, Bedrock, Llama) land as later patches via
the same Protocol.

Public surface:

- :func:`build_llm` — async helper that turns a ``ModelSpec`` +
  provider config + Secrets provider into a configured
  ``crewai.LLM``. Engine pre-flight step 6 calls this per exchange.
- :class:`ProviderConnector` / :class:`MetadataSourceConnector` — the
  Protocols every provider implements.
- The supporting dataclasses (:class:`AuthPromptSpec`,
  :class:`PromptField`, :class:`Credentials`,
  :class:`ValidationResult`, :class:`ModelEntry`,
  :class:`ModelMetadata`, :class:`ModelPricing`,
  :class:`ProviderConfig`).
- :mod:`llm.registry` — explicit registration + lookup. Connectors
  register themselves in :mod:`llm.providers` at import time.
"""

from llm.build_llm import build_llm
from llm.protocol import (
    AuthPromptSpec,
    Credentials,
    MetadataSourceConnector,
    ModelEntry,
    ModelMetadata,
    ModelPricing,
    PromptField,
    ProviderConfig,
    ProviderConnector,
    ValidationResult,
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
    "MetadataSourceConnector",
    "ModelEntry",
    "ModelMetadata",
    "ModelPricing",
    "PromptField",
    "ProviderConfig",
    "ProviderConnector",
    "ValidationResult",
    "build_llm",
    "connected_metadata_sources",
    "get",
    "known_types",
    "register",
]
