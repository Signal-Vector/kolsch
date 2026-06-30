"""LLM provider configuration errors.

The Fatal classes here all surface from the engine's pre-flight
`build_llm` call; the Recoverable `LlmDiscoveryFailed` surfaces from
the CLI's `connect` and `refresh` flows when a provider's discovery
endpoint is transiently unreachable. See the
[error model](../../docs/explanation/error-model.md) for the
classification contract.
"""

from __future__ import annotations

from errors.base import PlatformError
from errors.level import Level


class LlmProviderNotConfigured(PlatformError):
    """Raised when the ModelSpec names a provider not in `llm_providers`."""

    code = "llm.provider_not_configured"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        configured_providers: list[str],
        *,
        where: str = "llm.build_llm",
    ) -> None:
        super().__init__(
            message=(
                f"LLM provider {provider!r} is not configured in "
                f"platform.yaml's llm_providers. Run "
                f"`kolsch connect {provider}` first."
            ),
            where=where,
            detail={
                "provider": provider,
                "configured_providers": list(configured_providers),
            },
        )


class LlmSecretMissing(PlatformError):
    """Raised when an api_key_secret named in provider config cannot be resolved.

    Chains [`SecretNotFound`][errors.secrets.SecretNotFound] as
    `__cause__` so the secrets-side failure surfaces verbatim through
    the envelope's nested `cause`.
    """

    code = "llm.secret_missing"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        secret_name: str,
        *,
        where: str = "llm.build_llm",
    ) -> None:
        super().__init__(
            message=(
                f"Provider {provider!r} requires secret {secret_name!r} "
                f"but it is not available."
            ),
            where=where,
            detail={"provider": provider, "secret_name": secret_name},
        )


class LlmClientInitFailed(PlatformError):
    """Raised when a connector's `build_llm` raised during client construction."""

    code = "llm.client_init_failed"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        model: str,
        reason: str,
        *,
        where: str = "llm.build_llm",
    ) -> None:
        super().__init__(
            message=(
                f"Failed to construct LLM client for {provider}/{model}: {reason}"
            ),
            where=where,
            detail={"provider": provider, "model": model, "reason": reason},
        )


class ConfigUnknownAlias(PlatformError):
    """Raised when agent config references a model alias absent from `model_aliases`."""

    code = "config.unknown_alias"
    level = Level.FATAL

    def __init__(
        self,
        alias: str,
        available_aliases: list[str],
        *,
        where: str = "assets.assembler.resolve_model_alias",
    ) -> None:
        super().__init__(
            message=(
                f"Unknown model alias {alias!r}. Available aliases: "
                f"{sorted(available_aliases)}"
            ),
            where=where,
            detail={
                "alias": alias,
                "available_aliases": sorted(available_aliases),
            },
        )


class LlmConnectorUnknown(PlatformError):
    """Raised when a provider config's `type` matches no registered connector."""

    code = "llm.connector_unknown"
    level = Level.FATAL

    def __init__(
        self,
        type_name: str,
        registered_types: list[str],
        *,
        where: str = "llm.registry.get",
    ) -> None:
        super().__init__(
            message=(
                f"Unknown connector type {type_name!r}. Registered types: "
                f"{sorted(registered_types)}"
            ),
            where=where,
            detail={
                "type": type_name,
                "registered_types": sorted(registered_types),
            },
        )


class LlmDiscoveryFailed(PlatformError):
    """Raised when a connector's `discover_models` raised during CLI connect/refresh."""

    code = "llm.discovery_failed"
    level = Level.RECOVERABLE

    def __init__(
        self,
        provider: str,
        reason: str,
        *,
        where: str = "llm.connector.discover_models",
    ) -> None:
        super().__init__(
            message=(f"Failed to discover models for provider {provider!r}: {reason}"),
            where=where,
            detail={"provider": provider, "reason": reason},
        )


class LlmValidationFailed(PlatformError):
    """Raised when `validate_credentials` returned ok=False during connect."""

    code = "llm.validation_failed"
    level = Level.FATAL

    def __init__(
        self,
        provider: str,
        reason: str,
        *,
        where: str = "llm.connector.validate_credentials",
    ) -> None:
        super().__init__(
            message=(
                f"Credential validation failed for provider {provider!r}: {reason}"
            ),
            where=where,
            detail={"provider": provider, "reason": reason},
        )


class LlmConnectorDuplicate(PlatformError):
    """Raised when `registry.register(...)` is called twice for the same `type_name`.

    Surfaces at registry-init time when a provider module is imported
    twice; without the guard the registry would silently
    last-write-wins.
    """

    code = "llm.connector_duplicate"
    level = Level.FATAL

    def __init__(
        self,
        type_name: str,
        *,
        where: str = "llm.registry.register",
    ) -> None:
        super().__init__(
            message=(
                f"Connector type {type_name!r} is already registered. "
                f"Each connector module must register exactly once."
            ),
            where=where,
            detail={"type": type_name},
        )
