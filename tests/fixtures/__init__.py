"""Test fixtures: Mock LLM, event helpers, factories.

Per handbook §8.3 (Mock LLM) and §8.5-8.6 (factories + assertion
helpers). These are the reusable building blocks tests compose; a
test should rarely construct raw data by hand.
"""

from tests.fixtures.assertions import (
    assert_event_content,
    assert_event_sequence,
    assert_materialization,
)
from tests.fixtures.factories import (
    make_agent_config,
    make_event,
    make_session,
    temp_platform_root,
)
from tests.fixtures.mock_llm import (
    ErrorResponse,
    MockLLM,
    MockLLMResponse,
    TextResponse,
    ToolCallResponse,
)

__all__ = [
    "ErrorResponse",
    "MockLLM",
    "MockLLMResponse",
    "TextResponse",
    "ToolCallResponse",
    "assert_event_content",
    "assert_event_sequence",
    "assert_materialization",
    "make_agent_config",
    "make_event",
    "make_session",
    "temp_platform_root",
]
