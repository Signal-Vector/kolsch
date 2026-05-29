"""Connector registry — Module API Reference §11.5.4.

One process-global map of connector type names → connector classes.
Connectors register themselves in :mod:`llm.providers.__init__` at
import time; adding a new provider is one line there + one new file
under :mod:`llm.providers`.

The registry is the ONLY place the rest of the platform looks up a
connector by name — the engine's :func:`llm.build_llm`, the CLI's
``connect`` / ``refresh`` / ``disconnect`` / ``providers``
commands, and the augmentation loop's
:func:`connected_metadata_sources` all route through here.
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
    """Register a connector class under its `type_name`.

    Raises :class:`LlmConnectorDuplicate` (Fatal) if `type_name` is
    already registered — TC-LLM-09 pins this. Each connector module
    must register exactly once; importing the same module twice would
    otherwise silently last-write-wins.
    """
    if type_name in _REGISTRY:
        raise LlmConnectorDuplicate(type_name=type_name)
    _REGISTRY[type_name] = connector_cls


def get(type_name: str) -> ProviderConnector:
    """Return a connector INSTANCE for `type_name`.

    Raises :class:`LlmConnectorUnknown` (Fatal) if no connector is
    registered for the type — this is what surfaces when
    platform.yaml's `llm_providers.<X>.type` doesn't match any
    connector.
    """
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise LlmConnectorUnknown(
            type_name=type_name,
            registered_types=list(_REGISTRY),
        )
    return cls()


def known_types() -> list[str]:
    """All registered connector types, sorted. Used by the CLI's
    ``providers`` listing and by the unknown-provider error message
    in ``connect`` / ``disconnect``."""
    return sorted(_REGISTRY)


def connected_metadata_sources(
    llm_providers_config: dict[str, ProviderConfig] | None = None,
) -> list[MetadataSourceConnector]:
    """Instantiate every connected provider whose connector implements
    :class:`MetadataSourceConnector`.

    `llm_providers_config` is the parsed `llm_providers` map from
    platform.yaml. When ``None`` (the default for module-level use),
    returns an empty list — the augmentation loop needs the live
    config to know which connectors are actually connected.

    Today only OpenRouter implements MetadataSourceConnector; this
    function returns 0 or 1 instances. Returning a list keeps the
    augmentation loop in the CLI symmetric for the day a second
    metadata source ever exists.
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
    """Read-only iteration helper for tests. Not part of the public
    surface."""
    return list(_REGISTRY.items())


def _clear_for_tests() -> None:
    """Drop all registrations. Tests that exercise the
    duplicate-registration guard or build a custom registry use
    this. NOT part of the public surface."""
    _REGISTRY.clear()
