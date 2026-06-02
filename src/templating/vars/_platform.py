"""The ``platform`` template variable — Phase 2.2 Step 7 (spec
§4.4).

Module name has an underscore prefix to avoid clashing with the
stdlib ``platform`` module (the build plan Step 7 reviewer's
check 4). The class is still exported as :class:`PlatformVar`;
the engine binds the instance to ``platform`` in the Jinja
context.

## Sub-objects

Spec §4.4's ``platform`` is single-object with five attributes,
some of which are themselves objects:

- :attr:`PlatformVar.providers` — :class:`ProvidersVar`
  (list-shaped over :class:`llm.protocol.ProviderConfig`).
- :attr:`PlatformVar.authorization` — :class:`AuthorizationVar`
  (single-object with ``variant`` + ``rules``).
- :attr:`PlatformVar.workspace_summary` — same text as
  :attr:`templating.vars.workspace.WorkspaceVar.summary` (str |
  None).
- :attr:`PlatformVar.file_access` — :class:`FileAccessVar`
  (single-object with ``allowed_paths`` + ``denied_paths``).
- :attr:`PlatformVar.aliases` — :class:`AliasesVar` (dict-shaped
  over ``platform.yaml:model_aliases``).

:class:`ProvidersVar` exposes the standardized method family
(``.list()`` / ``.summary()`` / ``.names()`` / ``.get()`` /
``.count``) plus the four Pythonic protocols, mirroring
:class:`templating.vars.tools.ToolsVar` exactly so authors learn
one convention.

:class:`AliasesVar` is dict-shaped — exposes ``.get(name)`` and
``__contains__`` only; iteration yields the alias names. Spec
§4.4 doesn't mandate the full method family for aliases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from llm.protocol import ProviderConfig
    from providers.authz import Rule


# ---------------------------------------------------------------------------
# ProvidersVar — list-shaped over ProviderConfig.
# ---------------------------------------------------------------------------


class ProvidersVar:
    """The ``platform.providers`` collection — spec §4.4 + §4.2 +
    §4.3.

    Wraps ``list[ProviderConfig]``. Exposes the standardized
    method family AND the four Pythonic protocols, indexed by
    each provider's ``name`` field (the user-facing key from
    ``platform.yaml:llm_providers``).
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[ProviderConfig]) -> None:
        self._items: list[ProviderConfig] = list(items)
        self._by_name: dict[str, ProviderConfig] = {
            item.name: item for item in self._items
        }

    def list(self) -> str:
        """Each provider as ``- <name> (<type>)`` block. Empty
        for an empty list."""
        return "\n".join(
            f"- {item.name} ({item.type})" for item in self._items
        )

    def summary(self) -> str:
        """One-line per provider: ``<name>: <type>``. Matches the
        standardized .summary() shape from spec §4.2."""
        return "\n".join(f"{item.name}: {item.type}" for item in self._items)

    def names(self) -> str:
        """Comma-separated provider names — spec §4.4 example:
        ``"openrouter, opencode"``."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> ProviderConfig | None:
        """Look up by provider name; ``None`` for unknown."""
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        return len(self._items)

    def __str__(self) -> str:
        return self.list()

    def __iter__(self) -> Iterator[ProviderConfig]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, name: str) -> ProviderConfig | None:
        """Spec §4.3 subscript = .get() (returns None for
        missing, NOT raises)."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name


# ---------------------------------------------------------------------------
# AuthorizationVar — single-object with variant + rules.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuthorizationVar:
    """The ``platform.authorization`` sub-object — spec §4.4."""

    variant: str
    rules: list[Rule] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FileAccessVar — single-object with allow/deny lists.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FileAccessVar:
    """The ``platform.file_access`` sub-object — spec §4.4."""

    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AliasesVar — dict-shaped over model_aliases.
# ---------------------------------------------------------------------------


class AliasesVar:
    """The ``platform.aliases`` collection — spec §4.4.

    Dict-shaped: exposes :meth:`get` and :meth:`__contains__`
    only; iteration yields the alias names (the keys). The spec
    doesn't extend the full standardized method family to
    aliases because canonical refs are not meaningful as a
    user-facing list.
    """

    __slots__ = ("_aliases",)

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases: dict[str, str] = dict(aliases)

    def get(self, name: str) -> str | None:
        """Return the canonical ref for ``name``, or ``None``."""
        return self._aliases.get(name)

    def __getitem__(self, name: str) -> str | None:
        """Spec §4.3 subscript = .get()."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._aliases

    def __iter__(self) -> Iterator[str]:
        return iter(self._aliases)

    def __len__(self) -> int:
        return len(self._aliases)


# ---------------------------------------------------------------------------
# PlatformVar — the top-level variable.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlatformVar:
    """The ``platform`` template variable — spec §4.4."""

    providers: ProvidersVar
    authorization: AuthorizationVar
    workspace_summary: str | None
    file_access: FileAccessVar
    aliases: AliasesVar
    _workspace_path: str = ""

    def __str__(self) -> str:
        """Default rendering — spec §4.4:
        ``Workspace: <path> · Providers: <names> · <N> rules``."""
        return (
            f"Workspace: {self._workspace_path} · "
            f"Providers: {self.providers.names()} · "
            f"{len(self.authorization.rules)} rules"
        )
