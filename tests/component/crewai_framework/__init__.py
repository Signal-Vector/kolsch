"""Component-level tests for :class:`engine.CrewAIFramework`.

Adapter-only tests — no full engine. Verifies the seam contract
between CrewAI's executor and the engine's :class:`LLMHandle` /
:class:`ToolHandle` Protocols. The end-to-end behavior lives in
``tests/integration/test_engine_crewai.py``.
"""
