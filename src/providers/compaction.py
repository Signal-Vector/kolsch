"""Context compaction provider — modular, platform-config-driven.

Compaction shortens the projected message list an agent sends to the
LLM when a session approaches the model's context window. The provider
never rewrites the on-disk event log; it emits a
[`ContextCompactedData`][schemas.events.ContextCompactedData] payload
that Assembly consumes when projecting later exchanges.

This module owns the Protocol (:class:`ContextCompactionProvider`),
the v1 variant (:class:`ReferenceSummaryCompactionProvider`), a
token estimator (:func:`estimate_tokens_for_messages`), the boundary
snapper (:func:`snap_to_safe_boundary`), and a tiny registry seam
mirroring [`providers.memory`][providers.memory] and
[`providers.secrets`][providers.secrets] so future variants slot in
without touching bootstrap.

Section-absent semantics: when `platform.yaml` has no `compaction:`
section, :func:`make_compaction_provider` returns `None` and the
engine skips the trigger check entirely. This is the documented "off"
mode — matches the `author_workflow` precedent in
:mod:`assets.platform_config`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from errors import ConfigInvalid
from kernel import ValidationError, ValidationResult
from schemas import EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from engine import LLMRequest, LLMResponse
    from schemas import ModelSpec, ThreadId
    from schemas.events import ContextCompactedData, Event

_log = logging.getLogger("providers.compaction")


# ---------------------------------------------------------------------------
# Token estimator (tiktoken cl100k_base)
# ---------------------------------------------------------------------------


# Per-message and per-tool overhead constants — approximate the JSON
# framing every provider wraps around a chat message before the
# tokenizer sees it. Values chosen to match OpenAI's published
# `num_tokens_from_messages` reference for cl100k_base within a few
# tokens on typical inputs; provider variance is folded into the
# 5 %-ish safety margin the compaction trigger applies via
# `trigger_at` (default 0.80).
_PER_MESSAGE_OVERHEAD = 4
_PER_TOOL_OVERHEAD = 10


def estimate_tokens_for_messages(
    messages: list[dict[str, Any]],
    tools_schema: list[dict[str, Any]] | None,
) -> int:
    """Portable message-list token count using cl100k_base.

    Uses OpenAI's ``tiktoken`` cl100k_base encoding — the same
    tokenizer GPT-4-turbo / GPT-4o / GPT-3.5-turbo advertise. Anthropic
    and Google publish their own tokenizers but neither is materially
    different for typical chat text (differences fold into the
    ``trigger_at`` safety margin the platform already applies). The
    portability trade-off is deliberate: one estimator across every
    provider keeps the compaction seam simple, and the trigger is a
    threshold, not a hard cap.

    Args:
        messages: OpenAI-shape chat messages (``{"role", "content", ...}``).
        tools_schema: The tool-schema list the model saw for this call
            (from ``AssembledContext.tools``); ``None`` treated as empty.

    Returns:
        Estimated token count, always ``>= 0``. Empty inputs → ``0``.
    """
    import tiktoken  # noqa: PLC0415 — deferred to keep bootstrap cheap

    encoding = tiktoken.get_encoding("cl100k_base")

    total = 0
    for msg in messages:
        total += _PER_MESSAGE_OVERHEAD
        for _key, value in msg.items():
            if value is None:
                continue
            if isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                # Structured field (tool_calls list, name, tool_call_id
                # int) — JSON-serialize so the tokenizer sees the same
                # bytes the provider will.
                total += len(
                    encoding.encode(
                        json.dumps(value, sort_keys=True, separators=(",", ":"))
                    )
                )
    for tool in tools_schema or []:
        total += _PER_TOOL_OVERHEAD
        total += len(
            encoding.encode(
                json.dumps(tool, sort_keys=True, separators=(",", ":"))
            )
        )
    return total


# ---------------------------------------------------------------------------
# Boundary invariants — snap a candidate cut to a safe event group
# ---------------------------------------------------------------------------


def snap_to_safe_boundary(
    events: list[Event],
    candidate_cut: int,
    direction: Literal["earlier", "later"],
) -> int:
    """Adjust ``candidate_cut`` so it doesn't split any atomic event group.

    ``candidate_cut`` is a slice index: ``events[:candidate_cut]`` are
    on one side, ``events[candidate_cut:]`` on the other. The eight
    atomic groups per spec §4 are:

    1. ``tool.called`` ↔ its ``tool.result`` / ``tool.failed`` (same
       ``tool_call_id``).
    2. An assistant message with ``tool_calls`` ↔ ALL its
       ``tool.result``/``tool.failed`` events (approximated by the
       ``tool.called`` + ``tool.result`` pairing above — the assistant
       msg lives on ``agent.message`` outside a tool loop).
    3. ``MODEL_STREAMING_TOKEN`` sequences ↔ their ``MODEL_RESPONSE``
       (same ``call_id``).
    4. ``MODEL_REQUEST`` ↔ ``MODEL_RESPONSE`` (same ``call_id``).
    5. ``THREAD_EXCHANGE_STARTED`` ↔ its ``THREAD_EXCHANGE_COMPLETED``
       / ``THREAD_EXCHANGE_FAILED`` (linked via ``exchange_started_id``).
    6. ``SESSION_OPENED`` is never compacted (never included in the
       "middle span" — callers keep it in the head).
    7. ``MEMORY_RETRIEVED`` ↔ the following assistant turn
       (``agent.message``).
    8. Prior ``CONTEXT_COMPACTED`` events — v1 skips PAST the whole
       compacted span rather than nesting compaction. Callers who
       receive a cut inside a prior compaction span will get it
       extended past the span end (direction: later) or start
       (direction: earlier).

    Extends the cut monotonically in ``direction`` until no group
    straddles the boundary. ``"later"`` grows the tail (kept span
    grows, middle shrinks); ``"earlier"`` grows the head (rare — used
    by callers that want to force keeping less tail).
    """
    if not events:
        return candidate_cut
    n = len(events)
    cut = max(0, min(n, candidate_cut))
    step = 1 if direction == "later" else -1

    guard = n + 1  # safety: never loop more than the event count
    while 0 <= cut <= n and guard > 0:
        guard -= 1
        if _cut_is_safe(events, cut):
            return cut
        cut += step
        if cut < 0:
            return 0
        if cut > n:
            return n
    return cut


def _cut_is_safe(events: list[Event], cut: int) -> bool:
    """Return ``True`` when ``events[:cut] | events[cut:]`` respects invariants."""
    if cut <= 0 or cut >= len(events):
        return True
    head = events[:cut]
    tail = events[cut:]

    # Invariant 1 + 2: tool.called on one side, its result/failed on the
    # other. Match by ``tool_call_id``.
    called_ids_head: set[str] = set()
    called_ids_tail: set[str] = set()
    result_ids_head: set[str] = set()
    result_ids_tail: set[str] = set()
    # Invariant 3 + 4: MODEL_REQUEST / MODEL_STREAMING_TOKEN / MODEL_RESPONSE
    # for the same call_id.
    call_ids_head: set[str] = set()
    call_ids_tail: set[str] = set()
    response_ids_head: set[str] = set()
    response_ids_tail: set[str] = set()
    # Invariant 5: THREAD_EXCHANGE_STARTED / _COMPLETED / _FAILED.
    exchange_started_head: set[str] = set()
    exchange_started_tail: set[str] = set()
    exchange_closed_head: set[str] = set()
    exchange_closed_tail: set[str] = set()

    for e in head:
        _classify_event_for_boundary(
            e,
            called_ids_head,
            result_ids_head,
            call_ids_head,
            response_ids_head,
            exchange_started_head,
            exchange_closed_head,
        )
    for e in tail:
        _classify_event_for_boundary(
            e,
            called_ids_tail,
            result_ids_tail,
            call_ids_tail,
            response_ids_tail,
            exchange_started_tail,
            exchange_closed_tail,
        )

    # Invariant 1 + 2 — a tool.called whose result crossed the cut.
    if called_ids_head & result_ids_tail:
        return False
    if called_ids_tail & result_ids_head:
        return False
    # Invariant 3 + 4 — a MODEL_REQUEST whose response/tokens crossed.
    if call_ids_head & response_ids_tail:
        return False
    if call_ids_tail & response_ids_head:
        return False
    # Invariant 5 — exchange_started on one side, _completed/_failed on other.
    if exchange_started_head & exchange_closed_tail:
        return False
    if exchange_started_tail & exchange_closed_head:
        return False

    # Invariant 6 — SESSION_OPENED must be in the head (never split off).
    # Callers who supply `cut > 0` after a SESSION_OPENED at index 0 are
    # fine; we only reject if the cut would leave SESSION_OPENED in the
    # tail while events preceded it. Under normal inputs SESSION_OPENED
    # is the first event and any cut > 0 leaves it in head.
    for i, e in enumerate(tail):
        if e.type == EventType.SESSION_OPENED and (cut + i) != 0:
            return False

    # Invariant 7 — MEMORY_RETRIEVED must stay with the next agent.message.
    # If the last head event is MEMORY_RETRIEVED, the cut splits it from
    # the assistant turn.
    if head and head[-1].type == EventType.MEMORY_RETRIEVED:
        # If there IS a following agent.message in tail, we split it.
        for e in tail:
            if e.type == EventType.AGENT_MESSAGE:
                return False
            if e.type == EventType.USER_MESSAGE:
                break

    # Invariant 8 — prior CONTEXT_COMPACTED span. If the cut lands
    # inside the range spanned by a CONTEXT_COMPACTED's
    # ``compacted_event_ids`` (or on its emit event itself), the cut
    # is unsafe. v1 collapses each compaction into a single-emit
    # boundary — the emit event's own position — so any cut that
    # splits an emit from its span rejects.
    for e in head:
        if e.type == EventType.CONTEXT_COMPACTED:
            span_ids = set(e.data.get("compacted_event_ids") or [])
            for later in tail:
                if str(later.event_id) in span_ids:
                    return False
    for e in tail:
        if e.type == EventType.CONTEXT_COMPACTED:
            span_ids = set(e.data.get("compacted_event_ids") or [])
            for earlier in head:
                if str(earlier.event_id) in span_ids:
                    return False

    return True


def _classify_event_for_boundary(
    e: Event,
    called_ids: set[str],
    result_ids: set[str],
    call_ids: set[str],
    response_ids: set[str],
    exchange_started: set[str],
    exchange_closed: set[str],
) -> None:
    """Populate the boundary-check sets for a single event."""
    if e.type == EventType.TOOL_CALLED:
        tcid = str(e.data.get("tool_call_id") or "")
        if tcid:
            called_ids.add(tcid)
    elif e.type in (EventType.TOOL_RESULT, EventType.TOOL_FAILED):
        tcid = str(e.data.get("tool_call_id") or "")
        if tcid:
            result_ids.add(tcid)
    elif e.type == EventType.MODEL_REQUEST:
        cid = str(e.data.get("call_id") or "")
        if cid:
            call_ids.add(cid)
    elif e.type in (EventType.MODEL_RESPONSE, EventType.MODEL_STREAMING_TOKEN):
        cid = str(e.data.get("call_id") or "")
        if cid:
            response_ids.add(cid)
    elif e.type == EventType.THREAD_EXCHANGE_STARTED:
        exchange_started.add(str(e.event_id))
    elif e.type in (
        EventType.THREAD_EXCHANGE_COMPLETED,
        EventType.THREAD_EXCHANGE_FAILED,
    ):
        started_id = str(e.data.get("exchange_started_id") or "")
        if started_id:
            exchange_closed.add(started_id)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ContextCompactionProvider(Protocol):
    """The contract every compaction variant honours.

    Implementations receive the full assembled-event list plus the
    boundaries the engine has already computed (`keep_head` /
    `keep_tail`) and return a
    [`ContextCompactedData`][schemas.events.ContextCompactedData]
    payload. The engine emits the `context.compacted` event and
    Assembly folds it into the next projection.

    `keep_head` is 0 in v1 because kolsch's system prompt is derived at
    Assembly time (not stored on the event log) and therefore never
    part of `events`. The parameter is present so future variants that
    want to pin the first N events verbatim have a place to plug in.
    """

    async def compact(
        self,
        *,
        events: list[Event],
        keep_head: int,
        keep_tail: int,
        model_context_length: int,
        thread_id: ThreadId,
        config: dict[str, Any],
    ) -> ContextCompactedData:
        """Produce a compaction payload for `events`.

        Args:
            events: The full projected event list for the exchange.
            keep_head: Head events to preserve verbatim (0 in v1).
            keep_tail: Recent events to preserve verbatim.
            model_context_length: The model's declared context window
                in tokens; the provider budgets `refs`/`summary` size
                against this.
            thread_id: The thread whose context is being compacted.
            config: Variant-specific args from `platform.yaml`.
        """
        ...


# ---------------------------------------------------------------------------
# v1 variant — reference_summary
# ---------------------------------------------------------------------------


class ReferenceSummaryCompactionProvider:
    """The v1 `reference_summary` variant.

    Produces a JSON summary with `event_id` refs pointing back into
    the session log; downstream turns can retrieve originals via the
    `context.reference_lookup` tool. Phase 1 ships the constructor +
    registration only — the summariser algorithm lands in Phase 2.

    The `llm_for_model` factory is stored for Phase 2's summariser
    LLM call (the variant may reuse the exchange's model, or a
    configured `summarizer_model` — see `config`).
    """

    def __init__(
        self,
        *,
        llm_for_model: (
            Callable[
                [ModelSpec],
                Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
            ]
            | None
        ),
        config: dict[str, Any],
    ) -> None:
        self._llm_for_model = llm_for_model
        self._config = dict(config)

    async def compact(
        self,
        *,
        events: list[Event],
        keep_head: int,
        keep_tail: int,
        model_context_length: int,
        thread_id: ThreadId,
        config: dict[str, Any],
    ) -> ContextCompactedData:
        """Summarise the middle of ``events`` into a reference-summary payload.

        Algorithm (spec §4-§5):

        1. Compute the tail boundary — walk backward accumulating
           per-message token estimates until the running total exceeds
           ``keep_tail``. Snap the resulting cut ``later`` so no atomic
           group straddles it.
        2. Compute the head boundary — the head is the run of
           ``SESSION_OPENED`` / early config events; approximated as
           events preceding the first ``USER_MESSAGE`` (or ``0`` when
           no user message exists). Snap ``later``.
        3. Middle span is ``events[head_end:tail_start]``. If it is
           empty (short session) return a no-op payload.
        4. Invoke the summariser LLM with a JSON-only prompt asking
           for the spec §5 schema.
        5. Parse JSON, cap ``refs`` at ``max_refs`` (clustering
           overflow into ``event_id_range`` entries), and return.

        On summariser LLM failure or JSON parse failure, propagates
        the exception. The retry/fallback ladder lands in Phase 4.
        """
        from schemas.events import ContextCompactedData  # noqa: PLC0415

        max_refs = int(config.get("max_refs", 50))
        if max_refs < 1:
            max_refs = 1
        summarizer_model_cfg = config.get("summarizer_model")
        # feat-054 Phase 3 — `/compact --instructions "..."` and
        # `/compact --preview` reach the provider through these config
        # keys. `trigger` records what fired the compaction so the
        # emitted event's `trigger` field ("manual" / "manual_preview"
        # / "auto") reflects reality. `extra_instructions` bias the
        # summariser prompt without changing its JSON contract.
        extra_instructions = config.get("extra_instructions")
        if extra_instructions is not None and not isinstance(
            extra_instructions, str
        ):
            extra_instructions = str(extra_instructions)
        trigger_override = config.get("trigger")
        if trigger_override not in ("auto", "manual", "manual_preview"):
            trigger_override = "auto"
        preview_flag = bool(config.get("preview", False))

        # Step 1 — head boundary. Everything before the first user
        # message is considered head (SESSION_OPENED + early CONFIG_SET
        # + template warnings, if any). This matches the spec's
        # "after_system_events" boundary.
        head_end = 0
        for i, e in enumerate(events):
            if e.type == EventType.USER_MESSAGE:
                head_end = i
                break
        else:
            head_end = len(events)
        head_end = snap_to_safe_boundary(events, head_end, direction="later")

        # Step 2 — tail boundary. Walk backward accumulating tokens
        # from a rough per-event projection until we exceed keep_tail.
        # Snap the cut LATER so no atomic group straddles it (the tail
        # grows to keep the group whole; middle shrinks).
        cumulative = 0
        tail_start = len(events)
        for i in range(len(events) - 1, -1, -1):
            cumulative += _event_token_estimate(events[i])
            if cumulative >= keep_tail:
                tail_start = i
                break
        else:
            tail_start = 0
        tail_start = snap_to_safe_boundary(events, tail_start, direction="later")

        if tail_start <= head_end:
            # Middle is empty (short session) — Assembly will skip
            # this compaction, but emit it so /why-compact and tests
            # can see the attempt.
            return ContextCompactedData(
                compacted_event_ids=[],
                pre_compaction={
                    "message": "no-op — middle span empty",
                    "head_end": head_end,
                    "tail_start": tail_start,
                    "total_events": len(events),
                },
                summary={
                    "schema_version": 1,
                    "summary_text": "",
                    "refs": [],
                    "span": {
                        "first_event_id": None,
                        "last_event_id": None,
                        "event_count": 0,
                    },
                    "kept_files": [],
                    "compactor": {
                        "variant": "reference_summary",
                        "model": _summarizer_model_for(summarizer_model_cfg),
                    },
                },
                tokens_before=0,
                tokens_after=0,
                preview=preview_flag,
                trigger=trigger_override,  # type: ignore[arg-type]
            )

        middle = events[head_end:tail_start]
        # Materialised token estimate for the middle span (pre-compaction).
        pre_tokens = sum(_event_token_estimate(e) for e in middle)

        # Step 4 — summariser call. Prompt asks for the spec §5 JSON
        # schema explicitly; we defer to the caller-supplied
        # `llm_for_model` factory. On short-tests where no factory is
        # wired, raise so the trigger site can fall back / skip.
        if self._llm_for_model is None:
            raise RuntimeError(
                "ReferenceSummaryCompactionProvider.compact invoked without "
                "an `llm_for_model` factory — no way to reach a summariser."
            )
        model_spec = _resolve_summarizer_spec(summarizer_model_cfg)
        llm = await self._llm_for_model(model_spec)

        summariser_input = _build_summariser_prompt(
            middle,
            max_refs=max_refs,
            extra_instructions=extra_instructions,
        )
        from engine import LLMRequest  # noqa: PLC0415 — deferred import
        from engine.framework import Message  # noqa: PLC0415

        request = LLMRequest(
            messages=[
                Message(role=str(m["role"]), content=str(m["content"]))
                for m in summariser_input.messages
            ],
            tools=[],
            model=f"{model_spec.provider}/{model_spec.model}",
            parameters={},
        )
        response = await llm(request)
        raw_text = (response.content or "").strip()
        summary_body = _parse_summariser_json(raw_text)

        # Step 5 — cap refs. Anything beyond max_refs collapses into a
        # single trailing `event_id_range` entry per spec §5.
        refs = list(summary_body.get("refs") or [])
        if len(refs) > max_refs:
            kept = refs[: max_refs - 1]
            overflow = refs[max_refs - 1 :]
            overflow_ids: list[str] = []
            for r in overflow:
                if isinstance(r, dict):
                    eid = r.get("event_id")
                    if isinstance(eid, str):
                        overflow_ids.append(eid)
            if overflow_ids:
                kept.append(
                    {
                        "event_id_range": {
                            "first_event_id": overflow_ids[0],
                            "last_event_id": overflow_ids[-1],
                            "event_count": len(overflow_ids),
                        },
                        "note": (
                            "clustered overflow — original refs exceeded "
                            f"max_refs={max_refs}"
                        ),
                    }
                )
            summary_body["refs"] = kept

        compacted_event_ids = [str(e.event_id) for e in middle]
        summary_body.setdefault("schema_version", 1)
        summary_body.setdefault("summary_text", "")
        summary_body.setdefault("kept_files", [])
        summary_body.setdefault(
            "span",
            {
                "first_event_id": str(middle[0].event_id),
                "last_event_id": str(middle[-1].event_id),
                "event_count": len(middle),
            },
        )
        summary_body.setdefault(
            "compactor",
            {
                "variant": "reference_summary",
                "model": {
                    "provider": model_spec.provider,
                    "model": model_spec.model,
                },
            },
        )

        # Estimate tokens_after — projection of summary text.
        summary_text = str(summary_body.get("summary_text") or "")
        import tiktoken  # noqa: PLC0415

        encoding = tiktoken.get_encoding("cl100k_base")
        tokens_after = len(encoding.encode(summary_text)) + len(
            encoding.encode(
                json.dumps(summary_body, sort_keys=True, separators=(",", ":"))
            )
        )

        from schemas import EventId  # noqa: PLC0415

        return ContextCompactedData(
            compacted_event_ids=[EventId(eid) for eid in compacted_event_ids],
            pre_compaction={
                "head_end": head_end,
                "tail_start": tail_start,
                "total_events": len(events),
            },
            summary=summary_body,
            tokens_before=pre_tokens,
            tokens_after=tokens_after,
            preview=preview_flag,
            trigger=trigger_override,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Summariser helpers (v1 reference_summary internals)
# ---------------------------------------------------------------------------


class _SummariserInput:
    """Prompt bundle handed to the summariser LLM."""

    __slots__ = ("messages",)

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages


def _event_token_estimate(event: Event) -> int:
    """Rough per-event token estimate used to size the tail boundary.

    The Assembly Procedure eventually projects each event to a
    provider-shaped message; the exact projection is content-type
    dependent. For the tail-sizing walk we don't need the exact
    projection — a JSON-serialize + cl100k encode of the event's
    ``data`` dict is a stable upper bound that closely tracks
    Assembly's actual output. Deferring the tiktoken import keeps
    module-import cheap; the cost is one encoding lookup per event.
    """
    import tiktoken  # noqa: PLC0415

    encoding = tiktoken.get_encoding("cl100k_base")
    try:
        serialized = json.dumps(
            event.data, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        # Defensive — event.data is `dict[str, Any]` so any un-serializable
        # value implies a bug elsewhere. Fall back to a coarse estimate.
        serialized = str(event.data)
    # Constant per-message overhead (see estimate_tokens_for_messages).
    return len(encoding.encode(serialized)) + _PER_MESSAGE_OVERHEAD


def _summarizer_model_for(cfg: dict[str, Any] | None) -> dict[str, str]:
    """Return the summariser model dict for the compactor breadcrumb."""
    if cfg is None:
        return {"provider": "unknown", "model": "unknown"}
    return {
        "provider": str(cfg.get("provider", "unknown")),
        "model": str(cfg.get("model", "unknown")),
    }


def _resolve_summarizer_spec(cfg: dict[str, Any] | None) -> ModelSpec:
    """Turn the ``summarizer_model`` config dict into a ``ModelSpec``.

    Falls back to a placeholder when the operator hasn't declared one.
    The engine trigger site is expected to gate this behaviour
    (in practice `platform.yaml` always ships a `summarizer_model`
    when compaction is enabled); the placeholder keeps the type
    contract honest for tests.
    """
    from schemas import ModelSpec  # noqa: PLC0415

    if cfg is None:
        return ModelSpec(provider="mock", model="mock-summariser")
    return ModelSpec(
        provider=str(cfg.get("provider", "mock")),
        model=str(cfg.get("model", "mock-summariser")),
    )


_SUMMARISER_PROMPT_HEADER = (
    "You are the platform's context compactor. Read the numbered event "
    "list below and return ONLY a JSON object with the following schema "
    "(no prose before or after):\n"
    "{\n"
    '  "schema_version": 1,\n'
    '  "summary_text": "<3-8 sentence prose summary of what happened>",\n'
    '  "refs": [{"event_id": "<id>", "note": "<why this matters>"}...],\n'
    '  "kept_files": ["<path>", ...],\n'
    '  "span": {"first_event_id": "<id>", "last_event_id": "<id>", '
    '"event_count": <int>}\n'
    "}\n\n"
    "Guidance: summary_text captures decisions, blockers, and current "
    "task focus. refs cites individual event_ids the next turn is likely "
    "to need verbatim (file contents, error messages, computed values). "
    "kept_files lists any file paths whose contents were meaningful to "
    "the exchange. Return only the JSON object.\n\n"
    "Events (chronological):\n"
)


def _build_summariser_prompt(
    middle: list[Event],
    *,
    max_refs: int,
    extra_instructions: str | None = None,
) -> _SummariserInput:
    """Build the JSON-only summariser prompt.

    ``extra_instructions`` — feat-054 Phase 3 — is user-supplied
    guidance from `/compact --instructions "<text>"`. When present it
    is appended verbatim after the standard header so the summariser
    LLM can bias the ``summary_text`` accordingly (e.g. "focus on file
    edits", "preserve error messages verbatim"). The instructions
    never change the JSON schema contract — only the summary content.
    """
    lines: list[str] = []
    for e in middle:
        try:
            payload = json.dumps(
                e.data, sort_keys=True, separators=(",", ":"), default=str
            )
        except (TypeError, ValueError):
            payload = str(e.data)
        lines.append(f"[{e.event_id}] {e.type}: {payload}")
    body = _SUMMARISER_PROMPT_HEADER + "\n".join(lines)
    body += (
        f"\n\nCap refs at {max_refs} entries; if you would list more, "
        "keep the most useful ones."
    )
    if extra_instructions:
        body += (
            "\n\nAdditional operator guidance for this compaction "
            f"(honour it while still returning the JSON schema above): "
            f"{extra_instructions}"
        )
    return _SummariserInput(
        messages=[{"role": "user", "content": body}],
    )


def _parse_summariser_json(raw: str) -> dict[str, Any]:
    """Parse the summariser's JSON reply; be tolerant of code-fence framing.

    Accepts:

    - Bare JSON: ``{...}``.
    - Fenced JSON: ``` ```json\\n{...}\\n``` ``` or ``` ```{...}``` ```.

    Raises ``ValueError`` on parse failure — the trigger site catches
    to skip the compaction for this exchange (Phase 2 behaviour; the
    Phase 4 failure ladder adds retry).
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip fence — first line is ``` or ```json; last line is ```.
        parts = text.split("\n", 1)
        if len(parts) == 2:
            text = parts[1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")].rstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"summariser returned non-JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"summariser returned a non-object JSON value: {type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Layer 3 validator (kernel ConfigValidator signature)
# ---------------------------------------------------------------------------


def validate_reference_summary_config(
    body: dict[str, Any],
) -> ValidationResult:
    """Validate a `reference_summary` config body at Layer 3.

    Mirrors :func:`providers.memory.validate_recent_buffer_config`.
    Checks:

    - `trigger_at` (the platform-level threshold, but the whole
      compaction dict is passed to the validator per convention) must
      be a float in `(0, 1]` if present.
    - `config.keep_recent_fraction` must be a float in `(0, 1]` if
      present.
    - `config.max_refs` must be an int >= 1 if present.
    - `config.summarizer_model` must be a mapping with `provider` and
      `model` keys if present.

    Fields absent from `body` are permitted — the provider fills
    defaults at construction time.
    """
    errors: list[ValidationError] = []

    trigger_at = body.get("trigger_at")
    if trigger_at is not None:
        if isinstance(trigger_at, bool) or not isinstance(trigger_at, (int, float)):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/trigger_at",
                    code="not_a_number",
                    message=(
                        f"trigger_at must be a number in (0, 1]; "
                        f"got {type(trigger_at).__name__}"
                    ),
                )
            )
        elif not (0 < float(trigger_at) <= 1):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/trigger_at",
                    code="out_of_range",
                    message=(
                        f"trigger_at must be in (0, 1]; got {trigger_at}"
                    ),
                )
            )

    inner = body.get("config") or {}
    if not isinstance(inner, dict):
        errors.append(
            ValidationError(
                layer=3,
                source="compaction.reference_summary",
                path="/config",
                code="not_a_mapping",
                message=(
                    f"config must be a mapping; got {type(inner).__name__}"
                ),
            )
        )
        return ValidationResult(ok=not errors, errors=errors)

    keep_recent = inner.get("keep_recent_fraction")
    if keep_recent is not None:
        if isinstance(keep_recent, bool) or not isinstance(keep_recent, (int, float)):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/keep_recent_fraction",
                    code="not_a_number",
                    message=(
                        f"keep_recent_fraction must be a number in (0, 1]; "
                        f"got {type(keep_recent).__name__}"
                    ),
                )
            )
        elif not (0 < float(keep_recent) <= 1):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/keep_recent_fraction",
                    code="out_of_range",
                    message=(
                        f"keep_recent_fraction must be in (0, 1]; "
                        f"got {keep_recent}"
                    ),
                )
            )

    max_refs = inner.get("max_refs")
    if max_refs is not None:
        if isinstance(max_refs, bool) or not isinstance(max_refs, int):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/max_refs",
                    code="not_an_int",
                    message=(
                        f"max_refs must be an integer; got {type(max_refs).__name__}"
                    ),
                )
            )
        elif max_refs < 1:
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/max_refs",
                    code="out_of_range",
                    message=(f"max_refs must be >= 1; got {max_refs}"),
                )
            )

    summarizer = inner.get("summarizer_model")
    if summarizer is not None:
        if not isinstance(summarizer, dict):
            errors.append(
                ValidationError(
                    layer=3,
                    source="compaction.reference_summary",
                    path="/config/summarizer_model",
                    code="not_a_mapping",
                    message=(
                        f"summarizer_model must be a mapping; "
                        f"got {type(summarizer).__name__}"
                    ),
                )
            )
        else:
            for required in ("provider", "model"):
                value = summarizer.get(required)
                if not isinstance(value, str) or not value:
                    errors.append(
                        ValidationError(
                            layer=3,
                            source="compaction.reference_summary",
                            path=f"/config/summarizer_model/{required}",
                            code="missing_or_not_a_string",
                            message=(
                                f"summarizer_model.{required} must be a "
                                f"non-empty string"
                            ),
                        )
                    )

    return ValidationResult(ok=not errors, errors=errors)


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


CompactionFactory = "Callable[..., ContextCompactionProvider]"
_REGISTRY: dict[str, Callable[..., ContextCompactionProvider]] = {}


def register_compaction_variant(
    name: str,
    factory: Callable[..., ContextCompactionProvider],
) -> None:
    """Register a variant factory under `name`. Idempotent on same input."""
    _REGISTRY[name] = factory


def make_compaction_provider(
    *,
    config: dict[str, Any],
    llm_for_model: (
        Callable[
            [ModelSpec],
            Awaitable[Callable[[LLMRequest], Awaitable[LLMResponse]]],
        ]
        | None
    ),
) -> ContextCompactionProvider | None:
    """Dispatch by `config['variant']` — silent-disable on empty config.

    `config` is the whole `compaction:` block from `platform.yaml`
    (e.g. `{"variant": "reference_summary", "trigger_at": 0.8,
    "config": {...}}`). When the block is missing or empty, returns
    `None` — the engine treats `None` as "compaction disabled" and
    skips the trigger check.

    Raises:
        ConfigInvalid: `variant` is set but names no registered factory.
    """
    if not config:
        return None
    variant = config.get("variant")
    if variant is None:
        return None
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant)
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.compaction",
                    "path": "/compaction/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown compaction variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.compaction.make_compaction_provider",
        )
    return factory(llm_for_model=llm_for_model, config=body)


# Self-register the v1 variant at import time.
register_compaction_variant("reference_summary", ReferenceSummaryCompactionProvider)
