"""Hook matcher — pure `event` → `bool` decision for shell-command hooks (spec-131).

This module holds only the matching logic: given a
[`HookRule`][schemas.config.HookRule] and an [`Event`][schemas.events.Event],
decide whether the rule fires. No subprocess execution, no dispatcher,
no engine wiring lives here yet — those are later spec-131 steps
(3: subprocess runner, 4: `HookDispatcher`, 5/6: engine injection).

Matching has two stages, per spec-131's Scope § Matching table:

1. `event.type` must equal `rule.event` — EXCEPT for `EventType.UI_EVENT`,
   where the comparison uses the nested wire-kind string
   (`event.data["kind"]`, e.g. `"kolsch.prompt.requested"`) instead of
   the generic `"ui.event"` envelope type. This lets a `platform.yaml`
   author write `event: "kolsch.prompt.requested"` directly, matching
   the same string CrewAI/Claude-Code-style hook configs would use, one
   level more specific than the raw `EventType` vocabulary.
2. If the event type matches, a per-event-type "subject" string is
   computed (see `_subject_for`) and checked against `rule.matcher` via
   `fnmatch.fnmatchcase` — the same case-sensitive glob primitive
   `RuleBasedAuthorizationProvider._rule_matches` uses internally
   (`src/providers/authz.py:1074-1103`), imported directly rather than
   reusing that private, ACL-shape-coupled helper (see spec-131
   Technical Context).
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from schemas.events import EventType

if TYPE_CHECKING:
    from schemas.config import HookRule
    from schemas.events import Event

# Wire-kinds whose "subject" is the tool/action name carried in the
# payload's `action` key (spec-131 Matching table). `kolsch.prompt.requested`
# always carries `action` — it's merged from `InlineApprovalCallback`'s
# `metadata` dict (`src/cli/chat/approval.py:106-110`) into the
# `WIRE_PROMPT_REQUESTED` payload by `PromptsImpl.choice`
# (`src/cli/chat/services/prompts_impl.py:247-264`).
#
# `kolsch.prompt.answered` does NOT always carry `action` — the real
# answered-payload shapes (`PromptsImpl._emit_auto_deny`,
# `src/cli/chat/services/prompts_impl.py:590-596`, and the widget-bridge
# path at `src/cli/chat/event_adapter.py:2341-2360`) only ever include
# `prompt_id`, `response`, and an optional `rule` — never `action`. The
# subject extraction below tolerates this by falling back to `"*"` when
# `action` is absent, which keeps `kolsch.prompt.answered` matchable by
# a wildcard rule and by literal-tool rules on any future/legacy caller
# that does forward `action`.
_ACTION_SUBJECT_KINDS = frozenset(
    {
        "kolsch.prompt.requested",
        "kolsch.prompt.answered",
    }
)

# Wire-kind/event-type values whose subject is always the wildcard —
# there's no natural per-event tool/action to key on (spec-131 Matching
# table).
_WILDCARD_SUBJECT_TYPES = frozenset(
    {
        "thread.exchange_completed",
        "thread.exchange_failed",
        "session.opened",
    }
)

_WILDCARD_SUBJECT = "*"


def match(rule: HookRule, event: Event) -> bool:
    """Return `True` iff `event` fires `rule`.

    Compares `event.type` against `rule.event` — using the nested
    wire-kind string (`event.data["kind"]`) instead of the generic
    `"ui.event"` envelope type when `event.type is EventType.UI_EVENT`.
    When the event type matches, computes the per-event-type subject
    (see `_subject_for`) and checks it against `rule.matcher` via
    `fnmatch.fnmatchcase`.

    Args:
        rule: One `platform.yaml` `hooks:` entry.
        event: A real session event (or a fabricated one in tests)
            to test against `rule`.

    Returns:
        `True` when both the event-type and subject-matcher checks
        pass; `False` otherwise.
    """
    event_type_str = _event_type_string(event)
    if event_type_str != rule.event:
        return False
    subject = _subject_for(event_type_str, event)
    return fnmatch.fnmatchcase(subject, rule.matcher)


def _event_type_string(event: Event) -> str:
    """Return the string to compare against `rule.event`.

    For `EventType.UI_EVENT`, that's the nested wire-kind
    (`event.data["kind"]`) — e.g. `"kolsch.prompt.requested"` — per
    `UIEventData.kind` (`src/schemas/wire_kinds.py`). For every other
    `EventType`, it's the plain `event.type` value (already a `str`
    subclass via `StrEnum`).
    """
    if event.type is EventType.UI_EVENT:
        kind = event.data.get("kind")
        return kind if isinstance(kind, str) else ""
    return str(event.type)


def _subject_for(event_type_str: str, event: Event) -> str:
    """Compute the per-event "subject" matched against `rule.matcher`.

    Per spec-131's Matching table:

    - `kolsch.prompt.requested` / `kolsch.prompt.answered` — the
      tool/action name from the prompt's payload (`data["payload"]["action"]`),
      falling back to `"*"` when absent (see the `_ACTION_SUBJECT_KINDS`
      module comment for why `kolsch.prompt.answered` often lacks it).
    - `thread.exchange_completed` / `thread.exchange_failed` /
      `session.opened` — always `"*"` (no natural subject).
    - `tool.failed` — the failing tool's name (`data["tool_name"]`,
      `ToolFailedData.tool_name`).
    - Anything else (future/undocumented event types reaching this
      function after the type check already passed) — `"*"`, so a
      wildcard-matcher rule still fires generically per spec-131's
      "architecturally generic" design goal.
    """
    if event_type_str in _ACTION_SUBJECT_KINDS:
        payload = event.data.get("payload")
        action = payload.get("action") if isinstance(payload, dict) else None
        return action if isinstance(action, str) else _WILDCARD_SUBJECT
    if event_type_str in _WILDCARD_SUBJECT_TYPES:
        return _WILDCARD_SUBJECT
    if event_type_str == EventType.TOOL_FAILED:
        tool_name = event.data.get("tool_name")
        return tool_name if isinstance(tool_name, str) else _WILDCARD_SUBJECT
    return _WILDCARD_SUBJECT
