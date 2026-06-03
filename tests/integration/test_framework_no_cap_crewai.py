"""TC-FRM-NOCAP-03 — CrewAIFramework forwards the unbounded sentinel.

Regression pin for bug-framework-iteration-cap-removal.md. The
protocol's ``max_iterations=None`` translates to the module-level
``_UNBOUNDED_MAX_ITER`` constant when CrewAI's ``Agent.max_iter`` is
constructed — CrewAI requires a positive int, and ``10**9``
operationally means "no cap" (see
``crewai/agents/agent_builder/base_agent.py:258`` — plain ``int``
field, no validator; and ``crewai/utilities/agent_utils.py:271-281``
— simple ``>=`` compare).

If a future CrewAI bump validates ``max_iter`` against a smaller
ceiling, this test starts failing and forces a re-spike on the
sentinel value.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine import LLMRequest, LLMResponse, crewai_framework


class _AgentConstructed(Exception):  # noqa: N818
    """Sentinel exception raised by the patched ``Agent`` so the test
    short-circuits BEFORE CrewAI's Crew construction tries to
    validate a real Agent. We only need to inspect what was passed
    to ``Agent(...)``."""


async def test_tc_frm_nocap_03_crewai_uses_unbounded_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``run_exchange`` is called with no ``max_iterations``
    (the protocol default of ``None``), CrewAIFramework must
    construct ``Agent`` with ``max_iter == _UNBOUNDED_MAX_ITER``.

    Mechanism: monkeypatch ``engine.crewai_framework.Agent`` with a
    function that captures construction kwargs then raises a
    sentinel exception. The framework's call short-circuits before
    Crew construction; we then inspect the captured kwargs.
    """
    captured: dict[str, Any] = {}

    def _capture_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _AgentConstructed

    monkeypatch.setattr(crewai_framework, "Agent", _capture_agent)

    async def llm(_request: LLMRequest) -> LLMResponse:  # pragma: no cover
        # Unreachable — Agent construction raises before the LLM is
        # ever asked for a response.
        return LLMResponse(content="unreachable")

    fw = crewai_framework.CrewAIFramework()
    with pytest.raises(_AgentConstructed):
        await fw.run_exchange(
            system_prompt="x",
            user_message="x",
            llm=llm,
            tools=[],
            # max_iterations omitted on purpose — exercises the
            # protocol's None default, the path the engine takes
            # at engine.py:1096.
        )

    assert captured["max_iter"] == crewai_framework._UNBOUNDED_MAX_ITER
    assert captured["max_iter"] == 10**9
    # max_retry_limit=0 — engine still owns retry (TC-CRW-03's
    # invariant; pinning it here too so a future refactor that
    # forgets it surfaces in this regression suite).
    assert captured["max_retry_limit"] == 0


async def test_tc_frm_nocap_03b_crewai_honors_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller passes an explicit ``max_iterations`` value,
    the sentinel is NOT substituted — the caller's value reaches
    ``Agent.max_iter`` unchanged. Pins the per-framework override
    seam the protocol's ``int | None`` shape preserves.
    """
    captured: dict[str, Any] = {}

    def _capture_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _AgentConstructed

    monkeypatch.setattr(crewai_framework, "Agent", _capture_agent)

    async def llm(_request: LLMRequest) -> LLMResponse:  # pragma: no cover
        return LLMResponse(content="unreachable")

    fw = crewai_framework.CrewAIFramework()
    with pytest.raises(_AgentConstructed):
        await fw.run_exchange(
            system_prompt="x",
            user_message="x",
            llm=llm,
            tools=[],
            max_iterations=7,
        )

    assert captured["max_iter"] == 7
