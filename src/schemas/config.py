"""The fully-resolved agent configuration shape.

`ResolvedAgentConfig` is what the engine receives. The kernel produces
it from a list of partials via JSON Merge Patch (RFC 7386); this
module only defines the shape.

Deeper sub-types (`TypeSpec` under `IOSpec`, `RuntimeSpec`,
per-subsystem config bodies) are intentionally permissive here —
they're owned by the kernel and the subsystems and will tighten as
those modules land. The structural envelope is strict.

For the full schema reference (field semantics, validation rules,
default-application order), see the
[schemas reference](../reference/schemas.md).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.identifiers import AgentId

# Agent envelope path prefixes for `context_files[].path` and
# `extends:` values.
#
# - `kolsch:` resolves to the installed kolsch package.
# - `workspace:` resolves to the workspace root.
# - Plain paths are accepted and treated as workspace-rooted; the
#   resolver emits `config.deprecated_ref_form` for the implicit form.
# - For `extends:` specifically, the resolver also accepts the
#   2-segment catalog ref `<bundle>/<name>` and the 1-segment
#   shorthand `<name>`; it disambiguates by field.
#
# The schema validator is permissive; the heavy lifting is in
# `templating.context_files.resolve_path` and `assets.refs`.
_ENVELOPE_PATH_PREFIXES = ("kolsch:", "workspace:")

# `PromptSpec` is either a list of already-resolved layer contents or
# a single inline string. Asset Management pre-resolves refs into
# contents before the engine sees the config.
PromptSpec = list[str] | str


class ModelSpec(BaseModel):
    """The `model:` block of an agent config — provider, model, params.

    `fallback` is the ordered list of backup models the engine walks
    on Recoverable model errors. Each entry has the same shape as the
    primary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback: list[ModelSpec] = Field(default_factory=list)


class IOSpec(BaseModel):
    """The `io:` block of an agent config — mode plus typed I/O maps.

    `inputs` and `outputs` are typed maps (name → TypeSpec). The
    `TypeSpec` shape is permissive here; richer typing arrives with
    the asset-management / typespec spec.

    The platform recognises a `file_ref` TypeSpec kind whose runtime
    value is a `str` — a file path the engine materialises into
    `user.message` content. Schemas at this stage carry it via
    `{"type": "file_ref"}`; downstream code treats the corresponding
    value as a string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["single_shot", "conversational"]
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RuntimeSpec(BaseModel):
    """The `runtime:` block of an agent config — engine-tuned knobs.

    Today this carries the well-known fields used by the engine's
    retry policy and exchange budget. Unknown fields are forbidden so
    a typo in the YAML fails Layer 1 validation rather than being
    silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_retries: int = Field(default=2, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=0)


class ContextFileSpec(BaseModel):
    """One entry in the `context_files:` block.

    The fields:

    - `path` — a prefixed reference (`kolsch:` or `workspace:`) or a
      plain path (treated as workspace-rooted).
    - `summary` — an optional one-line description; the loader falls
      back to the file's first non-empty line when absent.
    - `primary` — when `True`, the file's content is inlined in the
      rendered prompt; when `False` (default), it's listed by path
      plus summary only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    summary: str | None = None
    primary: bool = False

    @field_validator("path")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        # Permissive — accepts `kolsch:` / `workspace:` prefixes AND
        # plain paths (workspace-rooted by default). The resolver at
        # `templating.context_files.resolve_path` does the work.
        if not isinstance(value, str) or not value:
            msg = f"context_files[].path must be a non-empty string; got {value!r}"
            raise ValueError(msg)
        return value


def legacy_rule_scope_violation(
    *,
    from_value: object,
    pattern: object,
    setup_can_migrate: bool = False,
) -> str | None:
    """Return why a legacy-shape rule's `from` / `pattern` is refused, or `None`.

    The legacy `{id, who, what, effect}` regex shape has no caller-chain
    or target filter: its compile path never reads `from` or `pattern`.
    A legacy rule carrying either would therefore load as a rule that
    applies to every caller and every target — wider than it reads. The
    authz provider, [`PermissionRule`][schemas.config.PermissionRule]
    and `providers.authz.Rule` all refuse such a rule with this text.

    `pattern: "*"` is the canonical default and a harmless no-op, so it
    is accepted; a `None` `from_value` (absent or `from: null`) is
    accepted for the same reason. Callers reading a raw mapping pass a
    non-`None` `from_value` when EITHER the `from` or `from_` key holds
    a value.

    The fix the text gives is converting the rule by hand. Only the
    `kolsch setup` migrator can do it automatically, and only for a
    `platform.yaml` rule with `who: ".*"`, a recognised `what`, a valid
    `from:` ref and no `pattern:`; the provider's platform-rule loader
    passes `setup_can_migrate=True` for exactly that case.

    Lives here (not in `providers.authz`) because the schemas layer must
    not depend on the providers layer; the provider imports it.

    Args:
        from_value: The rule's effective `from` value (`None` when absent).
        pattern: The rule's `pattern` value (`"*"` when absent).
        setup_can_migrate: Also point at `kolsch setup`, which converts
            this rule on re-run.

    Returns:
        The error text naming the offending field(s) and the fix, or
        `None` when the rule is acceptable.
    """
    fields: list[str] = []
    if from_value is not None:
        fields.append("`from:`")
    if pattern != "*":
        fields.append(f"`pattern: {pattern!r}`")
    if not fields:
        return None
    fix = (
        "Convert it by hand to the canonical shape `{what: <bundle>/tools/<name> "
        "| agent.execute.* | *, pattern: <glob>, from: <bundle>/agents/<name>, "
        "effect: ...}` (drop `who`)"
    )
    if setup_can_migrate:
        fix += "; for this `platform.yaml` rule, re-running `kolsch setup` does it"
    return (
        f"legacy `{{who, what}}` rule cannot carry {' or '.join(fields)} — the "
        "legacy regex shape has no caller or target filter, so the rule would "
        f"apply to every caller and every target. {fix}"
    )


class PermissionRule(BaseModel):
    """One entry in the agent config's `permissions:` block.

    Same shape as `platform.yaml`'s `authorization.config.rules`
    entries. The canonical shape is `{what, pattern, from?, effect}`:

    - `what` is the tool's fully-qualified ref (`kolsch/bash`), a
      system action namespace (`agent.execute.*`), or `*`.
    - `pattern` is a glob matched against the target.
    - `from` (optional) is a 3-seg caller-chain glob
      (`kolsch/agents/triage`, `kolsch/workflows/*`) that scopes the
      rule to invocations whose caller-chain contains a matching
      frame. Shape/asset-type/catalog validation happens in the authz
      provider.
    - `effect` is `allow`, `deny`, or `prompt`.

    The legacy shape `{id, who, what, effect}` is still accepted
    during the migration window — `who` is optional and, when
    present, flags the rule as legacy-regex (the authz provider's
    compile step keys off this). A legacy rule cannot carry `from`
    or a non-`*` `pattern` (see
    [`legacy_rule_scope_violation`][schemas.config.legacy_rule_scope_violation]);
    validation rejects the combination rather than let the field be
    silently ignored.

    Defined here (rather than imported from `providers.authz`)
    because the schemas layer must not depend on the providers layer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    what: str
    pattern: str = "*"
    effect: Literal["allow", "deny", "prompt"]
    from_: str | None = Field(default=None, alias="from")
    # Legacy fields — deprecated; present-but-None on new-shape rules.
    # `who` is the discriminator the authz provider uses to dispatch
    # regex vs. glob matching.
    id: str = ""
    who: str | None = None

    @model_validator(mode="after")
    def _reject_scoped_legacy(self) -> PermissionRule:
        if self.who is not None:
            reason = legacy_rule_scope_violation(
                from_value=self.from_, pattern=self.pattern
            )
            if reason is not None:
                raise ValueError(reason)
        return self


# Ceiling for `HookRule.timeout_seconds` — matches the bash tool's
# default max timeout (`_DEFAULT_BASH_TIMEOUT_SECONDS` in
# `tools/builtins.py`). Hook subprocesses are fire-and-forget
# side-effects (e.g. a desktop notification), not model-facing tool
# calls, so there is no reason for one to run longer than the bash
# tool's own default ceiling.
_HOOK_TIMEOUT_CEILING_SECONDS = 60


class HookRule(BaseModel):
    """One entry in `platform.yaml`'s `hooks:` block (spec-131).

    A hook rule pairs a kolsch event (by `EventType`/wire-kind string)
    with a shell `command` to run as a fire-and-forget notification
    side-effect — e.g. a desktop notification when an approval prompt
    is requested. v1 is notify-only: hooks cannot block or alter
    engine/authz behavior (see spec-131 Scope § Out of scope).

    - `event` is the exact kolsch event-type or `ui.event` wire-kind
      string (e.g. `"kolsch.prompt.requested"`,
      `"thread.exchange_completed"`) to match against.
    - `matcher` is an optional glob (default `"*"`, matches
      everything), evaluated with `fnmatch.fnmatchcase` against the
      per-event subject — see spec-131's matching table.
    - `command` is the shell command to run.
    - `timeout_seconds` bounds how long the subprocess may run before
      being killed; defaults to 10s and is capped at
      `_HOOK_TIMEOUT_CEILING_SECONDS` (60s), matching the bash tool's
      default max timeout.

    Unknown keys are rejected (`extra="forbid"`) so a typo'd field
    fails fast at config-load time rather than being silently
    ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: str
    matcher: str = "*"
    command: str
    timeout_seconds: int = Field(default=10, gt=0, le=_HOOK_TIMEOUT_CEILING_SECONDS)


class SubsystemConfig(BaseModel):
    """One entry in the agent config's `subsystems:` map.

    Absence in the resolved config means the subsystem is disabled.
    The wrapper carries the variant name; the body schema is owned by
    the named subsystem.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variant: str
    config: dict[str, Any] = Field(default_factory=dict)


class ResolvedAgentConfig(BaseModel):
    """The fully-resolved agent configuration the engine receives.

    Produced by the kernel from a list of partials via JSON Merge
    Patch (RFC 7386). Field names follow the runtime catalogue:
    `agent_id` rather than the YAML's `id`; when the runtime omits a
    field the YAML carries (e.g. `description`, `assembly_version`),
    the YAML's wins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Defaults applied at the resolved layer so YAMLs that omit these
    # envelope fields still merge cleanly. The kernel pipeline passes
    # raw dicts through `resolve()`; putting defaults on the partial
    # Pydantic class wouldn't propagate.
    schema_version: str = "0.1"
    assembly_version: str = "1"
    agent_id: AgentId
    name: str
    description: str
    model: ModelSpec
    # Ordered list of backup models the engine walks on Recoverable
    # model errors. Each entry is normalised to canonical
    # `{provider, model}` form during Layer 1; the resolver accepts
    # the three input shapes (object / compact string / alias).
    fallback_models: list[ModelSpec] = Field(default_factory=list)
    prompt: PromptSpec
    io: IOSpec
    subsystems: dict[str, SubsystemConfig] = Field(default_factory=dict)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    # Envelope additions for templating and permissions.
    extends: str | None = None
    permissions: list[PermissionRule] = Field(default_factory=list)
    context_files: list[ContextFileSpec] = Field(default_factory=list)

    @field_validator("extends")
    @classmethod
    def _validate_extends_prefix(cls, value: str | None) -> str | None:
        # Permissive — accepts all four ref forms in the project's
        # grammar (1-, 2-, and 3-segment catalog refs AND prefixed
        # file paths). The resolver (`asset_manager` / `refs`)
        # disambiguates and emits deprecation warnings as appropriate.
        if value is None:
            return value
        if not isinstance(value, str) or not value:
            msg = f"extends must be a non-empty string when present; got {value!r}"
            raise ValueError(msg)
        return value


# Self-referential `ModelSpec.fallback` — resolve the forward reference.
ModelSpec.model_rebuild()
