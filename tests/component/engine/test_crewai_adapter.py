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

import contextlib
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from engine._crewai_adapter import _engine_response_from_crewai, make_llm_callable
from engine.crewai_framework import CrewAIFramework
from engine.framework import LLMRequest, LLMResponse, Message


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
    # The wrapper now normalises to OpenAI spec shape (see
    # ``_tool_to_openai_spec``). Kolsch's internal ``{name, schema}``
    # shape doesn't reach litellm — Kimi K2 needs the proper envelope
    # to populate ``function.arguments``.
    assert len(recorded["tools"]) == 1
    forwarded = recorded["tools"][0]
    assert forwarded["type"] == "function"
    assert forwarded["function"]["name"] == "bash"
    assert forwarded["function"]["parameters"] == tools[0]["schema"]


async def test_make_llm_callable_normalises_kolsch_shape_to_openai_spec() -> None:
    """Engine-internal ``{name, description, schema}`` → OpenAI
    ``{type: function, function: {name, description, parameters}}``.

    Kimi K2 emits ``arguments: {}`` for every tool call when the
    advertisement lacks the ``function.parameters`` envelope; the
    kolsch-native shape from ``MinimalFramework`` was going to litellm
    unchanged. This test pins the conversion so a future refactor
    can't silently regress it.
    """
    stub = _StubCrewaiLLM(return_value="ok")
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    kolsch_tools = [
        {
            "name": "fetch_website",
            "description": "Fetch a URL and return content",
            "schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL"},
                },
                "required": ["url"],
            },
        },
    ]
    request = LLMRequest(
        messages=[Message(role="user", content="fetch example.com")],
        tools=kolsch_tools,
        model="m",
        parameters={},
    )
    await fn(request)

    forwarded = stub.recorded_calls[0]["tools"][0]
    assert forwarded == {
        "type": "function",
        "function": {
            "name": "fetch_website",
            "description": "Fetch a URL and return content",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL"},
                },
                "required": ["url"],
            },
        },
    }


async def test_make_llm_callable_passes_openai_shape_verbatim() -> None:
    """Tools already in ``{type: function, function: {...}}`` shape pass
    through unchanged.

    Guards the ``_CrewAILLM.call`` path — CrewAI's
    ``convert_tools_to_openai_schema`` already produces spec shape;
    double-wrapping would produce ``function.function.name`` which
    litellm doesn't handle.
    """
    stub = _StubCrewaiLLM(return_value="ok")
    fn = make_llm_callable(stub)  # type: ignore[arg-type]

    already_spec = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                "strict": True,
            },
        },
    ]
    request = LLMRequest(
        messages=[Message(role="user", content="ls")],
        tools=already_spec,
        model="m",
        parameters={},
    )
    await fn(request)

    forwarded = stub.recorded_calls[0]["tools"][0]
    assert forwarded == already_spec[0]


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


# ===========================================================================
# G1 — Stdout/stderr capture regression (bug-crewai-stdout-leak-investigation)
# ===========================================================================


async def test_run_exchange_suppresses_crewai_stdout_leakage() -> None:
    """G1 regression pin: ``run_exchange`` MUST wrap
    ``crew.kickoff_async`` in ``redirect_stdout`` + ``redirect_stderr``
    so CrewAI's singleton EventListener can't leak prose past the
    chat renderer.

    CrewAI 1.14.6 wires a process-singleton EventListener whose
    ``ConsoleFormatter`` is hardcoded ``verbose=True`` regardless
    of ``Agent(verbose=False)`` / ``Crew(verbose=False)``. Probe 1
    of the investigation doc captured 665 stdout bytes of EventsBus
    pairing-mismatch warnings on the failure path plus 2,673 stderr
    bytes of Python ``logging`` ERROR records; Probe 2 showed
    ``verbose=True`` adds ~12 KB of Rich panels. Probe 3 confirmed
    that ``contextlib.redirect_stdout`` + ``redirect_stderr``
    around ``crew.kickoff_async`` catches the byte stream cleanly
    in isolation.

    Mechanism: this test wraps the entire ``run_exchange`` call in
    an OUTER ``redirect_stdout``. If the inner wrap in
    ``run_exchange`` is intact, CrewAI's writes go to the inner
    StringIO (discarded) and the outer buffer stays empty. If
    someone deletes the inner wrap, CrewAI's prose escapes to the
    outer buffer; the marker assertions below catch it.

    Unlike the other tests in this file (which stub
    ``crewai.LLM``), this one drives the real
    ``CrewAIFramework.run_exchange`` end-to-end with a fake
    ``LLMHandle`` — the leak is a property of the real
    Agent/Crew/EventListener stack, not the lightweight stub.
    """
    framework = CrewAIFramework()

    async def fake_llm(_request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="Hi from the fake LLM.")

    outer_stdout = io.StringIO()
    outer_stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(outer_stdout),
        contextlib.redirect_stderr(outer_stderr),
    ):
        result = await framework.run_exchange(
            system_prompt="be brief",
            user_message="say hi",
            llm=fake_llm,
            tools=[],
        )

    assert "Hi from the fake LLM." in result

    captured_stdout = outer_stdout.getvalue()
    captured_stderr = outer_stderr.getvalue()

    # Markers spanning the shapes CrewAI's ConsoleFormatter is
    # known to emit (per the Step A investigation against
    # crewai/events/utils/console_formatter.py). If a future
    # CrewAI version adds new prose, extend this list — the
    # contract is "nothing CrewAI emits reaches the user's
    # terminal," not "this specific phrase doesn't."
    crewai_prose_markers = [
        "Thinking",  # ConsoleFormatter:889 "Thinking...\n"
        "Crew Execution",  # known crew-panel header
        "Agent Started",  # known agent-step panel
        "╭",  # Rich panel top border — any panel at all
        "╰",  # Rich panel bottom border
    ]
    for marker in crewai_prose_markers:
        assert marker not in captured_stdout, (
            f"CrewAI stdout leak: {marker!r} in outer stdout. "
            f"crewai_framework.run_exchange's redirect_stdout wrap "
            f"is missing or broken — bug-crewai-stdout-leak-"
            f"investigation Step G1 has regressed."
        )
        assert marker not in captured_stderr, (
            f"CrewAI stderr leak: {marker!r} in outer stderr."
        )


def test_run_exchange_suppresses_crewai_logger_leak_on_failing_llm(
    tmp_path: Path,
) -> None:
    """G1 regression pin — failure-path variant (TC-CREWAI-NOLEAK-02).

    Has actual pre-fix bite. The structural test above passes
    even pre-fix in the healthy path (CrewAI's ``verbose=False``
    on success emits nothing). To exercise the wrap's load-bearing
    contract we need CrewAI's FAILURE path, where the singleton
    listener's ``Error executing listener call_llm_and_parse: ...``
    log message hits ``stderr`` (CrewAI's flow.py:3161
    ``logger.error`` + Python's default ``lastResort`` handler).

    **Why this test runs in a subprocess.** Inside pytest, the
    root logger is reconfigured so ``lastResort`` doesn't fire —
    CrewAI's records get swallowed silently into pytest's capture
    machinery rather than reaching ``sys.stderr``. The pre-fix
    leak therefore can't be reproduced from inside a pytest test
    process. The Step H investigation Probe 4 verified the actual
    behaviour in standalone Python:

    - Pre-fix (no wrap) + raising fake_llm → 72 bytes to stderr:
      ``Error executing listener call_llm_and_parse: <reason>``.
    - Post-fix (wrap) + raising fake_llm → 0 bytes.

    Forking a clean Python subprocess restores the standalone
    logging behaviour; the subprocess writes its findings to its
    own stdout, this test reads them and asserts the wrap
    suppressed the leak.
    """
    probe_path = tmp_path / "failing_llm_probe.py"
    probe_path.write_text(
        textwrap.dedent("""
            import asyncio, contextlib, io, sys
            from engine.crewai_framework import CrewAIFramework
            from engine.framework import LLMRequest, LLMResponse

            async def main() -> int:
                framework = CrewAIFramework()

                async def failing_llm(_req: LLMRequest) -> LLMResponse:
                    raise RuntimeError("simulated upstream failure")

                out_buf, err_buf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out_buf), \\
                     contextlib.redirect_stderr(err_buf):
                    with contextlib.suppress(Exception):
                        await framework.run_exchange(
                            system_prompt="be brief",
                            user_message="say hi",
                            llm=failing_llm,
                            tools=[],
                        )

                captured = out_buf.getvalue() + err_buf.getvalue()
                # Same signature list as the in-process test below;
                # these are CrewAI's known failure-path emitters.
                signatures = ("Error executing listener", "[CrewAIEventsBus]")
                for sig in signatures:
                    if sig in captured:
                        print("LEAK_DETECTED", sig, file=sys.__stdout__)
                        print(
                            "STDOUT(first 500):", out_buf.getvalue()[:500],
                            file=sys.__stdout__,
                        )
                        print(
                            "STDERR(first 500):", err_buf.getvalue()[:500],
                            file=sys.__stdout__,
                        )
                        return 1
                print("CLEAN", file=sys.__stdout__)
                return 0

            sys.exit(asyncio.run(main()))
        """).strip()
    )

    result = subprocess.run(
        [sys.executable, str(probe_path)],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(Path(__file__).parent.parent.parent.parent / "src"),
    )
    assert result.returncode == 0, (
        f"Failing-LLM subprocess detected a CrewAI leak past the "
        f"G1 wrap. The wrap at crewai_framework.py:run_exchange has "
        f"regressed.\nProbe stdout:\n{result.stdout}\n"
        f"Probe stderr:\n{result.stderr}"
    )
    assert "CLEAN" in result.stdout, (
        f"Probe did not reach the success report; check for setup "
        f"errors. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
