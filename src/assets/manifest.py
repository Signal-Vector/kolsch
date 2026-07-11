"""Bundle manifest model — loading and structural validation.

Higher-level catalog rules (name/directory mismatch, duplicate names,
reserved `core` name) live in `assets.catalog`; this module focuses
on the per-bundle shape.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from _yamlio import YAMLError, load, load_with_positions
from schemas import PermissionRule

if TYPE_CHECKING:
    from pathlib import Path

# Lowercase alphanumeric, hyphens, underscores.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class BundleManifest(BaseModel):
    """The parsed `bundle.yaml` contents.

    `path` is the absolute path to the bundle directory, filled in by
    the catalog scanner after a successful load. `permissions` is the
    bundle-wide authz rule list — same shape as
    `platform.yaml:authorization.config.rules` and the agent-level
    `permissions:` field on
    [`ResolvedAgentConfig`][schemas.ResolvedAgentConfig]. The authz
    provider walks rules in priority order: agent first, then bundle,
    then platform, then no-rule callback, then default deny.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str
    description: str
    required_env_vars: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[PermissionRule] = Field(default_factory=list)
    # Auto-discovery opt-out list. Entries are paths relative to the
    # bundle root (e.g. ``"nodes/draft_echo.py"``,
    # ``"workflows/wip.yaml"``). Listed files are skipped during
    # workflow/node discovery; tools / agents / skills scanners do not
    # currently consult this list. Used for in-progress drafts,
    # deprecated assets, or files reserved for tests.
    exclude: list[str] = Field(default_factory=list)
    path: str


def load_manifest(bundle_dir: Path) -> BundleManifest | None:
    """Return the parsed manifest, or `None` when the file is missing.

    Malformed manifests (YAML errors, schema errors) also return
    `None` — the caller decides whether to skip with a warning or
    treat as fatal. The catalog scanner's documented behavior is
    "skip with warning."

    Args:
        bundle_dir: The bundle's directory under `bundles/`.

    Returns:
        The parsed [`BundleManifest`][assets.BundleManifest], or
        `None` when the file is absent or unparseable.
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


def permissions_source_map(yaml_path: Path) -> dict[int, tuple[str, int]]:
    """Return `{permission_index: (source_file, source_line)}` for a YAML.

    Re-reads a bundle / agent / workflow YAML via
    [`load_with_positions`][_yamlio.load_with_positions] and walks the
    top-level `permissions` list (or, for workflow YAMLs, the `policy`
    list) to record each rule's source line. 1-indexed for
    human-readable use.

    feat-041 uses this so the engine's `PermissionRule → Rule`
    conversion (engine.py:985 and dispatch.py:262) can pass sidecar
    metadata into `RuleBasedAuthorizationProvider` without touching
    the `extra="forbid"` `PermissionRule` schema.

    Never raises: an absent / malformed / `!tag`-carrying YAML returns
    an empty map and the compile step falls through to
    `source_file=None` per the spec's fallback contract.
    """
    from ruamel.yaml.constructor import ConstructorError  # noqa: PLC0415

    if not yaml_path.exists():
        return {}
    try:
        payload = load_with_positions(yaml_path.read_text(encoding="utf-8"))
    except (YAMLError, ConstructorError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # `permissions` for agent/bundle YAML; `policy` for workflow YAML.
    seq = payload.get("permissions")
    if seq is None:
        seq = payload.get("policy")
    if seq is None:
        return {}
    lc = getattr(seq, "lc", None)
    data = getattr(lc, "data", None) if lc is not None else None
    if not isinstance(data, dict):
        return {}
    source_str = str(yaml_path.resolve())
    source_map: dict[int, tuple[str, int]] = {}
    for rule_index, position in data.items():
        if not isinstance(rule_index, int):
            continue
        line_0 = position[0] if position else None
        if not isinstance(line_0, int):
            continue
        source_map[rule_index] = (source_str, line_0 + 1)
    return source_map


def is_valid_bundle_name(name: str) -> bool:
    """Return `True` when `name` matches the bundle-name grammar.

    The grammar is lowercase alphanumeric with hyphens and underscores
    permitted after the leading character.
    """
    return bool(_NAME_PATTERN.fullmatch(name))
