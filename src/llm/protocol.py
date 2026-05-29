"""Protocols + value types — Module API Reference §11.5.2 / §11.5.3 / §11.5.5.

Two Protocols:

- :class:`ProviderConnector` — every provider implements this. Four
  required methods: ``auth_prompt_spec``, ``validate_credentials``,
  ``discover_models``, ``build_llm`` (per-connector LLM construction).
- :class:`MetadataSourceConnector` — opt-in extension for connectors
  that ALSO serve as a cross-provider metadata source. Today only
  OpenRouter implements this; the engine treats one or any number
  uniformly via the registry's
  :func:`registry.connected_metadata_sources`.

Supporting dataclasses are all ``@dataclass(frozen=True)`` per the
spec — these are passed across the auth/discovery seam and must
not mutate.

``ProviderConfig`` is also defined here so connectors don't depend on
Asset Management directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

if TYPE_CHECKING:
    import crewai


# ---------------------------------------------------------------------------
# Auth prompt — the CLI walks this; no interactive code in the connector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptField:
    """One field the CLI prompts the user for during `kolsch connect`."""

    name: str
    label: str
    kind: Literal["secret", "url", "string", "choice"]
    default: str | None = None
    choices: list[str] | None = None
    required: bool = True


@dataclass(frozen=True)
class AuthPromptSpec:
    """Declarative auth prompt — the CLI walks this to interact."""

    provider_label: str
    intro: str
    fields: list[PromptField]


@dataclass(frozen=True)
class Credentials:
    """Result of prompting. Opaque to the platform; passed back to the
    connector for validation and (where applicable) LLM construction.
    """

    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of `validate_credentials`. ``ok=True`` means credentials
    work; ``ok=False`` populates ``reason``. ``metadata`` carries
    optional connector-specific info (e.g., ``{"credits_remaining":
    1247}`` for OpenRouter).
    """

    ok: bool
    reason: str | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Model metadata — discover_models + lookup_model_metadata return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing — populated by providers/sources that
    expose it (OpenRouter does; most others don't)."""

    prompt_per_million: float
    completion_per_million: float


@dataclass(frozen=True)
class ModelEntry:
    """One model from a provider's catalog. ``id`` always populated;
    parameter fields ``None`` when unknown."""

    id: str
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None

    def has_missing_fields(self) -> bool:
        """True when any parameter field is unset — used by the
        augmentation loop in the CLI's connect/refresh flow to decide
        whether a metadata-source lookup is worth running."""
        return (
            self.context_length is None
            or self.supports_tools is None
            or self.supports_streaming is None
            or self.pricing is None
        )


@dataclass(frozen=True)
class ModelMetadata:
    """Same shape as :class:`ModelEntry`'s parameter fields, no ``id``.
    What :func:`MetadataSourceConnector.lookup_model_metadata` returns.
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
    """One entry from platform.yaml's `llm_providers` section, plus
    the dictionary key (`name`).

    Connectors receive this in `build_llm` and may read connector-
    specific fields (e.g. OpenRouter's `serves_as_metadata_source`)
    out of `extras`.
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
    """The shape every provider implements. Lives in
    `src/llm/providers/<name>.py`.

    Five questions (one per method); see `src/llm/providers/README.md`.
    """

    type_name: ClassVar[str]
    """The string used in platform.yaml's `type` field and in the
    registry. Distinct from the dictionary key (user-facing provider
    name); usually they match."""

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Describe what to ask the user during `kolsch connect`. The
        CLI walks this spec; no interactive code lives in the
        connector itself."""
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
    """A connector that can also look up metadata for OTHER providers'
    models. Today only OpenRouter implements this."""

    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Try to resolve metadata for a `(provider, model_id)` pair.

        Returns the metadata if known, ``None`` if the source has no
        entry. Implementations translate the platform's
        `(provider, model_id)` tuple into whatever identifier the
        source uses (e.g., OpenRouter prefixes Anthropic models with
        ``anthropic/``)."""
        ...


# ---------------------------------------------------------------------------
# Runtime helpers used by the registry's metadata-source filter
# ---------------------------------------------------------------------------


def is_metadata_source(connector: ProviderConnector) -> bool:
    """True when `connector` implements :class:`MetadataSourceConnector`.

    Used by :func:`registry.connected_metadata_sources` to pick out
    the connectors whose catalog the connect-flow augmentation loop
    should consult.
    """
    return callable(getattr(connector, "lookup_model_metadata", None))
