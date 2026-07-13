"""feat-054 Phase 3 — helpers for ``/compaction-history`` and ``/why-compact``.

Both slash commands are read-only walks over the thread's event log:

- ``/compaction-history`` filters to ``CONTEXT_COMPACTED`` events and
  renders a compact table (event id, fired time, tokens before → after,
  compacted span, preview flag, revoked-by pointer).
- ``/why-compact <event_id>`` fetches one specific event and dumps its
  ``pre_compaction`` + ``summary`` payload as pretty-printed JSON.

Kept in its own module so :mod:`cli.chat.app` stays under the
readability ceiling and unit tests can construct + invoke the helpers
without instantiating a Textual App.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from schemas.events import EventType

if TYPE_CHECKING:
    from platform_runtime import PlatformRuntime
    from schemas import ThreadId
    from schemas.events import Event


def _format_hhmmss(event: Event) -> str:
    """Return a local ``HH:MM:SS`` stamp for the event's ``created_at``.

    Falls back to ``--:--:--`` on any format failure so a corrupt
    timestamp never crashes the table.
    """
    try:
        return event.created_at.astimezone().strftime("%H:%M:%S")
    except (AttributeError, ValueError, OSError):
        return "--:--:--"


def _format_tokens(before: int, after: int) -> str:
    """Return ``"12k→1k"`` style compact token deltas."""

    def _short(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.0f}k" if n >= 10000 else f"{n / 1000:.1f}k"
        return str(n)

    return f"{_short(before)}→{_short(after)}"


def _short_event_id(event_id: str) -> str:
    """Return the event id trimmed to a stable 12-char prefix.

    Kolsch event ids are ``ev_<uuid>`` (35 chars). Trimming at 12
    gives ``ev_<8-char-uuid-prefix>`` which is unique in practice for
    the handful of compactions on a single thread while staying
    scan-friendly in the ``/compaction-history`` table.
    """
    return str(event_id)[:12]


def _short_span(event_ids: list[str]) -> str:
    """Return ``"evt_abc..evt_def"`` from an ordered id list."""
    if not event_ids:
        return "(empty)"
    first = _short_event_id(event_ids[0])
    last = _short_event_id(event_ids[-1])
    if first == last:
        return first
    return f"{first}..{last}"


async def _collect_events(
    runtime: PlatformRuntime,
    thread_id: ThreadId,
) -> list[Event]:
    """Materialise the thread's events (small helper for testability)."""
    events: list[Event] = []
    async for event in runtime.store.read_path_to_leaf(thread_id):
        events.append(event)
    return events


async def build_compaction_history_table(
    runtime: PlatformRuntime,
    thread_id: ThreadId,
) -> str:
    """Return the ``/compaction-history`` table text.

    Two-pass walk:

    1. Collect every ``CONTEXT_COMPACTED`` event.
    2. Collect every ``CONTEXT_COMPACTION_REVOKED`` event; index by
       ``compaction_event_id`` so the table's ``revoked`` column
       shows the revocation event's id (matches spec §9 mockup).

    Renders a plain-text table (fixed-width columns; no box-drawing)
    the app mounts as a ``Static`` widget. Returns a "no compactions
    yet" one-liner when the thread has never fired one.
    """
    events = await _collect_events(runtime, thread_id)
    compactions: list[Event] = []
    revocations: dict[str, str] = {}
    for e in events:
        if e.type == EventType.CONTEXT_COMPACTED:
            compactions.append(e)
        elif e.type == EventType.CONTEXT_COMPACTION_REVOKED:
            target = str(e.data.get("compaction_event_id") or "")
            if target:
                revocations[target] = _short_event_id(str(e.event_id))

    if not compactions:
        return "↳ /compaction-history: no compactions on this thread yet."

    header = f"{'evt':<14} {'fired':<10} {'tokens':<11} {'compacted_range':<28} {'preview':<7} revoked"
    rule = "-" * len(header)
    rows: list[str] = [header, rule]
    for e in compactions:
        eid_short = _short_event_id(str(e.event_id))
        fired = _format_hhmmss(e)
        tokens_before = int(e.data.get("tokens_before") or 0)
        tokens_after = int(e.data.get("tokens_after") or 0)
        tokens = _format_tokens(tokens_before, tokens_after)
        compacted_ids = [
            str(x) for x in (e.data.get("compacted_event_ids") or [])
        ]
        span = _short_span(compacted_ids)
        preview_flag = "yes" if e.data.get("preview") else "no"
        revoked_id = revocations.get(str(e.event_id))
        # Fall back to `revoked_by` from the event itself (Assembly
        # may back-fill it via a store-level patch; today the
        # revocation path emits the pointer event and Assembly reads
        # the pointer separately, so the two views agree).
        if revoked_id is None:
            payload_revoked = e.data.get("revoked_by")
            if isinstance(payload_revoked, str) and payload_revoked:
                revoked_id = _short_event_id(payload_revoked)
        rows.append(
            f"{eid_short:<14} {fired:<10} {tokens:<11} "
            f"{span:<28} {preview_flag:<7} {revoked_id or '-'}"
        )
    return "\n".join(rows)


async def build_why_compact_card(
    runtime: PlatformRuntime,
    thread_id: ThreadId,
    *,
    event_id: str,
) -> str | None:
    """Return the ``/why-compact <event_id>`` dump, or ``None`` on miss.

    Accepts either the full ``evt_...`` id or a short prefix (matches
    :func:`_short_event_id`). When multiple compaction events share
    the prefix, the FIRST match wins (chronological).
    """
    events = await _collect_events(runtime, thread_id)
    target: Event | None = None
    query = event_id.strip()
    # Index revocations by the compaction they invalidate so the
    # dump's ``revoked_by`` field cross-references separate
    # ``CONTEXT_COMPACTION_REVOKED`` events — matching what the
    # ``/compaction-history`` table shows. Without this, a compaction
    # invalidated via ``/uncompact`` reads as ``revoked_by: null``
    # because the pointer lives on the revocation event, not the
    # compaction payload.
    revocations: dict[str, str] = {}
    for e in events:
        if e.type != EventType.CONTEXT_COMPACTION_REVOKED:
            continue
        rev_target = e.data.get("compaction_event_id")
        if isinstance(rev_target, str) and rev_target:
            revocations[rev_target] = str(e.event_id)
    for e in events:
        if e.type != EventType.CONTEXT_COMPACTED:
            continue
        eid = str(e.event_id)
        if eid == query or eid.startswith(query):
            target = e
            break
    if target is None:
        return None
    # Payload-level `revoked_by` (belt) wins over the cross-referenced
    # pointer (braces) so a variant that stamps the pointer inline
    # still shows the same value here.
    payload_revoked = target.data.get("revoked_by")
    revoked_by_event_id: str | None
    if isinstance(payload_revoked, str) and payload_revoked:
        revoked_by_event_id = payload_revoked
    else:
        revoked_by_event_id = revocations.get(str(target.event_id))
    payload = {
        "event_id": str(target.event_id),
        "created_at": (
            target.created_at.isoformat()
            if hasattr(target.created_at, "isoformat")
            else str(target.created_at)
        ),
        "trigger": target.data.get("trigger"),
        "preview": target.data.get("preview", False),
        "revoked_by": revoked_by_event_id,
        "revoked_by_event_id": revoked_by_event_id,
        "tokens_before": target.data.get("tokens_before"),
        "tokens_after": target.data.get("tokens_after"),
        "compacted_event_ids": target.data.get("compacted_event_ids") or [],
        "pre_compaction": target.data.get("pre_compaction") or {},
        "summary": target.data.get("summary") or {},
    }
    dumped = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return f"/why-compact {_short_event_id(str(target.event_id))}\n{dumped}"


__all__ = [
    "build_compaction_history_table",
    "build_why_compact_card",
]
