"""bug-024 Test 1 (AC #5) — `_ObservedToolHandle.description` populated.

Pins the Layer 2 fix that adds `description: str` to the ToolHandle
Protocol and populates it on the observer wrapper. Before this
change, the framework saw `{name, schema}` only and every advertised
tool shipped to the LLM as `"Tool: <name>"` — Kimi K2.6 then
preferred `bash` for anything spawnable as a subprocess.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from engine.observers import _ObservedToolHandle
from tools.decorator import ToolDefinition


class _EmptyArgs(BaseModel):
    """No-arg schema — the description ships regardless of args shape."""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — unused
    return None


async def _emit(*_: Any, **__: Any) -> None:  # pragma: no cover — unused
    return None


def test_observed_handle_carries_description() -> None:
    """The observer's `description` mirrors the ToolDefinition's."""
    defn = ToolDefinition(
        name="do_x",
        description="Do X. Prefer this over calling bash.",
        args_schema=_EmptyArgs,
        func=_noop_fn,
    )

    handle = _ObservedToolHandle(
        emit=_emit,
        agent_id="ag_test",
        session_id="ses_test",
        thread_id="th_test",
        tools_subsystem=object(),  # unused by attribute access
        defn=defn,
    )

    assert handle.name == "do_x"
    assert handle.description == "Do X. Prefer this over calling bash."
    # Regression: the description key must not be the `"Tool: <name>"`
    # placeholder the downstream OpenAI-spec adapter falls back to when
    # it's missing.
    assert handle.description != f"Tool: {handle.name}"
