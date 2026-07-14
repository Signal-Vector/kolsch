"""spec-001 Test 1 — `@tool(requires=[...])` decorator shape."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tools.decorator import get_tool_definition, tool


class _EmptyArgs(BaseModel):
    pass


def test_requires_stored_on_tool_definition() -> None:
    """`@tool(requires=["cards"])` -> `ToolDefinition.requires == ["cards"]`."""

    @tool(
        name="t_requires_cards",
        description="A tool that requires the cards service.",
        args_schema=_EmptyArgs,
        requires=["cards"],
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.requires == ["cards"]


def test_unknown_requires_raises_value_error() -> None:
    """`requires=["nonsense"]` raises `ValueError` at decoration time."""
    with pytest.raises(ValueError, match="nonsense"):

        @tool(
            name="t_bad_requires",
            description="Should fail at decoration.",
            args_schema=_EmptyArgs,
            requires=["nonsense"],  # type: ignore[list-item]
        )
        async def fn() -> None:
            return None


def test_requires_defaults_to_empty_list() -> None:
    """Omitting `requires` -> `ToolDefinition.requires == []`."""

    @tool(
        name="t_no_requires",
        description="A tool without a requires clause.",
        args_schema=_EmptyArgs,
    )
    async def fn() -> None:
        return None

    defn = get_tool_definition(fn)
    assert defn.requires == []
