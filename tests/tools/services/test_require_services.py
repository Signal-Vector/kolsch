"""spec-001 Test 3 — `require_services()` helper narrowing + errors."""

from __future__ import annotations

from uuid import uuid4

import pytest

from schemas import AgentId, CallerChain, SessionId, ThreadId
from tools.decorator import ToolContext
from tools.services import ToolNotAvailable, require_services


def _base_ctx(**overrides: object) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="test",
        secrets={},
        caller_chain=CallerChain.unattributed(),
        **overrides,
    )


def test_require_services_returns_narrowed_view() -> None:
    """`ctx.cards` set -> `services.cards` is the sentinel; other three `None`."""
    sentinel = object()
    ctx = _base_ctx(cards=sentinel)

    services = require_services(ctx, ["cards"])

    assert services.cards is sentinel
    assert services.prompts is None
    assert services.workflows is None
    assert services.session is None


def test_require_services_raises_tool_not_available_for_missing_field() -> None:
    """`ctx.cards is None` + `require_services(ctx, ["cards"])` raises."""
    ctx = _base_ctx()  # cards defaults to None

    with pytest.raises(ToolNotAvailable, match="cards"):
        require_services(ctx, ["cards"])


def test_require_services_populates_multiple_requested_fields() -> None:
    """All four set + `require_services(ctx, ["cards", "prompts"])` -> two set, two None."""
    cards_sent = object()
    prompts_sent = object()
    workflows_sent = object()
    session_sent = object()
    ctx = _base_ctx(
        cards=cards_sent,
        prompts=prompts_sent,
        workflows=workflows_sent,
        session=session_sent,
    )

    services = require_services(ctx, ["cards", "prompts"])

    assert services.cards is cards_sent
    assert services.prompts is prompts_sent
    assert services.workflows is None
    assert services.session is None


def test_require_services_raises_value_error_for_unknown_service_name() -> None:
    """Unknown service name in `which` raises `ValueError` — same shape as decorator."""
    ctx = _base_ctx()

    with pytest.raises(ValueError, match="nonsense"):
        require_services(ctx, ["nonsense"])  # type: ignore[list-item]
