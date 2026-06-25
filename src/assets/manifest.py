"""Bundle manifest model — spec §2.2.

Loading + structural validation of ``bundle.yaml``. Higher-level
catalog rules (name/directory mismatch, duplicate names, reserved
``core`` name) live in :mod:`assets.catalog`; this module focuses on
the per-bundle shape.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from _yamlio import YAMLError, load
from schemas import PermissionRule

if TYPE_CHECKING:
    from pathlib import Path

# Spec §2.2: lowercase alphanumeric, hyphens, underscores.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class BundleManifest(BaseModel):
    """The parsed ``bundle.yaml`` contents — spec §2.2 + Module API §2.2.

    ``path`` is the absolute path to the bundle directory; filled in by
    the catalog scanner after a successful load.

    ``permissions`` (qol-tool-permission-discovery-uniform-with-stacking.md
    Step 2) is the bundle-wide authz rule list — same shape as
    ``platform.yaml:authorization.config.rules`` and the agent-level
    ``permissions:`` field on :class:`schemas.ResolvedAgentConfig`. The
    authz provider walks rules in priority order — agent first, then
    bundle, then platform, then no-rule callback, then default deny.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str
    description: str
    required_env_vars: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[PermissionRule] = Field(default_factory=list)
    path: str


def load_manifest(bundle_dir: Path) -> BundleManifest | None:
    """Return the parsed manifest, or ``None`` if the file is missing.

    Malformed manifests (YAML errors, schema errors) also return
    ``None`` — the caller decides whether to skip with a warning or
    treat as fatal. Spec §4.1 step 2 says "skip with warning."
    """
    path = bundle_dir / "bundle.yaml"
    if not path.exists():
        return None
    try:
        payload = load(path.read_text(encoding="utf-8"))
    except YAMLError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["path"] = str(bundle_dir.resolve())
    try:
        return BundleManifest.model_validate(payload)
    except ValidationError:
        return None


def is_valid_bundle_name(name: str) -> bool:
    """Spec §2.2 — name must be lowercase alphanumeric + hyphens/underscores."""
    return bool(_NAME_PATTERN.fullmatch(name))
