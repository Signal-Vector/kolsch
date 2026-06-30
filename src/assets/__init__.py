"""Assets — filesystem discovery, asset catalog, platform bootstrap.

Implements the Asset Management subsystem per
[asset management contract](../../docs/explanation/asset-management-contract.md).
Builds the catalog of bundles, tools, skills, agents, and prompts;
assembles the partials list for the kernel resolver; pre-resolves
prompt refs to actual content while passing tool and skill refs
through unchanged. The single point of platform bootstrap — no other
module reads ``platform.yaml`` directly.
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
