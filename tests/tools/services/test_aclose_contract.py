"""spec-003 Test 4 — ABC `aclose()` no-op contract.

Four passes per the spec: `AbstractCards.aclose()` returns `None`;
second call also returns `None`; same shape on `AbstractPrompts`,
`AbstractWorkflows`, `AbstractSessionView`.

Fail means: method missing on any ABC, not async, or returns
something other than `None`.
"""

from __future__ import annotations

import inspect

import pytest

from tools.services import (
    AbstractCards,
    AbstractPrompts,
    AbstractSessionView,
    AbstractWorkflows,
)


class _CardsStub(AbstractCards):
    pass


class _PromptsStub(AbstractPrompts):
    pass


class _WorkflowsStub(AbstractWorkflows):
    pass


class _SessionViewStub(AbstractSessionView):
    """Trivial subclass — override the abstract properties so we can
    instantiate. `aclose` is inherited from the ABC."""

    @property
    def session_id(self) -> str:
        return "ses_stub"

    @property
    def thread_id(self) -> str:
        return "th_stub"

    @property
    def agent_name(self) -> str:
        return "agent"

    @property
    def agent_ref(self) -> str:
        return "kolsch/agents/stub"


@pytest.mark.asyncio
async def test_abstract_cards_aclose_returns_none_idempotent() -> None:
    stub = _CardsStub()
    # aclose is async.
    assert inspect.iscoroutinefunction(stub.aclose)
    assert await stub.aclose() is None
    # Second call is also None (idempotent no-op).
    assert await stub.aclose() is None


@pytest.mark.asyncio
async def test_abstract_prompts_aclose_returns_none_idempotent() -> None:
    stub = _PromptsStub()
    assert inspect.iscoroutinefunction(stub.aclose)
    assert await stub.aclose() is None
    assert await stub.aclose() is None


@pytest.mark.asyncio
async def test_abstract_workflows_aclose_returns_none_idempotent() -> None:
    stub = _WorkflowsStub()
    assert inspect.iscoroutinefunction(stub.aclose)
    assert await stub.aclose() is None
    assert await stub.aclose() is None


@pytest.mark.asyncio
async def test_abstract_session_view_aclose_returns_none_idempotent() -> None:
    stub = _SessionViewStub()
    assert inspect.iscoroutinefunction(stub.aclose)
    assert await stub.aclose() is None
    assert await stub.aclose() is None
