"""spec-119 end-to-end smoke — a REAL agent exchange through the refactored seam.

Drives ``AgentEngine.execute`` with ``MinimalFramework`` and a
scripted ``MockLLM`` that emits a ``tool_call`` — so the tool call
flows model → framework → ``_ObservedToolHandle`` → ``invoke_observed``
→ ``ToolsSubsystem.invoke_tool`` (real authz/sandbox/secrets dispatch)
→ real ``_emit`` → SQLite-backed store JSONL — and asserts the
observable behaviour is unchanged:

- (a) an ALLOWED call executes and lands ``tool.called`` →
  ``tool.result`` on the persisted path with the right data;
- (c) the value returned to the framework is the RAW tool result
  (the event carries the normalized ``{"value": ...}`` wrapper; the
  model's next-turn tool message carries the raw string);
- (b) a DENIED call (``ToolUnauthorized`` from a real deny rule)
  lands ``tool.failed`` with the ``recovery_hint`` in the event AND
  the caller-visible ``{"error": {..., "recovery_hint": "/perm allow
  ..."}}`` payload reaches the model's next turn.

The observed event stream is logged (INFO) so a gate run with
``log_cli`` shows the live sequence.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from schemas import EventType
from tests.component.test_engine import (
    _build_engine,
    _config_with_tool,
    _new_session_context,
)
from tests.fixtures.mock_llm import MockLLM, TextResponse, ToolCallResponse
from tools import ToolContext, tool

if TYPE_CHECKING:
    from pathlib import Path

    from schemas import Event

_log = logging.getLogger("tests.engine.observed_smoke")

_HINT = "/perm allow kolsch/tools/ping *"


class _PingArgs(BaseModel):
    message: str


@tool(
    name="ping",
    description="Echo a message.",
    args_schema=_PingArgs,
    action="kolsch/tools/ping",
)
async def _ping_tool(args: _PingArgs, ctx: ToolContext) -> str:
    # RAW non-dict result — the observed seam must emit the
    # normalized `{"value": "pong:..."}` while returning this string.
    _ = ctx
    return f"pong:{args.message}"


def _summarize(events: list[Event]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        row: dict[str, Any] = {"type": e.type.value, "actor": e.actor.kind}
        if e.type in (EventType.TOOL_CALLED, EventType.TOOL_RESULT):
            row["data"] = e.data
        elif e.type == EventType.TOOL_FAILED:
            row["data"] = {
                "tool_call_id": e.data["tool_call_id"],
                "tool_name": e.data["tool_name"],
                "error_type": e.data["error"]["type"],
                "recovery_hint": e.data["recovery_hint"],
            }
        out.append(row)
    return out


def _tool_message_of_last_request(events: list[Event]) -> str:
    """The tool-role message content the model saw on its final turn."""
    last_request = [e for e in events if e.type == EventType.MODEL_REQUEST][-1]
    tool_messages = [
        m for m in last_request.data["messages"] if m.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    assert isinstance(content, str)
    return content


async def test_smoke_allowed_tool_call_end_to_end(tmp_path: Path) -> None:
    """Allowed: tool.called → tool.result persisted; raw return to the model."""
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="ping",
                args={"message": "ack"},
                tool_call_id="call_ping",
            ),
            TextResponse(text="finished"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        mock,
        extra_tools=[_ping_tool],
    )
    ctx = await _new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert result.data["content"]["text"] == "finished"

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    _log.info(
        "smoke allowed — observed events: %s",
        json.dumps(_summarize(events), sort_keys=True),
    )

    # (a) tool.called → tool.result, adjacent, with the right data.
    types = [e.type for e in events]
    called_idx = types.index(EventType.TOOL_CALLED)
    assert types[called_idx + 1] == EventType.TOOL_RESULT
    called, result_evt = events[called_idx], events[called_idx + 1]
    assert called.data["tool_name"] == "ping"
    assert called.data["tool_call_id"] == "call_ping"
    assert called.data["arguments"] == {"message": "ack"}
    assert called.actor.kind == "agent"
    assert result_evt.data["tool_call_id"] == "call_ping"

    # The EVENT carries the normalized wrapper...
    assert result_evt.data["result"] == {"value": "pong:ack"}

    # (c) ...while the framework (and thus the model's next turn)
    # received the RAW string — not the normalized JSON.
    content = _tool_message_of_last_request(events)
    assert content == "pong:ack"


async def test_smoke_denied_tool_call_end_to_end(tmp_path: Path) -> None:
    """Denied: tool.failed with recovery_hint in the event AND in the
    caller-visible error payload the model sees next turn."""
    mock = MockLLM(
        script=[
            ToolCallResponse(
                tool="ping",
                args={"message": "ack"},
                tool_call_id="call_ping",
            ),
            TextResponse(text="finished"),
        ],
    )
    engine, store, _am = await _build_engine(
        tmp_path,
        mock,
        # First-match-wins: deny the ping ref, allow everything else.
        authz_rules=[
            {"what": "kolsch/tools/ping", "pattern": "*", "effect": "deny"},
            {"who": ".*", "what": ".*", "effect": "allow"},
        ],
        extra_tools=[_ping_tool],
    )
    ctx = await _new_session_context(store)

    result = await engine.execute(_config_with_tool(), ctx)
    assert result.data["content"]["text"] == "finished"

    events = [e async for e in store.read_path_to_leaf(ctx.thread_id)]
    _log.info(
        "smoke denied — observed events: %s",
        json.dumps(_summarize(events), sort_keys=True),
    )

    # (b) tool.called → tool.failed with the hint on the event data.
    types = [e.type for e in events]
    called_idx = types.index(EventType.TOOL_CALLED)
    assert types[called_idx + 1] == EventType.TOOL_FAILED
    failed = events[called_idx + 1]
    assert failed.data["tool_name"] == "ping"
    assert failed.data["error"]["type"] == "tool.unauthorized"
    assert failed.data["recovery_hint"] == _HINT
    assert failed.actor.kind == "agent"
    # No tool.result anywhere — the call never executed.
    assert EventType.TOOL_RESULT not in types

    # ...and the caller got `{"error": {..., "recovery_hint": ...}}`:
    # MinimalFramework JSON-serializes the returned dict into the
    # tool message, so the model's final request carries the hint.
    content = _tool_message_of_last_request(events)
    payload = json.loads(content)
    assert payload["error"]["type"] == "tool.unauthorized"
    assert payload["error"]["recovery_hint"] == _HINT
