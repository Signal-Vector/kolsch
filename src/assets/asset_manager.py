"""The :class:`AssetManagement` facade — Module API Reference §2.

Composes the catalog, ref resolver, prompt pre-resolver, and the
platform.yaml loader behind one object. The engine and CLI consume
this class; internal modules consume the building blocks directly.
"""

from __future__ import annotations

import asyncio
import logging
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
from errors import AssetsNotFound, ConfigDeprecatedRefForm
from kernel import resolve_and_validate
from schemas import ResolvedAgentConfig, derive_agent_id

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog, AssetEntry
    from assets.manifest import BundleManifest
    from kernel import ConfigValidator


_log = logging.getLogger("assets")


def _log_deprecated_ref(warning: ConfigDeprecatedRefForm) -> None:
    """Surface a deprecation warning at agent-load via the assets
    logger. Centralised so every caller path emits at the same
    level / format.
    """
    _log.warning("%s", warning)


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

    async def get_asset(
        self,
        ref: str,
        *,
        field_asset_type: str | None = None,
    ) -> AssetEntry:
        """Look up an asset by ref. Accepts the three catalog-ref
        forms (1 / 2 / 3-segment) when ``field_asset_type`` is
        supplied; falls back to the legacy 3-segment-only lookup
        when ``field_asset_type`` is ``None``.

        ``field_asset_type`` is the singular asset type
        ('tool' / 'skill' / 'agent' / 'prompt') the caller knows
        from the field context (e.g. an agent ref under
        ``extends:`` → 'agent'). When the input is a legacy
        3-segment ref or an explicit ``workspace:`` prefix is
        used elsewhere, the resolver emits a
        :class:`ConfigDeprecatedRefForm` via the assets logger.
        """
        _, catalog = self._require_initialized()
        if field_asset_type is not None:
            canonical = resolve_shorthand(
                catalog,
                ref,
                field_asset_type,
                emit_warning=_log_deprecated_ref,
            )
            return catalog.assets[canonical]
        # Backward-compat: 3-seg-only shape validation + direct
        # lookup. Callers without a field context fall through here.
        parse_ref(ref)
        entry = catalog.assets.get(ref)
        if entry is None:
            raise AssetsNotFound(ref=ref)
        return entry

    async def resolve_ref(self, ref: str, asset_type: str) -> str:
        _, catalog = self._require_initialized()
        return resolve_shorthand(
            catalog,
            ref,
            asset_type,
            emit_warning=_log_deprecated_ref,
        )

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

        # Step 1 — load the agent's definition partial. The agent
        # ref may arrive in any of the three input forms (1 / 2 /
        # 3-segment); pass field_asset_type="agent" so
        # get_asset routes through resolve_shorthand and emits a
        # deprecation warning for legacy 3-segment input.
        agent_entry = await self.get_asset(agent_ref, field_asset_type="agent")
        if agent_entry.asset_type != "agent":
            raise AssetsNotFound(ref=agent_ref, asset_type="agent")
        agent_partial = await asyncio.to_thread(
            load_agent_definition, Path(agent_entry.path)
        )

        # Phase 2.2 Step 14 — if the agent declares `extends:`, walk the
        # parent chain BEFORE the kernel merge so the parents participate
        # as additional layers. The leaf (this agent) wins per-field
        # wholesale; the kernel's RFC 7386 merge applied to the
        # parents-then-child sequence gives the same result as
        # apply_inheritance + downstream defaults composition.
        agent_partials_chain: list[dict[str, Any]] = [agent_partial]
        if isinstance(agent_partial, dict) and agent_partial.get("extends"):
            from templating.inheritance import (  # noqa: PLC0415
                resolve_extends_chain,
            )

            extends_chain = await asyncio.to_thread(
                resolve_extends_chain,
                Path(agent_entry.path),
                self._root,
                catalog,
            )
            # `resolve_extends_chain` returns leaf-first; strip the
            # extends key from each layer so it doesn't survive into
            # the merged config.
            agent_partials_chain = [
                {k: v for k, v in layer.items() if k != "extends"}
                for layer in extends_chain
            ]

        # Step 2 — assemble system defaults from platform.yaml.
        defaults = platform_cfg.get("defaults", {})

        # Step 3 — pre-resolve prompts on each layer (Am-I4 + Am-I5).
        # When extends: was walked above, the agent_partials_chain has
        # the leaf at index 0 and ascends — we reverse it for the kernel
        # merge (which applies layers in order, last-wins) so the leaf
        # ends up last and its values override parents.
        resolved_agent_partial = agent_partials_chain[0]
        if len(agent_partials_chain) > 1:
            composed: dict[str, Any] = {}
            for layer in reversed(agent_partials_chain):
                composed.update(layer)
            resolved_agent_partial = composed
        # Prepend a derived-agent-id partial as the lowest-priority
        # layer so an omitted `agent_id:` in YAML still produces a
        # canonical id in the resolved config (bug-agent-id-derive-
        # from-ref.md). RFC 7386 merge gives any explicit value from
        # defaults / extends / agent / overrides priority over the
        # derived baseline.
        #
        # Use the resolved AssetEntry rather than re-parsing
        # agent_ref — the input may be 1 / 2 / 3-segment, but
        # entry.ref is always the canonical 3-segment internal
        # key (qol-prefix-defaults-and-unified-refs.md).
        bundle = agent_entry.bundle
        agent_name = agent_entry.ref.rsplit("/", 1)[-1]
        partials: list[dict[str, Any]] = [
            {"agent_id": derive_agent_id(bundle, agent_name)},
            *assemble_partials(
                defaults=pre_resolve_prompts(defaults, catalog),
                agent_config=pre_resolve_prompts(resolved_agent_partial, catalog),
                call_overrides=(
                    pre_resolve_prompts(call_overrides, catalog)
                    if call_overrides
                    else None
                ),
            ),
        ]

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
