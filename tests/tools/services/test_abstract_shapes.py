"""spec-001 Test 4 — ABC shapes + Protocol non-inheritance.

The design principle: `AbstractCards` does NOT inherit from `Cards`.
Structural typing carries the Protocol shape at every use site so
the ABC stays a plain no-op base for tests + platform impls. The
`__mro__` asserts pin the design so a future refactor cannot silently
flip to `AbstractCards(Cards)`.

The stub-raise asserts pin the other half: each ABC method / property
raises `NotImplementedError` by default, so a test subclass that
forgets to override an entry gets a loud failure instead of a silent
no-op.
"""

from __future__ import annotations

import pytest

from tools.services import (
    AbstractCards,
    AbstractPrompts,
    AbstractSessionView,
    AbstractWorkflows,
    Cards,
    Prompts,
    PromptSource,
    SessionView,
    Workflows,
)


def test_cards_protocol_not_in_abstract_cards_mro() -> None:
    assert Cards not in AbstractCards.__mro__


def test_prompts_protocol_not_in_abstract_prompts_mro() -> None:
    assert Prompts not in AbstractPrompts.__mro__


def test_workflows_protocol_not_in_abstract_workflows_mro() -> None:
    assert Workflows not in AbstractWorkflows.__mro__


def test_session_view_protocol_not_in_abstract_session_view_mro() -> None:
    assert SessionView not in AbstractSessionView.__mro__


class _CardsStub(AbstractCards):
    pass


class _PromptsStub(AbstractPrompts):
    pass


class _WorkflowsStub(AbstractWorkflows):
    pass


class _SessionViewStub(AbstractSessionView):
    """Trivial subclass — the abstract properties get bare stubs so we
    can instantiate and then call `read_context_file` to prove it
    still raises."""

    @property
    def session_id(self) -> str:
        return "ses_stub"

    @property
    def thread_id(self) -> str:
        return "th_stub"

    @property
    def agent_name(self) -> str:
        return "agent_stub"

    @property
    def agent_ref(self) -> str:
        return "kolsch/agents/stub"


@pytest.mark.asyncio
async def test_abstract_cards_methods_raise_not_implemented() -> None:
    stub = _CardsStub()
    with pytest.raises(NotImplementedError):
        await stub.open("title")
    with pytest.raises(NotImplementedError):
        stub.notice("hello")


@pytest.mark.asyncio
async def test_abstract_prompts_methods_raise_not_implemented() -> None:
    stub = _PromptsStub()
    source: PromptSource = {"kind": "tool", "id": "t1"}
    with pytest.raises(NotImplementedError):
        await stub.choice("pick", ["a", "b"], source=source)
    with pytest.raises(NotImplementedError):
        await stub.text("say", source=source)
    with pytest.raises(NotImplementedError):
        await stub.wait_for_slot(source=source)


@pytest.mark.asyncio
async def test_abstract_workflows_methods_raise_not_implemented() -> None:
    from uuid import uuid4

    from tools.services.workflows import WorkflowRunId

    # spec-012 uses the shipped `wrk_<uuid>` prefixed id; keep the
    # stub happy by minting a real one.
    rid = WorkflowRunId(f"wrk_{uuid4()}")

    stub = _WorkflowsStub()
    with pytest.raises(NotImplementedError):
        await stub.start("kolsch/workflows/x", {})
    with pytest.raises(NotImplementedError):
        # spec-012: `status` is synchronous.
        stub.status(rid)
    with pytest.raises(NotImplementedError):
        await stub.cancel(rid)


def test_abstract_session_view_read_context_file_raises_not_implemented() -> None:
    stub = _SessionViewStub()
    # Instantiable because the four abstract properties are overridden;
    # `read_context_file` is not marked abstract and defaults to raising.
    with pytest.raises(NotImplementedError):
        stub.read_context_file("some/path.md")
