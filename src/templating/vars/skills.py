"""The `skills` template variable — presentation projection for enabled skills.

Two types: [`SkillItem`][templating.vars.skills.SkillItem] is one
skill's projection, and
[`SkillsVar`][templating.vars.skills.SkillsVar] is the collection
wrapping `list[SkillItem]` that the Jinja render context binds as
`skills`. The shape is intentionally symmetric with
`templating.vars.tools` so authors writing prompts can render skills
the same way they render tools:

```jinja
{{ skills }}                          {# default .list() rendering #}
{{ skills.summary() }}                {# compact one-line-per-skill #}
{{ skills.names() }}                  {# comma-separated #}
{% for s in skills %}{{ s.name }}{% endfor %}
```

Before this variable existed the platform appended a
`## Available Skills` block to every rendered prompt; the variable
hands that responsibility to the prompt author, who now controls
placement, wording, and whether to include it at all. A migration
warning in the engine catches the case where an agent declares
skills but its template doesn't reference them.

`SkillItem` exposes three fields (`name`, `description`, `ref`) — the
minimum that supports common prompt patterns. The full
[`EnabledSkill`][skills.EnabledSkill] record carries more; add those
to `SkillItem` when a real prompt-side use case appears.
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
    """One skill's projection — name, description, and catalog ref.

    `name` and `description` come from the skill's SKILL.md
    frontmatter. `ref` is the 3-segment catalog ref
    (`<bundle>/skills/<name>`), useful for templates that want the
    canonical `Path: <ref>/SKILL.md` line — they write
    `{{ s.ref }}/SKILL.md` directly.
    """

    name: str
    description: str
    ref: str


# ---------------------------------------------------------------------------
# SkillsVar — the ``skills`` template variable.
# ---------------------------------------------------------------------------


class SkillsVar:
    """The `skills` template variable — list-shaped over `SkillItem`.

    Symmetric with
    [`ToolsVar`][templating.vars.tools.ToolsVar]: wraps a list of
    [`SkillItem`][templating.vars.skills.SkillItem] and exposes the
    standardised method family (`list`, `summary`, `names`, `get`,
    `count`), the Pythonic protocols (`__iter__`, `__len__`,
    `__getitem__`, `__contains__`), and `__str__` defaulting to
    `list` so `{{ skills }}` renders as `{{ skills.list() }}`.
    Insertion order is preserved — matches the order the agent
    config's `subsystems.skills.config.enabled` list declared.

    `get` and `__getitem__` return `None` for an unknown name —
    matches `ToolsVar`'s deliberately un-Pythonic discipline so
    templates can pair with Jinja's `| default("...")` filter.
    """

    __slots__ = ("_by_name", "_items")

    def __init__(self, items: list[SkillItem]) -> None:
        self._items: list[SkillItem] = list(items)
        self._by_name: dict[str, SkillItem] = {
            item.name: item for item in self._items
        }

    # ---- Standardised method family --------------------------------------

    def list(self) -> str:
        """Render each skill as `- **<name>** — <description> Path: <ref>/SKILL.md`.

        Preserves the original per-skill rendering byte-identically
        so existing template authors who write `{{ skills }}` see
        the same block. Returns the empty string for an empty skill
        set so the variable contributes nothing in that case; the
        `agent.template_warning` mechanism catches the
        "referenced + empty" case separately.
        """
        return "\n".join(
            f"- **{item.name}** — {item.description} Path: `{item.ref}/SKILL.md`"
            for item in self._items
        )

    def summary(self) -> str:
        """Render one line per skill: `<name>: <description>`."""
        return "\n".join(
            f"{item.name}: {item.description}" for item in self._items
        )

    def names(self) -> str:
        """Comma-separated skill names — the minimal context form."""
        return ", ".join(item.name for item in self._items)

    def get(self, name: str) -> SkillItem | None:
        """Look up one skill by name; `None` when absent."""
        return self._by_name.get(name)

    @property
    def count(self) -> int:
        """The number of skills in the variable."""
        return len(self._items)

    # ---- Pythonic protocols ----------------------------------------------

    def __str__(self) -> str:
        """`{{ skills }}` defaults to `list`."""
        return self.list()

    def __iter__(self) -> Iterator[SkillItem]:
        """Iterate over items in declaration order."""
        return iter(self._items)

    def __len__(self) -> int:
        """`len(skills)` / `{{ skills | length }}` returns `count`."""
        return len(self._items)

    def __getitem__(self, name: str) -> SkillItem | None:
        """Return the item for `name` or `None` — `get`-equivalent."""
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        """Membership check by skill name."""
        return name in self._by_name
