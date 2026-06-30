"""Partial-shape Pydantic models used by Layer 1 validation.

Layer 1 runs on partials (every field optional) AND on the resolved
config (required fields enforced). The resolved schema lives in
`schemas.config`; the partial counterparts here mirror it with every
field made optional so a partial declaring only `model` (or only
`subsystems.memory.config`) passes validation.

We deliberately do NOT use Pydantic's `model_construct` or build
"deep partial" types programmatically — explicit Partial classes keep
the structural contract obvious to readers and make `extra="forbid"`
inheritance clear: the top-level and known sub-types forbid unknown
fields, while open-map fields accept anything.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.config import ContextFileSpec, ModelSpec, PermissionRule, PromptSpec
from schemas.identifiers import AgentId

# Mirror the resolved schema's prefix grammar at the partial layer so a
# malformed `extends:` surfaces in `validate_partial` rather than
# slipping through to merge + validate_resolved.
_PARTIAL_EXTENDS_PREFIXES = ("kolsch:", "workspace:")


class PartialModelSpec(BaseModel):
    """Partial counterpart of `schemas.ModelSpec`."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    parameters: dict[str, Any] | None = None
    fallback: list[ModelSpec] | None = None


class PartialIOSpec(BaseModel):
    """Partial counterpart of `schemas.IOSpec`."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["single_shot", "conversational"] | None = None
    inputs: dict[str, dict[str, Any]] | None = None
    outputs: dict[str, dict[str, Any]] | None = None


class PartialSubsystemConfig(BaseModel):
    """Partial counterpart of `schemas.SubsystemConfig`.

    Both `variant` and `config` may be omitted in a partial since a
    lower or higher layer may supply them. The resolved-level model
    enforces both as required.
    """

    model_config = ConfigDict(extra="forbid")

    variant: str | None = None
    config: dict[str, Any] | None = None


class PartialRuntimeSpec(BaseModel):
    """Partial counterpart of `schemas.RuntimeSpec`."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=0)


class PartialAgentConfig(BaseModel):
    """The Layer 1 partial type — every field optional.

    Used by
    [`validate_partial`][kernel.validation.validate_partial]. Matches
    `schemas.ResolvedAgentConfig` field-for-field but with each field
    made `T | None` and a default of `None` so any subset of fields
    constitutes a valid partial.

    `extra="forbid"` rejects unknown top-level fields. The
    `extends:` and `context_files` fields run their respective
    resolved-schema prefix validators during Layer 1 resolution
    (Pydantic short-circuits on construction) so a malformed prefix
    surfaces in `validate_partial` rather than later in the pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str | None = None
    assembly_version: str | None = None
    agent_id: AgentId | None = None
    name: str | None = None
    description: str | None = None
    model: PartialModelSpec | None = None
    prompt: PromptSpec | None = None
    io: PartialIOSpec | None = None
    subsystems: dict[str, PartialSubsystemConfig] | None = None
    runtime: PartialRuntimeSpec | None = None
    extends: str | None = None
    permissions: list[PermissionRule] | None = None
    context_files: list[ContextFileSpec] | None = None

    @field_validator("extends")
    @classmethod
    def _validate_extends_prefix(cls, value: str | None) -> str | None:
        # Permissive — accepts all four ref forms (mirrors the resolved
        # schema's validator). The resolver decides which form wins.
        if value is None:
            return value
        if not isinstance(value, str) or not value:
            msg = f"extends must be a non-empty string when present; got {value!r}"
            raise ValueError(msg)
        return value
