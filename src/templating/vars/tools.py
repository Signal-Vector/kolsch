"""The ``tools`` template variable — Phase 2.2 Step 3 (spec §4.4).

Two types:

- :class:`ToolItem` — one tool's presentation projection.
- :class:`ToolsVar` — the collection wrapping
  ``list[ToolItem]`` that the Jinja render context binds as
  ``tools``.

## Why a separate dataclass instead of reusing ``tools.ToolInfo``

Build-plan Step 3's escalation gate asked the developer to
choose: reuse :class:`tools.ToolInfo` / :class:`tools.ToolDefinition`
or define a new presentation type. :class:`ToolInfo` carries
``name`` / ``description`` / ``args_schema`` only; spec §4.4
mandates :class:`ToolItem` also expose ``summary`` and
``signature``, neither of which lives on :class:`ToolInfo`
today (and ``summary`` won't until Step 11 lands the
``@tool(summary=...)`` extension). Beyond the field-set
mismatch, the Step 3 reviewer prompt explicitly states **"No
engine or runtime coupling — ToolsVar can be constructed from
arbitrary item lists in tests."** A separate dataclass honours
that decoupling cleanly. Step 11 / Step 14 build
:class:`ToolItem` instances from :class:`ToolDefinition` at
pre-flight time; everything in between (the render harness,
the variable, the tests) sees only :class:`ToolItem`.

If the coordinator prefers widening :class:`ToolInfo` with
optional ``summary`` and ``signature`` fields and aliasing
:class:`ToolItem = ToolInfo`, the refactor is mechanical — every
attribute name here matches.
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
    """One tool's projection — spec §4.4 attributes.

    Fields mirror spec §4.4 verbatim:

    - ``name`` — the tool's identifier (matches
      :attr:`tools.ToolDefinition.name`).
    - ``description`` — the full description from
      ``@tool(description=...)``. Surfaced via
      :meth:`ToolsVar.list` for the prompt's verbose tool section.
    - ``summary`` — the one-line summary from
      ``@tool(summary=...)`` or, when omitted, the substring of
      ``description`` from start through the first period (Step
      11 lands the decorator extension; this dataclass field is
      ready for it now).
    - ``signature`` — a human-readable parameter projection
      (e.g. ``"path: str, max_bytes: int = 1048576"``) for the
      prompt's tool section header. Step 11 / Step 14 render this
      from the tool's ``args_schema``; tests construct it as a
      plain string.
    - ``schema`` — the JSON-schema dict
      (:meth:`pydantic.BaseModel.model_json_schema`'s output)
      exposed for advanced templates. Most prompts never reach
      for it.
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
    """The ``tools`` template variable — spec §4.4 + §4.2 + §4.3.

    Wraps a list of :class:`ToolItem` and exposes:

    - The standardized method family — :meth:`list`, :meth:`summary`,
      :meth:`names`, :meth:`get`, :attr:`count`.
    - The Pythonic protocols — ``__iter__``, ``__len__``,
      ``__getitem__``, ``__contains__``.
    - ``__str__`` defaulting to :meth:`list` so ``{{ tools }}`` in
      a Jinja template renders as ``{{ tools.list() }}`` per spec
      §4.2.

    Insertion order is preserved across every accessor (iteration,
    :meth:`names`, :meth:`list`, :meth:`summary`) — agent authors
    rely on declaration order matching prompt order.

    :meth:`get` returns ``None`` (not raise) for an unknown name;
    spec §4.3 mandates subscript-equivalence, so
    ``tools["unknown"]`` also returns ``None``. That is
    deliberately un-Pythonic — most ``__getitem__`` implementations
    raise :class:`KeyError` — but it matches the spec's
    ``| default`` filter contract for optional access in prompts.
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[ToolItem]) -> None:
        self._items: list[ToolItem] = list(items)
        # Name-indexed for O(1) get / __contains__ / __getitem__.
        # If two items share a name the LATER one wins — spec §4.4
        # doesn't address duplicates explicitly, but tools subsystem
        # invariant T-I10 says "one tool per name within an agent"
        # at the resolved-config layer, so this case is degenerate.
        self._by_name: dict[str, ToolItem] = {item.name: item for item in self._items}

    # ---- Standardized method family (§4.2) --------------------------------

    def list(self) -> str:
        """Each tool as a ``### <name>(<signature>)\\n<description>``
        block — spec §4.4's verbose rendering.

        Blocks are separated by a blank line so they render as
        independent markdown sections in the system prompt.
        Returns the empty string for an empty tool set so
        ``{{ tools.list() }}`` against an agent with no tools
        contributes nothing (the
        :event:`agent.template_warning` mechanism catches the
        agent-load case separately).
        """
        return "\n\n".join(
            f"### {item.name}({item.signature})\n{item.description}"
            for item in self._items
        )

    def summary(self) -> str:
        """One-line per tool: ``<name>: <summary>`` — spec §4.4's
        compact rendering. Lines separated by newlines."""
        return "\n".join(f"{item.name}: {item.summary}" for item in self._items)

    def names(self) -> str:
        """Comma-separated names — spec §4.4: ``"read_file,
        write_file, list_files, bash"``. The minimal context
        form."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> ToolItem | None:
        """Look up one tool by name; ``None`` when absent.

        Mirrors :meth:`__getitem__` exactly (spec §4.3 mandates
        subscript-equivalence). Tools that want "give me the bash
        item or fall back to a placeholder" can pair this with
        Jinja's ``| default("…")`` filter.
        """
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        """The number of tools — spec §4.2's ``.count``."""
        return len(self._items)

    # ---- Pythonic protocols (§4.3) ----------------------------------------

    def __str__(self) -> str:
        """``{{ tools }}`` defaults to :meth:`list` per spec §4.2."""
        return self.list()

    def __iter__(self) -> Iterator[ToolItem]:
        """``for t in tools`` iterates over items in declaration
        order — spec §4.3."""
        return iter(self._items)

    def __len__(self) -> int:
        """``len(tools)`` / ``{{ tools | length }}`` returns
        :attr:`count` — spec §4.3."""
        return len(self._items)

    def __getitem__(self, name: str) -> ToolItem | None:
        """``tools["bash"]`` returns the item or ``None`` — spec
        §4.3 mandates this is :meth:`get`-equivalent (NOT the
        usual ``__getitem__`` discipline of raising
        :class:`KeyError`)."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """``"bash" in tools`` — spec §4.3 containment check.

        Accepts any object for the comparison key (so
        ``42 in tools`` returns ``False`` rather than raising)
        because that is the
        :meth:`dict.__contains__` discipline this delegates to.
        """
        return name in self._by_name
