"""Kernel — pure config resolver, validators, JSON Merge Patch.

Per Kernel/Resolver Contract §1 and Module API Reference §1. Zero I/O,
zero state, no platform-module dependencies — every other module
imports the kernel; the kernel imports only Pydantic, the standard
library, and :mod:`schemas` (cross-module data types).

Public surface:

- :func:`merge_patch`, :func:`resolve` — RFC 7386 merge.
- :func:`validate_partial`, :func:`validate_resolved` — Layer 1 schema
  validators.
- :func:`resolve_and_validate` — the orchestrated Layer 1 → Layer 3 →
  Layer 2 pipeline returning a validated
  :class:`schemas.ResolvedAgentConfig`.
- :func:`export_json_schema` — JSON Schema for external consumers.
- :class:`ValidationError`, :class:`ValidationResult`,
  :class:`ConfigValidator` — the value/contract types shared with
  callers that supply Layer 2 or Layer 3 validators.
"""

from kernel.export import export_json_schema
from kernel.findings import ConfigValidator, ValidationError, ValidationResult
from kernel.pipeline import resolve_and_validate
from kernel.resolver import merge_patch, resolve
from kernel.validation import validate_partial, validate_resolved

__all__ = [
    "ConfigValidator",
    "ValidationError",
    "ValidationResult",
    "export_json_schema",
    "merge_patch",
    "resolve",
    "resolve_and_validate",
    "validate_partial",
    "validate_resolved",
]
