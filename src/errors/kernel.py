"""Kernel and resolver errors.

Configuration load and inheritance failures, plus the agent-load-time
context-file and template-render surfaces. All classes here are Fatal
except `config.deprecated_ref_form`, which is Recoverable so the
agent still loads. See the
[error model](../../docs/explanation/error-model.md) for the
classification contract.
"""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class ConfigInvalid(PlatformError):
    """Raised when resolved config failed validation.

    The envelope's `detail.validation_errors` carries every error the
    validator found so the caller can present the full list.
    """

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
    """Raised when a subsystem named a variant with no registered validator."""

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
    """Raised when `schema_version` or `assembly_version` is unsupported."""

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
    """Raised when an internal merge failed.

    Should not happen for validated input — a hit indicates a
    resolver bug rather than a user config issue.
    """

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
    """Raised when an `extends:` chain loops back on itself."""

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
    """Raised when an `extends:` reference resolves to a non-existent file."""

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
    """Raised when a child agent fails to override a parent's required block.

    Surfaces when the child's `{% extends parent %}` template does
    not provide a `{% block X required %}` the parent declared.
    """

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
    """Raised when a prompt uses `{% extends parent %}` without an `extends:` field.

    The template-level inheritance directive requires the YAML-level
    `extends:` to declare which parent agent supplies the base
    template; without it the resolver has nowhere to look.
    """

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
    """Raised when a `context_files[].path` resolves to a non-existent file.

    Surfaces at agent-load when the resolver maps
    `kolsch:foo.md` or `workspace:foo.md` to an absolute path that
    doesn't exist.
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
    """Raised when a `context_files[]` file exceeds the 64 KiB cap.

    Surfaces at agent-load. The cap prevents accidental "the whole
    architecture doc is in every exchange" inflation.
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
    """Raised when a config envelope ref uses a deprecated form.

    Recoverable — the agent still loads; the canonical 2-segment /
    unprefixed form is recommended. Surfaces at agent-load via the
    resolver's `emit_warning` callback, then logged and visible
    alongside the resolved config so authors can locate the
    deprecated entry by JSON-pointer.
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
    """Raised when a `context_files[].path` or `extends:` value is unprefixed.

    Agent envelope path references must be explicit about which root
    they mean — `kolsch:` or `workspace:` — so the meaning doesn't
    silently shift across source-tree dev, installed-wheel, and
    per-workspace contexts. Plain paths are rejected.
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
                f"plain paths are rejected"
            ),
            where=where,
            detail={"path": path},
        )


class ConfigTemplateRenderFailed(PlatformError):
    """Raised when Jinja render of an agent's `prompt:` template raised.

    Surfaces from `templating.render.render_prompt` on any rendering
    failure — `UndefinedError` (a typo'd variable name under
    StrictUndefined), `TemplateSyntaxError` (malformed `{{ }}` or
    `{% %}` syntax), or anything else the engine raises. The
    `template` detail identifies the source the engine was rendering
    (usually the agent's YAML path); `reason` is the underlying
    Jinja exception's `str(...)` projection — kept human-readable so
    the agent author can locate the bad reference quickly.
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
