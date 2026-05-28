"""File-handling errors — spec §4.11."""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class FileAccessDenied(PlatformError):
    """The path policy denied access (TC-FIL-02..05)."""

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
    """Path normalization (realpath / symlink resolution) failed."""

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


class FileAccessPolicyLoadFailed(PlatformError):
    """The file-access policy failed to load at platform bootstrap."""

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
