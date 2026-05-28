"""Kernel / Resolver errors — spec §4.1."""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ConfigInvalid(PlatformError):
    """Resolved config failed validation; carries every error found."""

    code = "config.invalid"
    level = Level.FATAL

    def __init__(
        self,
        validation_errors: list[dict[str, Any]],
        *,
        where: str = "kernel.resolver.validate",
    ) -> None:
        super().__init__(
            message=(
                f"Configuration validation failed with "
                f"{len(validation_errors)} error(s)"
            ),
            where=where,
            detail={"validation_errors": list(validation_errors)},
        )


class ConfigUnknownVariant(PlatformError):
    """A subsystem named a variant with no registered validator."""

    code = "config.unknown_variant"
    level = Level.FATAL

    def __init__(
        self,
        subsystem: str,
        variant: str,
        *,
        where: str = "kernel.resolver.validate",
    ) -> None:
        super().__init__(
            message=(f"Subsystem {subsystem!r} declares unknown variant {variant!r}"),
            where=where,
            detail={"subsystem": subsystem, "variant": variant},
        )


class ConfigVersionMismatch(PlatformError):
    """`schema_version` or `assembly_version` is out of the supported set."""

    code = "config.version_mismatch"
    level = Level.FATAL

    def __init__(
        self,
        expected: str,
        actual: str,
        *,
        where: str = "kernel.resolver.validate",
    ) -> None:
        super().__init__(
            message=(f"Config version mismatch: expected {expected!r}, got {actual!r}"),
            where=where,
            detail={"expected": expected, "actual": actual},
        )


class ConfigMergeFailed(PlatformError):
    """Internal merge failed — should not happen for validated input."""

    code = "config.merge_failed"
    level = Level.FATAL

    def __init__(
        self,
        layer_index: int,
        key_path: str,
        *,
        where: str = "kernel.resolver.merge",
    ) -> None:
        super().__init__(
            message=(f"Merge failed at layer {layer_index} on key {key_path!r}"),
            where=where,
            detail={"layer_index": layer_index, "key_path": key_path},
        )
