"""``@tool(summary=...)`` decorator tests — Phase 2.2 Step 11.

Pins:

- Explicit ``summary`` is stored verbatim.
- ``summary=None`` (default) derives from ``description``: the
  substring through the first period, trimmed.
- A description without any period → the full description (trimmed)
  is the summary.
- :class:`ToolDefinition.summary` is the canonical metadata field.
"""

from __future__ import annotations

from pydantic import BaseModel

from tools.decorator import get_tool_definition, tool


class _EmptyArgs(BaseModel):
    pass


def test_explicit_summary_stored_verbatim() -> None:
    @tool(
        name="t1",
        description="A long, detailed description.",
        args_schema=_EmptyArgs,
        summary="Short summary.",
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.summary == "Short summary."


def test_default_summary_derived_from_first_sentence() -> None:
    """Spec §5.1: when ``summary`` is omitted, the platform takes
    the substring through the first period (trimmed)."""

    @tool(
        name="t2",
        description=(
            "Read a file's text. When to use: blah blah blah. More details follow."
        ),
        args_schema=_EmptyArgs,
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.summary == "Read a file's text."


def test_summary_default_when_description_has_no_period() -> None:
    """Edge case: a description without any period → the full
    string (trimmed) becomes the summary."""

    @tool(
        name="t3",
        description="A short description with no terminator",
        args_schema=_EmptyArgs,
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.summary == "A short description with no terminator"


def test_summary_strips_surrounding_whitespace() -> None:
    """Derivation trims the substring — useful when descriptions
    start with ``\\n`` or have leading spaces from triple-quoted
    string indentation."""

    @tool(
        name="t4",
        description="\n   Do a thing.   Then explain.",
        args_schema=_EmptyArgs,
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.summary == "Do a thing."
