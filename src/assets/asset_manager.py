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
from errors import AssetsNotFound
from kernel import resolve_and_validate
from schemas import ResolvedAgentConfig, derive_agent_id

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog, AssetEntry
    from assets.manifest import BundleManifest
    from kernel import ConfigValidator


_log = logging.getLogger("assets")


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
        # qol-tool-permission-discovery-uniform-with-stacking.md Step 2
        # — populated as a side effect of build_resolved_config so the
        # engine's tool-registration site can map agent_id back to its
        # home bundle. Necessary because the resolved agent_id may be
        # either explicit (taken from the YAML's `agent_id:`) or
        # derived (`ag_<bundle>_<name>`); reversing the derivation
        # isn't reliable in the explicit case.
        self._agent_id_to_bundle: dict[str, str] = {}

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

    def catalog(self) -> AssetCatalog:
        """Return the built :class:`AssetCatalog`.

        qol-permission-system-human-readable.md Step 2 — the authz
        provider takes the catalog at construction so it can
        validate qualified tool refs in rule ``what:`` fields. Read
        access only; the catalog is built once at
        :meth:`initialize` and immutable thereafter (Am-I7)."""
        _, catalog = self._require_initialized()
        return catalog

    def list_bundles(self) -> list[BundleManifest]:
        _, catalog = self._require_initialized()
        return list(catalog.bundles.values())

    def get_bundle(self, name: str) -> BundleManifest | None:
        """Return the catalog's parsed manifest for ``name``, or
        ``None`` if no such bundle is registered.

        Read-only accessor used by the engine's tools-subsystem
        registration to look up the bundle's ``permissions:`` field
        for the four-level authz stack (qol-tool-permission-discovery-
        uniform-with-stacking.md Step 2)."""
        _, catalog = self._require_initialized()
        return catalog.bundles.get(name)

    def get_bundle_for_agent_id(self, agent_id: str) -> str | None:
        """Find the home bundle of a resolved agent.

        Used by the engine to look up which bundle's permissions
        apply to an agent's tool invocations. Two-step lookup:

        1. The ``_agent_id_to_bundle`` cache populated as a side
           effect of :meth:`build_resolved_config` — covers both
           explicit and derived agent_id YAMLs.
        2. Fallback for callers that bootstrapped the engine
           without going through ``build_resolved_config`` (rare —
           tests with synthetic configs): walk the catalog and
           match :func:`schemas.derive_agent_id` outputs.

        Returns ``None`` when no catalog agent matches (e.g., tests
        with synthetic configs not backed by a bundle on disk).
        """
        cached = self._agent_id_to_bundle.get(str(agent_id))
        if cached is not None:
            return cached
        _, catalog = self._require_initialized()
        for entry in catalog.assets.values():
            if entry.asset_type != "agent":
                continue
            bundle = entry.bundle
            agent_name = entry.ref.rsplit("/", 1)[-1]
            if str(derive_agent_id(bundle, agent_name)) == str(agent_id):
                return bundle
        return None

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
        """Look up an asset by 3-segment ref.

        Phase 3.0 Step 1B — refs are canonical 3-segment only
        (``<bundle>/<plural>/<name>``); the 1/2-segment shorthand
        forms have been removed. ``field_asset_type`` is the
        singular asset type ('tool' / 'skill' / 'agent' / 'prompt')
        the caller knows from the field context; when supplied it
        guards the lookup so a ref pointing at the wrong asset
        type surfaces as :class:`AssetsNotFound`.
        """
        _, catalog = self._require_initialized()
        entry = catalog.lookup(ref, expected_type=field_asset_type)
        if entry is None:
            raise AssetsNotFound(ref=ref, asset_type=field_asset_type)
        return entry

    async def resolve_ref(self, ref: str, asset_type: str) -> str:
        """Validate a 3-segment ref against the catalog; return it.

        Phase 3.0 Step 1B — under the strict 3-seg convention this
        is a shape-check + presence-check; the returned string is
        always identical to the input. Raises
        :class:`AssetsNotFound` when no catalog entry of
        ``asset_type`` matches.
        """
        _, catalog = self._require_initialized()
        entry = catalog.lookup(ref, expected_type=asset_type)
        if entry is None:
            raise AssetsNotFound(ref=ref, asset_type=asset_type)
        return ref

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

        # Step 1 — load the agent's definition partial. Phase 3.0
        # Step 1B — agent_ref must be a 3-segment catalog ref
        # (``<bundle>/agents/<name>``); ``field_asset_type="agent"``
        # guards the lookup against an asset-type mismatch.
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
            # `resolve_extends_chain` returns leaf-first. Strip
            # `extends:` from non-leaf layers (their parents already
            # got resolved during chain walk; preserving the keys
            # would re-merge stale refs). Preserve it on the LEAF
            # (index 0) so the engine's render-time dispatch at
            # `_render_prompt_if_needed` can see the directive and
            # route to `render_with_parents`
            # (bug-engine-extends-parent-not-rendered.md Step 2).
            agent_partials_chain = [
                layer
                if i == 0
                else {k: v for k, v in layer.items() if k != "extends"}
                for i, layer in enumerate(extends_chain)
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
        # agent_ref. The entry's bundle and name are authoritative
        # — Phase 3.0 Step 1B refs are 3-segment canonical, but
        # reading from the entry keeps the call site decoupled
        # from the ref grammar.
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
        resolved = await asyncio.to_thread(
            resolve_and_validate,
            partials,
            subsystem_validators or {},
            system_validators or [],
        )
        # Stash the resolved-agent-id → home-bundle mapping so the
        # engine's tool-registration site can look up the bundle's
        # ``permissions:`` block (qol-tool-permission-discovery-
        # uniform-with-stacking.md Step 2). The resolved agent_id may
        # be explicit (the YAML's ``agent_id:``) or derived; this
        # mapping covers both cases without re-parsing.
        self._agent_id_to_bundle[str(resolved.agent_id)] = bundle
        return resolved

    # --- internal ------------------------------------------------------

    def _require_initialized(self) -> tuple[dict[str, Any], AssetCatalog]:
        if self._platform_config is None or self._catalog is None:
            msg = "AssetManagement.initialize() must be called first"
            raise RuntimeError(msg)
        return self._platform_config, self._catalog
