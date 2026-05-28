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

from pydantic import BaseModel, ConfigDict, Field

from schemas.identifiers import AgentId

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
    TypeSpec is permissive here; the kernel/asset-mgmt steps tighten it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["single_shot", "conversational"]
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


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
    prompt: PromptSpec
    io: IOSpec
    subsystems: dict[str, SubsystemConfig] = Field(default_factory=dict)
    # RuntimeSpec is referenced in §13.3 but not detailed in §A; left
    # permissive until the kernel/runtime spec lands.
    runtime: dict[str, Any] = Field(default_factory=dict)


# Self-referential ModelSpec.fallback — resolve the forward reference.
ModelSpec.model_rebuild()
