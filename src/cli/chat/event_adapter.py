"""Event → widget dispatcher for the Textual chat surface.

Consumes events from
[`AgentEngine.execute_stream`][engine.engine.AgentEngine.execute_stream]
and mounts / updates widgets in the transcript. Preserves the
"renderer is a pure event consumer" invariant: dispatch is a class-
level table over ``EventType``; one ``_handle_<name>`` method per
event type; no widget-specific state carried in the adapter.

The single approval carve-out lives in ``cli/chat/approval.py`` —
the adapter has no idea approvals exist.

Text extraction, tool-arg formatting, and the fenced-code-block
heuristic are ported verbatim from ``src/cli/_renderer.py`` and
kept as module-level helpers so per-adapter tests can exercise
them without an ``App``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from schemas.events import Event, EventType

if TYPE_CHECKING:
    from cli.chat.app import ChatApp


# ---------------------------------------------------------------------------
# Helpers ported from `_renderer.py`
# ---------------------------------------------------------------------------


def _extract_text(content: dict[str, object] | None) -> str:
    """Pull the display string out of an event's ``content`` dict.

    Ported verbatim from ``Renderer._extract_text`` at
    ``_renderer.py:594``. Both ``agent.message`` and
    ``model.response`` carry their rendered text under
    ``content.text``.
    """
    if not content:
        return ""
    text = content.get("text")
    if isinstance(text, str):
        return text
    if text is None:
        return ""
    return str(text)


_PANEL_HEADER_ARG_MAX_LEN = 60
"""Match `_renderer.py:674` — truncation width for arg values so
the tool-block header stays one line."""


def _format_args(args: dict[str, object] | None) -> str:
    """Render a tool's arguments as ``key=val, key=val`` for the header.

    Ported verbatim from ``_renderer.py:680``. Strings get repr'd
    so the user sees the quotes; other types ``str()`` through;
    long values truncate with ``...``.
    """
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        rendered = repr(value) if isinstance(value, str) else str(value)
        if len(rendered) > _PANEL_HEADER_ARG_MAX_LEN:
            rendered = rendered[: _PANEL_HEADER_ARG_MAX_LEN - 3] + "..."
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def _format_tool_result(result: dict[str, object] | None) -> str:
    """Render a tool.result payload as displayable text.

    Ported from ``_renderer.py:700``.
    """
    if not result:
        return ""
    for key in ("content", "stdout", "text", "value"):
        if key in result:
            value = result[key]
            return value if isinstance(value, str) else str(value)
    try:
        return json.dumps(result, sort_keys=True, indent=2, default=str)
    except (TypeError, ValueError):
        return str(result)


def _detect_code_blocks(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into a sequence of ``(kind, body)`` chunks.

    ``kind`` is ``"prose"`` or ``"code"``. Ported from the inline
    heuristic at ``_renderer.py:103–149``; extracted here so the
    adapter can be tested in isolation without spinning up a
    ``Renderer``.

    Rules mirror the source:

    - Triple-backtick fences delimit code chunks.
    - The language label after the opening fence is ignored here;
      Textual's ``Markdown`` widget handles that natively.
    - A closing fence returns to prose. Any residual code with no
      closing fence stays as a code chunk to end-of-text.
    """
    chunks: list[tuple[str, str]] = []
    current_kind = "prose"
    current_lines: list[str] = []
    for raw_line in text.split("\n"):
        if raw_line.startswith("```"):
            if current_lines:
                chunks.append((current_kind, "\n".join(current_lines)))
                current_lines = []
            current_kind = "code" if current_kind == "prose" else "prose"
            continue
        current_lines.append(raw_line)
    if current_lines:
        chunks.append((current_kind, "\n".join(current_lines)))
    return chunks


def _now_ts(event: Event) -> datetime:
    """Return a ``datetime`` for the event's timestamp.

    ``Event.created_at`` is the canonical field; fall back to
    ``datetime.now(UTC)`` if the field is missing (should never
    happen but the adapter is defensive).
    """
    ts = getattr(event, "created_at", None)
    if isinstance(ts, datetime):
        return ts
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class EventAdapter:
    """Dispatch ``Event``s to widget public methods.

    Constructor takes the ``ChatApp`` so handlers can call
    ``app.mount_*`` helpers, query the transcript, or read app-level
    state. The adapter carries only ephemeral bookkeeping
    (tool-args by call id; streaming widgets by call id).
    """

    # Class-level dispatch table: EventType → method name. Missing
    # entries fail Test 5 (event-coverage smoke) — every EventType
    # member must have a branch.
    _DISPATCH: dict[EventType, str] = {
        EventType.SESSION_OPENED: "_handle_session_opened",
        EventType.THREAD_EXCHANGE_STARTED: "_handle_thread_exchange_started",
        EventType.THREAD_EXCHANGE_COMPLETED: "_handle_thread_exchange_completed",
        EventType.THREAD_EXCHANGE_FAILED: "_handle_thread_exchange_failed",
        EventType.USER_MESSAGE: "_handle_user_message",
        EventType.AGENT_MESSAGE: "_handle_agent_message",
        EventType.TOOL_CALLED: "_handle_tool_called",
        EventType.TOOL_RESULT: "_handle_tool_result",
        EventType.TOOL_FAILED: "_handle_tool_failed",
        EventType.MODEL_REQUEST: "_handle_model_request",
        EventType.MODEL_RESPONSE: "_handle_model_response",
        EventType.MODEL_STREAMING_TOKEN: "_handle_model_streaming_token",
        EventType.MODEL_FALLBACK: "_handle_model_fallback",
        EventType.CONFIG_SET: "_handle_config_set",
        EventType.MEMORY_WRITTEN: "_handle_memory_written",
        EventType.MEMORY_RETRIEVED: "_handle_memory_retrieved",
        EventType.MEMORY_DELETED: "_handle_memory_deleted",
        EventType.CONTEXT_COMPACTED: "_handle_context_compacted",
        EventType.AGENT_TEMPLATE_WARNING: "_handle_agent_template_warning",
        # spec-002 registers the UI event envelope. Emission +
        # per-kind dispatch land in spec-003 / spec-006 / spec-012+;
        # the adapter's branch here is a silent no-op so replay of a
        # future JSONL row doesn't raise `KeyError` on the dispatch
        # table lookup.
        EventType.UI_EVENT: "_handle_ui_event",
    }

    def __init__(self, app: ChatApp) -> None:
        self._app = app
        # tool_call_id → arguments dict (captured on TOOL_CALLED so
        # TOOL_RESULT / TOOL_FAILED can render the full signature).
        self._tool_call_args: dict[str, dict[str, Any]] = {}
        # call_id → AssistantProse widget (streaming assistant text).
        # Populated on first MODEL_STREAMING_TOKEN for a call; cleared
        # on MODEL_RESPONSE for the same call.
        self._streaming_widgets: dict[str, Any] = {}
        # Concatenated deltas per call_id (for the "concat matches
        # final text" defensive check on MODEL_RESPONSE).
        self._streaming_buffers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @classmethod
    def handles(cls, event_type: EventType) -> bool:
        """True if a handler is registered for ``event_type``."""
        return event_type in cls._DISPATCH

    async def handle(self, event: Event) -> None:
        """Dispatch ``event`` to the registered handler.

        Missing entries would raise ``KeyError``; the coverage test
        makes that a static failure.
        """
        method_name = self._DISPATCH[event.type]
        handler = getattr(self, method_name)
        await handler(event)

    # ------------------------------------------------------------------
    # Handlers — one per EventType member
    # ------------------------------------------------------------------

    async def _handle_session_opened(self, event: Event) -> None:
        """Silent — session identity is already reflected in the InfoBar."""
        _ = event

    async def _handle_thread_exchange_started(self, event: Event) -> None:
        """Kick off the transient turn status.

        Sets the verb to ``Thinking…`` so the user sees immediate
        feedback while the engine spins up the first model call.
        """
        _ = event
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_thread_exchange_completed(self, event: Event) -> None:
        """Clear the transient turn status; the transcript speaks for itself."""
        _ = event
        await self._app.set_turn_verb("", target="")

    async def _handle_thread_exchange_failed(self, event: Event) -> None:
        """Clear the turn status and surface the failure in the transcript."""
        error = event.data.get("error") or {}
        message = str(error.get("message", "") or error.get("type", "exchange failed"))
        await self._app.set_turn_verb("", target="")
        await self._app.mount_exchange_failed(message)

    async def _handle_user_message(self, event: Event) -> None:
        """Mount a ``UserChatbox`` for the just-submitted turn.

        The adapter mounts the user chatbox from the event so replay
        and live paths share a single mount site.
        """
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text:
            return
        await self._app.mount_user_message(text, _now_ts(event))

    async def _handle_agent_message(self, event: Event) -> None:
        """Mount assistant prose only if MODEL_RESPONSE didn't already."""
        content = event.data.get("content") or {}
        text = _extract_text(content)
        if not text:
            return
        # AGENT_MESSAGE fires after MODEL_RESPONSE with the same text
        # for the common case; the adapter defers to MODEL_RESPONSE
        # for streaming or single-shot mount. Only re-mount when the
        # transcript's last assistant text differs — matches the
        # legacy renderer behaviour at `_renderer.py:387`.
        if self._app.last_assistant_text() == text:
            return
        await self._app.mount_assistant_message(text)
        self._app.set_last_assistant_text(text)

    async def _handle_tool_called(self, event: Event) -> None:
        """Capture the call's arguments for the eventual result/failure."""
        tool_call_id = str(event.data.get("tool_call_id", ""))
        if tool_call_id:
            self._tool_call_args[tool_call_id] = dict(
                event.data.get("arguments") or {},
            )
        tool_name = str(event.data.get("tool_name", ""))
        if tool_name:
            args_summary = _format_args(event.data.get("arguments") or {})
            await self._app.set_turn_verb(
                f"Calling {tool_name}",
                target=args_summary,
            )

    async def _handle_tool_result(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with the completed result."""
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        args_str = _format_args(args)
        result = event.data.get("result") or {}
        body = _format_tool_result(result)
        await self._app.mount_tool_call(tool_name, args_str, body, failed=False)
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_tool_failed(self, event: Event) -> None:
        """Mount a ``ToolCallBlock`` with ``failed=True`` styling."""
        tool_name = str(event.data.get("tool_name", "<unknown>"))
        tool_call_id = str(event.data.get("tool_call_id", ""))
        args = self._tool_call_args.pop(tool_call_id, {})
        args_str = _format_args(args)
        error = event.data.get("error") or {}
        code = str(error.get("type", "") or error.get("code", "<unknown>"))
        message = str(error.get("message", ""))
        body = f"{code}: {message}" if code else message
        recovery_hint = event.data.get("recovery_hint")
        recovery_hint = recovery_hint if isinstance(recovery_hint, str) else None
        await self._app.mount_tool_call(
            tool_name,
            args_str,
            body,
            failed=True,
            recovery_hint=recovery_hint,
        )

    async def _handle_model_request(self, event: Event) -> None:
        """MODEL_REQUEST fires the "Thinking…" verb.

        No transcript widget mounted — the assistant prose widget
        lands on MODEL_STREAMING_TOKEN (streaming path) or
        MODEL_RESPONSE (non-streaming path).
        """
        _ = event
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_model_response(self, event: Event) -> None:
        """Finalise the streaming widget or mount a fresh one."""
        call_id = str(event.data.get("call_id", ""))
        content = event.data.get("content") or {}
        text = _extract_text(content)
        # Streaming path: the widget was mounted on the first
        # STREAMING_TOKEN; nothing further to append.
        if call_id and call_id in self._streaming_widgets:
            self._streaming_widgets.pop(call_id, None)
            self._streaming_buffers.pop(call_id, None)
            if text:
                self._app.set_last_assistant_text(text)
            await self._app.set_turn_verb("Thinking…", target="")
            return
        # Non-streaming path: mount once with the final text.
        if text:
            await self._app.mount_assistant_message(text)
            self._app.set_last_assistant_text(text)
        await self._app.set_turn_verb("Thinking…", target="")

    async def _handle_model_streaming_token(self, event: Event) -> None:
        """Append one delta to the assistant prose widget for this call."""
        call_id = str(event.data.get("call_id", ""))
        delta = str(event.data.get("text_delta", ""))
        if not call_id or not delta:
            return
        widget = self._streaming_widgets.get(call_id)
        if widget is None:
            widget = await self._app.mount_assistant_streaming_widget()
            self._streaming_widgets[call_id] = widget
            self._streaming_buffers[call_id] = ""
        self._streaming_buffers[call_id] += delta
        # `AssistantProse.append` awaits the underlying
        # `Markdown.append` — safe on the event-loop thread.
        await widget.append(delta)
        await self._app.set_turn_verb("Streaming…", target="")

    async def _handle_model_fallback(self, event: Event) -> None:
        """Surface the fallback transition as a dim italic notice."""
        to = event.data.get("to") or {}
        to_label = (
            f"{to.get('provider', '?')}/{to.get('model', '?')}"
            if isinstance(to, dict)
            else str(to)
        )
        reason = str(event.data.get("reason", "transient model failure"))
        await self._app.mount_notice(f"falling back to {to_label} because {reason}")

    async def _handle_config_set(self, event: Event) -> None:
        """Silent — config is diagnostic-only in the JSONL."""
        _ = event

    async def _handle_memory_written(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_memory_retrieved(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_memory_deleted(self, event: Event) -> None:
        """Silent — memory is diagnostic-only."""
        _ = event

    async def _handle_context_compacted(self, event: Event) -> None:
        """Silent — compaction is diagnostic-only."""
        _ = event

    async def _handle_agent_template_warning(self, event: Event) -> None:
        """Surface template warnings as a dim inline notice."""
        message = str(event.data.get("message") or event.data.get("reason", ""))
        if message:
            await self._app.mount_notice(f"template warning: {message}")

    async def _handle_ui_event(self, event: Event) -> None:
        """Silent — spec-002 registers the envelope only.

        Per-kind UI dispatch (card / prompt / workflow-UI verbs) lands
        in spec-003 (SessionStore's `logical_seq` allocation +
        EventAdapter bookmark) and the follow-up service specs. Until
        then the adapter is a no-op sink so replay of any future JSONL
        row typed `ui.event` doesn't raise.
        """
        _ = event


__all__ = ["EventAdapter"]
