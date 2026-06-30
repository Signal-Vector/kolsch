"""File-handling errors.

Most surfaces are Diagnostic so the model adapts to the failure;
policy-load failures are Fatal because they prevent bootstrap. See
the [error model](../../docs/explanation/error-model.md) for the
classification contract.
"""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class FileAccessDenied(PlatformError):
    """Raised when the path policy denied access to a file."""

    code = "file_access.denied"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        path: str,
        reason: str,
        matched_rule: dict[str, Any] | None = None,
        *,
        where: str = "tools.builtin.read",
    ) -> None:
        super().__init__(
            message=f"Access denied to path {path!r}: {reason}",
            where=where,
            detail={
                "path": path,
                "reason": reason,
                "matched_rule": matched_rule,
            },
        )


class FileAccessPathNormalizationFailed(PlatformError):
    """Raised when path normalization (realpath / symlink resolution) failed."""

    code = "file_access.path_normalization_failed"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        path: str,
        os_error: str,
        *,
        where: str = "tools.builtin.read",
    ) -> None:
        super().__init__(
            message=f"Could not normalize path {path!r}: {os_error}",
            where=where,
            detail={"path": path, "os_error": os_error},
        )


class FileAccessInvalidFileRef(PlatformError):
    """Raised when a `file_ref` value failed shape validation.

    Surfaces before policy evaluation runs — empty strings and null
    bytes in path data are caller bugs that the policy can't catch
    by itself.
    """

    code = "file_access.invalid_file_ref"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        value: str,
        reason: str,
        *,
        where: str = "files.policy.validate_file_ref",
    ) -> None:
        super().__init__(
            message=f"Invalid file_ref value: {reason}",
            where=where,
            detail={"value": value, "reason": reason},
        )


class FileAccessPolicyLoadFailed(PlatformError):
    """Raised when the file-access policy failed to load at platform bootstrap."""

    code = "file_access.policy_load_failed"
    level = Level.FATAL

    def __init__(
        self,
        config_section: str,
        error: str,
        *,
        where: str = "assets.bootstrap.load_file_policy",
    ) -> None:
        super().__init__(
            message=(
                f"Failed to load file-access policy from {config_section!r}: {error}"
            ),
            where=where,
            detail={"config_section": config_section, "error": error},
        )


# ---------------------------------------------------------------------------
# Builtin file-tool surfaces — file.* codes
# ---------------------------------------------------------------------------


class FileTooLarge(PlatformError):
    """Raised when `read_file` refused a target exceeding `max_bytes`."""

    code = "file.too_large"
    level = Level.RECOVERABLE

    def __init__(
        self,
        path: str,
        size_bytes: int,
        max_bytes: int,
        *,
        where: str = "tools.builtin.read_file",
    ) -> None:
        super().__init__(
            message=(
                f"File {path!r} is {size_bytes} bytes; refusing to read "
                f"more than max_bytes={max_bytes}"
            ),
            where=where,
            detail={
                "path": path,
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
            },
        )


class FileNotFound(PlatformError):
    """Raised when `read_file` or `list_files` targeted a path that doesn't exist."""

    code = "file.not_found"
    level = Level.RECOVERABLE

    def __init__(
        self,
        path: str,
        *,
        where: str = "tools.builtin.read_file",
    ) -> None:
        super().__init__(
            message=f"File not found: {path!r}",
            where=where,
            detail={"path": path},
        )


class FileLineTooLong(PlatformError):
    """Raised when a single line in `read_file`'s target exceeds the byte cap.

    Surfaces from the line-based `read_file` when any line's UTF-8
    byte length is greater than `max_bytes`. Distinct from
    `FileTooLarge` — under the soft-truncation model a file larger
    than the line budget paginates cleanly; only a single
    pathological line, usually a binary file masquerading as text,
    trips this guard.
    """

    code = "file.line_too_long"
    level = Level.RECOVERABLE

    def __init__(
        self,
        path: str,
        line_number: int,
        line_byte_length: int,
        max_bytes: int,
        *,
        where: str = "tools.builtin.read_file",
    ) -> None:
        super().__init__(
            message=(
                f"Line {line_number} of {path!r} is {line_byte_length} bytes; "
                f"refusing to return (max_bytes={max_bytes}). "
                "Likely a binary file or generated content."
            ),
            where=where,
            detail={
                "path": path,
                "line_number": line_number,
                "line_byte_length": line_byte_length,
                "max_bytes": max_bytes,
            },
        )
