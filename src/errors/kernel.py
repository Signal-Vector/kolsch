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


class ConfigAgentInheritanceCycle(PlatformError):
    """An ``extends:`` chain loops back on itself — Phase 2.2
    error-model §4.18 / spec §8.6."""

    code = "config.agent_inheritance_cycle"
    level = Level.FATAL

    def __init__(
        self,
        cycle: list[str],
        *,
        where: str = "templating.inheritance.resolve_extends_chain",
    ) -> None:
        super().__init__(
            message=f"agent inheritance cycle: {' → '.join(cycle)}",
            where=where,
            detail={"cycle": list(cycle)},
        )


class ConfigParentAgentNotFound(PlatformError):
    """An ``extends:`` reference resolves to a non-existent file
    — Phase 2.2 error-model §4.18."""

    code = "config.parent_agent_not_found"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        resolved: str,
        *,
        where: str = "templating.inheritance.resolve_extends_chain",
    ) -> None:
        super().__init__(
            message=(
                f"parent agent {path!r} not found (resolved to {resolved!r})"
            ),
            where=where,
            detail={"path": path, "resolved": resolved},
        )


class ConfigRequiredBlockMissing(PlatformError):
    """A child agent uses ``{% extends parent %}`` and does not
    override a parent's ``{% block X required %}`` — Phase 2.2
    error-model §4.18 / spec §8.4."""

    code = "config.required_block_missing"
    level = Level.FATAL

    def __init__(
        self,
        block_name: str,
        agent_path: str,
        *,
        where: str = "templating.inheritance.render_with_parent",
    ) -> None:
        super().__init__(
            message=(
                f"required block {block_name!r} missing in agent {agent_path!r}"
            ),
            where=where,
            detail={"block_name": block_name, "agent_path": agent_path},
        )


class ConfigExtendsTemplateWithoutInheritance(PlatformError):
    """A child agent's prompt uses ``{% extends parent %}`` but
    the agent YAML has no ``extends:`` field — Phase 2.2
    error-model §4.18 / spec §8.9."""

    code = "config.extends_template_without_inheritance"
    level = Level.FATAL

    def __init__(
        self,
        agent_path: str,
        *,
        where: str = "templating.inheritance.render_with_parent",
    ) -> None:
        super().__init__(
            message=(
                f"agent {agent_path!r} uses {{% extends parent %}} in its "
                f"prompt but has no extends: field"
            ),
            where=where,
            detail={"agent_path": agent_path},
        )


class ContextFileNotFound(PlatformError):
    """A ``context_files[].path`` resolved to a non-existent file
    — Phase 2.2 error-model §4.18.

    Surfaces at agent-load when the resolver maps
    ``kolsch:foo.md`` / ``workspace:foo.md`` to an absolute path
    that does not exist.
    """

    code = "context.file_not_found"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        resolved: str,
        *,
        where: str = "templating.context_files.load_context_file",
    ) -> None:
        super().__init__(
            message=(
                f"context_file {path!r} not found (resolved to {resolved!r})"
            ),
            where=where,
            detail={"path": path, "resolved": resolved},
        )


class ContextFileTooLarge(PlatformError):
    """A ``context_files[]`` file exceeds the 64 KiB cap — Phase
    2.2 error-model §4.18.

    Surfaces at agent-load. The 64 KiB cap prevents accidental
    "the whole architecture doc is in every exchange" inflation
    (Phase 2.2 §6.4).
    """

    code = "context.file_too_large"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        size_bytes: int,
        max_bytes: int,
        *,
        where: str = "templating.context_files.load_context_file",
    ) -> None:
        super().__init__(
            message=(
                f"context_file {path!r} is {size_bytes} bytes; max is {max_bytes}"
            ),
            where=where,
            detail={
                "path": path,
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
            },
        )


class ConfigDeprecatedRefForm(PlatformError):
    """A ref appears in a config envelope in a deprecated form
    (3-segment ``<bundle>/<asset_type>/<name>`` or explicit
    ``workspace:`` prefix). Recoverable — the agent still loads;
    the canonical 2-segment / unprefixed form is recommended.

    Surfaces at agent-load via the resolver's ``emit_warning``
    callback. Collected, logged, and (when the engine emits a
    ``config.set`` event under the new ref convention) visible
    alongside the resolved config so authors can locate the
    deprecated entry by JSON-pointer.

    Per qol-prefix-defaults-and-unified-refs.md Step 2.
    """

    code = "config.deprecated_ref_form"
    level = Level.RECOVERABLE

    def __init__(
        self,
        deprecated_form: str,
        canonical_form: str,
        *,
        location: str = "",
        where: str = "templating.context_files.resolve_path",
    ) -> None:
        super().__init__(
            message=(
                f"deprecated ref form {deprecated_form!r}; prefer "
                f"{canonical_form!r}"
                + (f" at {location}" if location else "")
            ),
            where=where,
            detail={
                "deprecated_form": deprecated_form,
                "canonical_form": canonical_form,
                "location": location,
            },
        )


class ConfigContextFileUnprefixed(PlatformError):
    """A ``context_files[].path`` or ``extends:`` value lacks the
    required ``kolsch:`` / ``workspace:`` prefix — Phase 2.2
    error-model §4.18.

    Spec §6.2 forbids plain paths in agent envelope path
    references so authors must be explicit about which root
    they mean — no silent surprises across source-tree dev,
    installed-wheel, and per-workspace contexts.
    """

    code = "config.context_file_unprefixed"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        *,
        where: str = "schemas.config.ContextFileSpec.validate_path",
    ) -> None:
        super().__init__(
            message=(
                f"path {path!r} lacks a kolsch: or workspace: prefix — "
                f"plain paths are rejected per Phase 2.2 §6.2"
            ),
            where=where,
            detail={"path": path},
        )


class ConfigTemplateRenderFailed(PlatformError):
    """Jinja render of an agent's ``prompt:`` template raised — Phase 2.2
    error-model §4.18.

    Raised by :func:`templating.render.render_prompt` when the Jinja
    engine surfaces an ``UndefinedError`` (typo'd variable name under
    StrictUndefined), a ``TemplateSyntaxError`` (malformed
    ``{{ }} / {% %}`` syntax), or any other rendering failure. The
    ``template`` detail identifies the source the engine was rendering
    (usually the agent's YAML path); ``reason`` is the underlying
    Jinja exception's ``str(...)`` projection — keep it human-readable
    so the agent author can locate the bad reference quickly.
    """

    code = "config.template_render_failed"
    level = Level.FATAL

    def __init__(
        self,
        template: str,
        reason: str,
        *,
        where: str = "templating.render.render_prompt",
    ) -> None:
        super().__init__(
            message=(f"template render failed: {reason}"),
            where=where,
            detail={"template": template, "reason": reason},
        )
