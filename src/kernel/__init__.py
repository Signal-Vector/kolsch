"""Kernel — pure config resolver, validators, JSON Merge Patch.

Implements the
[kernel/resolver contract](../../docs/explanation/kernel-resolver-contract.md):
zero I/O, zero state, and no platform-module dependencies. Every other
module imports the kernel; the kernel imports only Pydantic, the
standard library, and `schemas` (cross-module data types).
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
