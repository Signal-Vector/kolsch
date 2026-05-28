"""Sandbox provider errors — spec §4.8.

Policy validation errors are Fatal (config problem prevents setup);
runtime denial / resource-exceeded are Diagnostic (the model can adapt).
"""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class SandboxPolicyInvalid(PlatformError):
    """The sandbox policy itself is malformed (Fatal — config issue)."""

    code = "sandbox.policy_invalid"
    level = Level.FATAL

    def __init__(
        self,
        policy: dict[str, Any],
        reason: str,
        *,
        where: str = "sandbox.execute",
    ) -> None:
        super().__init__(
            message=f"Sandbox policy invalid: {reason}",
            where=where,
            detail={"policy": policy, "reason": reason},
        )


class SandboxExecutionDenied(PlatformError):
    """The sandbox denied execution at runtime."""

    code = "sandbox.execution_denied"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        reason: str,
        *,
        where: str = "sandbox.execute",
    ) -> None:
        super().__init__(
            message=f"Sandbox denied execution: {reason}",
            where=where,
            detail={"reason": reason},
        )


class SandboxResourceExceeded(PlatformError):
    """A sandboxed call exceeded a configured resource limit."""

    code = "sandbox.resource_exceeded"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        resource: str,
        limit: float,
        observed: float,
        *,
        where: str = "sandbox.execute",
    ) -> None:
        super().__init__(
            message=(
                f"Sandbox resource {resource!r} exceeded: "
                f"observed {observed}, limit {limit}"
            ),
            where=where,
            detail={
                "resource": resource,
                "limit": limit,
                "observed": observed,
            },
        )
