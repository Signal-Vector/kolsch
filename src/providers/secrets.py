"""Secrets provider — resolve named secrets without leaking values.

The v1 `dotenv` variant reads a `.env` file once at startup and serves
lookups from memory; the registry seam below lets future variants
(vault, cloud secret managers) slot in without core changes. For the
full contract — non-logging of values, missing-secret behaviour, the
async shape — see the [secrets-sandbox-authz
contract](../explanation/secrets-sandbox-authz-contract.md).
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
# Protocol
# ---------------------------------------------------------------------------


class SecretsProvider(Protocol):
    """The three-method contract every secrets variant honours.

    Implementations MUST NOT log secret values — the dotenv variant
    logs only the path and the count of loaded keys, never the values
    themselves.
    """

    async def resolve(self, name: str) -> str:
        """Return the secret value, or raise `SecretNotFound`."""

    async def exists(self, name: str) -> bool:
        """Return whether a secret is available; never returns the value."""

    async def list_names(self) -> list[str]:
        """Return all secret names. Values are never returned."""


# ---------------------------------------------------------------------------
# .env parsing (inline; no extra dep)
# ---------------------------------------------------------------------------


def _parse_dotenv(text: str) -> dict[str, str]:
    """Minimal dotenv parser.

    Supports `KEY=value` lines, `#` line comments, and basic
    single/double-quote pair stripping. Lines without `=` are silently
    skipped (malformed; comments are also skipped here).
    """
    result: dict[str, str] = {}
    # Minimum length for a string to be a matching pair of quotes
    # ("" or ''). Two chars exactly is still a valid empty-quoted
    # secret.
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
    """Read a `.env` file once at startup, serve lookups from memory.

    A missing `.env` resolves to an empty dict (the user may add the
    file later; restart picks it up). An unreadable `.env` raises
    `SecretReadFailed`.
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
        """Return the secret value for `name`.

        Raises:
            SecretNotFound: No secret with that name is loaded.
        """
        if name not in self._secrets:
            raise SecretNotFound(name=name)
        return self._secrets[name]

    async def exists(self, name: str) -> bool:
        """Return whether a secret with that name is loaded."""
        return name in self._secrets

    async def list_names(self) -> list[str]:
        """Return every loaded secret name; values are never returned."""
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
    """Register a variant factory. Idempotent on the same `(name, factory)`."""
    _REGISTRY[name] = factory


def make_secrets_provider(
    *,
    root: Path,
    config: dict[str, Any],
) -> SecretsProvider:
    """Dispatch by `config['variant']`.

    `config` is the `{variant, config}` wrapper from `platform.yaml`.
    Unknown variants raise `ConfigInvalid` so the user sees a clean
    platform-bootstrap failure rather than an `AttributeError` later.

    Raises:
        ConfigInvalid: `variant` is unset or names no registered factory.
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
