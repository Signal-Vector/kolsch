"""bug-025 Test 1 — ``PromptResponse`` carries the persist-rule dict.

Covers AC #2. Exercises the ``str``-subclass shape:

1. Fast path — bare choice-key response (``y``/``n``) surfaces as a
   ``PromptResponse`` with ``rule=None`` and stays ``str``-equal to
   the plain key.
2. Persist path — ``resolve_pending(prompt_id, "a", rule={...})``
   surfaces as a ``PromptResponse`` carrying the widget-built Rule
   dict, still comparing ``== "a"`` for backward-compat.
3. Backward-compat — reading ``.key`` gives back a plain ``str``;
   ``isinstance(x, str)`` is True; f-string embeds the key verbatim.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cli.chat.services.prompts_impl import PromptResponse, PromptsImpl
from tools.services.prompts import PromptSource
from store import SessionStore, SqliteBackend


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk_impl(store: SessionStore) -> PromptsImpl:
    session = await store.create_session("ag_test", name="prompts")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    return PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_choice_returns_prompt_response_with_key(
    store: SessionStore,
) -> None:
    """Bare ``AuthzEffect`` path: ``PromptResponse(key="y", rule=None)``."""
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "t1"}
    choice_task = asyncio.create_task(
        impl.choice("Q?", ["y", "n"], source=source),
    )
    # Wait for the pending future to appear.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if impl._pending_prompt_id is not None:
            break
    pending_id = impl._pending_prompt_id
    assert pending_id is not None
    impl.resolve_pending(str(pending_id), "y")
    result = await choice_task
    assert isinstance(result, PromptResponse)
    assert isinstance(result, str)  # backward-compat str-subclass
    assert result == "y"
    assert result.key == "y"
    assert result.rule is None


@pytest.mark.asyncio
async def test_choice_returns_prompt_response_with_rule_dict(
    store: SessionStore,
) -> None:
    """Tuple path: ``PromptResponse(key="a", rule={...})``."""
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "t2"}
    choice_task = asyncio.create_task(
        impl.choice("Q?", ["y", "a", "n", "d"], source=source),
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        if impl._pending_prompt_id is not None:
            break
    pending_id = impl._pending_prompt_id
    assert pending_id is not None
    rule_dict = {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"}
    impl.resolve_pending(str(pending_id), "a", rule=rule_dict)
    result = await choice_task
    assert isinstance(result, PromptResponse)
    assert result == "a"  # str comparison still works
    assert result.rule == rule_dict
    assert result.key == "a"


def test_choice_backward_compat_string_read() -> None:
    """Old callers that read ``.key`` or compare to a string still work."""
    r = PromptResponse("y", rule=None)
    # Every legacy comparison shape:
    assert r == "y"
    assert r != "n"
    assert isinstance(r, str)
    assert str(r) == "y"
    assert f"got {r}" == "got y"
    assert r.startswith("y")
    # And the explicit accessor:
    assert r.key == "y"
    assert isinstance(r.key, str)
    # A `PromptResponse` with a rule still string-compares by the key
    # only — the rule is metadata, not part of the equality.
    r_rule = PromptResponse("a", rule={"what": "x", "pattern": "*", "effect": "allow"})
    assert r_rule == "a"
