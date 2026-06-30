"""Provider registry — one process-global map of type name to class.

Providers register themselves in `llm.providers.__init__` at import
time; adding a new provider is one line there plus one new file under
`llm.providers`.

The registry is the only place the rest of the platform looks up a
provider by name — the engine's
[`build_llm`][llm.build_llm.build_llm], the CLI's `connect` /
`refresh` / `disconnect` / `providers` commands, and the augmentation
loop's
[`connected_metadata_sources`][llm.registry.connected_metadata_sources]
all route through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from errors import LlmConnectorDuplicate, LlmConnectorUnknown
from llm.protocol import is_metadata_source

if TYPE_CHECKING:
    from collections.abc import Iterable

    from llm.protocol import (
        MetadataSourceConnector,
        ProviderConfig,
        ProviderConnector,
    )


_REGISTRY: dict[str, type[ProviderConnector]] = {}


def register(
    type_name: str,
    connector_cls: type[ProviderConnector],
) -> None:
    """Register a provider class under its `type_name`.

    Each provider module must register exactly once; importing the
    same module twice would otherwise silently last-write-wins.

    Args:
        type_name: The string used in `platform.yaml`'s `type` field.
        connector_cls: The provider class.

    Raises:
        LlmConnectorDuplicate: `type_name` is already registered.
    """
    if type_name in _REGISTRY:
        raise LlmConnectorDuplicate(type_name=type_name)
    _REGISTRY[type_name] = connector_cls


def get(type_name: str) -> ProviderConnector:
    """Return a provider instance for `type_name`.

    Raised when `platform.yaml`'s `llm_providers.<X>.type` doesn't
    match any registered provider — this is what surfaces a typo'd
    provider type to the user.

    Args:
        type_name: The registered provider type string.

    Returns:
        A freshly-constructed provider instance.

    Raises:
        LlmConnectorUnknown: No provider is registered for the type.
    """
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise LlmConnectorUnknown(
            type_name=type_name,
            registered_types=list(_REGISTRY),
        )
    return cls()


def known_types() -> list[str]:
    """Return all registered provider types, sorted.

    Used by the CLI's `providers` listing and by the unknown-provider
    error message in `connect` and `disconnect`.
    """
    return sorted(_REGISTRY)


def connected_metadata_sources(
    llm_providers_config: dict[str, ProviderConfig] | None = None,
) -> list[MetadataSourceConnector]:
    """Instantiate every connected provider that is also a metadata source.

    Today only OpenRouter implements
    [`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector];
    this function returns zero or one instances. The list shape keeps
    the CLI's augmentation loop symmetric for the day a second
    metadata source ever exists.

    Args:
        llm_providers_config: The parsed `llm_providers` map from
            `platform.yaml`. `None` (the default for module-level use)
            returns an empty list — the augmentation loop needs the
            live config to know which providers are actually
            connected.

    Returns:
        Provider instances implementing `MetadataSourceConnector`.
    """
    if not llm_providers_config:
        return []
    out: list[MetadataSourceConnector] = []
    for cfg in llm_providers_config.values():
        try:
            connector = get(cfg.type)
        except LlmConnectorUnknown:
            continue
        if is_metadata_source(connector):
            out.append(connector)  # type: ignore[arg-type]
    return out


def _registered() -> Iterable[tuple[str, type[ProviderConnector]]]:
    """Read-only iteration helper for tests. Not part of the public surface."""
    return list(_REGISTRY.items())


def _clear_for_tests() -> None:
    """Drop all registrations. Test-only — not part of the public surface.

    Used by tests that exercise the duplicate-registration guard or
    build a custom registry.
    """
    _REGISTRY.clear()
