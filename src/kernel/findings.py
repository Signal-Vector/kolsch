"""Validation finding value types — spec §4.2 + Error Model §3.1.

A :class:`ValidationError` here is a *finding* describing one problem
in a partial or resolved config. It is NOT a :class:`PlatformError`
subclass; findings are values that the kernel collects and ships
inside ``ConfigInvalid.detail.validation_errors`` when raising.

The :class:`ConfigValidator` Protocol is the contract every Layer 2
and Layer 3 validator honours: a sync, pure function from input dict
to :class:`ValidationResult`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidationError(BaseModel):
    """One structured finding describing a single config problem.

    Fields match Kernel §4.2 exactly:

    - ``layer`` — 1 (structural), 2 (system concerns), or 3 (subsystem).
    - ``source`` — the validator that produced the finding, e.g.
      ``"kernel"``, ``"memory.recent_buffer"``,
      ``"agent_engine.provider_check"``.
    - ``path`` — JSON Pointer to the offending field, e.g.
      ``"/subsystems/memory/config/scope"``. Empty string for whole-
      config issues.
    - ``code`` — short machine-readable code, e.g. ``"missing"``,
      ``"unknown_variant"``, ``"invalid_provider"``.
    - ``message`` — human-readable description.

    Frozen so a finding can be safely passed through layers and shared
    across the aggregated list without aliasing surprises.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: Literal[1, 2, 3]
    source: str
    path: str
    code: str
    message: str


class ValidationResult(BaseModel):
    """The outcome of one validator call.

    ``ok`` and ``errors`` are kept in sync by convention — a validator
    returning ``ok=False`` MUST supply at least one error; conversely
    ``ok=True`` MUST have an empty error list. The kernel honours both
    forms but enforces the invariant when constructing results itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    errors: list[ValidationError] = Field(default_factory=list)


#: The shape every Layer 2 and Layer 3 validator must honour. Spec
#: §4.1 declares this as ``Callable[[dict], ValidationResult]``; we
#: tighten the value type to ``dict[str, Any]`` for clarity. A type
#: alias rather than a Protocol so plain functions and lambdas
#: satisfy it without explicit subtyping declarations.
#:
#: Layer 2 validators receive the full resolved config dict. Layer 3
#: validators receive the ``config`` body of a subsystem entry (the
#: dict inside ``{variant, config}``). Per spec §4.3 validators MUST
#: be sync and SHOULD be pure — the kernel does not enforce purity at
#: the type level, but a non-pure validator breaks the kernel's
#: overall purity guarantee.
ConfigValidator = Callable[[dict[str, Any]], ValidationResult]
