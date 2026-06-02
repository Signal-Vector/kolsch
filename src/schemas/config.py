"""ResolvedAgentConfig + sub-types — Schemas §A + Module API Reference §13.3.

The fully-resolved agent configuration the engine receives. The
kernel (Step 3) produces it from a list of partials via JSON Merge
Patch (RFC 7386); this module only defines the shape.

Deeper sub-types (``TypeSpec`` under ``IOSpec``, ``RuntimeSpec``,
per-subsystem config bodies) are intentionally permissive here —
they're owned by the kernel/subsystems and will tighten as those
modules land. The structural fields documented in §A and §13.3 are
strict.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.identifiers import AgentId

# Phase 2.2 (ENV-R1 / §6.2) — agent envelope path prefixes. Both
# context_files[].path and extends: values must use one of these
# two prefixes. Plain paths raise config.context_file_unprefixed
# at Layer 1 so authors are explicit about which root they mean.
_ENVELOPE_PATH_PREFIXES = ("kolsch:", "workspace:")

# §A.6 — PromptSpec is either a list of (already-resolved) layer
# contents or a single inline string. Asset Management pre-resolves
# refs into contents before the engine sees the config.
PromptSpec = list[str] | str


class ModelSpec(BaseModel):
    """`AgentConfig.model` — Schemas §A.4."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    fallback: list[ModelSpec] = Field(default_factory=list)


class IOSpec(BaseModel):
    """`AgentConfig.io` — Schemas §A.5.

    ``inputs`` and ``outputs`` are typed maps (name → TypeSpec).
    TypeSpec is permissive here; richer typing arrives with the asset-
    management/typespec spec.

    The platform recognises a ``file_ref`` TypeSpec kind whose runtime
    value is a ``str`` — a file path the engine materialises into
    ``user.message`` content per Materialisation Rule §B.6. Schemas at
    this stage carry it via ``{"type": "file_ref"}``; downstream code
    treats the corresponding value as a string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["single_shot", "conversational"]
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RuntimeSpec(BaseModel):
    """`AgentConfig.runtime` — referenced in Module API Reference §13.3.

    The full spec is deferred (open item in the kernel contract §7.5);
    Phase 1 captures the well-known fields used by the engine's retry
    policy and the exchange budget. Unknown fields are forbidden so a
    typo in the YAML fails Layer 1 validation rather than being silently
    ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_retries: int = Field(default=2, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=0)
    # Future RuntimeSpec fields land here as they're specified.


class ContextFileSpec(BaseModel):
    """One ``context_files`` entry — Phase 2.2 §6.1 + Schemas §A.11.5.

    Fields:

    - ``path`` — prefixed reference (``kolsch:`` or ``workspace:``).
      Plain paths raise ``config.context_file_unprefixed`` at Layer 1.
    - ``summary`` — optional one-line description; loader falls
      back to the file's first non-empty line when absent.
    - ``primary`` — when ``true``, the file's content is inlined
      in the rendered prompt; when ``false`` (default), it's
      listed by path + summary only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    summary: str | None = None
    primary: bool = False

    @field_validator("path")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith(_ENVELOPE_PATH_PREFIXES):
            msg = (
                f"context_files[].path {value!r} must start with one of "
                f"{_ENVELOPE_PATH_PREFIXES!r}"
            )
            raise ValueError(msg)
        return value


class PermissionRule(BaseModel):
    """One ``permissions:`` entry — Phase 2.2 §1.1 + Schemas §A.11.4.

    Same shape as ``platform.yaml:authorization.config.rules``
    entries. ``who`` matches the principal (e.g.
    ``agent.ag_helper``); ``what`` matches the action OR target
    (e.g. ``tool.bash``); ``effect`` is one of
    ``allow`` / ``deny`` / ``prompt``.

    Defined here (and NOT imported from :mod:`providers.authz`)
    because the schemas layer must not depend on the providers
    layer. Step 9's composition wires this through the existing
    :class:`providers.authz.Rule` shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    who: str
    what: str
    effect: Literal["allow", "deny", "prompt"]


class SubsystemConfig(BaseModel):
    """`AgentConfig.subsystems[name]` — Schemas §A.7.

    Absence in the resolved config means the subsystem is disabled
    (per §A.7). The wrapper carries the variant name; the body schema
    is owned by the named subsystem.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variant: str
    config: dict[str, Any] = Field(default_factory=dict)


class ResolvedAgentConfig(BaseModel):
    """The fully-resolved agent configuration — Module API Reference §13.3.

    Field names follow §13.3 (``agent_id`` rather than §A's ``id``);
    when §13.3 omits a field that §A specifies, §A's wins
    (``description``, ``assembly_version``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str
    assembly_version: str
    agent_id: AgentId
    name: str
    description: str
    model: ModelSpec
    # Phase 1.3 (FB-R1) — ordered list of backup models the engine
    # walks on Recoverable model.* errors. Each entry is normalised
    # to canonical {provider, model} form during Layer 1; the
    # resolver accepts the three input shapes (object / compact
    # string / alias) per Schemas §A.4.2.
    fallback_models: list[ModelSpec] = Field(default_factory=list)
    prompt: PromptSpec
    io: IOSpec
    subsystems: dict[str, SubsystemConfig] = Field(default_factory=dict)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    # Phase 2.2 envelope additions — §A.11.
    extends: str | None = None
    permissions: list[PermissionRule] = Field(default_factory=list)
    context_files: list[ContextFileSpec] = Field(default_factory=list)

    @field_validator("extends")
    @classmethod
    def _validate_extends_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith(_ENVELOPE_PATH_PREFIXES):
            msg = (
                f"extends {value!r} must start with one of "
                f"{_ENVELOPE_PATH_PREFIXES!r}"
            )
            raise ValueError(msg)
        return value


# Self-referential ModelSpec.fallback — resolve the forward reference.
ModelSpec.model_rebuild()
