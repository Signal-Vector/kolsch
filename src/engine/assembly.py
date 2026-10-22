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

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from engine.framework import Message
from schemas import EventType

if TYPE_CHECKING:
    from schemas import Event


_log = logging.getLogger("engine.assembly")

# bug-064: dedup key for "log once per (thread_id, span_id)". Bounded
# to keep long-lived processes from growing without limit — the cap is
# generous (a healthy process should hit this path effectively never),
# and once the cap is reached we forget the oldest entry FIFO-style.
_PAIRING_WARN_DEDUP: set[tuple[str, str]] = set()
_PAIRING_WARN_DEDUP_MAX = 1024


def _in_test_mode() -> bool:
    """True when the current process is running under pytest.

    Uses the standard ``PYTEST_CURRENT_TEST`` environment variable
    pytest injects for every test invocation — no new project-level
    flag introduced. Falls back to inspecting ``sys.modules`` so a
    caller that imports assembly outside a running test (e.g. a
    conftest fixture setup step) still sees the honest answer.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    import sys  # noqa: PLC0415

    return "pytest" in sys.modules


# feat-054 Phase 2: bump from "1" → "2" because the projection table
# grows a case for CONTEXT_COMPACTED (skip the compacted span, emit
# one synthetic system-role message). Old-format logs that carry no
# CONTEXT_COMPACTED events project byte-identically under either
# version — the bump is a contract-level signal, not a byte-level
# breakage.
ASSEMBLY_VERSION = "2"

# The opening tag of every compaction-summary system message
# (`_format_compaction_summary`). bug-156 — the engine's history split
# recognises these messages by role + this prefix, because `Message`
# carries no metadata slot; keep the knowledge next to the formatter.
COMPACTION_MESSAGE_TAG = "<context-compaction>"


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
    respect_compaction: bool = True,
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
        respect_compaction: When True (v1 default), an active
            ``CONTEXT_COMPACTED`` event replaces its
            ``compacted_event_ids`` span with a single synthetic
            system message. When False, the span projects verbatim —
            used by ``/uncompact`` previews, replay diagnostics, and
            the engine's own pre-compaction estimator.

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
    messages = _project_events(events, respect_compaction=respect_compaction)

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


def _project_events(
    events: list[Event],
    *,
    respect_compaction: bool = True,
) -> list[Message]:
    """Project each event to a model-facing message.

    Projection table (v2 — feat-054):

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
    - `context.compacted` (v2, feat-054) — when
      ``respect_compaction`` is True AND the event is not revoked AND
      ``preview`` is False, skip every event listed in
      ``compacted_event_ids`` and emit ONE synthetic system message
      wrapping the summary JSON. When any predicate is False, project
      as a no-op (the compacted span projects verbatim).
    - `context.compaction_revoked` (v2) — no direct projection; marks
      the referenced compaction event as revoked for subsequent
      predicate checks.
    - All other types — not projected.
    """
    # Build the "revoked compactions" set first — a single reverse
    # walk over the path so a later revoke ↔ earlier compaction
    # linkage is honoured no matter which order they appear on the
    # path. Cheap even for long threads because we only inspect the
    # two event types.
    revoked: set[str] = set()
    for e in events:
        if e.type == EventType.CONTEXT_COMPACTION_REVOKED:
            target = e.data.get("compaction_event_id")
            if isinstance(target, str):
                revoked.add(target)

    # When respect_compaction is True, first pass over the path
    # identifies which event_ids sit inside an active compaction span
    # so the projection loop can skip them. An "active" compaction is
    # one that: is not preview, is not revoked (by external
    # revoked_by field OR by a compaction_revoked event on the path).
    compacted_span_ids: set[str] = set()
    if respect_compaction:
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
                    compacted_span_ids.add(eid)

    messages: list[Message] = []
    for event in events:
        # Skip events swallowed by an active compaction.
        if str(event.event_id) in compacted_span_ids:
            continue
        if event.type == EventType.CONTEXT_COMPACTED:
            # No-op when the event is preview / revoked / disabled by
            # the caller. Otherwise emit one synthetic system message.
            if not respect_compaction:
                continue
            if event.data.get("preview", False):
                continue
            if event.data.get("revoked_by") is not None:
                continue
            if str(event.event_id) in revoked:
                continue
            summary = event.data.get("summary") or {}
            if (
                not summary.get("compacted_event_ids")
                and not summary.get("summary_text")
                and not (event.data.get("compacted_event_ids") or [])
            ):
                # No-op payload (short session).
                continue
            messages.append(
                Message(
                    role="system",
                    content=_format_compaction_summary(summary),
                ),
            )
            continue
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

    # bug-064 — post-projection bidirectional pairing backstop.
    # spec-068 is the primary defense (the engine reconciles orphans
    # on resume); this check catches anything that still slipped
    # through. On well-formed input it is a byte-identical no-op.
    return _enforce_pairing_invariant(messages, events)


# ---------------------------------------------------------------------------
# bug-064 — Post-projection pairing backstop
# ---------------------------------------------------------------------------


def _enforce_pairing_invariant(
    messages: list[Message],
    events: list[Event],
) -> list[Message]:
    """Verify assistant ``tool_calls`` ids pair adjacently with tool msgs.

    The provider invariant this check enforces (strict adjacency, per
    the OpenAI chat-completions contract):

    * When an assistant message carries ``tool_calls=[id1..idN]``, the
      immediately-following contiguous run of ``role="tool"`` messages
      must carry every ``tool_call_id`` in that set (order within the
      run may vary). Any id from ``tool_calls`` that is missing from
      that adjacent run is an orphan-call — even if some later,
      non-adjacent tool message happens to carry the id, the provider
      400s because a non-tool message (user / assistant) broke the run.
    * A ``role="tool"`` message must sit inside the run immediately
      following an assistant whose ``tool_calls[]`` contains its
      ``tool_call_id``. A tool message with no preceding assistant
      tool_calls run — or one whose id isn't in that run's owning
      assistant — is an orphan-result.

    Existence-based checks (was: "matches SOMEWHERE later") let the
    wedge shape ``[assistant(tool_calls=[t1]), user, tool(t1)]`` slip
    through; the real provider still 400s that shape because a user
    message broke the adjacency requirement. The primary defense is
    spec-068's engine-side reconciliation running BEFORE the trigger
    user.message emit — this check is the belt-and-braces backstop
    that would surface any future regression loudly.

    Violations of either direction cause the provider to 400 the
    entire request.

    Behaviour on violation:

    * **Test mode** (``PYTEST_CURRENT_TEST`` set OR ``pytest`` in
      ``sys.modules``): raise ``AssertionError`` so an engine bug
      surfaces loudly in CI and local dev.
    * **Production**: log ONCE per (thread_id, leaf_event_id) at
      WARNING with enough detail to diagnose, and drop the
      offending unpaired message(s) from the returned list. The
      durable event log is never mutated; events are never
      reordered; no events are synthesized.

    "Drop" choice:

    * Orphan assistant ``tool_calls`` id → remove that id from the
      ``tool_calls[]`` array (siblings survive). If the list ends up
      empty, drop the whole assistant message.
    * Orphan ``role="tool"`` message → drop just that tool message.

    Well-formed input is returned unchanged (byte-identical) — this
    is Acceptance Criterion 2 (determinism).
    """
    # Single linear walk over the projected message list. For each
    # assistant with tool_calls, consume the contiguous run of tool
    # messages that immediately follows; any id in the assistant's
    # tool_calls[] that isn't covered by that adjacent run is an
    # orphan-call. Any tool message that lands outside such a run
    # (either standalone or under an assistant whose tool_calls[]
    # doesn't include its id) is an orphan-result.
    orphan_call_ids_by_msg: dict[int, set[str]] = {}
    orphan_tool_msg_indexes: set[int] = set()

    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            declared_ids = {str(tc.get("id", "")) for tc in msg.tool_calls}
            # Consume the immediately-following contiguous run of tool
            # messages. Their order relative to declared_ids doesn't
            # matter; the provider only requires that every declared id
            # appears in the run.
            j = i + 1
            covered_ids: set[str] = set()
            while j < n and messages[j].role == "tool":
                tid = messages[j].tool_call_id or ""
                if tid and tid in declared_ids:
                    covered_ids.add(tid)
                else:
                    # A tool message inside the run whose id isn't part
                    # of THIS assistant's tool_calls[] is an
                    # orphan-result — the provider would 400 on the
                    # stray id even though it sits adjacent to some
                    # assistant with tool_calls[].
                    orphan_tool_msg_indexes.add(j)
                j += 1
            # Any declared id not covered by the adjacent run is an
            # orphan-call. Empty-string ids can never match.
            missing = {tid for tid in declared_ids if not tid or tid not in covered_ids}
            if missing:
                orphan_call_ids_by_msg[i] = missing
            # Advance past the run we just consumed.
            i = j
            continue
        if msg.role == "tool":
            # A tool message that isn't inside a run following an
            # assistant with tool_calls[] is an orphan-result. (If it
            # were, the branch above would have consumed it.)
            orphan_tool_msg_indexes.add(i)
            i += 1
            continue
        # Any other role — advance.
        i += 1

    # Fast path — well-formed input is byte-identical. No copy, no
    # log, no mutation.
    if not orphan_call_ids_by_msg and not orphan_tool_msg_indexes:
        return messages

    # Malformed path. In test mode we fail fast so engine bugs
    # surface loudly.
    diagnostic = _pairing_diagnostic(
        events,
        orphan_call_ids_by_msg,
        orphan_tool_msg_indexes,
    )
    if _in_test_mode():
        error_msg = f"bug-064: assembly pairing invariant violated — {diagnostic}"
        raise AssertionError(error_msg)

    # Production degrade: log once per (thread_id, span_id), then
    # rebuild the in-memory list with offending messages dropped.
    _warn_once_pairing(events, diagnostic)
    return _drop_unpaired(messages, orphan_call_ids_by_msg, orphan_tool_msg_indexes)


def _drop_unpaired(
    messages: list[Message],
    orphan_call_ids_by_msg: dict[int, set[str]],
    orphan_tool_msg_indexes: set[int],
) -> list[Message]:
    """Return a new list with orphan tool_calls ids / tool msgs removed.

    Blast radius is kept minimal: sibling call ids in a multi-call
    turn survive; only the offending id is stripped. If stripping
    leaves an assistant with an empty ``tool_calls[]``, the whole
    assistant message is dropped (an empty ``tool_calls`` array is
    itself an invalid provider input).
    """
    out: list[Message] = []
    for i, msg in enumerate(messages):
        if i in orphan_tool_msg_indexes:
            continue
        if i in orphan_call_ids_by_msg and msg.tool_calls:
            drop_ids = orphan_call_ids_by_msg[i]
            kept = [
                tc for tc in msg.tool_calls if str(tc.get("id", "")) not in drop_ids
            ]
            if not kept:
                # Nothing left — drop the whole assistant turn. If
                # the message also had prose content it's still
                # dropped: the fold at :261-265 only creates
                # tool_call-carrying assistants either from an
                # AGENT_MESSAGE + tool.called sequence (prose kept,
                # calls appended) or from a bare tool.called
                # (empty content). In the first case dropping the
                # assistant loses prose the model produced; that's
                # the price of a genuinely malformed path. The prod
                # log carries the detail needed to reconstruct.
                continue
            out.append(msg.model_copy(update={"tool_calls": kept}))
        else:
            out.append(msg)
    return out


def _pairing_diagnostic(
    events: list[Event],
    orphan_call_ids_by_msg: dict[int, set[str]],
    orphan_tool_msg_indexes: set[int],
) -> str:
    """Human-readable summary for logs / assertions."""
    all_orphan_call_ids: set[str] = set()
    for ids in orphan_call_ids_by_msg.values():
        all_orphan_call_ids.update(ids)
    parts: list[str] = []
    if all_orphan_call_ids:
        parts.append(
            "orphan_call_ids=" + ",".join(sorted(all_orphan_call_ids)),
        )
    if orphan_tool_msg_indexes:
        parts.append(
            "orphan_tool_msg_indexes="
            + ",".join(str(i) for i in sorted(orphan_tool_msg_indexes)),
        )
    tid, sid, eid = _pairing_context(events)
    if tid:
        parts.append(f"thread_id={tid}")
    if sid:
        parts.append(f"session_id={sid}")
    if eid:
        parts.append(f"leaf_event_id={eid}")
    return " ".join(parts)


def _pairing_context(
    events: list[Event],
) -> tuple[str, str, str]:
    """Extract (thread_id, session_id, leaf_event_id) for diagnostics.

    All three fall back to the empty string when the event list is
    empty — the projector accepts an empty list (see the trivial
    no-history branch in the engine wrapper), and even then the
    check must run correctly (it just has nothing to diagnose).
    """
    if not events:
        return "", "", ""
    leaf = events[-1]
    return (
        str(leaf.thread_id),
        str(leaf.session_id),
        str(leaf.event_id),
    )


def _warn_once_pairing(events: list[Event], diagnostic: str) -> None:
    """Emit a WARNING log at most once per (thread_id, leaf_event_id).

    The dedup key uses (thread_id, leaf_event_id) — a retrying
    exchange sees the same leaf until it succeeds or advances, so
    this suppresses spam on retry loops while still surfacing a new
    warning if the same thread malforms a different span later.
    """
    tid, _sid, eid = _pairing_context(events)
    key = (tid, eid)
    if key in _PAIRING_WARN_DEDUP:
        return
    # Bounded — evict an arbitrary entry (set.pop) once we hit the
    # cap. Simplest bound that keeps memory bounded without wiring
    # a full LRU for a path that should almost never fire.
    if len(_PAIRING_WARN_DEDUP) >= _PAIRING_WARN_DEDUP_MAX:
        _PAIRING_WARN_DEDUP.pop()
    _PAIRING_WARN_DEDUP.add(key)
    _log.warning(
        "bug-064: assembly pairing invariant violated — dropping "
        "unpaired message(s) from in-memory request (durable log "
        "NOT mutated). %s",
        diagnostic,
    )


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


def is_compaction_summary_message(message: Any) -> bool:
    """Return whether ``message`` is a projected compaction summary.

    True for a ``system``-role message whose content starts with
    :data:`COMPACTION_MESSAGE_TAG` — the shape
    :func:`_format_compaction_summary` produces for a
    `context.compacted` event. Users cannot author ``system`` messages
    and memory blocks start with ``<memory>``, so role + prefix is
    unambiguous. Accepts any object with ``role`` / ``content``
    attributes (``engine.framework.Message`` in practice).
    """
    if getattr(message, "role", None) != "system":
        return False
    content = getattr(message, "content", None)
    return isinstance(content, str) and content.startswith(COMPACTION_MESSAGE_TAG)


def _format_compaction_summary(summary: dict[str, Any]) -> str:
    """Format a `context.compacted.data.summary` block as a system msg.

    Wraps the summary text + a stable JSON block so the next model
    call sees both the human-readable prose and the machine-parseable
    references. Framing text follows spec §7 verbatim so replay
    reproduces bytes.
    """
    summary_text = str(summary.get("summary_text") or "").strip()
    body = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    lines = [
        COMPACTION_MESSAGE_TAG,
        "The following prose summary replaces a span of earlier events "
        "that were compacted to fit the model's context window. Use "
        "`context.reference_lookup` with the event_ids in `refs` to "
        "retrieve original content when needed.",
    ]
    if summary_text:
        lines.append(summary_text)
    lines.append(f"<summary-json>{body}</summary-json>")
    lines.append("</context-compaction>")
    return "\n".join(lines)
