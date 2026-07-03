"""``DocsVar`` + ``DocItem`` unit tests — Phase 2.2 Step 4 (spec
§4.4 + §6.3 + §4.2 + §4.3).

Pins the two-tier rendering contract Step 12's ``context_files``
loader and Step 14's engine pre-flight both consume:

- Constructor takes an ordered list of :class:`DocItem`.
- :meth:`primaries` and :meth:`secondaries` partition the items.
- :meth:`list` composes the full two-tier rendering (primary
  blocks + "Also available" listing).
- :meth:`summary` shows all items (both tiers) as one-line
  entries with NO content.
- All four Pythonic protocols work, indexed by ``path``.

Constructing :class:`DocItem` instances directly verifies the
Step 3-vintage decoupling check: ``DocsVar`` works against
arbitrary item lists with no dependency on the engine or the
asset / templating loader.
"""

from __future__ import annotations

import pytest

from templating.vars.docs import DocItem, DocsVar

# ---------------------------------------------------------------------------
# Fixtures — 2 primary + 2 secondary mock docs per the build plan.
# ---------------------------------------------------------------------------


def _make_items() -> list[DocItem]:
    """Two primaries + two secondaries in deliberate interleaved
    order (primary → secondary → primary → secondary) so the
    tests below verify both the per-tier filtering AND that
    iteration preserves declaration order across the partition.
    """
    return [
        DocItem(
            path="kolsch:docs/index.md",
            summary="Master index of all Kolsch documentation.",
            primary=True,
            content="# Index\n\nThis is the index content.",
            size_bytes=42,
        ),
        DocItem(
            path="workspace:CONVENTIONS.md",
            summary="This workspace's coding conventions.",
            primary=False,
            content="(read on demand — content NOT inlined)",
            size_bytes=128,
        ),
        DocItem(
            path="kolsch:AGENTS.md",
            summary="Operating rules for AI agents.",
            primary=True,
            content="# AGENTS\n\nRules go here.",
            size_bytes=32,
        ),
        DocItem(
            path="kolsch:docs/explanation/agent-engine-contract.md",
            summary="Engine invariants and exchange flow.",
            primary=False,
            content="(read on demand — content NOT inlined)",
            size_bytes=512,
        ),
    ]


@pytest.fixture
def docs_var() -> DocsVar:
    return DocsVar(_make_items())


# ---------------------------------------------------------------------------
# Constructor + .count (both tiers).
# ---------------------------------------------------------------------------


def test_constructor_accepts_list_of_doc_items(docs_var: DocsVar) -> None:
    """Smoke floor — four items in (2 primary + 2 secondary), four
    items reachable."""
    assert docs_var.count == 4


def test_count_covers_both_tiers_not_just_primaries() -> None:
    """Spec §4.4 explicitly says ``count`` is "integer count (all
    tiers)" — both primaries AND secondaries. Pinning so a future
    refactor that accidentally narrows it to primaries-only fails
    this test."""
    items = _make_items()
    primaries_only = [d for d in items if d.primary]
    assert DocsVar(items).count == 4
    assert DocsVar(primaries_only).count == 2


def test_count_zero_for_empty() -> None:
    """``count`` is an integer attribute, not a method. An empty
    var still has ``.count = 0`` — ``{{ docs.count }}`` in an
    agent with no context_files renders as ``0``."""
    assert DocsVar([]).count == 0


# ---------------------------------------------------------------------------
# .primaries / .secondaries partition.
# ---------------------------------------------------------------------------


def test_primaries_renders_only_primary_items(docs_var: DocsVar) -> None:
    """Spec §6.3: primaries inlined with full content. The
    rendered output should include each primary's path + content
    and NEITHER secondary."""
    rendered = docs_var.primaries()
    assert "### kolsch:docs/index.md" in rendered
    assert "This is the index content." in rendered
    assert "### kolsch:AGENTS.md" in rendered
    assert "Rules go here." in rendered
    # Secondaries' paths must not appear in the primaries
    # rendering — they belong in the "Also available" section.
    assert "workspace:CONVENTIONS.md" not in rendered
    assert "agent-engine-contract.md" not in rendered


def test_primaries_uses_horizontal_rule_separator(docs_var: DocsVar) -> None:
    """Step 4 format-choice pin — primary blocks separated by
    ``\\n\\n---\\n\\n`` (a horizontal rule). The build-plan Step 4
    escalation flagged this format as potentially ambiguous; this
    test pins the chosen layout so a future style change is a
    deliberate edit."""
    rendered = docs_var.primaries()
    assert "\n\n---\n\n" in rendered


def test_primaries_no_trailing_separator(docs_var: DocsVar) -> None:
    """The horizontal rule sits BETWEEN primary blocks, not after
    the last one — leaves the rendering clean for composition with
    the "Also available" section below."""
    rendered = docs_var.primaries()
    assert not rendered.endswith("\n---\n")
    assert not rendered.endswith("---")


def test_primaries_renders_empty_for_no_primaries() -> None:
    """Empty primaries list → empty string. Callers can compose
    safely; ``{{ docs.primaries() }}`` for a secondaries-only
    agent contributes nothing."""
    items = [d for d in _make_items() if not d.primary]
    assert DocsVar(items).primaries() == ""


def test_secondaries_renders_path_and_summary_no_content(docs_var: DocsVar) -> None:
    """Spec §6.3: secondaries listed by path + summary only — the
    file content must NOT appear in the secondary rendering. The
    agent reads the file on demand via ``read_file``."""
    rendered = docs_var.secondaries()
    assert "workspace:CONVENTIONS.md" in rendered
    assert "This workspace's coding conventions." in rendered
    assert "kolsch:docs/explanation/agent-engine-contract.md" in rendered
    assert "Engine invariants and exchange flow." in rendered
    # The placeholder content strings must NOT appear in the
    # secondaries rendering — that's the load-bearing two-tier
    # discipline.
    assert "read on demand" not in rendered


def test_secondaries_uses_also_available_header(docs_var: DocsVar) -> None:
    """Pin the section header text so a future refactor that
    drops it (or renames to "Other docs", "See also", etc.)
    breaks loudly and forces a deliberate choice."""
    assert docs_var.secondaries().startswith("## Also available")


def test_secondaries_renders_empty_for_no_secondaries() -> None:
    """Mirror of the primaries-empty case."""
    items = [d for d in _make_items() if d.primary]
    assert DocsVar(items).secondaries() == ""


# ---------------------------------------------------------------------------
# .list — the full two-tier rendering (spec §6.3).
# ---------------------------------------------------------------------------


def test_list_includes_primary_content_and_secondary_path_summary(
    docs_var: DocsVar,
) -> None:
    """The headline contract — :meth:`DocsVar.list` shows
    primaries inlined with content AND secondaries listed by
    path + summary. The Build Plan Step 4 acceptance check
    against the spec §6.3 layout."""
    rendered = docs_var.list()
    # Primaries — full path + content.
    assert "### kolsch:docs/index.md" in rendered
    assert "This is the index content." in rendered
    assert "### kolsch:AGENTS.md" in rendered
    # Secondaries — path + summary only.
    assert "workspace:CONVENTIONS.md" in rendered
    assert "This workspace's coding conventions." in rendered
    # Secondary CONTENT must NOT leak into the rendered prompt.
    assert "read on demand" not in rendered


def test_list_composes_primaries_block_then_secondaries(docs_var: DocsVar) -> None:
    """Spec §6.3 ordering: primary blocks first, then the "Also
    available" section. Pinning the order so a future refactor
    that swaps them breaks loudly."""
    rendered = docs_var.list()
    primary_index = rendered.index("### kolsch:docs/index.md")
    secondaries_index = rendered.index("## Also available")
    assert primary_index < secondaries_index


def test_list_renders_empty_for_no_docs() -> None:
    """An agent declaring no ``context_files`` contributes nothing
    to its prompt's ``{{ docs }}`` slot. The
    :event:`agent.template_warning` mechanism is what catches the
    "references docs but has none" case at agent-load
    (Phase 2.2 §9)."""
    assert DocsVar([]).list() == ""


def test_list_with_primaries_only_omits_also_available_section() -> None:
    """A primaries-only doc set renders without the "Also
    available" header — the secondaries section should be entirely
    absent rather than a stub heading with no entries."""
    items = [d for d in _make_items() if d.primary]
    rendered = DocsVar(items).list()
    assert "## Also available" not in rendered
    assert "### kolsch:docs/index.md" in rendered


def test_list_with_secondaries_only_omits_primary_blocks() -> None:
    """Symmetric counterpart — a secondaries-only doc set renders
    just the "Also available" section, no ``### `` headers."""
    items = [d for d in _make_items() if not d.primary]
    rendered = DocsVar(items).list()
    assert rendered.startswith("## Also available")
    assert "### " not in rendered  # no primary path headers


# ---------------------------------------------------------------------------
# .summary — one-line entries for ALL tiers (spec §4.4).
# ---------------------------------------------------------------------------


def test_summary_includes_all_items_both_tiers(docs_var: DocsVar) -> None:
    """Spec §4.4 ``.summary()`` is "all docs (both tiers), path +
    summary, no content" — distinct from
    :meth:`DocsVar.secondaries`, which is secondaries-only."""
    rendered = docs_var.summary()
    # Every path appears (both primary and secondary).
    assert "kolsch:docs/index.md" in rendered
    assert "kolsch:AGENTS.md" in rendered
    assert "workspace:CONVENTIONS.md" in rendered
    assert "kolsch:docs/explanation/agent-engine-contract.md" in rendered
    # And every summary.
    assert "Master index of all Kolsch documentation." in rendered
    assert "Engine invariants and exchange flow." in rendered
    # No content leaks (the load-bearing two-tier discipline).
    assert "This is the index content." not in rendered
    assert "read on demand" not in rendered


def test_summary_produces_one_line_per_item(docs_var: DocsVar) -> None:
    """Newline-delimited bullet list, exactly one line per item.
    Pinning the line count so a future refactor that adds blank
    lines (or merges entries) breaks loudly."""
    rendered = docs_var.summary()
    lines = [line for line in rendered.split("\n") if line]
    assert len(lines) == 4


# ---------------------------------------------------------------------------
# .names — comma-separated paths.
# ---------------------------------------------------------------------------


def test_names_renders_comma_separated_paths(docs_var: DocsVar) -> None:
    """Spec §4.4 ``.names()`` format. Order matches declaration
    order — primary/secondary partition does NOT affect this."""
    assert docs_var.names() == (
        "kolsch:docs/index.md, "
        "workspace:CONVENTIONS.md, "
        "kolsch:AGENTS.md, "
        "kolsch:docs/explanation/agent-engine-contract.md"
    )


# ---------------------------------------------------------------------------
# .get / __getitem__ — same surface; subscript = get per spec §4.3.
# ---------------------------------------------------------------------------


def test_get_returns_item_for_known_path(docs_var: DocsVar) -> None:
    """``.get("kolsch:AGENTS.md")`` returns the corresponding
    :class:`DocItem`. Lookup uses the exact prefixed path — no
    normalisation, no prefix-stripping."""
    item = docs_var.get("kolsch:AGENTS.md")
    assert item is not None
    assert item.path == "kolsch:AGENTS.md"
    assert item.primary is True


def test_get_returns_none_for_unknown_path(docs_var: DocsVar) -> None:
    """Spec §4.2 ``.get()`` discipline — ``None`` for absent keys
    so ``{{ docs.get("ghost.md") | default(noop) }}`` renders the
    noop instead of crashing."""
    assert docs_var.get("kolsch:ghost.md") is None


def test_subscript_equals_get(docs_var: DocsVar) -> None:
    """Spec §4.3: ``docs["kolsch:AGENTS.md"]`` is equivalent to
    ``.get("kolsch:AGENTS.md")``. Object identity check pins the
    spec-mandated equivalence."""
    assert docs_var["kolsch:AGENTS.md"] is docs_var.get("kolsch:AGENTS.md")


def test_subscript_returns_none_for_unknown_no_raise(docs_var: DocsVar) -> None:
    """The same deliberately un-Pythonic spec §4.3 contract as
    :class:`ToolsVar`. Templates that want exception-style lookup
    iterate via ``{% for d in docs %}…{% endfor %}`` or check via
    ``'path' in docs`` first."""
    assert docs_var["kolsch:nonexistent.md"] is None


# ---------------------------------------------------------------------------
# Pythonic protocols.
# ---------------------------------------------------------------------------


def test_iteration_covers_both_tiers_in_declaration_order(docs_var: DocsVar) -> None:
    """``for d in docs`` iterates over BOTH primaries and
    secondaries in the order they were declared. Templates that
    want one tier use :meth:`primaries` / :meth:`secondaries`
    directly. Pinning interleaved-order so a future refactor that
    groups primaries-first via iteration breaks loudly."""
    paths = [item.path for item in docs_var]
    assert paths == [
        "kolsch:docs/index.md",
        "workspace:CONVENTIONS.md",
        "kolsch:AGENTS.md",
        "kolsch:docs/explanation/agent-engine-contract.md",
    ]


def test_len_returns_count_across_both_tiers(docs_var: DocsVar) -> None:
    """``len(docs)`` matches :attr:`count` and covers both tiers."""
    assert len(docs_var) == 4
    assert len(docs_var) == docs_var.count


def test_contains_true_for_known_path(docs_var: DocsVar) -> None:
    """``"kolsch:AGENTS.md" in docs`` — used for gates like
    ``{% if 'kolsch:AGENTS.md' in docs %}…{% endif %}``"""
    assert "kolsch:AGENTS.md" in docs_var


def test_contains_false_for_unknown_path(docs_var: DocsVar) -> None:
    """Negative side of the membership check."""
    assert "kolsch:never_declared.md" not in docs_var


def test_contains_handles_non_string_without_raising(docs_var: DocsVar) -> None:
    """``__contains__`` delegates to :meth:`dict.__contains__`,
    which accepts any object and returns ``False`` rather than
    raising. Pinning so a template that passes
    ``{{ None in docs }}`` produces ``False`` cleanly."""
    assert (None in docs_var) is False


# ---------------------------------------------------------------------------
# __str__ = .list() (spec §4.2).
# ---------------------------------------------------------------------------


def test_str_defaults_to_list_rendering(docs_var: DocsVar) -> None:
    """``{{ docs }}`` in a Jinja template is shorthand for
    ``{{ docs.list() }}`` per spec §4.2. Pinning so a future
    change to ``__str__`` breaks this test loudly."""
    assert str(docs_var) == docs_var.list()
