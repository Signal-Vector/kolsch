"""The :class:`AssetManagement` facade — Module API Reference §2.

Composes the catalog, ref resolver, prompt pre-resolver, and the
platform.yaml loader behind one object. The engine and CLI consume
this class; internal modules consume the building blocks directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assets import catalog as catalog_module
from assets.assembler import (
    assemble_partials,
    load_agent_definition,
    pre_resolve_prompts,
)
from assets.platform_config import load_or_create
from assets.refs import parse_ref, resolve_shorthand
from errors import AssetsNotFound
from kernel import resolve_and_validate
from schemas import ResolvedAgentConfig

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog, AssetEntry
    from assets.manifest import BundleManifest
    from kernel import ConfigValidator


class AssetManagement:
    """Platform-bootstrap module + asset catalog + ref resolver.

    Construct with the platform root (the CWD at startup per Am-I9);
    call :meth:`initialize` once before any other method to load
    ``platform.yaml`` and build the catalog. Per Am-I7 the catalog
    does not mutate during the platform's run; per Am-I11 no other
    module reads ``platform.yaml`` — they go through
    :meth:`get_platform_config`.
    """

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._platform_config: dict[str, Any] | None = None
        self._catalog: AssetCatalog | None = None

    # --- bootstrap -----------------------------------------------------

    async def initialize(self) -> None:
        """Load platform.yaml (generating defaults on first run) and
        build the asset catalog. Idempotent."""
        if self._catalog is not None:
            return
        self._platform_config = await asyncio.to_thread(load_or_create, self._root)
        self._catalog = await asyncio.to_thread(catalog_module.scan, self._root)

    def get_platform_config(self, section: str) -> dict[str, Any]:
        """Return one section of ``platform.yaml`` (Module API Ref §2.2).

        Raises ``KeyError`` for unknown sections — Module API Reference
        §2.2 specifies a programmer error rather than a PlatformError.
        """
        cfg = self._require_initialized()[0]
        if section not in cfg:
            msg = (
                f"platform.yaml section {section!r} not present; "
                f"known: {sorted(cfg.keys())}"
            )
            raise KeyError(msg)
        value: dict[str, Any] = cfg[section]
        return value

    # --- catalog read API ---------------------------------------------

    def list_bundles(self) -> list[BundleManifest]:
        _, catalog = self._require_initialized()
        return list(catalog.bundles.values())

    def list_assets(
        self,
        asset_type: str | None = None,
    ) -> list[AssetEntry]:
        _, catalog = self._require_initialized()
        if asset_type is None:
            return list(catalog.assets.values())
        return [a for a in catalog.assets.values() if a.asset_type == asset_type]

    async def get_asset(self, ref: str) -> AssetEntry:
        _, catalog = self._require_initialized()
        # parse_ref enforces the three-segment shape and raises
        # AssetsInvalidRef for malformed input.
        parse_ref(ref)
        entry = catalog.assets.get(ref)
        if entry is None:
            raise AssetsNotFound(ref=ref)
        return entry

    async def resolve_ref(self, ref: str, asset_type: str) -> str:
        _, catalog = self._require_initialized()
        return resolve_shorthand(catalog, ref, asset_type)

    # --- layer assembly (spec §6) -------------------------------------

    async def build_resolved_config(
        self,
        agent_ref: str,
        call_overrides: dict[str, Any] | None = None,
        *,
        subsystem_validators: dict[str, dict[str, ConfigValidator]] | None = None,
        system_validators: list[ConfigValidator] | None = None,
    ) -> ResolvedAgentConfig:
        """Three-layer assembly + pre-resolution + kernel pipeline.

        Spec §6.1 ordering: ``[system_defaults, agent_definition,
        call_overrides]``. Spec §6.2: prompt refs pre-resolved (Am-I4),
        tool/skill refs pass through (Am-I5). The kernel pipeline
        (Step 3) does the merge + validation + Pydantic construction.

        ``subsystem_validators`` and ``system_validators`` default to
        empty; callers (the engine) supply concrete validators when
        the relevant subsystems land.
        """
        platform_cfg, catalog = self._require_initialized()

        # Step 1 — load the agent's definition partial.
        agent_entry = await self.get_asset(agent_ref)
        if agent_entry.asset_type != "agent":
            raise AssetsNotFound(ref=agent_ref, asset_type="agent")
        agent_partial = await asyncio.to_thread(
            load_agent_definition, Path(agent_entry.path)
        )

        # Step 2 — assemble system defaults from platform.yaml.
        defaults = platform_cfg.get("defaults", {})

        # Step 3 — pre-resolve prompts on each layer (Am-I4 + Am-I5).
        partials = assemble_partials(
            defaults=pre_resolve_prompts(defaults, catalog),
            agent_config=pre_resolve_prompts(agent_partial, catalog),
            call_overrides=(
                pre_resolve_prompts(call_overrides, catalog) if call_overrides else None
            ),
        )

        # Step 4 — hand to the kernel for merge + validation.
        return await asyncio.to_thread(
            resolve_and_validate,
            partials,
            subsystem_validators or {},
            system_validators or [],
        )

    # --- internal ------------------------------------------------------

    def _require_initialized(self) -> tuple[dict[str, Any], AssetCatalog]:
        if self._platform_config is None or self._catalog is None:
            msg = "AssetManagement.initialize() must be called first"
            raise RuntimeError(msg)
        return self._platform_config, self._catalog
