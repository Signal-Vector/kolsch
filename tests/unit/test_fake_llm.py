"""Unit tests for the Fake LLM stub.

Per handbook §8.3, the Fake LLM is the single most important test
fixture; if it drifts from contract, every integration test built on
top inherits the drift. These tests exercise:

- text / tool-call / error response delivery;
- script exhaustion;
- the recorder (calls list);
- preservation of script across reset_calls.
"""

import pytest

from tests.fixtures.fake_llm import (
    ErrorResponse,
    FakeLLM,
    FakeLLMScriptedError,
    FakeLLMScriptExhaustedError,
    TextResponse,
    ToolCallResponse,
)


async def test_text_response_returned_and_recorded() -> None:
    fake = FakeLLM(script=[TextResponse(text="hello")])
    context: dict[str, object] = {"system_prompt": "be brief"}

    response = await fake.agenerate(context)

    assert isinstance(response, TextResponse)
    assert response.text == "hello"
    assert len(fake.calls) == 1
    assert fake.calls[0].context == context
    assert fake.calls[0].response == response


async def test_tool_call_response_returned_and_recorded() -> None:
    fake = FakeLLM(
        script=[
            ToolCallResponse(
                tool="read",
                args={"path": "data/x.md"},
                tool_call_id="call_1",
            )
        ]
    )

    response = await fake.agenerate({})

    assert isinstance(response, ToolCallResponse)
    assert response.tool == "read"
    assert response.args == {"path": "data/x.md"}
    assert response.tool_call_id == "call_1"
    assert fake.calls[0].response == response


async def test_error_response_raises_and_still_records_call() -> None:
    fake = FakeLLM(script=[ErrorResponse(error_type="model.transient", message="boom")])
    context: dict[str, object] = {"turn": 1}

    with pytest.raises(FakeLLMScriptedError) as exc_info:
        await fake.agenerate(context)

    assert exc_info.value.error_type == "model.transient"
    assert str(exc_info.value) == "boom"
    # The call must still be recorded — tests inspect the recorded
    # context to verify what the model was asked at the moment of
    # failure (e.g., for retry-path coverage).
    assert len(fake.calls) == 1
    assert fake.calls[0].context == context


async def test_script_exhausted_raises_dedicated_error() -> None:
    fake = FakeLLM(script=[TextResponse(text="only one")])

    await fake.agenerate({})

    with pytest.raises(FakeLLMScriptExhaustedError):
        await fake.agenerate({})


async def test_responses_returned_in_script_order() -> None:
    fake = FakeLLM(
        script=[
            ToolCallResponse(tool="read", args={"path": "a"}),
            TextResponse(text="answer"),
        ]
    )

    first = await fake.agenerate({"turn": 1})
    second = await fake.agenerate({"turn": 2})

    assert isinstance(first, ToolCallResponse)
    assert isinstance(second, TextResponse)
    # Recorded calls preserve order and pair correctly with contexts.
    assert [call.context["turn"] for call in fake.calls] == [1, 2]


async def test_reset_calls_clears_history_but_preserves_script() -> None:
    fake = FakeLLM(script=[TextResponse(text="hi"), TextResponse(text="again")])

    await fake.agenerate({"turn": 1})
    fake.reset_calls()

    assert fake.calls == []
    # Script has one remaining entry — reset_calls didn't touch it.
    response = await fake.agenerate({"turn": 2})
    assert isinstance(response, TextResponse)
    assert response.text == "again"


async def test_empty_script_raises_exhausted_on_first_call() -> None:
    fake = FakeLLM()

    with pytest.raises(FakeLLMScriptExhaustedError):
        await fake.agenerate({})

    assert fake.calls == []
