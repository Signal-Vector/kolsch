"""Secrets provider — spec Module 1 + Module API Reference §6.

v1 ships the ``dotenv`` variant; the registry seam below lets future
variants (vault, cloud secret managers) slot in without core changes.

Conformance invariants:
- S-I1 — Secret values MUST NOT appear in the provider's own logs.
  The dotenv variant logs only metadata (path, count of keys); never
  values. Lookups (resolve / exists / list_names) emit no log lines.
- S-I3 — Missing secret raises :class:`SecretNotFound`. Never None
  or empty string.
- S-I5 — All methods are ``async``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from errors import ConfigInvalid, SecretNotFound, SecretReadFailed

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger("providers.secrets")


# ---------------------------------------------------------------------------
# Protocol — Module API Reference §6.1
# ---------------------------------------------------------------------------


class SecretsProvider(Protocol):
    """The three-method contract every secrets variant honours."""

    async def resolve(self, name: str) -> str:
        """Return the secret value, or raise :class:`SecretNotFound`."""

    async def exists(self, name: str) -> bool:
        """Return whether a secret is available; does NOT return value."""

    async def list_names(self) -> list[str]:
        """Return all secret names. Values NEVER returned (S-I1)."""


# ---------------------------------------------------------------------------
# .env parsing (inline; no extra dep)
# ---------------------------------------------------------------------------


def _parse_dotenv(text: str) -> dict[str, str]:
    """Minimal dotenv parser per spec §1.4.2.

    Supports ``KEY=value`` lines, ``#`` line comments, and basic
    single/double-quote pair stripping. Lines without ``=`` are
    silently skipped (malformed; comments are also skipped here).
    """
    result: dict[str, str] = {}
    # Minimum length for a string to be a matching pair of quotes
    # ("" or ''). Two chars exactly is still a valid empty-quoted
    # secret per spec §1.4.4.
    quoted_min = 2
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= quoted_min
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        if not key:
            continue
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Dotenv variant
# ---------------------------------------------------------------------------


class DotenvSecretsProvider:
    """Spec §1.4 — read once at startup, serve from memory.

    Missing .env → empty dict (the user may add the file later;
    restart picks it up). Unreadable .env → :class:`SecretReadFailed`.
    """

    def __init__(self, *, root: Path, config: dict[str, Any]) -> None:
        relative = config.get("path", ".env")
        candidate = Path(relative)
        self._path = candidate if candidate.is_absolute() else (root / candidate)
        self._secrets = self._load()
        _log.debug(
            "DotenvSecretsProvider loaded %d secrets from %s",
            len(self._secrets),
            self._path,
        )

    def _load(self) -> dict[str, str]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SecretReadFailed(
                path=str(self._path),
                os_error=str(exc),
            ) from exc
        return _parse_dotenv(text)

    async def resolve(self, name: str) -> str:
        if name not in self._secrets:
            raise SecretNotFound(name=name)
        return self._secrets[name]

    async def exists(self, name: str) -> bool:
        return name in self._secrets

    async def list_names(self) -> list[str]:
        return list(self._secrets)


# ---------------------------------------------------------------------------
# Registry seam — variants register here; make_secrets_provider dispatches
# ---------------------------------------------------------------------------


SecretsFactory = "Callable[..., SecretsProvider]"
_REGISTRY: dict[str, Callable[..., SecretsProvider]] = {}


def register_secrets_variant(
    name: str,
    factory: Callable[..., SecretsProvider],
) -> None:
    """Register a variant factory. Idempotent on the same (name, factory)."""
    _REGISTRY[name] = factory


def make_secrets_provider(
    *,
    root: Path,
    config: dict[str, Any],
) -> SecretsProvider:
    """Dispatch by ``config['variant']``.

    ``config`` is the ``{variant, config}`` wrapper from
    ``platform.yaml`` (spec §1.4.1). Unknown variants raise
    :class:`ConfigInvalid` so the user sees a clean platform-bootstrap
    failure rather than an AttributeError later.
    """
    variant = config.get("variant")
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant or "")
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.secrets",
                    "path": "/secrets/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown secrets variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.secrets.make_secrets_provider",
        )
    return factory(root=root, config=body)


# Self-register the dotenv variant at import time.
register_secrets_variant("dotenv", DotenvSecretsProvider)
