"""Tests for ``engine._crewai_adapter`` — bug-engine-tools-wiring-gap PR 3.

Pin the two load-bearing properties of ``make_llm_callable`` that
the user's deepseek-v4-pro repro surfaced as missing:

1. ``request.tools`` is FORWARDED to ``crewai.LLM.call(tools=...)``.
   Before PR 3 the wrapper dropped tools silently; the upstream
   API never saw them; tool_use-capable models reverted to text
   responses (or prompt-based XML calls). Pre-fix verification:
   git-stashing the PR 3 change reproduces the user's symptom.

2. When ``crewai.LLM.call`` returns the OpenAI tool-call list
   shape (``[{id, function: {name, arguments}}]``), the wrapper
   translates back into the engine's
   ``LLMResponse(content=None, tool_calls=[{id, name, arguments}])``
   shape. Without this translation, structured tool calls would
   stringify into text and the framework wouldn't dispatch.

These tests use a stand-in for ``crewai.LLM`` so we don't import
crewai's heavy module just for the seam test (crewai's
optional-install pattern, per ``engine/__init__.py``).
"""

from __future__ import annotations

from typing import Any

from engine._crewai_adapter import _engine_response_from_crewai, make_llm_callable
from engine.framework import LLMRequest, Message


class _StubCrewaiLLM:
    """Stand-in for ``crewai.LLM`` that records calls + returns scripted shapes.

    crewai.LLM has a heavy dependency surface; this stub keeps the
    test fast and isolated. The adapter only touches the
    ``.call(messages, tools=...)`` method.
    """

    def __init__(self, return_value: Any = "default text reply") -> None:
        self._return_value = return_value
        self.recorded_calls: list[dict[str, Any]] = []

    def call(
        self,
        messages: Any,
        tools: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.recorded_calls.append(
            {"messages": messages, "tools": tools, "kwargs": dict(kwargs)},
        )
        return self._return_value


# ===========================================================================
# Tools forwarding — the load-bearing property the user's bug surfaced
# ===========================================================================


async def test_make_llm_callable_forwards_tools_to_crewai_llm() -> None:
    """PR 3 regression: ``request.tools`` MUST reach ``crewai.LLM.call(tools=)``.

    Before the fix the wrapper called ``crewai_llm.call(messages)``
    with no tools kwarg; litellm built the upstream request
    without tools; the model produced text replies even when
    the user asked for a tool action. Stashing PR 3 reproduces
    this failure here.
    """
    stub = _StubCrewaiLLM(return_value="ok")
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    tools = [
        {
            "name": "bash",
            "schema": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
    ]
    request = LLMRequest(
        messages=[Message(role="user", content="run the bash command ls")],
        tools=tools,
        model="test-model",
        parameters={},
    )
    await fn(request)

    assert len(stub.recorded_calls) == 1
    recorded = stub.recorded_calls[0]
    assert recorded["tools"] is not None, (
        "tools=None was passed to crewai.LLM.call — the wrapper "
        "is dropping request.tools (bug-engine-tools-wiring-gap PR 3 "
        "has regressed)."
    )
    assert recorded["tools"] == tools


async def test_make_llm_callable_passes_none_when_no_tools_declared() -> None:
    """Empty request.tools → ``tools=None`` (NOT ``tools=[]``).

    Some OpenAI-compatible providers reject ``tools: []`` per the
    spec. The wrapper normalises to None so the upstream request
    omits the field entirely when no tools are declared.
    """
    stub = _StubCrewaiLLM(return_value="hi")
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="test-model",
        parameters={},
    )
    await fn(request)

    assert stub.recorded_calls[0]["tools"] is None


# ===========================================================================
# Response translation — list[dict with function key] → tool_calls
# ===========================================================================


async def test_make_llm_callable_translates_tool_call_list_into_llm_response() -> None:
    """The OpenAI tool-call shape ``crewai.LLM.call`` returns when
    the model invokes a tool — a list of ``{id, function:
    {name, arguments(JSON-string)}}`` — translates back into the
    engine's ``LLMResponse(content=None, tool_calls=[...])`` form
    with arguments parsed back into a dict.
    """
    crewai_tool_call_response = [
        {
            "id": "call_abc123",
            "function": {
                "name": "bash",
                "arguments": '{"command": "ls -la"}',
            },
        },
    ]
    stub = _StubCrewaiLLM(return_value=crewai_tool_call_response)
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    request = LLMRequest(
        messages=[Message(role="user", content="list files")],
        tools=[{"name": "bash", "schema": {"type": "object"}}],
        model="test-model",
        parameters={},
    )
    response = await fn(request)

    assert response.content is None
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call["id"] == "call_abc123"
    assert call["name"] == "bash"
    # arguments parsed from JSON-string to dict — what the framework
    # expects when dispatching.
    assert call["arguments"] == {"command": "ls -la"}


async def test_make_llm_callable_translates_text_response_into_content() -> None:
    """Plain string return → ``LLMResponse(content=text)``."""
    stub = _StubCrewaiLLM(return_value="hello back")
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="test-model",
        parameters={},
    )
    response = await fn(request)
    assert response.content == "hello back"
    assert not response.tool_calls


async def test_make_llm_callable_handles_dict_arguments() -> None:
    """Some providers return ``arguments`` as a dict rather than a
    JSON-encoded string. The translator handles both."""
    stub = _StubCrewaiLLM(
        return_value=[
            {
                "id": "call_xyz",
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "README.md"},  # dict, not JSON string
                },
            },
        ],
    )
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    request = LLMRequest(
        messages=[Message(role="user", content="read README")],
        tools=[],
        model="test-model",
        parameters={},
    )
    response = await fn(request)
    assert response.tool_calls is not None
    assert response.tool_calls[0]["arguments"] == {"path": "README.md"}


async def test_make_llm_callable_synthesises_id_when_provider_omits_it() -> None:
    """Some providers return tool calls without an ``id``. The
    translator synthesises one (``call_<uuid8>``) so the
    framework's dispatch loop has something to correlate
    tool.called → tool.result events with."""
    stub = _StubCrewaiLLM(
        return_value=[
            {
                "function": {
                    "name": "bash",
                    "arguments": "{}",
                },
            },
        ],
    )
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    response = await fn(
        LLMRequest(
            messages=[Message(role="user", content="x")],
            tools=[],
            model="m",
            parameters={},
        ),
    )
    assert response.tool_calls is not None
    assert response.tool_calls[0]["id"].startswith("call_")


# ===========================================================================
# Defensive paths — malformed responses don't crash the engine
# ===========================================================================


def test_engine_response_from_crewai_handles_litellm_pydantic_object_shape() -> None:
    """litellm returns ``ChatCompletionMessageFunctionToolCall``
    Pydantic objects, NOT plain dicts, when the upstream provider
    emits ``tool_use``. The user's deepseek-v4-pro repro surfaced
    this: after the first PR 3 attempt fixed tool forwarding,
    crewai.LLM.call started returning these Pydantic objects, the
    translator's dict-only path skipped them, and the engine
    stringified the list into ``agent.message`` content
    (``[ChatCompletionMessageFunctionToolCall(...)]`` literal repr).

    The translator must duck-type over dict vs. object access so
    both shapes work.
    """

    class _Function:
        def __init__(self, name: str, arguments: str) -> None:
            self.name = name
            self.arguments = arguments

    class _ToolCall:
        def __init__(self, call_id: str, function: _Function) -> None:
            self.id = call_id
            self.function = function
            self.type = "function"

    result = [
        _ToolCall(
            call_id="call_00_abc",
            function=_Function(name="bash", arguments='{"command": "ls"}'),
        ),
    ]
    response = _engine_response_from_crewai(result)
    assert response.content is None
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["id"] == "call_00_abc"
    assert response.tool_calls[0]["name"] == "bash"
    assert response.tool_calls[0]["arguments"] == {"command": "ls"}


def test_engine_response_from_crewai_handles_malformed_argument_json() -> None:
    """Provider returns ``arguments`` as malformed JSON → arguments
    becomes ``{}``; the call still surfaces so the framework can
    decide what to do (likely report ``tool.invalid_arguments``)."""
    response = _engine_response_from_crewai(
        [
            {
                "id": "c1",
                "function": {"name": "bash", "arguments": "{this is not JSON"},
            },
        ],
    )
    assert response.tool_calls is not None
    assert response.tool_calls[0]["arguments"] == {}


def test_engine_response_from_crewai_skips_entries_without_a_tool_name() -> None:
    """A list entry missing ``function.name`` is skipped (not crashed
    on, not stringified)."""
    response = _engine_response_from_crewai(
        [
            {"id": "c1", "function": {"arguments": "{}"}},  # no name
            {
                "id": "c2",
                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
            },
        ],
    )
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "bash"


def test_engine_response_from_crewai_stringifies_unknown_shapes() -> None:
    """An int (or any non-str / non-list) result stringifies into
    ``content`` so the exchange completes rather than crashing
    on an unknown crewai shape."""
    response = _engine_response_from_crewai(42)
    assert response.content == "42"
    assert not response.tool_calls
