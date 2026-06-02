"""platform.yaml loading + first-run defaults — spec §3.2 + §3.10.

Asset Management is the platform-bootstrap module (Am-I11): no other
module reads ``platform.yaml`` directly. This module provides:

- Default generation for first-run (no file present).
- YAML load + structural validation (the top-level must be a mapping).
- A section dict accessor consumed by every subsystem at its own
  initialisation.

The exact per-section shape is owned by the consuming subsystem; this
loader only validates that each declared section is a mapping and
exposes it as a dict.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

from errors import ConfigInvalid

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger("assets")

# Valid section names per spec §3.10 (file_access included) + §3.2
# (storage included). The accessor is permissive — unknown sections
# raise ``KeyError`` per Module API Reference §2.2.
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
        # Phase 1.1 LLM Provider Configuration — patch §2.2.
        "llm_providers",
        "model_aliases",
        # Phase 2.2 (ENV-R2) — optional multi-line text describing the
        # workspace's purpose. Surfaced in agent prompts via
        # `{{ workspace.summary }}` and `{{ platform.workspace_summary }}`.
        "workspace_summary",
    }
)


def default_platform_payload() -> dict[str, Any]:
    """The first-run default ``platform.yaml`` payload.

    Defaults follow spec §3.2 and require zero external services:
    SQLite + in-process bus + dotenv secrets + native sandbox +
    allow-all authorization. File-access defaults per File Handling
    §4.1 (allow data/, bundles/, sessions/, content/, logs/,
    platform.yaml; deny .env and .platform/).
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
            "allowed_paths": [
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
    """Return the parsed ``platform.yaml``; generate defaults on first run.

    Spec §3.10 + Module API Reference §2.1:
    - Missing file → write the defaults and return them (TC-ASM-12).
    - Present but malformed → ``ConfigInvalid``.
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
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
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
    """Each declared section must itself be a mapping (with two
    Phase 2.2 exceptions: ``workspace_summary`` is a string;
    ``model_aliases`` is a flat alias→ref string map but happens
    to satisfy mapping shape already)."""
    errors: list[dict[str, Any]] = []
    for key, value in payload.items():
        # Phase 2.2 (ENV-R2) — workspace_summary is a string, not a
        # mapping. Skip the shape check; downstream consumers treat
        # any non-string as None.
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
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def is_known_section(name: str) -> bool:
    """Return True if ``name`` is one of the documented sections."""
    return name in _KNOWN_SECTIONS
