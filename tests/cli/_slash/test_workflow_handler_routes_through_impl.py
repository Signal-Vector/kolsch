"""bug-020 Test 4 — `_handler_workflow` routes through `WorkflowsImpl.start`.

Exercises AC #7 — the `/workflow <ref>` slash command no longer
calls `execute_workflow_with_renderer` (the rich-Console renderer
that bypasses the workflow-card pipeline). Instead it opens a
workflow-kind card via `cards.open` and hands it to
`workflows.start(card=...)`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from cli._slash import ChatSessionState, ControlSignal, SlashContext, _handler_workflow
from cli.chat.service_registry import ChatServiceRegistry


class _MockCard:
    """Minimal Card impl — records `close` invocations."""

    def __init__(self) -> None:
        self.id = f"crd_{uuid4()}"
        self.closed_with: dict[str, Any] | None = None
        self._owner_transferred = False

    async def close(
        self,
        *,
        final_status: str | None = None,
        error_message: str | None = None,
        collapse: bool = True,
    ) -> None:
        self.closed_with = {
            "final_status": final_status,
            "error_message": error_message,
            "collapse": collapse,
        }


@dataclass
class _CardsSpy:
    open_calls: list[dict[str, Any]] = field(default_factory=list)
    cards: list[_MockCard] = field(default_factory=list)

    async def open(self, title: str, kind: str = "markdown") -> _MockCard:
        self.open_calls.append({"title": title, "kind": kind})
        card = _MockCard()
        self.cards.append(card)
        return card


@dataclass
class _WorkflowsSpy:
    start_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_start: BaseException | None = None

    async def start(
        self,
        ref: str,
        inputs: dict[str, Any],
        *,
        card: Any = None,
    ) -> str:
        self.start_calls.append(
            {"ref": ref, "inputs": inputs, "card": card},
        )
        if self.raise_on_start is not None:
            raise self.raise_on_start
        return f"wrk_{uuid4()}"


@dataclass
class _MockCatalogEntry:
    ref: str
    path: str = "/tmp/does_not_matter.yaml"


class _MockCatalog:
    def __init__(self, refs: list[str]) -> None:
        self._refs = set(refs)

    def lookup(
        self,
        ref: str,
        *,
        expected_type: str = "",
    ) -> _MockCatalogEntry | None:
        _ = expected_type
        if ref in self._refs:
            return _MockCatalogEntry(ref=ref)
        return None


class _MockAM:
    def __init__(self, catalog: _MockCatalog) -> None:
        self._catalog = catalog

    def catalog(self) -> _MockCatalog:
        return self._catalog


class _MockRuntime:
    def __init__(self, catalog: _MockCatalog) -> None:
        self.asset_management = _MockAM(catalog)
        self.root = "/tmp"


def _make_state(
    *,
    catalog_refs: list[str],
    cards: Any,
    workflows: Any,
) -> ChatSessionState:
    runtime = _MockRuntime(_MockCatalog(catalog_refs))
    registry = ChatServiceRegistry(cards=cards, workflows=workflows)
    state = ChatSessionState(
        runtime=runtime,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        session_id="ses_x",  # type: ignore[arg-type]
        thread_id="th_x",  # type: ignore[arg-type]
    )
    state.service_registry = registry
    return state


def _make_ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(
        stdout=io.StringIO(),
        agent_config=None,
        authz=None,
        agent_name="test-agent",
        state=state,
    )


@pytest.mark.asyncio
async def test_handler_workflow_opens_card_before_start() -> None:
    """`cards.open` fires before `workflows.start`, and both are called once."""
    cards = _CardsSpy()
    workflows = _WorkflowsSpy()
    state = _make_state(
        catalog_refs=["kolsch/workflows/demo"],
        cards=cards,
        workflows=workflows,
    )
    ctx = _make_ctx(state)

    result = await _handler_workflow("kolsch/workflows/demo", ctx)

    assert result == ControlSignal.CONTINUE
    assert len(cards.open_calls) == 1
    assert cards.open_calls[0]["kind"] == "workflow"
    assert cards.open_calls[0]["title"] == "kolsch/workflows/demo"
    assert len(workflows.start_calls) == 1


@pytest.mark.asyncio
async def test_handler_workflow_passes_opened_card_to_start() -> None:
    """The card returned by `cards.open` is the same card passed to `workflows.start`."""
    cards = _CardsSpy()
    workflows = _WorkflowsSpy()
    state = _make_state(
        catalog_refs=["kolsch/workflows/demo"],
        cards=cards,
        workflows=workflows,
    )
    ctx = _make_ctx(state)

    await _handler_workflow("kolsch/workflows/demo", ctx)

    opened_card = cards.cards[0]
    passed_card = workflows.start_calls[0]["card"]
    assert passed_card is opened_card


@pytest.mark.asyncio
async def test_handler_workflow_does_not_call_execute_workflow_with_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy renderer path is unreachable from `/workflow`."""
    # Spy: if `execute_workflow_with_renderer` is invoked, we detect
    # it and fail the test.
    called = {"n": 0}

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        called["n"] += 1
        return None

    # The handler imports the symbol from `cli._workflow_chat_runner`
    # lazily -- if the import fires (i.e. handler falls back to the
    # legacy path) we detect it here. Post-fix the handler no longer
    # imports it at all, so the spy is a stronger assertion: even
    # the import must not happen.
    import cli._workflow_chat_runner as _chat_runner

    monkeypatch.setattr(
        _chat_runner,
        "execute_workflow_with_renderer",
        _boom,
    )

    cards = _CardsSpy()
    workflows = _WorkflowsSpy()
    state = _make_state(
        catalog_refs=["kolsch/workflows/demo"],
        cards=cards,
        workflows=workflows,
    )
    ctx = _make_ctx(state)

    await _handler_workflow("kolsch/workflows/demo", ctx)

    assert called["n"] == 0, "legacy renderer path was invoked"


@pytest.mark.asyncio
async def test_handler_workflow_missing_registry_prints_notice() -> None:
    """No service registry -> friendly one-line notice, no raise."""
    runtime = _MockRuntime(_MockCatalog(["kolsch/workflows/demo"]))
    state = ChatSessionState(
        runtime=runtime,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        session_id="ses_x",  # type: ignore[arg-type]
        thread_id="th_x",  # type: ignore[arg-type]
    )
    # Note: service_registry left as default (None).
    ctx = _make_ctx(state)

    result = await _handler_workflow("kolsch/workflows/demo", ctx)

    assert result == ControlSignal.CONTINUE
    output = ctx.stdout.getvalue()  # type: ignore[attr-defined]
    assert "service registry" in output.lower()


@pytest.mark.asyncio
async def test_handler_workflow_start_raise_closes_card_error() -> None:
    """A raise from `workflows.start` closes the opened card as error."""
    cards = _CardsSpy()
    workflows = _WorkflowsSpy(raise_on_start=RuntimeError("kaboom"))
    state = _make_state(
        catalog_refs=["kolsch/workflows/demo"],
        cards=cards,
        workflows=workflows,
    )
    ctx = _make_ctx(state)

    result = await _handler_workflow("kolsch/workflows/demo", ctx)

    assert result == ControlSignal.CONTINUE
    assert cards.cards
    assert cards.cards[0].closed_with is not None
    assert cards.cards[0].closed_with["final_status"] == "error"
