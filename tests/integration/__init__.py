"""Integration tests — multiple subsystems with real backings.

Per handbook §8.1 + AGENTS.md: integration tier wires multiple
subsystems (engine, store, providers, tools, skills) against a
real Framework adapter (CrewAIFramework) driven by the Mock LLM.

Step 12b lands TC-ENG-01..11 against CrewAIFramework here. The
same case bodies live as component tests (against MinimalFramework)
under tests/component/test_engine.py; this directory adds the
CrewAI dimension.
"""
