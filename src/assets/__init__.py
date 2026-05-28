"""Assets — filesystem discovery, asset catalog, platform bootstrap.

Per Module API Reference §2 + docs/explanation/asset-management-contract.md.
Builds the catalog of bundles, tools, skills, agents, and prompts;
assembles the partials list for the kernel resolver; pre-resolves
prompt refs to actual content (Am-I4) while passing tool/skill refs
through unchanged (Am-I5). The single point of platform bootstrap
(Am-I11) — no other module reads ``platform.yaml`` directly.

Public surface:

- :class:`AssetManagement` — the public facade the engine + CLI use.
- :class:`AssetEntry`, :class:`AssetCatalog`, :class:`BundleManifest`
  — the typed catalog records.
"""

from assets.asset_manager import AssetManagement
from assets.catalog import AssetCatalog, AssetEntry
from assets.manifest import BundleManifest

__all__ = [
    "AssetCatalog",
    "AssetEntry",
    "AssetManagement",
    "BundleManifest",
]
