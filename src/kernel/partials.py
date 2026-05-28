"""Partial-shape Pydantic models — spec §2 + §4.1 step 1.

Layer 1 runs on partials (every field optional) AND on the resolved
config (required fields enforced). The resolved schema lives in
:mod:`schemas.config`; the partial counterparts here mirror it with
every field made optional so a partial declaring only ``model`` (or
only ``subsystems.memory.config``) passes validation.

We deliberately do NOT use Pydantic's ``model_construct`` or build
"deep partial" types programmatically — explicit Partial classes keep
the structural contract obvious to readers and make ``extra="forbid"``
inheritance clear (spec Open Item §1: top-level + known sub-types
forbid unknown fields; open-map fields accept anything).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas.config import ModelSpec, PromptSpec
from schemas.identifiers import AgentId


class PartialModelSpec(BaseModel):
    """Partial counterpart of :class:`schemas.ModelSpec` — §A.4."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    parameters: dict[str, Any] | None = None
    fallback: list[ModelSpec] | None = None


class PartialIOSpec(BaseModel):
    """Partial counterpart of :class:`schemas.IOSpec` — §A.5."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["single_shot", "conversational"] | None = None
    inputs: dict[str, dict[str, Any]] | None = None
    outputs: dict[str, dict[str, Any]] | None = None


class PartialSubsystemConfig(BaseModel):
    """Partial counterpart of :class:`schemas.SubsystemConfig` — §A.7.

    Both ``variant`` and ``config`` may be omitted in a partial since a
    lower or higher layer may supply them. The resolved-level model
    enforces both as required.
    """

    model_config = ConfigDict(extra="forbid")

    variant: str | None = None
    config: dict[str, Any] | None = None


class PartialRuntimeSpec(BaseModel):
    """Partial counterpart of :class:`schemas.RuntimeSpec`."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=0)


class PartialAgentConfig(BaseModel):
    """The Layer 1 partial type — every field optional.

    Used by :func:`kernel.validation.validate_partial`. Matches
    :class:`schemas.ResolvedAgentConfig` field-for-field but with each
    field made ``T | None`` and a default of ``None`` so any subset of
    fields constitutes a valid partial.

    ``extra="forbid"`` rejects unknown top-level fields (spec Open
    Item §1 recommendation, confirmed for v1).
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
