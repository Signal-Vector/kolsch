"""LLM Provider Configuration errors — spec §4.13 (Phase 1.1).

Seven catalog codes from `docs/explanation/error-model.md` §4.13, plus
:class:`LlmConnectorDuplicate` for the registry's duplicate-registration
guard (catalog-listed as ``llm.connector_duplicate`` — flagged for spec
amendment alongside the other post-spec additions).

The Fatal codes here all surface from the engine's pre-flight step 6
``build_llm`` call; the Recoverable code surfaces from the CLI's
``connect`` / ``refresh`` flows when a provider's discovery endpoint is
transiently unreachable.
"""

from __future__ import annotations

from errors.base import PlatformError
from errors.level import Level


class LlmProviderNotConfigured(PlatformError):
    """Agent's ModelSpec names a provider absent from `llm_providers`."""

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
    """Provider config names an api_key_secret the Secrets provider
    cannot resolve. Chains :class:`SecretNotFound` as ``__cause__``."""

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
    """Connector's `build_llm` raised during client construction."""

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
    """Agent config references a model alias absent from `model_aliases`."""

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
    """Provider config's `type` does not match any registered connector."""

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
    """Connector's `discover_models` raised during CLI connect/refresh."""

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
    """Connector's `validate_credentials` returned ok=False during connect."""

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
    """`registry.register(...)` called twice for the same `type_name`.

    NOT in the §4.13 spec table but needed for TC-LLM-09's
    duplicate-registration guard. Added at registry-init time when a
    provider module is imported twice; otherwise the registry would
    silently last-write-wins.
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
