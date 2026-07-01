"""The [`SkillsSubsystem`][skills.subsystem.SkillsSubsystem] facade.

Resolves enabled skill refs to [`EnabledSkill`]
[skills.models.EnabledSkill] records (catalog metadata only — skill
bodies never load here; they materialise into the event log later
through the Tools subsystem's `read` dispatch) and prepares
force-invoke handoffs.

This module is pure with respect to the event log. The engine emits
the synthetic `tool.called` / `tool.result` pair when processing a
force-invoke; the model's voluntary skill loads are also engine-side
via the regular `read` dispatch. See the
[skills subsystem](../explanation/skills-subsystem.md) for
the full contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from errors import AssetsNotFound, SkillForcedNotEnabled, SkillNotFound
from skills.models import EnabledSkill, SkillInvocation

if TYPE_CHECKING:
    from assets import AssetManagement
    from assets.catalog import AssetEntry
    from schemas import ResolvedAgentConfig


_SUBSYSTEM_KEY = "skills"


class SkillsSubsystem:
    """Per-agent skill discovery facade.

    Constructed once at engine startup with the platform's
    [`AssetManagement`][assets.AssetManagement]. Methods are async even
    though the underlying catalog lookups are sync — keeping the public
    surface async leaves room for future network-backed catalogs
    without an API break.

    No per-agent state is cached. Re-resolution on each call is O(N) on
    the enabled list (small in practice) and sidesteps the cache-
    invalidation bugs that would surface when the agent's config
    changes mid-session.
    """

    def __init__(self, *, asset_management: AssetManagement) -> None:
        self._am = asset_management

    async def get_enabled_skills(
        self,
        config: ResolvedAgentConfig,
    ) -> list[EnabledSkill]:
        """Resolve each enabled ref to an `EnabledSkill` record.

        Skills with `disable-model-invocation: true` ARE included. The
        engine filters them out of the system-prompt listing at render
        time but still needs them in the returned list so force-invoke
        can route to them.

        Args:
            config: The resolved agent configuration.

        Returns:
            One [`EnabledSkill`][skills.models.EnabledSkill] per ref
            in `skills.config.enabled`, in declaration order.

        Raises:
            SkillNotFound: An enabled ref does not resolve to a
                catalogued skill.
        """
        out: list[EnabledSkill] = []
        for entry in _enabled_entries(config):
            ref = entry["ref"]
            asset = await self._lookup_skill_asset(ref)
            out.append(_make_enabled_skill(ref=ref, metadata=asset.metadata))
        return out

    async def force_invoke(
        self,
        skill_ref: str,
        config: ResolvedAgentConfig,
    ) -> SkillInvocation:
        """Prepare a force-invoke handoff for the engine.

        Verifies the ref is in the agent's enabled list, then resolves
        its catalogued SKILL.md path. The returned [`SkillInvocation`]
        [skills.models.SkillInvocation] is what the engine drives
        through a synthetic `tool.called` for `read` on
        `skill_md_path`; authorization is the regular `tool.read`
        check, so this method performs no authz of its own.

        Args:
            skill_ref: The 3-segment catalog ref the caller wants to
                force-invoke.
            config: The resolved agent configuration. Used to verify
                that `skill_ref` is enabled.

        Returns:
            The prepared handoff record.

        Raises:
            SkillForcedNotEnabled: `skill_ref` is not in the agent's
                enabled list.
            SkillNotFound: The catalog has no skill at `skill_ref`
                (catalog and config disagree).
        """
        if skill_ref not in _enabled_ref_set(config):
            raise SkillForcedNotEnabled(
                ref=skill_ref,
                agent_id=str(config.agent_id),
            )

        asset = await self._lookup_skill_asset(skill_ref)
        return SkillInvocation(ref=skill_ref, skill_md_path=asset.path)

    # --- internal --------------------------------------------------------

    async def _lookup_skill_asset(self, ref: str) -> AssetEntry:
        try:
            asset = await self._am.get_asset(ref, field_asset_type="skill")
        except AssetsNotFound as exc:
            raise SkillNotFound(ref=ref) from exc
        # The catalog stores skills under the same map as other asset
        # types; the ref convention separates them, but a config that
        # names a non-skill ref under skills.config.enabled is still
        # ill-formed.
        if asset.asset_type != "skill":
            raise SkillNotFound(ref=ref)
        return asset


# ---------------------------------------------------------------------------
# Helpers — pure dict reads off the resolved config
# ---------------------------------------------------------------------------


def _skills_body(config: ResolvedAgentConfig) -> dict[str, Any]:
    """Return the `skills.config` body dict, or empty if disabled."""
    sub = config.subsystems.get(_SUBSYSTEM_KEY)
    if sub is None:
        return {}
    return sub.config


def _enabled_entries(config: ResolvedAgentConfig) -> list[dict[str, Any]]:
    """Yield well-formed `{"ref": str}` entries from the enabled list.

    Malformed entries are silently skipped here — Layer 3 validation
    (the [`make_standard_validator`][skills.validator.make_standard_validator]
    factory) is responsible for surfacing those at config-load time so
    the runtime path stays narrow.
    """
    raw = _skills_body(config).get("enabled", []) or []
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("ref"), str):
            out.append({"ref": entry["ref"]})
    return out


def _enabled_ref_set(config: ResolvedAgentConfig) -> set[str]:
    return {entry["ref"] for entry in _enabled_entries(config)}


def _make_enabled_skill(*, ref: str, metadata: dict[str, Any]) -> EnabledSkill:
    """Project a catalog metadata dict into an `EnabledSkill`.

    Tolerates both `allowed-tools` (kebab — the SKILL.md frontmatter
    parser does not canonicalise this field) and `allowed_tools`
    (snake). Missing optional fields collapse to their defaults.
    """
    allowed = metadata.get("allowed-tools")
    if allowed is None:
        allowed = metadata.get("allowed_tools", [])
    if not isinstance(allowed, list):
        allowed = []

    nested = metadata.get("metadata", {})
    if not isinstance(nested, dict):
        nested = {}

    name = metadata.get("name")
    if not isinstance(name, str):
        # Fall back to the last ref segment so misauthored frontmatter
        # still produces a usable record; the Layer 3 validator (or
        # frontmatter validator at catalog build) is the right place
        # to surface authoring errors.
        name = ref.rsplit("/", 1)[-1]
    description = metadata.get("description")
    if not isinstance(description, str):
        description = ""

    license_value = metadata.get("license")
    compatibility_value = metadata.get("compatibility")

    return EnabledSkill(
        ref=ref,
        name=name,
        description=description,
        license=license_value if isinstance(license_value, str) else None,
        compatibility=(
            compatibility_value if isinstance(compatibility_value, str) else None
        ),
        metadata=dict(nested),
        allowed_tools=[str(t) for t in allowed],
        disable_model_invocation=bool(metadata.get("disable_model_invocation", False)),
    )
