"""The Assembly Procedure — projects an event path to a model call.

**Normative and versioned.** Given the same tree path and the same
declared `assembly_version`, this procedure MUST produce
byte-identical output. v1 `assembly_version` is `"1"`; future
versions land alongside their projection-rule changes in the test
catalog.

Pure: no I/O, no clock reads, no random ordering. The engine wraps
the output and hands it to the framework via
[`Framework.run_exchange`][engine.framework.Framework.run_exchange].
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from engine.framework import Message
from schemas import EventType

if TYPE_CHECKING:
    from schemas import Event


ASSEMBLY_VERSION = "1"


class AssembledContext(BaseModel):
    """The output of the Assembly Procedure for one model call.

    Carries what the procedure hands "to the model" — a system
    prompt, a chronological message list, and the tool list. The
    engine bundles these into
    [`LLMRequest`][engine.framework.LLMRequest] when calling the LLM
    seam.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: str
    messages: list[Message]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    assembly_version: str = ASSEMBLY_VERSION
    config_event_id: str | None = None


def assemble_context(
    events: list[Event],
    *,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> AssembledContext:
    """Apply the Assembly Procedure to a path of events.

    Skills are rendered into the system prompt via the
    `{{ skills }}` Jinja variable at pre-flight so the prompt
    template controls placement and wording rather than receiving an
    unconditional bolt-on at assembly time.

    Args:
        events: The chronological list from thread root to leaf — the
            output of `SessionStore.read_path_to_leaf`.
        tool_schemas: The resolved tool list at call time. Not
            derived from events here — the engine passes it in from
            the Tools subsystem's `list_registered_tools`.

    Returns:
        The [`AssembledContext`][engine.assembly.AssembledContext]
        the engine bundles into the LLM request.
    """
    # Step 2 — find the most recent config.set on the path.
    config_event = _latest_event_of_type(events, EventType.CONFIG_SET)
    if config_event is None:
        # No config yet — first call before pre-flight emission. Use
        # empty defaults; the engine's pre-flight always emits a
        # config.set before the first model call, so this branch only
        # runs in synthetic test paths.
        resolved_config: dict[str, Any] = {}
        config_event_id: str | None = None
    else:
        resolved_config = config_event.data.get("resolved_config", {})
        config_event_id = str(config_event.event_id)

    # Step 3 — compose the system prompt from the prompt layers in
    # declared order. The skills block is part of the template now
    # (rendered at pre-flight), so there's no post-assembly append.
    system_prompt = _compose_system_prompt(resolved_config)

    # Step 4 — project events to messages per the projection table.
    messages = _project_events(events)

    # Step 5 — tool definitions come from the engine, not events.
    tools = list(tool_schemas or [])

    return AssembledContext(
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        assembly_version=ASSEMBLY_VERSION,
        config_event_id=config_event_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers — Assembly Procedure steps 2-4
# ---------------------------------------------------------------------------


def _latest_event_of_type(
    events: list[Event],
    event_type: EventType,
) -> Event | None:
    """Walk backward from the leaf; return the most recent match."""
    for event in reversed(events):
        if event.type == event_type:
            return event
    return None


def _compose_system_prompt(resolved_config: dict[str, Any]) -> str:
    """Concatenate prompt layers in declared order.

    `PromptSpec` is `list[str] | str`; both shapes are accepted so the
    projection is robust to either form.
    """
    prompt = resolved_config.get("prompt", "")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n\n".join(str(layer) for layer in prompt)
    return ""


def _project_events(events: list[Event]) -> list[Message]:
    """Project each event to a model-facing message.

    Projection table (subset shipped in v1):

    - `user.message` → one user message, content verbatim.
    - `agent.message` → one assistant message, content verbatim.
    - `tool.called` → folded into the preceding assistant message's
      `tool_calls` (so the projection mirrors the chat-completions
      shape). If the preceding event isn't an assistant message,
      synthesize a tool-call-only assistant turn.
    - `tool.result` → one tool message with the result verbatim.
    - `tool.failed` → one tool message with the error content.
    - `memory.retrieved` → one system message wrapping the retrieved
      content (variant: a system "context injection").
    - All other types — not projected.
    """
    messages: list[Message] = []
    for event in events:
        if event.type == EventType.USER_MESSAGE:
            messages.append(
                Message(
                    role="user",
                    content=_extract_content_text(event.data.get("content", {})),
                ),
            )
        elif event.type == EventType.AGENT_MESSAGE:
            messages.append(
                Message(
                    role="assistant",
                    content=_extract_content_text(event.data.get("content", {})),
                ),
            )
        elif event.type == EventType.TOOL_CALLED:
            tool_call = {
                "id": event.data.get("tool_call_id", ""),
                "name": event.data.get("tool_name", ""),
                "arguments": event.data.get("arguments", {}),
            }
            # Fold into a preceding assistant turn, or synthesize one.
            if messages and messages[-1].role == "assistant":
                prev = messages[-1]
                merged_calls = list(prev.tool_calls or [])
                merged_calls.append(tool_call)
                messages[-1] = prev.model_copy(update={"tool_calls": merged_calls})
            else:
                messages.append(
                    Message(role="assistant", content="", tool_calls=[tool_call]),
                )
        elif event.type == EventType.TOOL_RESULT:
            result_payload = event.data.get("result", {})
            messages.append(
                Message(
                    role="tool",
                    content=_extract_content_text(result_payload),
                    tool_call_id=str(event.data.get("tool_call_id", "")),
                ),
            )
        elif event.type == EventType.TOOL_FAILED:
            err = event.data.get("error", {})
            messages.append(
                Message(
                    role="tool",
                    content=f"<error: {_error_message(err)}>",
                    tool_call_id=str(event.data.get("tool_call_id", "")),
                ),
            )
        elif event.type == EventType.MEMORY_RETRIEVED:
            results = event.data.get("results", [])
            messages.append(
                Message(
                    role="system",
                    content=_format_memory_retrieval(results),
                ),
            )
        # All other event types: not projected.
    return messages


def _extract_content_text(content: dict[str, Any] | str | None) -> str:
    """Best-effort text projection for `content` blobs.

    The events store `content` as a permissive dict (the engine
    materializes whatever `IOSpec.outputs` produces); projection
    here pulls a canonical text field for the message body. v1
    convention: a `text` key wins; otherwise the dict is stringified
    deterministically.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if "text" in content and isinstance(content["text"], str):
        return content["text"]
    # Deterministic JSON-style projection — sort keys so byte
    # equality holds for replay.
    import json  # noqa: PLC0415

    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def _error_message(err: dict[str, Any]) -> str:
    msg = err.get("message")
    if isinstance(msg, str) and msg:
        return msg
    type_ = err.get("type")
    if isinstance(type_, str) and type_:
        return type_
    return "tool failed"


def _format_memory_retrieval(results: list[dict[str, Any]]) -> str:
    """Format a `memory.retrieved.data.results` list as a system msg.

    Deterministic: each item dumped via `json.dumps(sort_keys=True)`.
    Replay produces the same string from the same captured content.
    """
    if not results:
        return "<memory: no results>"
    import json  # noqa: PLC0415

    lines = ["<memory>"]
    for item in results:
        lines.append(json.dumps(item, sort_keys=True, separators=(",", ":")))
    lines.append("</memory>")
    return "\n".join(lines)
