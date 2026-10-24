"""Authz introspection surface — feat-041.

Three chat-surface features share this module:

- ``format_denial_hint`` — the 2-line hint appended to a failed
  ``ToolCallBlock``'s body when the failure was an authz deny (see
  spec §Mockup 1). Proactive: no command required, every deny gets
  the breadcrumb inline.
- ``render_denial_card`` — the boxed card `/why` mounts in the
  transcript (see spec §Mockup 2). Fills in the deny's fuller
  "here's exactly why" explanation, correlated to a past
  ``tool.failed`` in the current thread.
- ``handle_reload`` — the ``/reload`` handler that rebuilds the
  runtime's authz provider from every config layer, atomically
  swapping on success and printing the failure while preserving the
  old provider on any parse / validate error.

All Static widgets that mount card / hint text ride ``markup=False``
so rule patterns containing ``[...]`` render literally (bug-040 +
bug-042 lessons).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from platform_runtime import PlatformRuntime
    from schemas import ThreadId
    from schemas.events import Event


# ---------------------------------------------------------------------------
# Path formatting
# ---------------------------------------------------------------------------


def _tildify(path_str: str | None) -> str:
    """Return ``~/rest`` when the path is under ``$HOME``, else the input.

    Missing/absent paths return ``"(unknown)"`` so downstream
    formatters get a printable value without branching.
    """
    if not path_str:
        return "(unknown)"
    home = str(Path.home())
    if path_str.startswith(home + "/"):
        return "~" + path_str[len(home) :]
    if path_str == home:
        return "~"
    return path_str


def _osc8(target_path: str | None, display: str) -> str:
    """Wrap ``display`` in an OSC-8 hyperlink pointing at ``file://<target>``.

    Terminals that don't support OSC-8 (older gnome-terminal, plain
    xterm) render the display text verbatim, so callers pay no
    graceful-degradation cost. Passing ``None`` for the target
    returns the display text untouched — no hyperlink to attach.
    """
    if not target_path:
        return display
    return f"\x1b]8;;file://{target_path}\x07{display}\x1b]8;;\x07"


# ---------------------------------------------------------------------------
# Inline hint (Mockup 1)
# ---------------------------------------------------------------------------


def format_denial_hint(matched_rule: dict[str, Any]) -> str:
    """Return the 2-line hint appended to a failed ``ToolCallBlock``.

    Line 1: ``denied by <rule_id> (<effect>) at <source_or_unknown>``
    (source path OSC-8-linked when present).
    Line 2: ``pattern <pattern_or_asterisk> — /reload to reapply
    after editing``.

    Rendered as plain text (the enclosing Static uses
    ``markup=False``). ``matched_rule`` is the model-dumped
    :class:`providers.authz.Rule` — a dict — so this formatter is
    trivially unit-testable.
    """
    rule_id = matched_rule.get("id") or "(unknown-id)"
    effect = matched_rule.get("effect") or "?"
    source_file = matched_rule.get("source_file")
    source_line = matched_rule.get("source_line")
    if source_file and source_line:
        display_source = f"{_tildify(source_file)}:{source_line}"
        linked = _osc8(source_file, display_source)
        at_clause = f"at {linked}"
    else:
        at_clause = "at (unknown source)"
    pattern = matched_rule.get("pattern") or matched_rule.get("what") or "*"
    line1 = f"denied by {rule_id} ({effect}) {at_clause}"
    if _is_locked_denial(matched_rule):
        line2 = (
            "locked rule — workflow/agent/bundle allows can't override it; "
            "edit authorization.config.locked_rules in platform.yaml, then /reload"
        )
    else:
        line2 = f"pattern {pattern} — /reload to reapply after editing"
    return f"{line1}\n{line2}"


def _is_locked_denial(matched_rule: dict[str, Any]) -> bool:
    """Return True when `matched_rule` is a locked platform rule's deny.

    Keyed on the `rl_locked_<hash>` id, which only the locked stage
    assigns (author-written ids with the prefix are refused at load),
    and on `effect: deny`, the only effect a locked rule can have — so
    a rule that somehow carried the prefix with another effect is not
    reported as locked.
    """
    from providers import LOCKED_RULE_ID_PREFIX  # noqa: PLC0415

    rule_id = matched_rule.get("id")
    return (
        isinstance(rule_id, str)
        and rule_id.startswith(LOCKED_RULE_ID_PREFIX)
        and matched_rule.get("effect") == "deny"
    )


# ---------------------------------------------------------------------------
# /why card (Mockup 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DenialCardData:
    """The fields ``render_denial_card`` reads.

    All optional-string fields default to ``None`` so a partial
    correlation (``tool.failed`` present but no
    ``policy.decision.made``) still assembles a card with
    ``(unknown)`` placeholders.
    """

    tool_name: str
    target: str | None
    time_hhmmss: str
    rule_id: str | None
    effect: str | None
    layer: str | None
    source_file: str | None
    source_line: int | None
    what: str | None
    who: str | None
    pattern: str | None
    reason: str | None
    compaction_badge: str | None = None
    """feat-054 Phase 3 — set when the referenced tool call sits inside
    an active ``CONTEXT_COMPACTED.compacted_event_ids`` range. The
    renderer appends the badge on a trailing line so the operator
    knows the live model may not remember the call.

    ``None`` when the call is not in any active compacted range OR
    when compaction is not in use on this thread.
    """


_CARD_INNER_WIDTH = 60
_CARD_LABEL_WIDTH = 12


def _card_line(text: str) -> str:
    """One card body line: `│ <padded text> │`.

    Pads with spaces to keep the right border aligned regardless of
    text length. Text longer than the inner width is truncated at
    render time by the wrapping caller — never inline.
    """
    padded = text.ljust(_CARD_INNER_WIDTH)
    return f"│ {padded} │"


def _card_field(label: str, value: str) -> str:
    """One `<label>: <value>` line inside the card body.

    Label sits at column 0 padded to `_CARD_LABEL_WIDTH`; value
    starts immediately after. Values fitting inside
    ``_CARD_INNER_WIDTH`` render on a single line.
    """
    label_col = f"{label}:".ljust(_CARD_LABEL_WIDTH)
    return _card_line(f"{label_col}{value}")


def _card_wrapped_field(label: str, value: str) -> list[str]:
    """Word-wrap ``value`` with a hanging indent at ``_CARD_LABEL_WIDTH``.

    Long reasons (spec calls out >200 chars) render across multiple
    body lines, each aligned under the initial value column. No
    truncation — the card grows vertically as needed.
    """
    label_col = f"{label}:".ljust(_CARD_LABEL_WIDTH)
    remaining_width = _CARD_INNER_WIDTH - _CARD_LABEL_WIDTH
    lines: list[str] = []
    words = value.split()
    if not words:
        return [_card_line(f"{label_col}(no value)")]
    current: list[str] = []
    current_len = 0
    for word in words:
        # +1 for the space between words (skipped on the first).
        add = (1 if current else 0) + len(word)
        if current_len + add > remaining_width and current:
            joined = " ".join(current)
            prefix = label_col if not lines else " " * _CARD_LABEL_WIDTH
            lines.append(_card_line(f"{prefix}{joined}"))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += add
    if current:
        joined = " ".join(current)
        prefix = label_col if not lines else " " * _CARD_LABEL_WIDTH
        lines.append(_card_line(f"{prefix}{joined}"))
    return lines


def render_denial_card(data: DenialCardData) -> str:
    """Render the deny card (see Mockup 2) as plain text.

    Uses rounded box-drawing chars (`╭╮╰╯`), fixed left label
    column, and word-wrapped `reason:`. Path fields tildified under
    `$HOME`; unset optionals render as `(unknown)` or `(no target)`.

    The caller mounts the result as ``Static(text, markup=False)`` —
    rule patterns and reasons often contain ``[...]`` which Textual's
    markup parser would otherwise choke on (bug-040 lesson).
    """
    # Title lives inside the top border (`╭─ denied ────╮`); redo the
    # length math to keep the total width `_CARD_INNER_WIDTH + 2`.
    title = "─ denied "
    top = "╭" + title + ("─" * (_CARD_INNER_WIDTH + 2 - len(title))) + "╮"
    bottom = "╰" + ("─" * (_CARD_INNER_WIDTH + 2)) + "╯"

    body_lines: list[str] = []
    body_lines.append(_card_field("tool", data.tool_name or "(unknown)"))
    body_lines.append(_card_field("target", data.target or "(no target)"))
    body_lines.append(_card_field("time", data.time_hhmmss))
    body_lines.append(_card_line(""))
    rule_display = data.rule_id or "(unknown)"
    if data.effect:
        rule_display = f"{rule_display}  ({data.effect})"
    body_lines.append(_card_field("rule", rule_display))
    body_lines.append(_card_field("layer", data.layer or "(unknown)"))
    if data.source_file and data.source_line:
        source_display = f"{_tildify(data.source_file)}:{data.source_line}"
        # OSC-8 the reference so cmd-click / ctrl-click opens the
        # file in supporting terminals; unsupported terminals render
        # the display text as plain output.
        source_display_osc8 = _osc8(data.source_file, source_display)
        body_lines.append(_card_field("source", source_display_osc8))
    else:
        body_lines.append(_card_field("source", "(unknown)"))
    body_lines.append(_card_line(""))
    body_lines.append(_card_field("what", data.what or "(unknown)"))
    body_lines.append(_card_field("who", data.who or "*"))
    body_lines.append(_card_field("pattern", data.pattern or "*"))
    body_lines.append(_card_line(""))
    body_lines.extend(_card_wrapped_field("reason", data.reason or "(unknown)"))

    lines = [top, *body_lines, bottom]
    if data.source_file and data.source_line:
        lines.append(
            f"  ↳ Edit {_tildify(data.source_file)} at line "
            f"{data.source_line}, then /reload.",
        )
    else:
        lines.append(
            "  ↳ grep your platform.yaml / bundle.yaml / workflow YAML "
            f"for `id: {data.rule_id or '<rule-id>'}`.",
        )
    if data.compaction_badge:
        lines.append(f"  {data.compaction_badge}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /why — event correlation via the store
# ---------------------------------------------------------------------------


async def find_last_denial_via_store(
    runtime: PlatformRuntime,
    thread_id: ThreadId,
    *,
    tool_call_id: str | None = None,
) -> DenialCardData | None:
    """Walk the thread's events; return card data for the target deny.

    Iterates ``runtime.store.read_path_to_leaf(thread_id)`` (same
    pattern as ``/export`` at ``src/cli/chat/app.py:1347``). Two
    passes over the collected event list:

    1. From the tail, find the target ``tool.failed`` — the specific
       ``tool_call_id`` when supplied, else the most recent one whose
       ``error.detail.matched_rule`` is non-null.
    2. Correlate to a ``policy.decision.made`` — preferred lookup is
       by ``tool_call_id`` (populated post-feat-041); fallback is the
       most-recent-preceding ``policy.decision.made`` whose ``action``
       matches the failed tool's action.

    Returns ``None`` when no matching ``tool.failed`` exists (caller
    prints the "no recent denial" hint). Returns a partial
    ``DenialCardData`` (with ``(unknown)`` optionals) when the deny
    exists but no correlated ``policy.decision.made`` does — feat-040
    or earlier sessions carry `tool.failed` without a paired policy
    event; the fallback keeps `/why` useful even then.
    """
    events: list[Event] = []
    async for event in runtime.store.read_path_to_leaf(thread_id):
        events.append(event)

    tool_failed = _find_target_tool_failed(events, tool_call_id=tool_call_id)
    if tool_failed is None:
        return None
    data = _assemble_card_data(events, tool_failed)
    # feat-054 Phase 3 — badge the card when the failed tool call sits
    # inside an active compacted range. The correlated tool.called (or
    # the failure itself, when the call preceded it) is the anchor we
    # look up in each active CONTEXT_COMPACTED.compacted_event_ids.
    badge = _compaction_badge_for_call(
        events,
        tool_failed=tool_failed,
    )
    if badge is not None:
        data = _replace_card(data, compaction_badge=badge)
    return data


def _replace_card(
    card: DenialCardData,
    **updates: Any,
) -> DenialCardData:
    """Return a new ``DenialCardData`` with ``updates`` merged in.

    Kept as a small helper so the badge injection stays one line at
    the call site (the dataclass is frozen — ``dataclasses.replace``
    is the idiomatic path but takes an import; a plain constructor
    call is clearer and avoids reaching for the stdlib helper).
    """
    from dataclasses import replace  # noqa: PLC0415

    return replace(card, **updates)


def _compaction_badge_for_call(
    events: list[Event],
    *,
    tool_failed: Event,
) -> str | None:
    """Return the compaction-range badge text, or ``None`` when not applicable.

    Walks every ``CONTEXT_COMPACTED`` event on the thread (skipping
    revoked ones — a revocation is recorded as a following
    ``CONTEXT_COMPACTION_REVOKED`` pointing back at the compaction's
    ``event_id``). For each active compaction, checks whether the
    ``tool.failed`` event or its correlated ``tool.called`` event id
    appears in ``compacted_event_ids``. On hit, returns the badge
    string carrying the compaction event id so the operator can jump
    to ``/why-compact <event_id>``.
    """
    # Index revocations so we skip inactive compactions.
    revoked_ids: set[str] = set()
    for e in events:
        if str(e.type) != "context.compaction_revoked":
            continue
        target = _event_data(e).get("compaction_event_id")
        if isinstance(target, str) and target:
            revoked_ids.add(target)

    # Anchor id: the tool.failed event itself, plus the correlated
    # tool.called (looked up by tool_call_id) — either being inside a
    # compacted range means the model has forgotten the call.
    anchor_ids: set[str] = set()
    failed_id = getattr(tool_failed, "event_id", None)
    if failed_id is not None:
        anchor_ids.add(str(failed_id))
    tool_call_id = _event_data(tool_failed).get("tool_call_id")
    if tool_call_id:
        for e in events:
            if str(e.type) != "tool.called":
                continue
            payload = _event_data(e)
            if payload.get("tool_call_id") == tool_call_id:
                called_id = getattr(e, "event_id", None)
                if called_id is not None:
                    anchor_ids.add(str(called_id))
                break

    if not anchor_ids:
        return None

    for e in events:
        if str(e.type) != "context.compacted":
            continue
        compaction_id = getattr(e, "event_id", None)
        if compaction_id is not None and str(compaction_id) in revoked_ids:
            continue
        payload = _event_data(e)
        # `revoked_by` set on the payload → also inactive (belt +
        # braces with the revocation-event scan above).
        if payload.get("revoked_by"):
            continue
        compacted_ids = payload.get("compacted_event_ids") or []
        if not isinstance(compacted_ids, list):
            continue
        compacted_set = {str(cid) for cid in compacted_ids}
        if anchor_ids & compacted_set:
            display_id = compaction_id if compaction_id is not None else "?"
            return (
                f"↳ (this call is inside compacted range {display_id}; "
                "the LIVE model may not remember it)"
            )
    return None


def _find_target_tool_failed(
    events: list[Event],
    *,
    tool_call_id: str | None,
) -> Event | None:
    """Return the target ``tool.failed`` event, or ``None`` on miss."""
    for event in reversed(events):
        if str(event.type) != "tool.failed":
            continue
        data = _event_data(event)
        if tool_call_id is not None:
            if data.get("tool_call_id") == tool_call_id:
                return event
            continue
        # No arg supplied — grab the most recent tool.failed whose
        # error carries a matched_rule (i.e. authz denial). Non-authz
        # failures (crashes, timeouts) don't have anything for the
        # card to show; skip them.
        error = data.get("error") or {}
        detail = error.get("detail") or {}
        matched = detail.get("matched_rule")
        if matched:
            return event
    return None


def _assemble_card_data(
    events: list[Event],
    tool_failed: Event,
) -> DenialCardData:
    """Build ``DenialCardData`` from a ``tool.failed`` + correlated policy event."""
    data = _event_data(tool_failed)
    error = data.get("error") or {}
    detail = error.get("detail") or {}
    matched_rule = detail.get("matched_rule") or {}
    action = detail.get("action") or ""
    tool_call_id = data.get("tool_call_id")
    tool_name = data.get("tool_name") or _tool_from_action(action)

    policy_event = _find_correlated_policy(
        events,
        tool_call_id=tool_call_id,
        action=action,
        before_event=tool_failed,
    )
    policy_payload = _event_data(policy_event) if policy_event is not None else {}

    # Source metadata: prefer the matched_rule (present since the AC #1
    # Rule extension). Fall back to the policy event (feat-041 payload
    # additions) when the tool.failed predates AC #1.
    source_file = matched_rule.get("source_file") or policy_payload.get("source_file")
    source_line = matched_rule.get("source_line") or policy_payload.get("source_line")

    # Layer name isn't hoisted onto the wire today (spec §Design
    # ack); the reason string encodes it (see
    # `_match_against_rules._reason_for_match` in authz.py) and the
    # renderer trims that off when it lands.
    layer = _extract_layer_from_reason(
        matched_rule.get("reason") or policy_payload.get("reason") or "",
    )
    # A locked rule is identifiable from its id alone, so its layer is
    # known even though no reason string reaches this reader.
    if layer is None and _is_locked_denial(matched_rule):
        layer = "locked"

    # Target string — prefer the policy event (which normalises None
    # → the empty string) so the card shows what the matcher saw.
    target = policy_payload.get("target") if policy_event is not None else None
    if target is None:
        # Best-effort — tool.failed payload doesn't carry the target
        # directly; the arguments do but the target-extraction rule
        # varies per tool. Leave as None so the card renders
        # `(no target)`.
        target = None

    reason = matched_rule.get("reason") or error.get("message") or None

    return DenialCardData(
        tool_name=tool_name,
        target=target,
        time_hhmmss=_format_event_time(tool_failed),
        rule_id=matched_rule.get("id"),
        effect=matched_rule.get("effect"),
        layer=layer,
        source_file=source_file,
        source_line=source_line,
        what=matched_rule.get("what"),
        who=matched_rule.get("who") or "*",
        pattern=matched_rule.get("pattern"),
        reason=reason,
    )


def _find_correlated_policy(
    events: list[Event],
    *,
    tool_call_id: str | None,
    action: str,
    before_event: Event,
) -> Event | None:
    """Return the ``policy.decision.made`` event tied to ``before_event``.

    Preferred lookup is by ``tool_call_id`` (post-feat-041). Fallback:
    the most recent preceding ``policy.decision.made`` whose
    ``action`` matches. ``before_event`` is the ``tool.failed``
    envelope; policy events that come after it can't be the cause.
    """
    seen_before = False
    result: Event | None = None
    for event in events:
        if event is before_event:
            seen_before = True
        if str(event.type) != "policy.decision.made":
            continue
        # A policy event that lands after the tool.failed we're
        # correlating can't be its cause. Bail once we've walked
        # past the failure.
        if seen_before:
            break
        payload = _event_data(event)
        if tool_call_id is not None and payload.get("tool_call_id") == tool_call_id:
            return event
        if payload.get("action") == action:
            result = event
    return result


def _event_data(event: Event) -> dict[str, Any]:
    """Return the event's payload dict regardless of typed / dict shape.

    ``store.read_path_to_leaf`` yields typed :class:`schemas.Event`
    objects; the payload lives on ``event.data``. Convert to dict for
    consistent field access — the runtime pydantic model won't
    complain about extra fields the introspection reads.
    """
    raw = getattr(event, "data", None)
    if raw is None:
        return {}
    if hasattr(raw, "model_dump"):
        dumped = raw.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


def _tool_from_action(action: str) -> str:
    """Extract a display tool name from an action string.

    Actions look like ``kolsch/tools/write_file`` or
    ``mcp.server.tool``; the card renders just the trailing segment.
    """
    if not action:
        return "(unknown)"
    return action.rsplit("/", 1)[-1]


def _format_event_time(event: Event) -> str:
    """Return ``HH:MM:SS`` for the event's timestamp.

    ``Event.event_id`` carries a monotonic tag but not a
    human-readable time; ``Event.time`` or ``Event.created_at`` is
    the common field. Falls back to the current wall clock so the
    card always has something printable.
    """
    for attr in ("time", "created_at", "timestamp"):
        value = getattr(event, attr, None)
        if isinstance(value, datetime):
            return value.strftime("%H:%M:%S")
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.strftime("%H:%M:%S")
            except ValueError:
                continue
    # Wall-clock fallback for events whose typed time field went
    # unfilled — chat surface reads local wall-clock; the card
    # is display-only, so a naive `now()` is fine here.
    return datetime.now().strftime("%H:%M:%S")  # noqa: DTZ005


def _extract_layer_from_reason(reason: str) -> str | None:
    """Best-effort layer name pulled off the reason string.

    `AuthorizationResult.reason` looks like
    ``matched deny rule 'rl_...' in layer 'platform' — ...``.
    Return the quoted layer name when the shape matches, else None
    so the card renders `(unknown)`.
    """
    marker = "in layer '"
    idx = reason.find(marker)
    if idx == -1:
        return None
    end = reason.find("'", idx + len(marker))
    if end == -1:
        return None
    return reason[idx + len(marker) : end] or None


# ---------------------------------------------------------------------------
# /reload — atomic provider swap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReloadResult:
    """Outcome of a ``/reload`` invocation.

    Exactly one of ``summary`` / ``error`` is populated. Callers
    (``ChatApp._handle_slash`` for ``/reload``) inspect these to
    decide whether to render the success line or the error line.
    """

    summary: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _CatalogDiff:
    """Rescan diff — added / removed bundles + agents + workflows.

    Populated by ``_diff_catalogs`` when the fresh filesystem scan
    reveals new or deleted YAMLs since the last reload. Formatted into
    the reload success summary via ``_format_catalog_diff``.
    """

    bundles_added: tuple[str, ...] = ()
    bundles_removed: tuple[str, ...] = ()
    agents_added: tuple[str, ...] = ()
    agents_removed: tuple[str, ...] = ()
    workflows_added: tuple[str, ...] = ()
    workflows_removed: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when the fresh scan matches the previous catalog exactly."""
        return not (
            self.bundles_added
            or self.bundles_removed
            or self.agents_added
            or self.agents_removed
            or self.workflows_added
            or self.workflows_removed
        )


# Missing-YAML contract (qol-051 C2): both ``_reload_bundles`` and
# ``_reload_agents`` treat a YAML that has vanished from disk between
# rescan and reload as an ABORT — the rescan step at the top of
# ``handle_reload`` (qol-051 C1) drops disappeared YAMLs from the fresh
# catalog before either helper runs, so a missing-YAML failure at this
# layer means the file disappeared MID-RELOAD (the rescan built the
# catalog entry, then the per-layer reload tried to re-parse it and
# found nothing). That's an unusual condition — safer to fail loud than
# preserve stale state that no longer has a filesystem source. Both
# ``_reload_bundles`` and ``_reload_agents`` share the same shape:
# typed error string, "Old rules still active" clause, offending file
# path in the message.
async def handle_reload(runtime: PlatformRuntime) -> ReloadResult:
    """Atomically rebuild every authz layer from disk (bug-044 + qol-051 C1).

    Rescans the filesystem for NEW / DELETED bundle / agent / workflow
    YAMLs (qol-051 C1) and refreshes rules across all four authz layers
    — platform, bundle, workflow, agent — reporting the true rule count
    + a diff summary on success. Before C1 the reload iterated only the
    boot-time catalog, so YAMLs added or removed since startup stayed
    invisible until the next restart.

    Strategy (Path B — partial rescan). Rather than rebuild every
    catalog-derived cache (agent-id→bundle map, tool registrations,
    engine bindings — which would force a re-run of
    ``build_resolved_config``), we:

    1. Re-scan the platform root via ``assets.catalog.scan`` to pick
       up new bundle/agent/workflow YAMLs and drop deleted ones.
    2. Refresh the rule state each layer's authz decisions read from:

       - **platform** — ``runtime.authz`` gets a fresh
         ``RuleBasedAuthorizationProvider``, same as feat-041.
       - **bundle** — ``BundleManifest.permissions`` on every fresh
         catalog entry is re-parsed off ``bundle.yaml``, and every
         ``_AgentRegistration.bundle_permissions`` for an agent in
         that bundle is overwritten.
       - **agent** — ``_AgentRegistration.agent_permissions`` is
         overwritten from each agent YAML's ``permissions:`` field
         (parsed off disk, keyed by the derived ``AgentId``).
       - **workflow** — ``load_workflow(entry.path)`` is already
         invoked fresh on every workflow run, so ``policy:`` edits are
         picked up automatically. We still parse each workflow YAML
         here to enforce the "abort-on-any-failure" contract.
    3. Swap the fresh ``AssetCatalog`` into ``runtime.asset_management``
       so future agent registrations (a workflow spawning a sub-agent,
       e.g.) see the new/deleted assets.

    Failure model (AC #2): any single YAML that fails to parse aborts
    the entire reload BEFORE any state mutates. The error message
    cites the offending ``file:line`` and the old provider + old
    catalog stay live. Never leaves the system half-loaded. A missing
    ``platform.yaml`` aborts the same way (bug-157): the reload never
    re-creates the file, so deleting it cannot swap in generated
    default rules.

    Success summary (AC #1 / Mockup 3 + C1): ``reloaded N rules across
    4 layers (platform, bundle, workflow, agent) [+X bundles, -Y agents
    since last reload] — new decisions use the updated config.`` ``N``
    is the true total across every layer. The diff clause only appears
    when the rescan surfaced additions or deletions.

    **spec-131 Step 7 — hooks config is deliberately NOT one of the four
    layers this function reloads.** ``platform.yaml`` isn't read-once in
    general — this function re-reads it via its own ``load_or_create``
    call below and rebuilds the authorization/bundle/workflow/agent
    layers from that fresh read every time ``/reload`` runs. Hook rules
    are the one section that IS read-once, at process start
    (``bootstrap_runtime`` → ``AssetManagement.hooks_config()``, see
    that method's docstring), and this function never re-reads or
    re-derives them: the ``payload`` parsed here is used only for its
    ``authorization`` key, and nothing below ever touches
    ``runtime.engine._observer`` (the live ``HookDispatcher``) or calls
    ``hooks_config()`` again. That is a deliberate trust-boundary
    control (spec-131 Rev. 2), not an oversight — see
    ``test_reload_does_not_change_hook_dispatcher_rules`` in
    ``tests/platform_runtime/test_hooks_reload_isolation.py`` for the
    locking-in test. It narrows, but does not eliminate, the residual
    risk that a model-driven ``write_file`` (allow-by-default in
    kolsch's starter ACL) could append a ``hooks:`` entry to
    ``platform.yaml`` on disk: with this guarantee, that edit only takes
    effect on the *next full process restart*, never on the next
    ``/reload`` and never immediately. If a future change to this
    function ever needs to reload hooks too, that decision must be made
    deliberately (and re-litigate the trust-boundary tradeoff above),
    not fall out accidentally from generalizing this function's rescan
    loop.
    """
    from assets import catalog as catalog_module  # noqa: PLC0415
    from assets.platform_config import (  # noqa: PLC0415
        authorization_rule_source_map,
        load_or_create,
    )
    from errors import (  # noqa: PLC0415
        AssetsBundleNameMismatch,
        AssetsDuplicateBundleName,
        AssetsDuplicateName,
        ConfigInvalid,
    )
    from providers.authz import RuleBasedAuthorizationProvider  # noqa: PLC0415

    root = runtime.root

    # --- Phase A: parse every layer, no mutations yet ------------------
    #
    # Any failure at this phase returns an error immediately — the
    # runtime is untouched, so the old provider + old bundle/agent
    # bindings stay active.

    # bug-157: /reload is a read. A missing platform.yaml aborts it
    # rather than letting `load_or_create` regenerate the file and
    # swap its default rules in live.
    platform_yaml = root / "platform.yaml"
    if not platform_yaml.is_file():
        return ReloadResult(
            error=(
                f"platform.yaml is missing at {platform_yaml}. "
                "Old rules still active; no change to enforcement. "
                "Restore the file (or run `kolsch setup`), then /reload."
            ),
        )

    try:
        payload = load_or_create(root)
    except ConfigInvalid as exc:
        return ReloadResult(error=_format_config_invalid(exc, "platform.yaml"))
    except OSError as exc:
        return ReloadResult(
            error=(
                f"could not read platform.yaml: {exc}. "
                "Old rules still active; no change to enforcement."
            ),
        )

    authz_cfg = payload.get("authorization") or {}
    if not isinstance(authz_cfg, dict):
        return ReloadResult(
            error=(
                "platform.yaml authorization section is not a mapping. "
                "Old rules still active; no change to enforcement."
            ),
        )

    # spec-131 Step 7: `payload` above is used ONLY for `authorization`.
    # Deliberately not reading `payload.get("hooks")` here or anywhere
    # else in this function — see this function's docstring ("spec-131
    # Step 7" section) for why hooks config stays read-once at process
    # start rather than joining this function's rescan.

    # --- qol-051 C1: filesystem rescan ---------------------------------
    #
    # The boot-time ``scan`` walks ``bundles/`` and every asset subdir;
    # running it again picks up bundle YAMLs added since startup and
    # drops bundles whose YAML was deleted. Catalog-integrity failures
    # (duplicate bundle names, name/directory mismatches, duplicate
    # asset refs) raise the matching ``AssetsError`` — surface these
    # as reload errors so the old catalog stays live.
    #
    # ``scan`` skips bundles whose ``bundle.yaml`` is malformed with a
    # WARNING (catalog's "keep booting on broken input" contract).
    # /reload's AC #2 demands the opposite — surface the parse error
    # loudly and preserve the old provider — so after the scan we
    # cross-check every ``bundles/<name>/bundle.yaml`` against the
    # fresh catalog and re-parse strictly for any that got dropped.
    old_catalog = runtime.asset_management.catalog()
    try:
        fresh_catalog = catalog_module.scan(root)
    except (
        AssetsDuplicateBundleName,
        AssetsBundleNameMismatch,
        AssetsDuplicateName,
    ) as exc:
        return ReloadResult(
            error=(
                f"catalog rescan failed: {type(exc).__name__}: {exc}. "
                "Old rules still active; no change to enforcement."
            ),
        )
    except OSError as exc:
        return ReloadResult(
            error=(
                f"could not rescan bundles/: {exc}. "
                "Old rules still active; no change to enforcement."
            ),
        )
    strict_error = _check_scan_dropped_bundles(root, fresh_catalog)
    if strict_error is not None:
        return ReloadResult(error=strict_error)
    catalog_diff = _diff_catalogs(old_catalog, fresh_catalog)

    # Bundle rescan — one BundleManifest per catalogued bundle. Failure
    # to reload a bundle.yaml (parse or shape error) aborts the reload.
    bundle_reload = _reload_bundles(fresh_catalog)
    if bundle_reload.error is not None:
        return ReloadResult(error=bundle_reload.error)

    # Agent rescan — one PermissionRule list per catalogued agent.
    # Parse errors abort. Empty permissions list is fine.
    agent_reload = _reload_agents(fresh_catalog)
    if agent_reload.error is not None:
        return ReloadResult(error=agent_reload.error)

    # Workflow rescan — we don't cache policy, but we still verify each
    # workflow YAML parses so a broken file surfaces at /reload rather
    # than at first invocation. Rules are counted here for the summary.
    workflow_reload = _reload_workflows(fresh_catalog)
    if workflow_reload.error is not None:
        return ReloadResult(error=workflow_reload.error)

    # Build the fresh platform provider. Do this AFTER the bundle /
    # agent / workflow parses succeed so a failure downstream still
    # leaves everything untouched.
    try:
        source_map = authorization_rule_source_map(root)
        new_authz = RuleBasedAuthorizationProvider(
            config=authz_cfg.get("config", {}),
            catalog=fresh_catalog,
            source_map=source_map,
            locked_source_map=authorization_rule_source_map(
                root, list_key="locked_rules"
            ),
            config_file=str(root / "platform.yaml"),
        )
    except Exception as exc:  # noqa: BLE001 — surface any construction error
        return ReloadResult(
            error=(
                f"failed to construct authz provider: "
                f"{type(exc).__name__}: {exc}. "
                "Old rules still active; no change to enforcement."
            ),
        )

    # bug-162 — carry the chat's approval registration over to the new
    # provider BEFORE it goes live. Without this, after `/reload` every
    # `prompt` rule collapsed to deny and uncovered actions hit default
    # deny: approvals silently stopped for the rest of the chat. Done
    # here (after every parse succeeded) so a failed reload — which
    # returned above — leaves the old provider and its registration
    # untouched.
    old_authz = getattr(runtime, "authz", None)
    if (
        isinstance(old_authz, RuleBasedAuthorizationProvider)
        and old_authz.registered_prompt_callback is not None
    ):
        new_authz.register_prompt_callback(
            old_authz.registered_prompt_callback,
            on_rule_added=old_authz.rule_added_hook,
        )

    # --- Phase B: atomic swap. All parses succeeded above ----------
    #
    # From here on the mutations are non-recoverable — but each is a
    # single-attribute rebind, and Python's GIL guarantees each write
    # is atomic. In-flight tool calls that already resolved through
    # the old state are unaffected (they're past their `allow()` call
    # by construction).

    # Platform layer — the feat-041 swap.
    object.__setattr__(runtime, "authz", new_authz)
    # The engine holds its own reference for the `agent.execute.*`
    # pre-flight. Without this rebind a rule (or locked rule) added via
    # /reload on that action would be reported as applied but not
    # enforced until restart. Every other holder reads through
    # `runtime.authz` or `tools_subsystem._authz` at use time: slash
    # contexts are built per command, workflow executors per run (an
    # in-flight run keeps the provider it started with), and the MCP
    # tool bridge dispatches through the tools subsystem.
    engine = getattr(runtime, "engine", None)
    if engine is not None and hasattr(engine, "_authz"):
        object.__setattr__(engine, "_authz", new_authz)
    tools_sub = getattr(runtime, "tools_subsystem", None)
    if tools_sub is not None:
        object.__setattr__(tools_sub, "_authz", new_authz)

        # Bundle + agent layers — push the freshly-parsed rules into
        # every existing agent registration. Registrations for agents
        # that never bound to an on-disk YAML (test fixtures, synthetic
        # configs) keep whatever they had.
        _apply_bundle_and_agent_rules(
            tools_sub=tools_sub,
            catalog=fresh_catalog,
            bundle_manifests=bundle_reload.manifests,
            agent_rules_by_id=agent_reload.rules_by_id,
        )

    # Push the fresh manifests back into the catalog's `bundles` map
    # so future agent registrations (a workflow spawning a sub-agent,
    # e.g.) see the freshly-parsed ``permissions:`` list — the
    # strict-parse ``bundle_reload.manifests`` is authoritative over
    # the scan's silent-fallback manifests when both succeeded.
    if bundle_reload.manifests:
        for name, manifest in bundle_reload.manifests.items():
            fresh_catalog.bundles[name] = manifest

    # Install the fresh catalog into ``asset_management`` so subsequent
    # reads (``.catalog()``, ``.get_bundle(name)``, ``.list_bundles()``)
    # see added and deleted YAMLs — the boot-time invariant "catalog
    # is immutable" is by design broken by /reload.
    _install_fresh_catalog(runtime.asset_management, fresh_catalog)

    rules = await new_authz.list_rules()
    n_platform = len(rules)
    total = (
        n_platform
        + bundle_reload.total_rules
        + workflow_reload.total_rules
        + agent_reload.total_rules
    )
    diff_clause = _format_catalog_diff(catalog_diff)
    return ReloadResult(
        summary=(
            f"reloaded {total} rules across 4 layers "
            f"(platform, bundle, workflow, agent)"
            f"{diff_clause} — new decisions use the updated config."
        ),
    )


# ---------------------------------------------------------------------------
# /reload — helper: per-layer rescan pipelines
# ---------------------------------------------------------------------------


def _check_scan_dropped_bundles(root: Any, fresh_catalog: Any) -> str | None:
    """Return an error string if the rescan silently dropped a bundle.

    ``AssetCatalog.scan`` skips a bundle whose ``bundle.yaml`` is
    malformed with a WARNING log and no reflected error — the boot
    contract preserves other bundles when one is broken. /reload's
    AC #2 wants the exact opposite: a single broken YAML must abort
    the whole reload with the offending file cited. Cross-reference
    the filesystem with the fresh catalog; any bundle directory whose
    ``bundle.yaml`` exists but got dropped runs through the strict
    re-parse to surface the concrete error.
    """
    from pathlib import Path  # noqa: PLC0415

    bundles_dir = Path(root) / "bundles"
    if not bundles_dir.exists():
        return None
    catalogued = set(getattr(fresh_catalog, "bundles", {}).keys())
    for bundle_dir in sorted(bundles_dir.iterdir()):
        if not bundle_dir.is_dir():
            continue
        yaml_path = bundle_dir / "bundle.yaml"
        if not yaml_path.exists():
            # No YAML at all — the scan skipped this directory silently.
            # /reload doesn't need to invent a rule here; the operator
            # never authored a bundle here.
            continue
        if bundle_dir.name in catalogued:
            continue
        # YAML present, but scan dropped it — surface the concrete
        # error via the strict parser.
        try:
            _reparse_bundle_manifest(bundle_dir)
        except _YamlReloadError as exc:
            return (
                f"yaml parse error in {exc.source} — {exc.message}. "
                "Old rules still active; no change to enforcement."
            )
        # Reparse succeeded — the scan dropped this bundle for a
        # different reason (invalid directory name, name/directory
        # mismatch caught in a later pass, etc.). Report a generic
        # error so the operator knows the reload can't proceed.
        return (
            f"bundle directory {bundle_dir.name} exists on disk but "
            f"was dropped from the catalog rescan. "
            "Old rules still active; no change to enforcement."
        )
    return None


def _diff_catalogs(old: Any, fresh: Any) -> _CatalogDiff:
    """Compute added / removed bundles + agents + workflows across two scans.

    Sorted tuples give the summary formatter a deterministic listing —
    tests can assert the exact clause without depending on dict-hash
    ordering.
    """

    def _refs_by_type(catalog: Any, asset_type: str) -> set[str]:
        out: set[str] = set()
        assets = getattr(catalog, "assets", None) or {}
        for ref, entry in assets.items():
            if entry.asset_type == asset_type:
                out.add(ref)
        return out

    old_bundles = set(getattr(old, "bundles", {}).keys())
    fresh_bundles = set(getattr(fresh, "bundles", {}).keys())
    old_agents = _refs_by_type(old, "agent")
    fresh_agents = _refs_by_type(fresh, "agent")
    old_workflows = _refs_by_type(old, "workflow")
    fresh_workflows = _refs_by_type(fresh, "workflow")

    return _CatalogDiff(
        bundles_added=tuple(sorted(fresh_bundles - old_bundles)),
        bundles_removed=tuple(sorted(old_bundles - fresh_bundles)),
        agents_added=tuple(sorted(fresh_agents - old_agents)),
        agents_removed=tuple(sorted(old_agents - fresh_agents)),
        workflows_added=tuple(sorted(fresh_workflows - old_workflows)),
        workflows_removed=tuple(sorted(old_workflows - fresh_workflows)),
    )


def _format_catalog_diff(diff: _CatalogDiff) -> str:
    """Return the ``[+X bundles, -Y agents, ...]`` clause, or ``''`` if empty.

    Renders the additions/removals detected by the rescan into a
    space-prefixed bracketed clause so the reload summary shows what
    the filesystem changed since the last reload. Empty diffs return
    the empty string so the summary reads cleanly on no-op reloads.
    """
    if diff.is_empty():
        return ""
    parts: list[str] = []
    if diff.bundles_added:
        parts.append(
            f"+{len(diff.bundles_added)} bundle"
            + ("s" if len(diff.bundles_added) != 1 else "")
        )
    if diff.bundles_removed:
        parts.append(
            f"-{len(diff.bundles_removed)} bundle"
            + ("s" if len(diff.bundles_removed) != 1 else "")
        )
    if diff.agents_added:
        parts.append(
            f"+{len(diff.agents_added)} agent"
            + ("s" if len(diff.agents_added) != 1 else "")
        )
    if diff.agents_removed:
        parts.append(
            f"-{len(diff.agents_removed)} agent"
            + ("s" if len(diff.agents_removed) != 1 else "")
        )
    if diff.workflows_added:
        parts.append(
            f"+{len(diff.workflows_added)} workflow"
            + ("s" if len(diff.workflows_added) != 1 else "")
        )
    if diff.workflows_removed:
        parts.append(
            f"-{len(diff.workflows_removed)} workflow"
            + ("s" if len(diff.workflows_removed) != 1 else "")
        )
    return " [" + ", ".join(parts) + " since last reload]"


def _install_fresh_catalog(asset_management: Any, fresh_catalog: Any) -> None:
    """Swap the rescan's fresh ``AssetCatalog`` into ``asset_management``.

    The catalog is a private attribute (``_catalog``) on both the real
    ``AssetManagement`` and the test mocks — direct assignment is
    fine. Failures here (e.g. the field renamed underneath us)
    silently no-op so the reload's Phase B rebinds still land; the
    only observable effect of the miss is that the next
    ``asset_management.catalog()`` returns the old catalog.
    """
    try:
        asset_management._catalog = fresh_catalog
    except Exception:  # noqa: BLE001 — never raise out of the catalog install
        return


def _format_config_invalid(exc: object, source: str) -> str:
    """Compose the user-visible error string for a ``ConfigInvalid``.

    Extracts the first validation error's ``message`` from the
    exception's ``detail`` dict (feat-041 shape) and appends the
    "old rules still active" clause so the caller has a complete
    line to render.
    """
    detail = getattr(exc, "detail", {}) or {}
    errors = detail.get("validation_errors") if isinstance(detail, dict) else None
    first_error = errors[0] if errors else {}
    message = first_error.get("message") if isinstance(first_error, dict) else None
    message = message or str(exc)
    return (
        f"yaml parse error in {source} — {message}. "
        "Old rules still active; no change to enforcement."
    )


@dataclass(frozen=True)
class _BundleReloadOutcome:
    """Phase-A outcome for the bundle layer.

    ``manifests`` is keyed by bundle name; ``total_rules`` is the sum
    of ``len(permissions)`` across every fresh manifest. ``error``, if
    present, aborts the reload before mutation.
    """

    manifests: dict[str, Any] = field(default_factory=dict)
    total_rules: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _AgentReloadOutcome:
    """Phase-A outcome for the agent layer.

    ``rules_by_id`` maps a derived ``AgentId`` (str form) to the
    freshly-parsed permission-rule list — the tools subsystem's
    registration for that agent gets overwritten with the value.
    """

    rules_by_id: dict[str, Any] = field(default_factory=dict)
    total_rules: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _WorkflowReloadOutcome:
    """Phase-A outcome for the workflow layer.

    Workflow policy is not cached (each ``load_workflow`` call re-reads
    the YAML), so nothing needs to be pushed back at swap time. We
    still parse every workflow to enforce the "abort-on-any-failure"
    contract and to count rules for the summary.
    """

    total_rules: int = 0
    error: str | None = None


def _reload_bundles(catalog: Any) -> _BundleReloadOutcome:
    """Re-parse every catalogued bundle's ``bundle.yaml``.

    Failure to parse a bundle YAML aborts the whole reload — the
    error string cites the offending ``file:line`` when the parser's
    exception exposes it, otherwise the file path plus the error
    message.

    Missing-YAML semantics (qol-051 C2): after C1's rescan, the
    catalog only contains bundles whose ``bundle.yaml`` existed at
    rescan time. A missing YAML here means the file vanished BETWEEN
    rescan and re-parse — a rare mid-reload race. Abort with a typed
    error (symmetric with ``_reload_agents``'s OSError branch) so we
    don't ship a partial reload silently preserving stale state that
    no longer has a filesystem source.
    """
    from pathlib import Path  # noqa: PLC0415

    from assets.manifest import load_manifest  # noqa: PLC0415

    manifests: dict[str, Any] = {}
    total = 0
    for name, existing in catalog.bundles.items():
        bundle_dir = Path(existing.path)
        yaml_path = bundle_dir / "bundle.yaml"
        if not yaml_path.exists():
            # Mid-reload race: the rescan saw the YAML, then it
            # disappeared before we could re-parse. Abort so we don't
            # ship a partial reload — mirrors ``_reload_agents``.
            return _BundleReloadOutcome(
                error=(
                    f"could not read bundle YAML {yaml_path}: "
                    "file disappeared between rescan and reload. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        # `load_manifest` returns None on parse/shape failure — for
        # /reload we want to surface the failure with a file
        # reference instead of the silent None-fallback the catalog
        # scanner uses at boot time. Re-parse ourselves to catch the
        # error class.
        try:
            manifest = _reparse_bundle_manifest(bundle_dir)
        except _YamlReloadError as exc:
            return _BundleReloadOutcome(
                error=(
                    f"yaml parse error in {exc.source} — {exc.message}. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        if manifest is None:
            # Parse succeeded but shape validation failed silently
            # inside `load_manifest`. Re-run through the strict path
            # to extract the concrete error.
            fresh = load_manifest(bundle_dir)
            if fresh is None:
                return _BundleReloadOutcome(
                    error=(
                        f"yaml parse error in {yaml_path} — "
                        "bundle.yaml shape validation failed. "
                        "Old rules still active; no change to enforcement."
                    ),
                )
            manifest = fresh
        manifests[name] = manifest
        total += len(manifest.permissions)
    return _BundleReloadOutcome(manifests=manifests, total_rules=total)


def _reload_agents(catalog: Any) -> _AgentReloadOutcome:
    """Re-parse every catalogued agent YAML's ``permissions:`` field.

    Result is keyed by derived ``AgentId`` (str) so the tools subsystem
    can look up the corresponding registration on the swap. Agent
    YAMLs that don't parse cleanly abort the reload with the file's
    path in the error message.
    """
    from pathlib import Path  # noqa: PLC0415

    from assets.assembler import load_agent_definition  # noqa: PLC0415
    from errors import ConfigInvalid  # noqa: PLC0415
    from schemas import PermissionRule, derive_agent_id  # noqa: PLC0415

    rules_by_id: dict[str, Any] = {}
    total = 0
    for entry in catalog.assets.values():
        if entry.asset_type != "agent":
            continue
        # `entry.path` on an agent entry is the YAML file itself.
        yaml_path = Path(entry.path)
        try:
            payload = load_agent_definition(yaml_path)
        except ConfigInvalid as exc:
            return _AgentReloadOutcome(
                error=_format_config_invalid(exc, str(yaml_path)),
            )
        except OSError as exc:
            return _AgentReloadOutcome(
                error=(
                    f"could not read agent YAML {yaml_path}: {exc}. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        raw_permissions = payload.get("permissions") or []
        if not isinstance(raw_permissions, list):
            return _AgentReloadOutcome(
                error=(
                    f"agent YAML {yaml_path} 'permissions:' must be a list; "
                    f"got {type(raw_permissions).__name__}. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        parsed: list[PermissionRule] = []
        for idx, raw in enumerate(raw_permissions):
            try:
                parsed.append(PermissionRule.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 — surface pydantic + shape errors uniformly
                return _AgentReloadOutcome(
                    error=(
                        f"invalid permission rule at {yaml_path} "
                        f"(index {idx}): {exc}. "
                        "Old rules still active; no change to enforcement."
                    ),
                )
        agent_name = entry.ref.rsplit("/", 1)[-1]
        agent_id = str(derive_agent_id(entry.bundle, agent_name))
        rules_by_id[agent_id] = parsed
        total += len(parsed)
    return _AgentReloadOutcome(rules_by_id=rules_by_id, total_rules=total)


def _reload_workflows(catalog: Any) -> _WorkflowReloadOutcome:
    """Re-parse every catalogued workflow YAML.

    Workflow policy rules ARE re-read on every workflow run (via
    ``load_workflow`` at invocation time), so no state needs pushing
    back at swap time. The rescan here just enforces the
    "abort-on-any-failure" contract — a broken workflow YAML surfaces
    at /reload rather than at first invocation — and totals the policy
    rules for the summary.
    """
    from pathlib import Path  # noqa: PLC0415

    from workflows.errors import WorkflowError  # noqa: PLC0415
    from workflows.parser import load_workflow  # noqa: PLC0415

    total = 0
    for entry in catalog.assets.values():
        if entry.asset_type != "workflow":
            continue
        try:
            workflow = load_workflow(Path(entry.path))
        except WorkflowError as exc:
            return _WorkflowReloadOutcome(
                error=(
                    f"workflow YAML {entry.path} failed to parse: {exc}. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        except OSError as exc:
            return _WorkflowReloadOutcome(
                error=(
                    f"could not read workflow YAML {entry.path}: {exc}. "
                    "Old rules still active; no change to enforcement."
                ),
            )
        total += len(workflow.policy)
    return _WorkflowReloadOutcome(total_rules=total)


class _YamlReloadError(Exception):
    """Internal marker carrying source + message for one YAML failure."""

    def __init__(self, *, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


def _reparse_bundle_manifest(bundle_dir: Any) -> Any:
    """Re-parse ``bundle.yaml`` and surface the concrete parse error.

    ``assets.manifest.load_manifest`` swallows YAMLError + ValidationError
    to keep the catalog scanner's "skip with warning" contract, which
    would silently hide the failure at /reload. Here we run the same
    load path but re-raise as ``_YamlReloadError`` so the caller can
    build a user-visible message.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from assets.manifest import BundleManifest  # noqa: PLC0415
    from kolsch._yamlio import YAMLError, load  # noqa: PLC0415

    yaml_path = bundle_dir / "bundle.yaml"
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _YamlReloadError(
            source=str(yaml_path),
            message=str(exc),
        ) from exc
    try:
        payload = load(text)
    except YAMLError as exc:
        line = _yaml_error_line(exc)
        raise _YamlReloadError(
            source=f"{yaml_path}:{line}",
            message=str(exc),
        ) from exc
    if payload is None or not isinstance(payload, dict):
        raise _YamlReloadError(
            source=str(yaml_path),
            message=(f"top-level must be a mapping; got {type(payload).__name__}"),
        )
    payload["path"] = str(bundle_dir.resolve())
    try:
        return BundleManifest.model_validate(payload)
    except ValidationError as exc:
        raise _YamlReloadError(
            source=str(yaml_path),
            message=str(exc),
        ) from exc


def _yaml_error_line(exc: Any) -> int:
    """Best-effort ruamel-YAML line extraction (1-indexed).

    Mirrors ``workflows.parser._extract_yaml_line`` — falls back to
    ``1`` when the error class doesn't expose a ``problem_mark``.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return 1
    return int(getattr(mark, "line", 0)) + 1


def _apply_bundle_and_agent_rules(
    *,
    tools_sub: Any,
    catalog: Any,
    bundle_manifests: dict[str, Any],
    agent_rules_by_id: dict[str, Any],
) -> None:
    """Push freshly-parsed bundle + agent rules into every registration.

    Walks the tools subsystem's ``_registry._agents`` map and, for each
    registered agent, overwrites ``agent_permissions`` and
    ``bundle_permissions`` in place. Uses the catalog to map an
    ``AgentId`` back to its home bundle (via the entry's ``bundle``
    field on the matching ``agent`` asset entry). Agents that no longer
    have a matching catalog entry (test fixtures that never came from
    an on-disk YAML) are left as-is — the reload is a best-effort
    refresh, not a re-registration.
    """
    from providers.authz import Rule  # noqa: PLC0415
    from schemas import derive_agent_id  # noqa: PLC0415

    registry = getattr(tools_sub, "_registry", None)
    if registry is None:
        return
    agents_map = getattr(registry, "_agents", None)
    if not isinstance(agents_map, dict):
        return

    # Build reverse map AgentId (str) → (bundle, agent_yaml_path) once.
    id_to_agent: dict[str, tuple[str, str]] = {}
    for entry in catalog.assets.values():
        if entry.asset_type != "agent":
            continue
        agent_name = entry.ref.rsplit("/", 1)[-1]
        agent_id_str = str(derive_agent_id(entry.bundle, agent_name))
        id_to_agent[agent_id_str] = (entry.bundle, entry.path)

    for agent_id, registration in agents_map.items():
        meta = id_to_agent.get(str(agent_id))
        if meta is None:
            # Synthetic registration (test fixture or programmatic
            # tools=... caller). Nothing on disk to reload from.
            continue
        bundle_name, agent_yaml_path = meta

        # --- agent layer ---
        raw_agent_rules = agent_rules_by_id.get(str(agent_id), [])
        agent_source_map = _load_source_map(agent_yaml_path)
        new_agent_rules = [
            Rule(
                id=r.id,
                who=r.who,
                what=r.what,
                pattern=r.pattern,
                from_=r.from_,
                effect=r.effect,
                source_file=agent_source_map.get(i, (None, None))[0],
                source_line=agent_source_map.get(i, (None, None))[1],
            )
            for i, r in enumerate(raw_agent_rules)
        ]
        registration.agent_permissions = new_agent_rules

        # --- bundle layer ---
        manifest = bundle_manifests.get(bundle_name)
        if manifest is not None:
            bundle_yaml_path = f"{manifest.path}/bundle.yaml"
            bundle_source_map = _load_source_map(bundle_yaml_path)
            new_bundle_rules = [
                Rule(
                    id=r.id,
                    who=r.who,
                    what=r.what,
                    pattern=r.pattern,
                    from_=r.from_,
                    effect=r.effect,
                    source_file=bundle_source_map.get(i, (None, None))[0],
                    source_line=bundle_source_map.get(i, (None, None))[1],
                )
                for i, r in enumerate(manifest.permissions)
            ]
            registration.bundle_permissions = new_bundle_rules


def _load_source_map(yaml_path: str) -> dict[int, tuple[str, int]]:
    """Return the ``permissions_source_map`` for a YAML path, or empty.

    Wraps ``assets.manifest.permissions_source_map`` in try/except to
    match the engine-side "empty map on failure" contract feat-041
    established — the reload path should never crash because the
    source-tag lookup missed.
    """
    from pathlib import Path  # noqa: PLC0415

    from assets.manifest import permissions_source_map  # noqa: PLC0415

    try:
        return permissions_source_map(Path(yaml_path))
    except Exception:  # noqa: BLE001 — never raise out of source-tag path
        return {}
