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

from dataclasses import dataclass
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
    line2 = f"pattern {pattern} — /reload to reapply after editing"
    return f"{line1}\n{line2}"


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
    return _assemble_card_data(events, tool_failed)


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


async def handle_reload(runtime: PlatformRuntime) -> ReloadResult:
    """Atomically rebuild the runtime's authz provider from disk.

    Steps:

    1. Re-read ``platform.yaml`` (via ``platform_config.load_or_create``
       so the same error surface applies) and its rule source map.
    2. Construct a NEW ``RuleBasedAuthorizationProvider`` from the
       fresh config.
    3. On success: swap ``runtime.authz`` atomically and return the
       summary. In-flight tool calls that already resolved through
       the old provider are unaffected — they're past their
       ``allow()`` call by construction.
    4. On any failure (YAML parse, malformed rule, missing required
       field): DO NOT touch the runtime. Return a ``ReloadResult``
       with the error message so the caller can display it. The old
       provider stays live.

    Cross-layer note: bundle / workflow / agent YAMLs aren't loaded
    here — they're consumed at agent-preflight / node-dispatch time
    from the catalog (which is itself immutable after startup). A
    future extension can rescan the catalog; today the primary
    iteration loop is the operator editing `platform.yaml` and
    typing `/reload`.
    """
    from assets.platform_config import (  # noqa: PLC0415
        authorization_rule_source_map,
        load_or_create,
    )
    from errors import ConfigInvalid  # noqa: PLC0415
    from providers.authz import RuleBasedAuthorizationProvider  # noqa: PLC0415

    root = runtime.root
    try:
        payload = load_or_create(root)
    except ConfigInvalid as exc:
        # `ConfigInvalid` stashes the per-error list on
        # `detail["validation_errors"]`; grab the first one's
        # `message` if present, else str(exc).
        detail = getattr(exc, "detail", {}) or {}
        errors = detail.get("validation_errors") if isinstance(detail, dict) else None
        first_error = errors[0] if errors else {}
        message = first_error.get("message") if isinstance(first_error, dict) else None
        message = message or str(exc)
        return ReloadResult(
            error=(
                f"yaml parse error in platform.yaml — {message}. "
                "Old rules still active; no change to enforcement."
            ),
        )
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

    try:
        source_map = authorization_rule_source_map(root)
        new_authz = RuleBasedAuthorizationProvider(
            config=authz_cfg.get("config", {}),
            catalog=runtime.asset_management.catalog(),
            source_map=source_map,
        )
    except Exception as exc:  # noqa: BLE001 — surface any construction error
        return ReloadResult(
            error=(
                f"failed to construct authz provider: "
                f"{type(exc).__name__}: {exc}. "
                "Old rules still active; no change to enforcement."
            ),
        )

    # Atomic swap. `PlatformRuntime` is a frozen dataclass — bypass
    # the freeze with `object.__setattr__` since the semantic is
    # "same runtime, updated provider". Alternative (rebuild the
    # dataclass) would ripple through every closure that captured
    # the old runtime.
    object.__setattr__(runtime, "authz", new_authz)

    # Also update the tools subsystem's cached authz reference — the
    # dispatch flow reads `runtime.authz` at dispatch time, but the
    # subsystem holds its own reference set at bootstrap.
    tools_sub = getattr(runtime, "tools_subsystem", None)
    if tools_sub is not None:
        object.__setattr__(tools_sub, "_authz", new_authz)

    rules = await new_authz.list_rules()
    n_rules = len(rules)
    return ReloadResult(
        summary=(
            f"reloaded {n_rules} rules (platform layer) — "
            "new decisions use the updated config."
        ),
    )
