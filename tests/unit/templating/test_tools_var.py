"""``ToolsVar`` + ``ToolItem`` unit tests — Phase 2.2 Step 3
(spec §4.4 + §4.2 + §4.3).

Pins the contract Step 14's engine pre-flight and Step 11's
``@tool`` extension both depend on:

- Constructor takes an ordered list of :class:`ToolItem`.
- All five standardized methods (``.list()`` / ``.summary()`` /
  ``.names()`` / ``.get()`` / ``.count``) produce the spec §4.4
  output formats.
- All four Pythonic protocols (``__iter__`` / ``__len__`` /
  ``__getitem__`` / ``__contains__``) work.
- ``__str__`` defaults to ``.list()`` so ``{{ tools }}`` in
  templates renders as ``{{ tools.list() }}``.
- Insertion order is preserved across every accessor.
- The un-Pythonic ``__getitem__`` returning ``None`` (NOT raising
  :class:`KeyError`) for missing keys — the spec §4.3 contract.

Constructing :class:`ToolItem` instances directly here verifies
the Step 3 reviewer's decoupling check: ``ToolsVar`` works
against arbitrary item lists with no dependency on the engine
or tools subsystem.
"""

from __future__ import annotations

import pytest

from templating.vars.tools import ToolItem, ToolsVar


# ---------------------------------------------------------------------------
# Fixtures — three mock items per the build plan's sample.
# ---------------------------------------------------------------------------


def _make_items() -> list[ToolItem]:
    """Three representative items in deliberate declaration order
    (read_file → bash → list_files). Order is load-bearing — the
    tests below assert that EVERY accessor preserves it."""
    return [
        ToolItem(
            name="read_file",
            description="Read a file's text contents verbatim. Use for see / show / cat.",
            summary="Read a file's text contents.",
            signature="path: str, max_bytes: int = 1048576",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer", "default": 1048576},
                },
                "required": ["path"],
            },
        ),
        ToolItem(
            name="bash",
            description="Run a shell command and return stdout / stderr / exit code.",
            summary="Run a shell command.",
            signature="command: str, timeout_seconds: int = 30",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 30},
                },
                "required": ["command"],
            },
        ),
        ToolItem(
            name="list_files",
            description="List paths matching a glob under the workspace.",
            summary="List files under a glob.",
            signature="pattern: str, max_entries: int = 500",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "max_entries": {"type": "integer", "default": 500},
                },
                "required": ["pattern"],
            },
        ),
    ]


@pytest.fixture
def tools_var() -> ToolsVar:
    return ToolsVar(_make_items())


# ---------------------------------------------------------------------------
# Constructor + .count.
# ---------------------------------------------------------------------------


def test_constructor_accepts_list_of_tool_items(tools_var: ToolsVar) -> None:
    """Smoke floor — the spec §4.4 entry point. Three items in →
    three items reachable. Pins the constructor signature so a
    refactor that tries to make ``items`` keyword-only would
    fail this test."""
    assert tools_var.count == 3


def test_count_matches_item_list_length() -> None:
    """``count`` is the spec §4.2 ``.count`` attribute — integer,
    not a method (no parens). An empty var still has ``.count = 0``
    rather than raising; ``{{ tools.count }}`` in an agent with no
    tools renders as ``0``."""
    assert ToolsVar([]).count == 0
    assert ToolsVar(_make_items()[:1]).count == 1


# ---------------------------------------------------------------------------
# .list / .summary / .names format contracts.
# ---------------------------------------------------------------------------


def test_list_renders_markdown_blocks_per_spec_format(tools_var: ToolsVar) -> None:
    """Spec §4.4 ``.list()`` format: each tool as
    ``### <name>(<signature>)\\n<description>`` separated by a
    blank line. Pins the exact concatenation so a future refactor
    of the format breaks loudly."""
    rendered = tools_var.list()
    # Each tool's header block lands as its own ### section.
    assert "### read_file(path: str, max_bytes: int = 1048576)" in rendered
    assert "Read a file's text contents verbatim" in rendered
    assert "### bash(command: str, timeout_seconds: int = 30)" in rendered
    assert "Run a shell command and return stdout / stderr / exit code." in rendered
    assert "### list_files(pattern: str, max_entries: int = 500)" in rendered
    # Blocks separated by a blank line so markdown renders them as
    # independent sections in the system prompt.
    blocks = rendered.split("\n\n")
    assert len(blocks) == 3


def test_list_renders_empty_for_no_tools() -> None:
    """An empty tool set renders as the empty string — contributes
    nothing to the prompt. The :event:`agent.template_warning`
    mechanism catches the "agent declares no tools but references
    {{ tools }}" case at agent-load (Phase 2.2 §9), not here."""
    assert ToolsVar([]).list() == ""


def test_summary_renders_one_line_per_tool(tools_var: ToolsVar) -> None:
    """Spec §4.4 ``.summary()`` format: ``<name>: <summary>`` per
    tool, newline-separated. Pins the format so future
    customizations (e.g. adding an emoji prefix) require an
    explicit choice."""
    rendered = tools_var.summary()
    lines = rendered.split("\n")
    assert lines == [
        "read_file: Read a file's text contents.",
        "bash: Run a shell command.",
        "list_files: List files under a glob.",
    ]


def test_names_renders_comma_separated(tools_var: ToolsVar) -> None:
    """Spec §4.4 ``.names()`` format: comma + space separator.
    Used in prompts like ``"You have tools: {{ tools.names() }}."``"""
    assert tools_var.names() == "read_file, bash, list_files"


def test_names_renders_empty_for_no_tools() -> None:
    """Mirror of .list() for the empty case."""
    assert ToolsVar([]).names() == ""


# ---------------------------------------------------------------------------
# .get / __getitem__ — same surface; subscript = get per spec §4.3.
# ---------------------------------------------------------------------------


def test_get_returns_item_for_known_name(tools_var: ToolsVar) -> None:
    """``.get("bash")`` returns the corresponding :class:`ToolItem`.
    Pins that lookup honours the exact name as declared (no
    normalisation, no case-fold)."""
    item = tools_var.get("bash")
    assert item is not None
    assert item.name == "bash"
    assert item.summary == "Run a shell command."


def test_get_returns_none_for_unknown_name(tools_var: ToolsVar) -> None:
    """The spec §4.2 ``.get()`` contract: ``None`` for absent keys
    so ``{{ tools.get('ghost') | default(noop) }}`` renders the
    noop instead of crashing."""
    assert tools_var.get("ghost") is None


def test_subscript_equals_get(tools_var: ToolsVar) -> None:
    """Spec §4.3: ``tools["bash"]`` is equivalent to
    ``.get("bash")``. Object identity check pins the
    spec-mandated equivalence."""
    assert tools_var["bash"] is tools_var.get("bash")


def test_subscript_returns_none_for_unknown_no_raise(tools_var: ToolsVar) -> None:
    """**The deliberately un-Pythonic contract** — spec §4.3 says
    subscript = .get(), so ``tools["nonexistent"]`` returns
    ``None`` rather than raising :class:`KeyError`. Templates
    that want assertion-style lookup should iterate via
    ``{% for t in tools %}…{% endfor %}`` or check via
    ``'name' in tools`` first."""
    assert tools_var["nonexistent"] is None


# ---------------------------------------------------------------------------
# Pythonic protocols.
# ---------------------------------------------------------------------------


def test_iteration_preserves_declaration_order(tools_var: ToolsVar) -> None:
    """``for t in tools`` iterates over items in the order the
    list was constructed. Templates use this for
    ``{% for t in tools %}- {{ t.name }}{% endfor %}`` — order
    matters because agent authors arrange tools intentionally."""
    names = [item.name for item in tools_var]
    assert names == ["read_file", "bash", "list_files"]


def test_len_returns_count(tools_var: ToolsVar) -> None:
    """``len(tools)`` and ``{{ tools | length }}`` both go through
    ``__len__``. Pins that ``__len__`` returns the same value as
    the ``.count`` attribute."""
    assert len(tools_var) == 3
    assert len(tools_var) == tools_var.count


def test_contains_true_for_known_name(tools_var: ToolsVar) -> None:
    """``"bash" in tools`` — used in templates like
    ``{% if 'bash' in tools %}…{% endif %}``."""
    assert "bash" in tools_var


def test_contains_false_for_unknown_name(tools_var: ToolsVar) -> None:
    """Negative side of the membership check — used to gate
    optional behaviour like
    ``{% if 'sql' in tools %}You may also query the DB.{% endif %}``"""
    assert "sql_query" not in tools_var


def test_contains_handles_non_string_without_raising(tools_var: ToolsVar) -> None:
    """``__contains__`` delegates to :meth:`dict.__contains__`
    which accepts any object as the comparison key and returns
    ``False`` rather than raising. Pinning so a template that
    accidentally passes ``{{ 42 in tools }}`` produces
    ``False`` cleanly instead of a render error."""
    assert (42 in tools_var) is False


# ---------------------------------------------------------------------------
# __str__ = .list() (spec §4.2).
# ---------------------------------------------------------------------------


def test_str_defaults_to_list_rendering(tools_var: ToolsVar) -> None:
    """``{{ tools }}`` in a Jinja template is shorthand for
    ``{{ tools.list() }}`` per spec §4.2. Pinning so a future
    change to ``__str__`` (e.g. someone making it ``.summary()``)
    breaks this test loudly and forces a design review."""
    assert str(tools_var) == tools_var.list()
