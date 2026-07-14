"""`ChatMountSink` — the single mount surface for chat + workflow rows.

bug-036d consolidates two dispatch paths (top-level chat handlers +
per-workflow-row `apply_sub_agent_event`) into one. Every event
handler in :class:`~cli.chat.event_adapter.EventAdapter` picks a
`ChatMountSink` — either the main :class:`~cli.chat.app.ChatApp` or
a :class:`~cli.chat.widgets.workflow_node_row.WorkflowNodeRow` —
and calls the same mount methods regardless. This deletes the
duplicate handler chain and closes the routing gap around events
(``USER_MESSAGE``, ``MODEL_RESPONSE``, ``TOOL_FAILED``) that only
routed through one path before.

The main App already implements every method on this Protocol as
of bug-036c (see ``ChatApp.mount_*``). The row grows a matching
surface in bug-036d; :class:`WorkflowNodeRow` collapses its
``_saw_agent_message`` + ``_event_buffer`` fields into
``_last_assistant_text`` + ``_pending_mounts`` closures to match.

`event_id` is optional on every mount method — the row uses it as
a dedupe key for spec-021 replay-on-expand; the main App ignores
it (the transcript never dedupes by event_id).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatMountSink(Protocol):
    """A container that hosts a chat: main App or a WorkflowNodeRow.

    Every mount method is ``async`` because the main App awaits
    Textual mount operations; the row's implementation is
    trivially awaitable even when queued.

    Row implementations MAY wrap mounted widgets in a ``NodeSection``
    for per-type section navigation. App implementations mount flat.
    Semantic contract: mount ONE unit of content in the container's
    natural presentation.
    """

    async def mount_user_message(
        self,
        text: str,
        timestamp: object | None = None,
        *,
        event_id: str | None = None,
        content: dict[str, Any] | None = None,
    ) -> Any | None:
        """Mount a ``UserChatbox`` for a user turn.

        ``content`` carries the raw dict payload when the source
        event's ``data.content`` is a dict — sinks that wrap
        per-key (workflow rows) split by key; App sinks ignore it.

        bug-060 Part 1: App sinks return the mounted widget so the
        adapter can register it under the source event's id. Row
        sinks return ``None`` (their mounts are deferred closures);
        widgets inside a row aren't reached by the outer compaction
        unmount pass.
        """
        ...

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: str | None = None,
    ) -> Any | None:
        """Mount an ``AssistantProse`` for a model response / agent message.

        bug-060 Part 1: see ``mount_user_message`` note on returns.
        """
        ...

    async def mount_tool_call(
        self,
        tool_name: str,
        args_str: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
        matched_rule: dict[str, object] | None = None,
        event_id: str | None = None,
    ) -> Any | None:
        """Mount a ``ToolCallBlock`` for a tool result / failure.

        ``matched_rule`` (feat-041): the authz rule that fired, as a
        dict shaped like ``Rule.model_dump()``. Non-None on authz
        denials; the widget renders the 2-line hint (Mockup 1)
        inline in the body when present.

        bug-060 Part 1: see ``mount_user_message`` note on returns.
        """
        ...

    async def mount_notice(
        self,
        notice_id: str,
        text: str,
        *,
        level: str = "info",
        event_id: str | None = None,
    ) -> Any | None:
        """Mount a ``NoticeWidget`` (info / warning / error banner).

        bug-060 Part 1: see ``mount_user_message`` note on returns.
        """
        ...

    def last_assistant_text(self) -> str:
        """Return the last-mounted assistant text (for dedupe)."""
        ...

    def set_last_assistant_text(self, text: str) -> None:
        """Stamp the last-mounted assistant text (for dedupe)."""
        ...


__all__ = ["ChatMountSink"]
