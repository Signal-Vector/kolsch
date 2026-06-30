r"""The `docs` template variable — context-file projection for prompts.

Two types: [`DocItem`][templating.vars.docs.DocItem] is one context
file's projection (path, summary, primary flag, content, size), and
[`DocsVar`][templating.vars.docs.DocsVar] is the collection wrapping
`list[DocItem]` that the Jinja render context binds as `docs`.

**Two-tier rendering.** Primary context files (`primary: true`) have
content inlined in the rendered prompt — they cost context tokens
every exchange, so they suit small, always-relevant content (an
index, hard rules). Secondary context files (`primary: false`,
default) are listed by path and summary only; the agent reads the
file on demand via `read_file` if it needs the content.
[`DocsVar.list`][templating.vars.docs.DocsVar.list] composes both
tiers — primary blocks first, then an "Also available" bullet list
for secondaries.

**Markdown layout.** Each primary renders as
`### <path>\n<content>`, separated by `\n\n---\n\n` (a
horizontal rule between blocks, not trailing — leaves the last
primary clean). Secondaries render as a `## Also available` header
followed by a `- **<path>** — <summary>` bullet list. An empty doc
set renders as the empty string; the `agent.template_warning`
mechanism catches the "references docs but has none" case separately
at agent-load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# DocItem — one context file's projection.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DocItem:
    """One context file's projection — path, summary, primary, content, size.

    Constructed by the
    [`load_context_file`][templating.context_files.load_context_file]
    loader from the resolved agent config's
    `context_files: [{path, summary, primary}]` entries; tests
    construct items directly to keep the variable layer decoupled.

    `path` is the path STRING with its original `kolsch:` or
    `workspace:` prefix so the model sees the same identifier the
    YAML uses. `summary` is the agent-provided summary or, when
    absent, the first non-empty line of the file's content. `primary`
    `True` inlines content; `False` lists path and summary only.
    `content` is held on the dataclass even for secondaries so an
    explicit `{{ docs.get(path).content }}` template still works.
    `size_bytes` is surfaced for advanced templates that want to
    gate inclusion on size.
    """

    path: str
    summary: str
    primary: bool
    content: str
    size_bytes: int


# ---------------------------------------------------------------------------
# DocsVar — the ``docs`` template variable.
# ---------------------------------------------------------------------------


_PRIMARY_SEPARATOR = "\n\n---\n\n"
_SECONDARIES_HEADER = "## Also available"


class DocsVar:
    """The `docs` template variable — list-shaped over `DocItem` with two tiers.

    Wraps a list of [`DocItem`][templating.vars.docs.DocItem] and
    exposes the two-tier partition (`primaries`, `secondaries`), the
    standardised method family (`list`, `summary`, `names`, `get`,
    `count`), the Pythonic protocols (`__iter__`, `__len__`,
    `__getitem__`, `__contains__`), and `__str__` defaulting to
    `list`. `count` covers both tiers; `summary` shows all items as
    one-line entries with no content. Insertion order is preserved
    across every accessor — the YAML's declaration order determines
    prompt order.
    """

    __slots__ = ("_by_path", "_items")

    def __init__(self, items: list[DocItem]) -> None:
        self._items: list[DocItem] = list(items)
        # Path-indexed for O(1) get / __contains__ / __getitem__.
        # Duplicate paths within a single agent's context_files would
        # be a config error caught upstream; if one ever reaches here,
        # the LATER entry wins (degenerate case).
        self._by_path: dict[str, DocItem] = {item.path: item for item in self._items}

    # ---- Two-tier partition -----------------------------------------------

    def primaries(self) -> str:
        r"""Render primary blocks: `### <path>\n<content>` between horizontal rules.

        The rule sits between blocks (not after) so the last primary
        stays clean.
        """
        return self._render_primaries(
            [item for item in self._items if item.primary],
        )

    def secondaries(self) -> str:
        """Render the secondary-tier listing — bullet entries with path and summary.

        Returns the empty string when no secondaries exist so a
        template chaining `{{ docs.secondaries() | default("none") }}`
        works as expected.
        """
        return self._render_secondaries(
            [item for item in self._items if not item.primary],
        )

    # ---- Standardised method family ---------------------------------------

    def list(self) -> str:
        """Render the full two-tier composition — primaries first, then secondaries.

        Primary blocks render first (via `_render_primaries`),
        followed by a blank line and the secondary "Also available"
        listing (via `_render_secondaries`) when present. Returns the
        empty string for an empty doc set; that is the intentional
        contribution to `{{ docs }}` for an agent declaring no
        `context_files`.
        """
        primaries = [item for item in self._items if item.primary]
        secondaries = [item for item in self._items if not item.primary]
        parts: list[str] = []
        if primaries:
            parts.append(self._render_primaries(primaries))
        if secondaries:
            parts.append(self._render_secondaries(secondaries))
        return "\n\n".join(parts)

    def summary(self) -> str:
        """Render one path-and-summary line per item across both tiers.

        Used in prompts that want a complete compact reference
        without inlining anything.
        """
        return "\n".join(f"- **{item.path}** — {item.summary}" for item in self._items)

    def names(self) -> str:
        """Comma-separated doc paths — the minimum-context form."""
        return ", ".join(item.path for item in self._items)

    def get(self, path: str) -> DocItem | None:
        """Look up one doc by its prefixed path; `None` when absent.

        Same un-Pythonic discipline as
        [`ToolsVar.get`][templating.vars.tools.ToolsVar.get] —
        `__getitem__` also returns `None` for a missing path rather
        than raising `KeyError`, so templates can pair access with
        Jinja's `| default("...")` filter.
        """
        return self._by_path.get(path)

    @property
    def count(self) -> int:
        """Total count across both tiers."""
        return len(self._items)

    # ---- Pythonic protocols -----------------------------------------------

    def __str__(self) -> str:
        """`{{ docs }}` defaults to `list`."""
        return self.list()

    def __iter__(self) -> Iterator[DocItem]:
        """Iterate over every item across both tiers in declaration order.

        Templates that want one tier use `primaries` or
        `secondaries` directly.
        """
        return iter(self._items)

    def __len__(self) -> int:
        """`len(docs)` / `{{ docs | length }}` returns `count` across both tiers."""
        return len(self._items)

    def __getitem__(self, path: str) -> DocItem | None:
        """Return the item for `path` or `None` — `get`-equivalent.

        Deliberately not the usual `KeyError` discipline; see the
        class docstring.
        """
        return self.get(path)

    def __contains__(self, path: object) -> bool:
        """Membership check by prefixed path."""
        return path in self._by_path

    # ---- Internal rendering helpers ---------------------------------------

    @staticmethod
    def _render_primaries(items: list[DocItem]) -> str:
        r"""Render primaries as `### <path>\n<content>` blocks joined by `---`.

        Empty input produces the empty string so callers can safely
        concatenate.
        """
        if not items:
            return ""
        return _PRIMARY_SEPARATOR.join(
            f"### {item.path}\n{item.content}" for item in items
        )

    @staticmethod
    def _render_secondaries(items: list[DocItem]) -> str:
        """Render secondaries as a bulleted "Also available" section.

        Header line followed by one `- **<path>** — <summary>` entry
        per item. Empty input produces the empty string.
        """
        if not items:
            return ""
        bullets = "\n".join(f"- **{item.path}** — {item.summary}" for item in items)
        return f"{_SECONDARIES_HEADER}\n\n{bullets}"
