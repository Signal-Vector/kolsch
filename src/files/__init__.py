"""Path-access policy and `file_ref` validation.

Implements the file-access subsystem per
[file handling](../explanation/file-handling.md). Consumed by:

- The builtin file-touching tools (`read`, `write`, `edit`) to gate
  each invocation's path argument.
- The engine's `execute` boundary to validate `file_ref` inputs
  before any agent runs.
- Tool dispatch and the authorization provider, which match rule
  patterns against the canonical form of a file tool's `path`
  argument via [`files.authz_target`][files.authz_target].
- The platform bootstrap to load the policy via
  [`load_policy`][files.policy.load_policy] from the `file_access`
  section of `platform.yaml`.
- Every write-capable file tool, via
  [`assert_writable_destination`][files.policy.assert_writable_destination],
  which refuses `content/` and the
  [`PROTECTED_WRITE_PATHS`][files.policy.PROTECTED_WRITE_PATHS]
  floor whatever authorization allows.

The package is a top-level home rather than co-located in
`src/tools/` because both tools and the engine consume it; routing
through `tools/` would force the engine to depend on tools.
"""

from files.authz_target import (
    PathTarget,
    comparable,
    is_case_insensitive_fs,
    normalise_path_target,
    path_pattern_forms,
    path_pattern_matches,
)
from files.policy import (
    PROTECTED_WRITE_PATHS,
    PathPolicy,
    assert_writable_destination,
    check_path_allowed,
    is_path_allowed,
    load_policy,
    validate_file_ref,
)

__all__ = [
    "PROTECTED_WRITE_PATHS",
    "PathPolicy",
    "PathTarget",
    "assert_writable_destination",
    "check_path_allowed",
    "comparable",
    "is_case_insensitive_fs",
    "is_path_allowed",
    "load_policy",
    "normalise_path_target",
    "path_pattern_forms",
    "path_pattern_matches",
    "validate_file_ref",
]
