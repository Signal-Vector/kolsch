"""The `platform` template variable — single-object platform projection.

The module name has an underscore prefix to avoid clashing with the
stdlib `platform` module. The class is still exported as
[`PlatformVar`][templating.vars._platform.PlatformVar]; the engine
binds the instance to `platform` in the Jinja context.

`PlatformVar` is single-object with five attributes, some of which
are themselves objects:
[`providers`][templating.vars._platform.ProvidersVar] (list-shaped
over `ProviderConfig`),
[`authorization`][templating.vars._platform.AuthorizationVar]
(single-object), `workspace_summary` (`str | None`, mirroring
[`WorkspaceVar.summary`][templating.vars.workspace.WorkspaceVar]),
[`file_access`][templating.vars._platform.FileAccessVar]
(single-object), and
[`aliases`][templating.vars._platform.AliasesVar] (dict-shaped over
`platform.yaml:model_aliases`).

`ProvidersVar` mirrors
[`ToolsVar`][templating.vars.tools.ToolsVar] exactly so authors
learn one convention. `AliasesVar` is dict-shaped (`.get(name)` and
`__contains__` only); iteration yields the alias names.
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
    """The `platform.providers` collection — list-shaped over `ProviderConfig`.

    Wraps `list[ProviderConfig]` and exposes the standardised
    method family plus the four Pythonic protocols, indexed by each
    provider's `name` field (the user-facing key from
    `platform.yaml:llm_providers`). Mirrors
    [`ToolsVar`][templating.vars.tools.ToolsVar] in shape.
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[ProviderConfig]) -> None:
        self._items: list[ProviderConfig] = list(items)
        self._by_name: dict[str, ProviderConfig] = {
            item.name: item for item in self._items
        }

    def list(self) -> str:
        """Render each provider as `- <name> (<type>)`."""
        return "\n".join(f"- {item.name} ({item.type})" for item in self._items)

    def summary(self) -> str:
        """Render one line per provider: `<name>: <type>`."""
        return "\n".join(f"{item.name}: {item.type}" for item in self._items)

    def names(self) -> str:
        """Comma-separated provider names (e.g. `"openrouter, opencode"`)."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> ProviderConfig | None:
        """Look up by provider name; `None` for unknown."""
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        """The number of providers in the variable."""
        return len(self._items)

    def __str__(self) -> str:
        """`{{ platform.providers }}` defaults to `list`."""
        return self.list()

    def __iter__(self) -> Iterator[ProviderConfig]:
        """Iterate over providers in declaration order."""
        return iter(self._items)

    def __len__(self) -> int:
        """Return `count`."""
        return len(self._items)

    def __getitem__(self, name: str) -> ProviderConfig | None:
        """Return the provider for `name` or `None` — `get`-equivalent."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """Membership check by provider name."""
        return name in self._by_name


# ---------------------------------------------------------------------------
# AuthorizationVar — single-object with variant + rules.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuthorizationVar:
    """The `platform.authorization` sub-object — variant and rule list."""

    variant: str
    rules: list[Rule] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FileAccessVar — single-object with allow/deny lists.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FileAccessVar:
    """The `platform.file_access` sub-object — allowed and denied path lists."""

    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AliasesVar — dict-shaped over model_aliases.
# ---------------------------------------------------------------------------


class AliasesVar:
    """The `platform.aliases` collection — dict-shaped over model aliases.

    Exposes `get` and `__contains__` only; iteration yields the
    alias names (the keys). The full standardised method family is
    skipped because canonical refs aren't meaningful as a
    user-facing list.
    """

    __slots__ = ("_aliases",)

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases: dict[str, str] = dict(aliases)

    def get(self, name: str) -> str | None:
        """Return the canonical ref for `name`, or `None`."""
        return self._aliases.get(name)

    def __getitem__(self, name: str) -> str | None:
        """Return the canonical ref for `name` or `None` — `get`-equivalent."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """Membership check by alias name."""
        return name in self._aliases

    def __iter__(self) -> Iterator[str]:
        """Iterate over alias names (the keys) in insertion order."""
        return iter(self._aliases)

    def __len__(self) -> int:
        """Return the number of aliases."""
        return len(self._aliases)


# ---------------------------------------------------------------------------
# PlatformVar — the top-level variable.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlatformVar:
    """The `platform` template variable — single-object platform projection.

    See the module docstring for the field surface.
    """

    providers: ProvidersVar
    authorization: AuthorizationVar
    workspace_summary: str | None
    file_access: FileAccessVar
    aliases: AliasesVar
    _workspace_path: str = ""

    def __str__(self) -> str:
        """Render `Workspace: <path> · Providers: <names> · <N> rules`."""
        return (
            f"Workspace: {self._workspace_path} · "
            f"Providers: {self.providers.names()} · "
            f"{len(self.authorization.rules)} rules"
        )
