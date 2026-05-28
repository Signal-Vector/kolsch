"""FakeFramework — reference implementation of :class:`Framework`.

The v1 default framework for engine tests AND for development
scenarios where users want zero-LLM-cost runs against the Fake LLM.
Engine-internal (lives in ``src/engine/``, not ``tests/fixtures/``)
because it is shipped behavior, not a test fixture.

The loop is intentionally minimal: build ``[system, user]``,
call the LLM, dispatch any tool calls the LLM emits, append the
tool result + assistant turn to the message list, repeat until the
LLM returns a content-only response or ``max_iterations`` is hit.

What this Framework promises (per the prereq design note):

- Calls ``LLMHandle`` exactly once per iteration with the current
  message list.
- Dispatches every ``tool_call`` in the LLM's response through the
  matching ``ToolHandle``; raises if no handle matches.
- Polls ``cancellation`` between iterations; raises
  :class:`asyncio.CancelledError` (Python idiom) if it fires.
- Returns the LLM's text content when no tool calls remain.

What this Framework does NOT promise (kept symmetric with what
CrewAI would and would NOT provide):

- No extra observability beyond what the LLM/tool seams surface.
- No internal retry policy — retry is the engine's responsibility.
- No prompt-template injection (CrewAI does this; FakeFramework
  does not — the §B.6 amendment covers both shapes).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from engine.framework import LLMRequest, Message, ToolCall

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from engine.framework import LLMHandle, ToolHandle


_DEFAULT_MAX_ITERATIONS = 10


class FakeFrameworkLoopExceededError(RuntimeError):
    """The LLM kept emitting tool calls past ``max_iterations``.

    Surfaced as a non-PlatformError on purpose — the engine catches it
    at the boundary and wraps it as a Fatal ``model.retry_exhausted``
    (or similar) per spec §1.6. Framework-internal errors are not
    PlatformError subclasses; the engine is the boundary.
    """


class FakeFramework:
    """Reference Framework for v1 — deterministic, zero-LLM-cost."""

    def __init__(self, *, model_name: str = "fake") -> None:
        self._model_name = model_name

    async def run_exchange(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Sequence[Message] = (),
        llm: LLMHandle,
        tools: Sequence[ToolHandle],
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        cancellation: Callable[[], bool] | None = None,
    ) -> str:
        # The engine's Assembly Procedure (§B.5) supplies `history`
        # for multi-turn exchanges (prior user/assistant/tool turns
        # already materialized in the log). For first-turn exchanges
        # history is empty. The current `user_message` is appended
        # AFTER history so the model sees the new turn last.
        messages: list[Message] = [
            Message(role="system", content=system_prompt),
            *history,
            Message(role="user", content=user_message),
        ]
        tool_schemas = [{"name": t.name, "schema": t.schema} for t in tools]
        tool_map = {t.name: t for t in tools}

        for _ in range(max_iterations):
            if cancellation is not None and cancellation():
                raise asyncio.CancelledError

            request = LLMRequest(
                messages=messages,
                tools=tool_schemas,
                model=self._model_name,
                parameters={},
            )
            response = await llm(request)

            # Text-only response → exchange complete.
            if response.tool_calls is None or len(response.tool_calls) == 0:
                return response.content or ""

            # Tool-call path. Append the assistant turn that issued the
            # calls, then dispatch each one and append its result.
            messages.append(
                Message(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=list(response.tool_calls),
                ),
            )

            for call_dict in response.tool_calls:
                tool_name = str(call_dict.get("name", ""))
                tool_call_id = str(call_dict.get("id", ""))
                arguments = dict(call_dict.get("arguments", {}))

                handle = tool_map.get(tool_name)
                if handle is None:
                    # The framework cannot route an unknown tool. Push
                    # an error message so the model can react on the
                    # next turn; do NOT raise (Diagnostic semantics).
                    messages.append(
                        Message(
                            role="tool",
                            content=f"<error: unknown tool {tool_name!r}>",
                            tool_call_id=tool_call_id,
                        ),
                    )
                    continue

                result = await handle(
                    ToolCall(
                        name=tool_name,
                        arguments=arguments,
                        tool_call_id=tool_call_id or None,
                    ),
                )
                messages.append(
                    Message(
                        role="tool",
                        content=_stringify(result),
                        tool_call_id=tool_call_id,
                    ),
                )

        msg = (
            f"FakeFramework: max_iterations={max_iterations} reached "
            "without a content-only response"
        )
        raise FakeFrameworkLoopExceededError(msg)


def _stringify(value: object) -> str:
    """Coerce a tool result to a string for the tool message body.

    Strings pass through; everything else is JSON-serialized with
    sorted keys + minimal separators so the projection is byte-stable
    and round-trips through the Assembly Procedure's tool.result
    projection rule (Materialization §B.6).
    """
    if isinstance(value, str):
        return value
    import json  # noqa: PLC0415

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
