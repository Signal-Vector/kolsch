"""Path-access policy and `file_ref` validation.

Implements the file-access subsystem per
[file handling](../explanation/file-handling.md). Consumed by:

- The builtin file-touching tools (`read`, `write`, `edit`) to gate
  each invocation's path argument.
- The engine's `execute` boundary to validate `file_ref` inputs
  before any agent runs.
- The platform bootstrap to load the policy via
  [`load_policy`][files.policy.load_policy] from the `file_access`
  section of `platform.yaml`.

The package is a top-level home rather than co-located in
`src/tools/` because both tools and the engine consume it; routing
through `tools/` would force the engine to depend on tools.
"""

from files.policy import (
    PathPolicy,
    assert_writable_destination,
    check_path_allowed,
    is_path_allowed,
    load_policy,
    validate_file_ref,
)

__all__ = [
    "PathPolicy",
    "assert_writable_destination",
    "check_path_allowed",
    "is_path_allowed",
    "load_policy",
    "validate_file_ref",
]
