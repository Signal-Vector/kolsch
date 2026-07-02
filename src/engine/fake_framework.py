"""Reference implementation of [`Framework`][engine.framework.Framework].

`MinimalFramework` (formerly `FakeFramework`) is kolsch's default
production framework — `bootstrap_runtime` wires it up regardless of
whether a real LLM factory is present. Renamed from `FakeFramework`
because the old name misled reviewers into treating the class as a
test fixture; see `process/feat-clarify-framework-selection.md` for
the rename rationale + the shipped bug that revealed the confusion
(kimi K2 empty-args tool calls, 2026-07-02).

The loop is deliberately minimal: build `[system, user]`, call the
LLM, dispatch any tool calls the LLM emits, append the tool result +
assistant turn to the message list, repeat until the LLM returns a
content-only response or `cancellation` fires. No per-exchange
iteration cap; runaway loops are terminated by the user via Esc
(which flips `cancellation`).

Trade-off vs `CrewAIFramework`: this class provides no
cancellation-during-hang polling, no `ModelUpstreamError`
retry translation, no stdout/stderr capture around a noisy agent
library, and no tracing-consent short-circuit. Users who need those
production affordances should pass `framework=CrewAIFramework()`
explicitly to `bootstrap_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from engine._cancellation import race_with_cancel
from engine.framework import LLMRequest, Message, ToolCall

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from engine.framework import LLMHandle, ToolHandle


class MinimalFramework:
    """Kolsch's minimal reference framework — the `bootstrap_runtime` default.

    Renamed from `FakeFramework` — see the module docstring.
    """

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
        max_iterations: int | None = None,  # noqa: ARG002
        cancellation: Callable[[], bool] | None = None,
        cancel_token: Any = None,
    ) -> str:
        """Drive one deterministic exchange to completion and return the final text.

        Implements [`Framework`][engine.framework.Framework] without an
        external LLM — every reply comes from the engine-supplied
        `llm` seam, which test code wires to a fake driver.
        """
        # The engine's Assembly Procedure supplies `history` for
        # multi-turn exchanges (prior user/assistant/tool turns
        # already materialized in the log). For first-exchange runs
        # history is empty. The current `user_message` is appended
        # AFTER history so the model sees the new turn last.
        messages: list[Message] = [
            Message(role="system", content=system_prompt),
            *history,
            Message(role="user", content=user_message),
        ]
        tool_schemas = [{"name": t.name, "schema": t.schema} for t in tools]
        tool_map = {t.name: t for t in tools}

        while True:
            if cancellation is not None and cancellation():
                raise asyncio.CancelledError

            request = LLMRequest(
                messages=messages,
                tools=tool_schemas,
                model=self._model_name,
                parameters={},
            )
            # Race the LLM call against the cancel token so an Esc
            # during a hanging stream aborts within one event-loop
            # tick of the flip instead of waiting for the call to
            # return on its own. `race_with_cancel` is a no-op
            # pass-through when `cancel_token` is None (test paths).
            response = await race_with_cancel(
                llm(request), cancel_token, what="llm_call"
            )

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

                # Same race treatment as the LLM call above. A 30s
                # bash that the user cancels at 5s aborts within ~1
                # event-loop tick of the flip; the tool body itself
                # sees the cancellation via `ctx.cancellation` and
                # handles subprocess-side cleanup (SIGINT etc).
                result = await race_with_cancel(
                    handle(
                        ToolCall(
                            name=tool_name,
                            arguments=arguments,
                            tool_call_id=tool_call_id or None,
                        ),
                    ),
                    cancel_token,
                    what=f"tool_call[{tool_name}]",
                )
                messages.append(
                    Message(
                        role="tool",
                        content=_stringify(result),
                        tool_call_id=tool_call_id,
                    ),
                )


def _stringify(value: object) -> str:
    """Coerce a tool result to a string for the tool message body.

    Strings pass through; everything else is JSON-serialized with
    sorted keys + minimal separators so the projection is byte-stable
    and round-trips through the Assembly Procedure's `tool.result`
    projection rule.
    """
    if isinstance(value, str):
        return value
    import json  # noqa: PLC0415

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# DEPRECATED: renamed to `MinimalFramework` (see
# process/feat-clarify-framework-selection.md). Alias kept for one
# release so external callers upgrading from the previous release do
# not break immediately; remove in the next feature release.
FakeFramework = MinimalFramework
