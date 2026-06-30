"""The `tools` template variable — presentation projection for `@tool` functions.

Two types: [`ToolItem`][templating.vars.tools.ToolItem] is one tool's
presentation projection, and
[`ToolsVar`][templating.vars.tools.ToolsVar] is the collection
wrapping `list[ToolItem]` that the Jinja render context binds as
`tools`.

`ToolItem` is a separate dataclass rather than a reuse of
[`ToolInfo`][tools.decorator.ToolInfo] so the templating layer stays
decoupled from the tools subsystem — the variable can be constructed
from arbitrary item lists in tests, and the engine builds `ToolItem`
instances from
[`ToolDefinition`][tools.decorator.ToolDefinition] at agent-load
time. Field names match, so a future merge of the two types would be
mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# ToolItem — one tool's projection.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolItem:
    """One tool's projection — name, description, summary, signature, schema.

    `name`, `description`, and `summary` mirror the matching fields
    on [`ToolDefinition`][tools.decorator.ToolDefinition].
    `signature` is a human-readable parameter projection (e.g.
    `"path: str, max_bytes: int = 1048576"`) for the prompt's tool
    section header. `schema` is the JSON-schema dict from
    `pydantic.BaseModel.model_json_schema()` exposed for advanced
    templates; most prompts never reach for it.
    """

    name: str
    description: str
    summary: str
    signature: str
    schema: dict[str, Any]


# ---------------------------------------------------------------------------
# ToolsVar — the ``tools`` template variable.
# ---------------------------------------------------------------------------


class ToolsVar:
    """The `tools` template variable — list-shaped over `ToolItem`.

    Wraps a list of [`ToolItem`][templating.vars.tools.ToolItem] and
    exposes the standardised method family (`list`, `summary`,
    `names`, `get`, `count`), the Pythonic protocols (`__iter__`,
    `__len__`, `__getitem__`, `__contains__`), and `__str__`
    defaulting to `list` so `{{ tools }}` in a Jinja template
    renders as `{{ tools.list() }}`. Insertion order is preserved
    across every accessor — agent authors rely on declaration order
    matching prompt order.

    `get` returns `None` (not raise) for an unknown name, and
    `__getitem__` mirrors that exactly. The discipline is
    deliberately un-Pythonic — most `__getitem__` implementations
    raise `KeyError` — but it pairs cleanly with Jinja's
    `| default("...")` filter for optional access in prompts.
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[ToolItem]) -> None:
        self._items: list[ToolItem] = list(items)
        # Name-indexed for O(1) get / __contains__ / __getitem__.
        # If two items share a name the LATER one wins — the tools
        # subsystem invariant "one tool per name within an agent"
        # holds at the resolved-config layer, so this case is
        # degenerate.
        self._by_name: dict[str, ToolItem] = {item.name: item for item in self._items}

    # ---- Standardised method family ----------------------------------------

    def list(self) -> str:
        """Render each tool as a `### <name>(<signature>)\\n<description>` block.

        Blocks are separated by a blank line so they render as
        independent markdown sections in the system prompt. Returns
        the empty string for an empty tool set so
        `{{ tools.list() }}` against an agent with no tools
        contributes nothing — the `agent.template_warning`
        mechanism catches the agent-load case separately.
        """
        return "\n\n".join(
            f"### {item.name}({item.signature})\n{item.description}"
            for item in self._items
        )

    def summary(self) -> str:
        """Render one line per tool: `<name>: <summary>`."""
        return "\n".join(f"{item.name}: {item.summary}" for item in self._items)

    def names(self) -> str:
        """Comma-separated tool names — the minimal context form."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> ToolItem | None:
        """Look up one tool by name; `None` when absent.

        Mirrors `__getitem__` exactly. Templates that want "give me
        the bash item or fall back to a placeholder" can pair this
        with Jinja's `| default("...")` filter.
        """
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        """The number of tools in the variable."""
        return len(self._items)

    # ---- Pythonic protocols -----------------------------------------------

    def __str__(self) -> str:
        """`{{ tools }}` defaults to `list`."""
        return self.list()

    def __iter__(self) -> Iterator[ToolItem]:
        """Iterate over items in declaration order."""
        return iter(self._items)

    def __len__(self) -> int:
        """`len(tools)` / `{{ tools | length }}` returns `count`."""
        return len(self._items)

    def __getitem__(self, name: str) -> ToolItem | None:
        """Return the item for `name` or `None` — `get`-equivalent.

        Deliberately not the usual `__getitem__` discipline of
        raising `KeyError`; see the class docstring.
        """
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """Membership check by tool name.

        Accepts any object for the comparison key (so
        `42 in tools` returns `False` rather than raising) because
        that is the `dict.__contains__` discipline this delegates to.
        """
        return name in self._by_name
