"""The ``skills`` template variable — qol-skills-jinja-variable-symmetric-with-tools.

Two types:

- :class:`SkillItem` — one skill's presentation projection.
- :class:`SkillsVar` — the collection wrapping ``list[SkillItem]``
  that the Jinja render context binds as ``skills``.

The shape is intentionally symmetric with
:mod:`templating.vars.tools` so authors writing prompts can render
skills the same way they render tools:

.. code-block:: jinja

    {{ skills }}                          {# default .list() rendering #}
    {{ skills.summary() }}                {# compact one-line-per-skill #}
    {{ skills.names() }}                  {# comma-separated #}
    {% for s in skills %}{{ s.name }}{% endfor %}

The pre-QoL platform appended a ``## Available Skills`` block to
every rendered prompt; this QoL hands that responsibility to the
prompt author, who now controls placement / wording / whether to
include it at all. The migration warning at
``engine._render_prompt_if_needed`` catches the case where an
agent declares skills but its template doesn't reference them.

## Field surface (minimum)

Per the Step 1 writeup §3, :class:`SkillItem` exposes three
fields — ``name``, ``description``, ``ref`` — chosen as the
minimum that supports the common prompt patterns. The full
:class:`skills.EnabledSkill` record carries more (licence,
compatibility, metadata, allowed_tools, etc.); add those to
:class:`SkillItem` when a real prompt-side use case appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# SkillItem — one skill's projection.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SkillItem:
    """One skill's projection — three fields.

    - ``name`` — from the skill's SKILL.md frontmatter (matches
      :attr:`skills.EnabledSkill.name`).
    - ``description`` — from the SKILL.md frontmatter.
    - ``ref`` — the 3-segment catalog ref
      (``<bundle>/skills/<name>``). Useful for the canonical
      ``Path: <ref>/SKILL.md`` line that the pre-QoL listing
      shipped — templates that want it write
      ``{{ s.ref }}/SKILL.md`` directly.
    """

    name: str
    description: str
    ref: str


# ---------------------------------------------------------------------------
# SkillsVar — the ``skills`` template variable.
# ---------------------------------------------------------------------------


class SkillsVar:
    """The ``skills`` template variable — symmetric with
    :class:`templating.vars.tools.ToolsVar`.

    Wraps a list of :class:`SkillItem` and exposes:

    - The standardized method family — :meth:`list`,
      :meth:`summary`, :meth:`names`, :meth:`get`,
      :attr:`count`.
    - The Pythonic protocols — ``__iter__``, ``__len__``,
      ``__getitem__``, ``__contains__``.
    - ``__str__`` defaults to :meth:`list` so ``{{ skills }}`` in
      a Jinja template renders as ``{{ skills.list() }}``.

    Insertion order is preserved (matches the order the agent
    config's ``subsystems.skills.config.enabled`` list declared
    the skills).

    :meth:`get` and :meth:`__getitem__` return ``None`` for an
    unknown name — matches :class:`ToolsVar`'s deliberately
    un-Pythonic ``__getitem__`` so templates can pair with
    Jinja's ``| default("…")`` filter.
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[SkillItem]) -> None:
        self._items: list[SkillItem] = list(items)
        self._by_name: dict[str, SkillItem] = {
            item.name: item for item in self._items
        }

    # ---- Standardized method family --------------------------------------

    def list(self) -> str:
        """Each skill as a ``- **<name>** — <description> Path: `<ref>/SKILL.md```
        bullet — preserves the pre-QoL ``format_skills_listing``
        per-skill rendering byte-identically so existing template
        authors who write ``{{ skills }}`` see the same block they
        used to see from the auto-append.

        Returns the empty string for an empty skill set so
        ``{{ skills.list() }}`` against an agent with no skills
        contributes nothing (the
        :event:`agent.template_warning` mechanism catches the
        ``referenced + empty`` case separately).
        """
        return "\n".join(
            f"- **{item.name}** — {item.description} Path: `{item.ref}/SKILL.md`"
            for item in self._items
        )

    def summary(self) -> str:
        """One-line per skill: ``<name>: <description>``. Lines
        separated by newlines. Mirrors :meth:`ToolsVar.summary`."""
        return "\n".join(
            f"{item.name}: {item.description}" for item in self._items
        )

    def names(self) -> str:
        """Comma-separated names — the minimal context form.
        Mirrors :meth:`ToolsVar.names`."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> SkillItem | None:
        """Look up one skill by name; ``None`` when absent."""
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        """The number of skills — matches :attr:`ToolsVar.count`."""
        return len(self._items)

    # ---- Pythonic protocols ----------------------------------------------

    def __str__(self) -> str:
        """``{{ skills }}`` defaults to :meth:`list`."""
        return self.list()

    def __iter__(self) -> Iterator[SkillItem]:
        """``for s in skills`` iterates over items in declaration
        order."""
        return iter(self._items)

    def __len__(self) -> int:
        """``len(skills)`` / ``{{ skills | length }}`` returns
        :attr:`count`."""
        return len(self._items)

    def __getitem__(self, name: str) -> SkillItem | None:
        """``skills["name"]`` returns the item or ``None`` —
        :meth:`get`-equivalent, matching :class:`ToolsVar`."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """``"name" in skills`` — containment check delegated to
        the name-indexed dict."""
        return name in self._by_name
