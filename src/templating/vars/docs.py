"""The ``docs`` template variable — Phase 2.2 Step 4 (spec §4.4 + §6.3).

Two types:

- :class:`DocItem` — one context file's projection (path, summary,
  primary flag, content, size).
- :class:`DocsVar` — the collection wrapping ``list[DocItem]`` that
  the Jinja render context binds as ``docs``.

## Two-tier rendering

Phase 2.2 §6.3 distinguishes between **primary** and **secondary**
context files:

- **Primary** (``primary: true``) — content inlined in the
  rendered prompt under :meth:`DocsVar.list` /
  :meth:`DocsVar.primaries`. Costs context tokens every exchange;
  use for small, always-relevant content (an index, hard rules).
- **Secondary** (``primary: false``, default) — listed by path +
  summary only. Agent reads the file on demand via ``read_file``
  if it needs the content. Use for situationally-relevant
  content.

The :meth:`DocsVar.list` rendering composes both tiers — primary
blocks first, then an "Also available" bullet list for
secondaries — per spec §6.3.

## Format choice (build-plan Step 4 escalation gate)

The build plan flagged the default rendering as a potential
ambiguity. Spec §6.3 names the two-tier behaviour but leaves the
exact markdown to interpretation. The §3.5 explicit-form example
uses ``### <path>\\n<content>\\n---`` per primary, which informs
the canonical rendering chosen here:

- Each primary as ``### <path>\\n<content>``; primary blocks
  separated by ``\\n\\n---\\n\\n`` (a horizontal rule between
  blocks, NOT trailing — leaves the last primary clean).
- "## Also available\\n\\n- **<path>** — <summary>" bullet list
  for secondaries when any exist.
- Empty :meth:`DocsVar.list` for an empty doc set so an agent
  declaring no ``context_files`` contributes nothing to the
  prompt (Phase 2.2 §9's ``agent.template_warning`` mechanism
  surfaces the "references docs but has none" case separately
  at agent-load).

If the coordinator prefers a different markdown style (different
heading levels, dashes vs. asterisks, no horizontal rules, etc.),
:meth:`DocsVar._render_primaries` and
:meth:`DocsVar._render_secondaries` are the two seams.
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
    """One context file's projection — spec §4.4 attributes.

    Constructed by Step 12's ``context_files`` loader from the
    resolved agent config's ``context_files: [{path, summary,
    primary}]`` entries; tests construct items directly to keep
    the variable layer decoupled per the Step 3 reviewer's
    no-engine-coupling discipline.

    Fields:

    - ``path`` — the resolved path STRING (with its original
      ``kolsch:`` or ``workspace:`` prefix per spec §6.2).
      Surfaced in :meth:`DocsVar.list` headers and bullets so
      the model sees the same identifier the YAML uses.
    - ``summary`` — from ``context_files[].summary`` when the
      author provided one, otherwise the first non-empty line of
      the file's content (Step 12's loader resolves the
      fallback).
    - ``primary`` — boolean. ``True`` → content inlined;
      ``False`` → path + summary listing only.
    - ``content`` — the file's full text. For secondaries this
      is held in the dataclass so an explicit
      ``{{ docs.get(path).content }}`` template still works, but
      it is NOT inlined by the default :meth:`DocsVar.list`
      rendering.
    - ``size_bytes`` — the file's byte length; surfaced for
      advanced templates that want to gate inclusion on size.
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
    """The ``docs`` template variable — spec §4.4 + §6.3 + §4.2 + §4.3.

    Wraps a list of :class:`DocItem` and exposes:

    - The two-tier partition — :meth:`primaries`, :meth:`secondaries`.
    - The standardized method family — :meth:`list`, :meth:`summary`,
      :meth:`names`, :meth:`get`, :attr:`count`.
    - The Pythonic protocols — ``__iter__``, ``__len__``,
      ``__getitem__``, ``__contains__``.
    - ``__str__`` defaulting to :meth:`list`.

    :attr:`count` covers **both** tiers per spec §4.4 ("integer
    count (all tiers)"); :meth:`primaries` /
    :meth:`secondaries` partition; :meth:`summary` shows all
    items as one-line entries with NO content.

    Insertion order is preserved across every accessor (the
    YAML's declaration order determines prompt order).
    """

    __slots__ = ("_by_path", "_items")

    def __init__(self, items: list[DocItem]) -> None:
        self._items: list[DocItem] = list(items)
        # Path-indexed for O(1) get / __contains__ / __getitem__.
        # Duplicate paths within a single agent's context_files would
        # be a Layer 1 / Layer 3 config error caught upstream; if one
        # ever reaches here, the LATER entry wins (degenerate case).
        self._by_path: dict[str, DocItem] = {item.path: item for item in self._items}

    # ---- Two-tier partition (§4.4 + §6.3) ---------------------------------

    def primaries(self) -> str:
        """Render the primary-tier blocks: each as
        ``### <path>\\n<content>`` separated by a horizontal-rule
        block. Spec §3.5's explicit-form example informs this
        layout; the rule between blocks (not after) leaves the
        last primary clean."""
        return self._render_primaries(
            [item for item in self._items if item.primary],
        )

    def secondaries(self) -> str:
        """Render the secondary-tier listing: bullet entries with
        path + summary, no content. Returns the empty string when
        no secondaries exist so a template chaining
        ``{{ docs.secondaries() | default("none") }}`` works as
        expected."""
        return self._render_secondaries(
            [item for item in self._items if not item.primary],
        )

    # ---- Standardized method family (§4.2 + §4.4) -------------------------

    def list(self) -> str:
        """The full two-tier rendering — spec §6.3's "primaries
        inlined + secondaries listed in 'Also available'."

        Composition:

        - Primary blocks (when any) — via :meth:`_render_primaries`.
        - A blank line + "## Also available" + secondaries listing
          (when any) — via :meth:`_render_secondaries`.

        Returns the empty string for an empty doc set; that is
        the intentional contribution to ``{{ docs }}`` in a
        prompt for an agent declaring no ``context_files``.
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
        """All items (both tiers), one path + summary line per
        item — spec §4.4. Used in prompts that want a complete
        compact reference without inlining anything."""
        return "\n".join(f"- **{item.path}** — {item.summary}" for item in self._items)

    def names(self) -> str:
        """Comma-separated paths — spec §4.4's minimum-context
        form."""
        return ", ".join(item.path for item in self._items)

    def get(self, path: str) -> DocItem | None:
        """Look up one doc by its prefixed path; ``None`` when
        absent.

        Same un-Pythonic discipline as :class:`ToolsVar.get` —
        spec §4.3 mandates subscript-equivalence with this
        method, so :meth:`__getitem__` also returns ``None`` for
        a missing path rather than raising :class:`KeyError`.
        """
        return self._by_path.get(path)

    @property
    def count(self) -> int:
        """Total count across both tiers — spec §4.4 explicitly
        says ``count`` is "all tiers", not just primaries."""
        return len(self._items)

    # ---- Pythonic protocols (§4.3) ----------------------------------------

    def __str__(self) -> str:
        """``{{ docs }}`` defaults to :meth:`list` per spec §4.2."""
        return self.list()

    def __iter__(self) -> Iterator[DocItem]:
        """``for d in docs`` iterates over every item — both tiers
        — in declaration order. Templates that want one tier use
        :meth:`primaries` / :meth:`secondaries` directly."""
        return iter(self._items)

    def __len__(self) -> int:
        """``len(docs)`` / ``{{ docs | length }}`` returns the
        total count across both tiers (matches :attr:`count`)."""
        return len(self._items)

    def __getitem__(self, path: str) -> DocItem | None:
        """``docs["kolsch:docs/index.md"]`` returns the item or
        ``None`` — :meth:`get`-equivalent per spec §4.3 (NOT the
        usual ``KeyError`` discipline)."""
        return self.get(path)

    def __contains__(self, path: object) -> bool:
        """``"kolsch:AGENTS.md" in docs`` — membership check by
        prefixed path."""
        return path in self._by_path

    # ---- Internal rendering helpers ---------------------------------------

    @staticmethod
    def _render_primaries(items: list[DocItem]) -> str:
        """Render a list of primaries as ``### <path>\\n<content>``
        blocks separated by ``\\n\\n---\\n\\n``. Empty input
        produces the empty string so callers can safely
        concatenate."""
        if not items:
            return ""
        return _PRIMARY_SEPARATOR.join(
            f"### {item.path}\n{item.content}" for item in items
        )

    @staticmethod
    def _render_secondaries(items: list[DocItem]) -> str:
        """Render a list of secondaries as a bulleted "Also
        available" section: a header line followed by one
        ``- **<path>** — <summary>`` entry per item. Empty input
        produces the empty string."""
        if not items:
            return ""
        bullets = "\n".join(f"- **{item.path}** — {item.summary}" for item in items)
        return f"{_SECONDARIES_HEADER}\n\n{bullets}"
