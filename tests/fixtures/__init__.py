"""Test fixtures: Fake LLM, event helpers, factories.

Per handbook §8.3 (Fake LLM) and §8.5-8.6 (factories + assertion
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
from tests.fixtures.fake_llm import (
    ErrorResponse,
    FakeLLM,
    FakeLLMResponse,
    TextResponse,
    ToolCallResponse,
)

__all__ = [
    "ErrorResponse",
    "FakeLLM",
    "FakeLLMResponse",
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
