"""The [`AssetManagement`][assets.AssetManagement] facade.

Composes the catalog, ref resolver, prompt pre-resolver, and the
`platform.yaml` loader behind one object. The engine and CLI consume
this class; internal modules consume the building blocks directly.
For the full subsystem contract see the
[asset management contract](../explanation/asset-management-contract.md).
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
from assets.platform_config import load_or_create, parse_hooks_section
from errors import AssetsNotFound, ConfigInputsInAgentTemplate
from kernel import resolve_and_validate
from schemas import HookRule, ResolvedAgentConfig, derive_agent_id

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog, AssetEntry
    from assets.manifest import BundleManifest
    from kernel import ConfigValidator


_log = logging.getLogger("assets")


def _check_no_inputs_in_agent_prompt_layers(
    agent_partials_chain: list[dict[str, Any]],
    *,
    agent_ref: str,
) -> None:
    """Raise ``ConfigInputsInAgentTemplate`` if any layer's prompt references inputs.

    Walks every layer in the pre-merge chain (leaf + extends parents)
    and reports the first layer that carries an ``inputs.*``
    reference. Fires before the run starts so authors see the error
    at agent-load rather than at first invocation.
    """
    # Local import — templating.inheritance imports Jinja + AST types,
    # which are only needed on this check path.
    from templating.inheritance import prompt_references_inputs  # noqa: PLC0415

    for layer in agent_partials_chain:
        prompt = layer.get("prompt")
        if not isinstance(prompt, str):
            continue
        refs = prompt_references_inputs(prompt)
        if refs:
            raise ConfigInputsInAgentTemplate(
                agent_path=agent_ref,
                references=refs,
            )


class AssetManagement:
    """Platform-bootstrap module, asset catalog, and ref resolver.

    Construct with the platform root (the CWD at startup) and call
    [`initialize`][assets.asset_manager.AssetManagement.initialize] once before any
    other method to load `platform.yaml` and build the catalog. The
    catalog does not mutate during the platform's run, and no other
    module reads `platform.yaml` — every consumer goes through
    [`get_platform_config`][assets.asset_manager.AssetManagement.get_platform_config].
    """

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._platform_config: dict[str, Any] | None = None
        self._catalog: AssetCatalog | None = None
        # Populated as a side effect of build_resolved_config so the
        # engine's tool-registration site can map agent_id back to its
        # home bundle. Necessary because the resolved agent_id may be
        # either explicit (taken from the YAML's `agent_id:`) or
        # derived (`ag_<bundle>_<name>`); reversing the derivation
        # isn't reliable in the explicit case.
        self._agent_id_to_bundle: dict[str, str] = {}
        # bug-158: sibling cache — resolved agent_id → canonical
        # catalog ref, so the engine can put the agent's own frame at
        # the head of the exchange's caller chain.
        self._agent_id_to_ref: dict[str, str] = {}

    # --- bootstrap -----------------------------------------------------

    async def initialize(self) -> None:
        """Load `platform.yaml` and build the asset catalog.

        Generates the default `platform.yaml` on first run when the
        file is absent. Idempotent — subsequent calls are no-ops
        because the catalog is built once and immutable thereafter.
        """
        if self._catalog is not None:
            return
        self._platform_config = await asyncio.to_thread(load_or_create, self._root)
        self._catalog = await asyncio.to_thread(catalog_module.scan, self._root)

    def get_platform_config(self, section: str) -> dict[str, Any]:
        """Return one section of `platform.yaml`.

        Unknown sections raise `KeyError` — the contract treats them
        as a programmer error rather than a `PlatformError`.

        Args:
            section: The top-level section name (e.g. `defaults`,
                `secrets`, `authorization`).

        Returns:
            The section's mapping.

        Raises:
            KeyError: No such section is present in `platform.yaml`.
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

    def hooks_config(self) -> list[HookRule]:
        """Return `platform.yaml`'s parsed `hooks:` rules (spec-131).

        Unlike every other section, `hooks` is a *list*, not a
        mapping, so it cannot go through
        [`get_platform_config`][assets.asset_manager.AssetManagement.get_platform_config]
        (typed `dict[str, Any]`). This accessor mirrors that method's
        shape for the one section that needs a different return type:
        absent `hooks:` key returns `[]` (the documented "zero hooks,
        zero overhead" default — see
        `assets.platform_config._KNOWN_SECTIONS`'s `hooks` entry), a
        present key returns the already-validated `HookRule` objects.

        Re-parses via `parse_hooks_section` rather than caching parsed
        rules on `self`, since `load_or_create` already validated the
        raw payload at `initialize()` time (`_validate_section_shape`
        calls `parse_hooks_section` eagerly) — re-parsing here is
        re-deriving typed objects from already-known-valid data, not a
        new failure mode, and keeps this class from holding two
        representations of the same config in memory.

        Callers (`bootstrap_runtime`, Step 6) MUST call this only once
        at process start and never again mid-session — hooks config is
        deliberately excluded from `/reload` (spec-131 Step 7); this
        method itself has no reload-awareness, the exclusion is the
        caller's responsibility.

        Returns:
            The parsed hook rules, in file order; `[]` when the
            `hooks:` section is absent.
        """
        cfg = self._require_initialized()[0]
        if "hooks" not in cfg:
            return []
        return parse_hooks_section(cfg["hooks"])

    # --- catalog read API ---------------------------------------------

    def catalog(self) -> AssetCatalog:
        """Return the built [`AssetCatalog`][assets.AssetCatalog].

        Read access only — the catalog is built once at
        [`initialize`][assets.asset_manager.AssetManagement.initialize] and immutable
        thereafter. The authz provider takes the catalog at
        construction so it can validate qualified tool refs in rule
        `what:` fields.
        """
        _, catalog = self._require_initialized()
        return catalog

    def list_bundles(self) -> list[BundleManifest]:
        """Return every parsed bundle manifest in catalog order."""
        _, catalog = self._require_initialized()
        return list(catalog.bundles.values())

    def get_bundle(self, name: str) -> BundleManifest | None:
        """Return the catalog's parsed manifest for `name`, or `None`.

        Read-only accessor used by the engine's tools-subsystem
        registration to look up the bundle's `permissions:` field for
        the four-level authz stack.

        Args:
            name: The bundle name (matches the directory name under
                `bundles/`).

        Returns:
            The parsed [`BundleManifest`][assets.BundleManifest], or
            `None` when no such bundle is registered.
        """
        _, catalog = self._require_initialized()
        return catalog.bundles.get(name)

    def get_bundle_for_agent_id(self, agent_id: str) -> str | None:
        """Find the home bundle of a resolved agent.

        Used by the engine to look up which bundle's permissions
        apply to an agent's tool invocations. Two-step lookup:

        1. The `_agent_id_to_bundle` cache populated as a side effect
           of
           [`build_resolved_config`][assets.asset_manager.AssetManagement.build_resolved_config]
           — covers both explicit and derived `agent_id` YAMLs.
        2. Fallback for callers that bootstrapped the engine without
           going through `build_resolved_config` (rare — tests with
           synthetic configs): walk the catalog and match
           [`schemas.derive_agent_id`][schemas.derive_agent_id]
           outputs.

        Args:
            agent_id: The resolved agent id to attribute.

        Returns:
            The home bundle name, or `None` when no catalog agent
            matches (e.g. synthetic test configs not backed by a
            bundle on disk).
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

    def get_ref_for_agent_id(self, agent_id: str) -> str | None:
        """Find the canonical catalog ref of a resolved agent.

        The engine uses it to build the exchange's caller chain: the
        agent's own ref (`<bundle>/agents/<name>`) is the proximate
        frame every `from:`-scoped rule is matched against. Same
        two-step lookup as
        [`get_bundle_for_agent_id`][assets.asset_manager.AssetManagement.get_bundle_for_agent_id]:

        1. The `_agent_id_to_ref` cache populated by
           [`build_resolved_config`][assets.asset_manager.AssetManagement.build_resolved_config]
           — covers explicit and derived `agent_id` YAMLs. When two
           YAMLs declare the same explicit `agent_id`, the most
           recently built one wins: the cache is overwritten on every
           build, so in a long-lived process an exchange can be
           attributed to the other agent's ref (a duplicate explicit
           `agent_id` is a workspace misconfiguration that no loader
           rejects today).
        2. Fallback: walk the catalog and match
           [`schemas.derive_agent_id`][schemas.derive_agent_id]
           outputs (derived ids only).

        Args:
            agent_id: The resolved agent id to attribute.

        Returns:
            The agent's 3-segment catalog ref, or `None` when no
            catalog agent matches (synthetic configs not backed by a
            bundle on disk) — the engine then omits the agent frame.
        """
        cached = self._agent_id_to_ref.get(str(agent_id))
        if cached is not None:
            return cached
        _, catalog = self._require_initialized()
        for entry in catalog.assets.values():
            if entry.asset_type != "agent":
                continue
            agent_name = entry.ref.rsplit("/", 1)[-1]
            if str(derive_agent_id(entry.bundle, agent_name)) == str(agent_id):
                return entry.ref
        return None

    def list_assets(
        self,
        asset_type: str | None = None,
    ) -> list[AssetEntry]:
        """Return catalogued assets, optionally filtered by type.

        Args:
            asset_type: Singular asset type to filter on
                (`tool` / `skill` / `agent` / `prompt` / `workflow` /
                `node`). `None` returns every asset.
        """
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

        Refs are canonical 3-segment only
        (`<bundle>/<plural>/<name>`); the 1/2-segment shorthand forms
        no longer exist. `field_asset_type` is the singular asset
        type the caller knows from the field context; when supplied
        it guards the lookup so a ref pointing at the wrong asset
        type surfaces as
        [`AssetsNotFound`][errors.AssetsNotFound] rather than a
        silent type mismatch.

        Args:
            ref: The canonical 3-segment catalog ref.
            field_asset_type: Optional singular-type guard.

        Returns:
            The matching [`AssetEntry`][assets.AssetEntry].

        Raises:
            AssetsNotFound: The ref does not resolve, or its asset
                type differs from `field_asset_type`.
        """
        _, catalog = self._require_initialized()
        entry = catalog.lookup(ref, expected_type=field_asset_type)
        if entry is None:
            raise AssetsNotFound(ref=ref, asset_type=field_asset_type)
        return entry

    async def resolve_ref(self, ref: str, asset_type: str) -> str:
        """Validate a 3-segment ref against the catalog and return it.

        Under the strict 3-segment convention this is a shape-check
        plus presence-check; the returned string is always identical
        to the input.

        Args:
            ref: The canonical 3-segment catalog ref.
            asset_type: Singular asset type the ref must resolve to.

        Returns:
            `ref` unchanged.

        Raises:
            AssetsNotFound: No catalog entry of `asset_type` matches.
        """
        _, catalog = self._require_initialized()
        entry = catalog.lookup(ref, expected_type=asset_type)
        if entry is None:
            raise AssetsNotFound(ref=ref, asset_type=asset_type)
        return ref

    # --- layer assembly ----------------------------------------------

    async def build_resolved_config(
        self,
        agent_ref: str,
        call_overrides: dict[str, Any] | None = None,
        *,
        subsystem_validators: dict[str, dict[str, ConfigValidator]] | None = None,
        system_validators: list[ConfigValidator] | None = None,
    ) -> ResolvedAgentConfig:
        """Run three-layer assembly, pre-resolution, and the kernel pipeline.

        Layer ordering is `[system_defaults, agent_definition,
        call_overrides]`; prompt refs are pre-resolved into inline
        content while tool and skill refs pass through unchanged. The
        kernel then merges, validates, and constructs the Pydantic
        [`ResolvedAgentConfig`][schemas.ResolvedAgentConfig].

        Args:
            agent_ref: Canonical 3-segment ref to the target agent
                (`<bundle>/agents/<name>`).
            call_overrides: Optional top layer applied above the
                agent definition.
            subsystem_validators: Subsystem-keyed validator maps the
                kernel runs at layer 2. Defaults to empty.
            system_validators: System-wide validators the kernel runs
                at layer 3. Defaults to empty.

        Returns:
            The fully resolved [`ResolvedAgentConfig`][schemas.ResolvedAgentConfig].
        """
        platform_cfg, catalog = self._require_initialized()

        # Step 1 — load the agent's definition partial. agent_ref
        # must be a 3-segment catalog ref
        # (``<bundle>/agents/<name>``); ``field_asset_type="agent"``
        # guards the lookup against an asset-type mismatch.
        agent_entry = await self.get_asset(agent_ref, field_asset_type="agent")
        if agent_entry.asset_type != "agent":
            raise AssetsNotFound(ref=agent_ref, asset_type="agent")
        agent_partial = await asyncio.to_thread(
            load_agent_definition, Path(agent_entry.path)
        )

        # If the agent declares `extends:`, walk the parent chain
        # BEFORE the kernel merge so the parents participate as
        # additional layers. The leaf (this agent) wins per-field
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
            # route to `render_with_parents`.
            agent_partials_chain = [
                layer if i == 0 else {k: v for k, v in layer.items() if k != "extends"}
                for i, layer in enumerate(extends_chain)
            ]

        # Step 2 — assemble system defaults from platform.yaml.
        defaults = platform_cfg.get("defaults", {})

        # Step 3 — pre-resolve prompts on each layer (tool/skill refs
        # pass through). When extends: was walked above, the
        # agent_partials_chain has the leaf at index 0 and ascends —
        # we reverse it for the kernel merge (which applies layers in
        # order, last-wins) so the leaf ends up last and its values
        # override parents.
        resolved_agent_partial = agent_partials_chain[0]
        if len(agent_partials_chain) > 1:
            composed: dict[str, Any] = {}
            for layer in reversed(agent_partials_chain):
                composed.update(layer)
            resolved_agent_partial = composed
        # Prepend a derived-agent-id partial as the lowest-priority
        # layer so an omitted `agent_id:` in YAML still produces a
        # canonical id in the resolved config. RFC 7386 merge gives
        # any explicit value from defaults / extends / agent /
        # overrides priority over the derived baseline.
        #
        # Use the resolved AssetEntry rather than re-parsing
        # agent_ref. The entry's bundle and name are authoritative —
        # refs are 3-segment canonical, but reading from the entry
        # keeps the call site decoupled from the ref grammar.
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

        # Step 5 — agent-template scope check. ``inputs`` is not in
        # the agent-template Jinja scope (the render harness binds
        # tools/docs/workspace/agent/platform/skills, never inputs).
        # Users following ``docs/reference/workflow-yaml.md`` — which
        # heavily uses ``{{ inputs.* }}`` in WORKFLOW-NODE examples —
        # naturally reach for the same convention in AGENT prompts
        # and hit ``UndefinedError: 'inputs' is undefined`` at first
        # invocation. Fire a typed ``ConfigInputsInAgentTemplate``
        # here at agent-load, before the run starts, with a message
        # pointing at the correct ``io.inputs`` + prose pattern.
        #
        # Walk every layer's prompt: a parent in the extends chain
        # can carry the offending reference just as easily as the
        # leaf, and the leaf-only check would miss it.
        _check_no_inputs_in_agent_prompt_layers(
            agent_partials_chain,
            agent_ref=agent_ref,
        )

        # Stash the resolved-agent-id → home-bundle mapping so the
        # engine's tool-registration site can look up the bundle's
        # ``permissions:`` block. The resolved agent_id may be
        # explicit (the YAML's ``agent_id:``) or derived; this
        # mapping covers both cases without re-parsing.
        self._agent_id_to_bundle[str(resolved.agent_id)] = bundle
        self._agent_id_to_ref[str(resolved.agent_id)] = agent_entry.ref
        return resolved

    # --- internal ------------------------------------------------------

    def _require_initialized(self) -> tuple[dict[str, Any], AssetCatalog]:
        if self._platform_config is None or self._catalog is None:
            msg = "AssetManagement.initialize() must be called first"
            raise RuntimeError(msg)
        return self._platform_config, self._catalog
