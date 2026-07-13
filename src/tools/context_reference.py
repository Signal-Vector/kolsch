"""``context.reference_lookup`` builtin — feat-054 Phase 2.

Fetches the verbatim content of an ``event_id`` cited in an active
``context.compacted`` event's ``refs`` list. Scope is hard-gated: only
event ids that appear in at least one active (not-revoked, not-preview)
``CONTEXT_COMPACTED.summary.refs`` entry on the current thread are
addressable. Out-of-scope ids get an actionable error.

Auto-registered by :class:`engine.AgentEngine` when the engine holds a
compaction provider AND the agent's resolved config declares
compaction. Agents without an active compaction never see the tool in
their tool list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from errors import ToolExecutionFailed
from schemas import EventType
from tools.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from store import SessionStore
    from tools.decorator import ToolContext


class ReferenceLookupArgs(BaseModel):
    """Argument shape for the ``context.reference_lookup`` builtin."""

    event_id: str


def make_reference_lookup(
    *,
    session_store: SessionStore,
) -> Callable[..., Awaitable[Any]]:
    """Build the ``reference_lookup`` builtin bound to ``session_store``.

    The returned tool is name-scoped ``reference_lookup`` so model
    prompts can refer to it without a colon-qualified path (the
    outward name in the model's tool list). The dotted ``context.``
    prefix documented in the spec is the *public* label; internally
    the tool is a normal builtin.

    Args:
        session_store: The platform's :class:`SessionStore`; the tool
            reads events by id on the current thread.
    """

    @tool(
        name="reference_lookup",
        description=(
            "Fetch the verbatim content of an event_id cited in an active "
            "context compaction summary. Use when the compaction summary "
            "cites an event_id and you need its full details. Refuses "
            "event ids not scoped to an active compaction."
        ),
        args_schema=ReferenceLookupArgs,
    )
    async def reference_lookup(
        args: ReferenceLookupArgs,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        thread_id = ctx.thread_id
        # Walk the thread's events to build:
        # - the scope set: every event_id cited in any active
        #   CONTEXT_COMPACTED.refs entry (or its compacted_event_ids).
        # - a resolver: event_id → the event object itself.
        scope: set[str] = set()
        by_id: dict[str, Any] = {}
        revoked: set[str] = set()
        # First pass: collect revoke pointers so we filter them.
        events: list[Any] = []
        async for e in session_store.read_path_to_leaf(thread_id):
            events.append(e)
            by_id[str(e.event_id)] = e
            if e.type == EventType.CONTEXT_COMPACTION_REVOKED:
                target = e.data.get("compaction_event_id")
                if isinstance(target, str):
                    revoked.add(target)
        # Second pass: build the scope from active compactions.
        for e in events:
            if e.type != EventType.CONTEXT_COMPACTED:
                continue
            if e.data.get("preview", False):
                continue
            if e.data.get("revoked_by") is not None:
                continue
            if str(e.event_id) in revoked:
                continue
            for eid in e.data.get("compacted_event_ids") or []:
                if isinstance(eid, str):
                    scope.add(eid)
            summary = e.data.get("summary") or {}
            for ref in summary.get("refs") or []:
                if not isinstance(ref, dict):
                    continue
                ref_id = ref.get("event_id")
                if isinstance(ref_id, str):
                    scope.add(ref_id)
                # Overflow clusters (see spec §5 event_id_range).
                rng = ref.get("event_id_range")
                if isinstance(rng, dict):
                    first = rng.get("first_event_id")
                    last = rng.get("last_event_id")
                    if isinstance(first, str):
                        scope.add(first)
                    if isinstance(last, str):
                        scope.add(last)

        target_id = args.event_id
        if target_id not in scope:
            # Actionable error message — the model sees this via
            # tool.failed and can adjust its next turn.
            raise ToolExecutionFailed(
                tool_name="reference_lookup",
                error_class=(
                    f"OutOfScope: event_id {target_id!r} is not addressable — "
                    "reference_lookup only serves event_ids cited in an "
                    "active context compaction. Check the summary's `refs` "
                    "and `compacted_event_ids` lists."
                ),
            )

        target = by_id.get(target_id)
        if target is None:
            raise ToolExecutionFailed(
                tool_name="reference_lookup",
                error_class=(
                    f"NotFound: event_id {target_id!r} is in scope but was "
                    "not found on the current thread — the id may belong to "
                    "a different session."
                ),
            )
        return {
            "event_id": str(target.event_id),
            "type": str(target.type),
            "data": dict(target.data),
            "created_at": target.created_at.isoformat(),
        }

    return reference_lookup
