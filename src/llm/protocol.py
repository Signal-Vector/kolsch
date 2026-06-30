"""Protocols and value types for the LLM provider seam.

Two Protocols:

- [`ProviderConnector`][llm.protocol.ProviderConnector] — every
  provider implements this. Four required methods:
  `auth_prompt_spec`, `validate_credentials`, `discover_models`,
  `build_llm` (per-provider LLM construction).
- [`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector] —
  opt-in extension for providers that ALSO serve as a cross-provider
  metadata source. Today only OpenRouter implements this; the engine
  treats one or any number uniformly via
  [`connected_metadata_sources`][llm.registry.connected_metadata_sources].

The supporting dataclasses are all `@dataclass(frozen=True)` — they
cross the auth/discovery seam and must not mutate.
[`ProviderConfig`][llm.protocol.ProviderConfig] also lives here so
providers don't depend on Asset Management directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

if TYPE_CHECKING:
    import crewai


# ---------------------------------------------------------------------------
# Auth prompt — the CLI walks this; no interactive code in the provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptField:
    """One field the CLI prompts the user for during `kolsch connect`.

    The CLI walks an
    [`AuthPromptSpec`][llm.protocol.AuthPromptSpec]'s field list; each
    `PromptField` describes one input (label, kind, optional default
    and choice list). The provider never reads stdin itself.
    """

    name: str
    label: str
    kind: Literal["secret", "url", "string", "choice"]
    default: str | None = None
    choices: list[str] | None = None
    required: bool = True


@dataclass(frozen=True)
class AuthPromptSpec:
    """Declarative auth prompt the CLI walks during `kolsch connect`.

    The provider returns one of these from
    [`ProviderConnector.auth_prompt_spec`][llm.protocol.ProviderConnector.auth_prompt_spec];
    the CLI renders the prompts and collects responses into a
    [`Credentials`][llm.protocol.Credentials] payload it hands back.
    """

    provider_label: str
    intro: str
    fields: list[PromptField]


@dataclass(frozen=True)
class Credentials:
    """The result of prompting the user.

    Opaque to the platform; passed back to the provider for validation
    and (where applicable) LLM construction.
    """

    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of a credential validation check.

    Returned by
    [`ProviderConnector.validate_credentials`][llm.protocol.ProviderConnector.validate_credentials].
    `ok=True` means the credentials work; `ok=False` populates
    `reason`. `metadata` carries optional provider-specific info — for
    example, `{"credits_remaining": 1247}` from OpenRouter.
    """

    ok: bool
    reason: str | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Model metadata — discover_models + lookup_model_metadata return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing for one model.

    Populated by providers and metadata sources that expose pricing
    (OpenRouter does; most others don't).
    """

    prompt_per_million: float
    completion_per_million: float


@dataclass(frozen=True)
class ModelEntry:
    """One model from a provider's catalog.

    `id` is always populated; the parameter fields are `None` when the
    provider didn't expose them. The CLI's augmentation loop fills the
    gaps from a connected metadata source.
    """

    id: str
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None

    def has_missing_fields(self) -> bool:
        """Return `True` when any parameter field is unset.

        Used by the augmentation loop in the CLI's connect / refresh
        flow to decide whether a metadata-source lookup is worth
        running.
        """
        return (
            self.context_length is None
            or self.supports_tools is None
            or self.supports_streaming is None
            or self.pricing is None
        )


@dataclass(frozen=True)
class ModelMetadata:
    """Parameter metadata for one model, without an `id`.

    Mirrors the parameter fields of
    [`ModelEntry`][llm.protocol.ModelEntry]. Returned by
    [`MetadataSourceConnector.lookup_model_metadata`][llm.protocol.MetadataSourceConnector.lookup_model_metadata].
    """

    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None


# ---------------------------------------------------------------------------
# Provider config — the platform.yaml entry the engine + CLI pass around
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """One entry from `platform.yaml`'s `llm_providers` section.

    Carries the dictionary key as `name`, the canonical fields the
    seam uses (`type`, `api_key_secret`, `base_url`, `timeout_seconds`),
    and provider-specific keys under `extras` — for example,
    OpenRouter's `serves_as_metadata_source`. Providers receive this
    in `build_llm` and may read from `extras` directly.
    """

    name: str
    type: str
    api_key_secret: str | None
    base_url: str | None
    timeout_seconds: int | None
    extras: dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The Protocols
# ---------------------------------------------------------------------------


class ProviderConnector(Protocol):
    """The shape every LLM provider implements.

    One implementation per provider, living in
    `src/llm/providers/<name>.py`. Four required methods —
    `auth_prompt_spec`, `validate_credentials`, `discover_models`,
    `build_llm` — answer the five questions documented in
    `src/llm/providers/README.md`.

    Attributes:
        type_name: The string used in `platform.yaml`'s `type` field
            and in the registry. Distinct from the dictionary key (the
            user-facing provider name); usually they match.
    """

    type_name: ClassVar[str]

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Describe what to ask the user during `kolsch connect`.

        The CLI walks the returned spec; no interactive code lives in
        the provider itself.
        """
        ...

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify the credentials work via a tiny live API call."""
        ...

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Return the models available from this provider."""
        ...

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
    ) -> crewai.LLM:
        """Construct a configured `crewai.LLM` for the named model."""
        ...


class MetadataSourceConnector(ProviderConnector, Protocol):
    """A provider that can also look up metadata for OTHER providers' models.

    Today only OpenRouter implements this. The CLI's augmentation loop
    consults every connected provider matching this Protocol to fill in
    parameter fields the upstream catalog left unset.
    """

    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Resolve metadata for a `(provider, model_id)` pair.

        Returns the metadata when known, `None` when the source has no
        entry. Implementations translate the platform's
        `(provider, model_id)` tuple into whatever identifier the
        source uses — for example, OpenRouter prefixes Anthropic
        models with `anthropic/`.
        """
        ...


# ---------------------------------------------------------------------------
# Runtime helpers used by the registry's metadata-source filter
# ---------------------------------------------------------------------------


def is_metadata_source(connector: ProviderConnector) -> bool:
    """Return `True` when `connector` implements `MetadataSourceConnector`.

    Used by
    [`connected_metadata_sources`][llm.registry.connected_metadata_sources]
    to pick out the providers whose catalog the connect-flow
    augmentation loop should consult.
    """
    return callable(getattr(connector, "lookup_model_metadata", None))
