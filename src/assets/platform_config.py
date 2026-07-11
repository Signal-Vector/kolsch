"""`platform.yaml` loading and first-run defaults.

Asset Management is the platform-bootstrap module: no other module
reads `platform.yaml` directly. This file provides:

- Default generation for first-run (no file present).
- YAML load plus structural validation (the top level must be a
  mapping).
- A section-dict accessor consumed by every subsystem at its own
  initialisation.

The exact per-section shape is owned by the consuming subsystem; this
loader only validates that each declared section is a mapping and
exposes it as a dict.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from _yamlio import YAMLError, dump, load, load_with_positions
from errors import ConfigInvalid

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger("assets")

# Valid section names. The accessor is permissive — unknown sections
# raise ``KeyError`` rather than a PlatformError.
_KNOWN_SECTIONS: frozenset[str] = frozenset(
    {
        "secrets",
        "sandbox",
        "authorization",
        "storage",
        "event_bus",
        "file_access",
        "defaults",
        "logging",
        # LLM provider configuration.
        "llm_providers",
        "model_aliases",
        # Optional multi-line text describing the workspace's purpose.
        # Surfaced in agent prompts via `{{ workspace.summary }}` and
        # `{{ platform.workspace_summary }}`.
        "workspace_summary",
        # Meta-agent settings. The only knob today is
        # ``target_bundle: <name>`` controlling which bundle the
        # ``author_workflow`` agent writes new files into. When the
        # key is absent, the agent's skill resolves the target to the
        # workspace's first non-``kolsch`` bundle (the primary user
        # bundle); if only ``kolsch`` exists, the agent escalates to
        # the author. No default is written at setup time — leaving
        # the section absent IS the documented "auto-resolve" mode.
        "author_workflow",
    }
)


def default_platform_payload() -> dict[str, Any]:
    """Return the first-run default `platform.yaml` payload.

    The defaults require zero external services: SQLite + in-process
    bus + dotenv secrets + native sandbox + allow-all authorization.
    File-access defaults allow `data/`, `bundles/`, `sessions/`,
    `content/`, `logs/`, and `platform.yaml`; they deny `.env` and
    `.platform/`.
    """
    return {
        "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
        "sandbox": {"variant": "native", "config": {}},
        "authorization": {
            "variant": "rule_based",
            "config": {
                "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
            },
        },
        "storage": {
            "entity_backend": "sqlite",
            "entity_path": ".platform/entities.db",
        },
        "event_bus": {"backend": "in_process"},
        "file_access": {
            # `.` resolves to the workspace root and lets `list_files(path=".")`
            # answer "what's in my workspace?" — per `qol-workspace-root-listable.md`.
            # Without it, default-deny fires on the natural workspace inspection;
            # the fallback to `bash ls -la` only works because bash is broader.
            "allowed_paths": [
                ".",
                "data/",
                "bundles/",
                "sessions/",
                "content/",
                "logs/",
                "platform.yaml",
            ],
            "denied_paths": [".env", ".platform/"],
        },
        "defaults": {
            "model": {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
            },
            "memory": {
                "variant": "recent_buffer",
                "config": {"recent_turns": 20, "max_tokens": 4000},
            },
        },
    }


def load_or_create(root: Path) -> dict[str, Any]:
    """Return the parsed `platform.yaml`, generating defaults on first run.

    Args:
        root: The platform root.

    Returns:
        The parsed top-level mapping. When the file was absent at
        call time, the defaults are written to disk and returned.

    Raises:
        ConfigInvalid: The file is present but malformed.
    """
    path = root / "platform.yaml"
    if not path.exists():
        payload = default_platform_payload()
        _write_yaml(path, payload)
        _log.info("Generated default platform.yaml at %s", path)
        return payload

    return _load_existing(path)


def _load_existing(path: Path) -> dict[str, Any]:
    try:
        payload = load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 1,
                    "source": "platform.yaml",
                    "path": "/",
                    "code": "yaml_parse_error",
                    "message": str(exc),
                }
            ],
            where="assets.bootstrap.load_platform_yaml",
        ) from exc

    if payload is None:
        # Empty file → treat as "use defaults" (every section absent).
        return {}
    if not isinstance(payload, dict):
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 1,
                    "source": "platform.yaml",
                    "path": "/",
                    "code": "not_a_mapping",
                    "message": (
                        f"platform.yaml top-level must be a mapping; "
                        f"got {type(payload).__name__}"
                    ),
                }
            ],
            where="assets.bootstrap.load_platform_yaml",
        )

    _validate_section_shape(payload)
    return payload


def _validate_section_shape(payload: dict[str, Any]) -> None:
    """Validate that each declared section is a mapping.

    Two exceptions: `workspace_summary` is a string, and
    `model_aliases` is a flat alias-to-ref string map (which still
    satisfies the mapping check).
    """
    errors: list[dict[str, Any]] = []
    for key, value in payload.items():
        # workspace_summary is a string, not a mapping. Skip the
        # shape check; downstream consumers treat any non-string as
        # None.
        if key == "workspace_summary":
            continue
        if not isinstance(value, dict):
            errors.append(
                {
                    "layer": 1,
                    "source": "platform.yaml",
                    "path": f"/{key}",
                    "code": "not_a_mapping",
                    "message": (
                        f"section {key!r} must be a mapping; got {type(value).__name__}"
                    ),
                }
            )
    if errors:
        raise ConfigInvalid(
            validation_errors=errors,
            where="assets.bootstrap.load_platform_yaml",
        )


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(payload), encoding="utf-8")


def is_known_section(name: str) -> bool:
    """Return `True` when `name` is one of the documented sections."""
    return name in _KNOWN_SECTIONS


def authorization_rule_source_map(root: Path) -> dict[int, tuple[str, int]]:  # noqa: PLR0911 — one early-return per graceful-fallback path (§Design)
    """Return `{rule_index: (source_file, source_line)}` for the platform's rules.

    Re-reads `platform.yaml` via
    [`load_with_positions`][_yamlio.load_with_positions] and walks the
    `authorization.config.rules` list, recording each rule's YAML line
    (1-indexed) alongside the absolute source path.

    feat-041 uses this to tag every rule the
    `RuleBasedAuthorizationProvider` compiles so `/why` +
    `ToolCallBlock` inline hint can display a `platform.yaml:47`
    breadcrumb pointing at the deny rule's authoring site.

    Returns an empty map when:

    - `platform.yaml` is absent (first-run defaults are used).
    - The `authorization` section is missing or malformed.
    - `load_with_positions` raises a `ConstructorError` because a
      custom `!tag` type is present (fallback path — spec §Design).

    Never raises: the caller uses the empty-map fallback (rules
    compile with `source_file=None`, hint degrades to
    `(unknown source)`).
    """
    from ruamel.yaml.constructor import ConstructorError  # noqa: PLC0415

    path = root / "platform.yaml"
    if not path.exists():
        return {}
    try:
        payload = load_with_positions(path.read_text(encoding="utf-8"))
    except (YAMLError, ConstructorError):
        return {}
    if not isinstance(payload, dict):
        return {}
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return {}
    config = authorization.get("config")
    if not isinstance(config, dict):
        return {}
    rules = config.get("rules")
    if rules is None:
        return {}
    # `rules` is a `CommentedSeq` from ruamel; its `.lc.data` maps
    # `rule_index → (line, col)` (0-indexed). Add 1 for the
    # human-readable form. When `lc` / `lc.data` is missing (a plain
    # list slipped in, or a synthetic fixture), degrade gracefully.
    lc = getattr(rules, "lc", None)
    data = getattr(lc, "data", None) if lc is not None else None
    if not isinstance(data, dict):
        return {}
    source_map: dict[int, tuple[str, int]] = {}
    source_str = str(path.resolve())
    for rule_index, position in data.items():
        if not isinstance(rule_index, int):
            continue
        # ruamel's `.lc.data[i]` shape is `(line, col)` for sequences
        # or `(key_line, key_col, val_line, val_col)` for maps. Rules
        # are mappings inside a sequence, but the sequence itself
        # tracks the item's start; take element 0 regardless.
        line_0 = position[0] if position else None
        if not isinstance(line_0, int):
            continue
        source_map[rule_index] = (source_str, line_0 + 1)
    return source_map
