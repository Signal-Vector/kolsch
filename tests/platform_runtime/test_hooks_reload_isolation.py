"""spec-131 Step 7 — hooks config survives `/reload` unchanged.

**Trust-boundary control under test (spec-131 Rev. 2):** `platform.yaml`
is not read-once in general — `/reload`
(`cli.chat.services.authz_introspection.handle_reload`) re-reads it and
rebuilds the authorization/bundle/workflow/agent layers on every
invocation. Hooks config is the one section that IS read-once, at
process start (`bootstrap_runtime` → `AssetManagement.hooks_config()`),
and `handle_reload` never re-reads or re-derives it — see that
function's docstring ("spec-131 Step 7" section) for the reasoning and
the documented residual-risk framing (this narrows, but does not
eliminate, the window in which a model-driven `write_file` could append
a `hooks:` entry to `platform.yaml` on disk; the guarantee under test
here is that such an edit cannot take effect via `/reload` — only a full
process restart picks it up).

This module drives the REAL `bootstrap_runtime()` and the REAL
`handle_reload()` — not a mock/duck-typed runtime stand-in — matching
`tests/platform_runtime/test_bootstrap_hooks_wiring.py`'s established
pattern (Step 6), because the whole point of this step is genuine
integration coverage of the actual guarantee, not a re-assertion of
intent against a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from assets.platform_config import default_platform_payload
from cli.chat.services.authz_introspection import handle_reload
from platform_runtime import bootstrap_runtime
from platform_runtime.hooks import HookDispatcher
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

if TYPE_CHECKING:
    from pathlib import Path

_SESSION_ID = SessionId("ses_11111111-1111-1111-1111-111111111111")
_THREAD_ID = ThreadId("th_22222222-2222-2222-2222-222222222222")
_EVENT_ID = EventId("ev_33333333-3333-3333-3333-333333333333")
_SYSTEM_ACTOR = Actor(kind="system")
_CREATED_AT = datetime(2026, 8, 11, tzinfo=UTC)


def _write_platform_yaml(root: Path, overrides: dict[str, Any]) -> None:
    """Write a platform.yaml with setup-time defaults + test overrides.

    Mirrors `test_bootstrap_hooks_wiring.py`'s helper of the same name —
    `bootstrap_runtime` requires the mandatory sections, so starting
    from `default_platform_payload()` and merging the override at the
    top level is the minimal valid fixture.
    """
    payload = default_platform_payload()
    payload.update(overrides)
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _make_event(event_type: EventType, data: dict[str, object] | None = None) -> Event:
    """Build a real `Event` envelope with fabricated but valid content.

    Mirrors `tests/platform_runtime/test_hooks.py`'s helper of the same
    name (Step 2) — real `schemas.events.Event`, not an invented shape.
    """
    return Event(
        event_id=_EVENT_ID,
        parent_event_id=None,
        thread_id=_THREAD_ID,
        session_id=_SESSION_ID,
        type=event_type,
        data=data or {},
        actor=_SYSTEM_ACTOR,
        created_at=_CREATED_AT,
    )


async def test_reload_does_not_change_hook_dispatcher_rules(tmp_path: Path) -> None:
    """Editing `hooks:` on disk and running `/reload` leaves the live
    `HookDispatcher`'s rule set — and therefore its firing behavior —
    byte-identical to what `bootstrap_runtime` built at process start.

    Sequence:
    1. Boot a real `PlatformRuntime` with one hook rule configured
       (`event: "tool.failed"`, matcher `"kolsch/original"`).
    2. Rewrite `platform.yaml`'s `hooks:` section on disk to a
       DIFFERENT rule (`event: "session.opened"`, matcher `"*"`,
       different command) — simulating exactly the on-disk edit a
       `/reload` is meant to pick up for every other config layer.
    3. Call the real `handle_reload(runtime)`.
    4. Assert `runtime.engine._observer` is still the SAME
       `HookDispatcher` object built at bootstrap, still holding the
       ORIGINAL rule, with no trace of the new one — and confirm via
       `match()` that the new rule's event would not fire while the
       old rule's would, proving this is a behavioral guarantee, not
       just an identity check.
    """
    from platform_runtime.hooks import match

    original_rule = {
        "event": "tool.failed",
        "matcher": "kolsch/original",
        "command": "true",
        "timeout_seconds": 5,
    }
    _write_platform_yaml(tmp_path, {"hooks": [original_rule]})

    runtime = await bootstrap_runtime(root=tmp_path)

    observer_before = runtime.engine._observer
    assert isinstance(observer_before, HookDispatcher)
    assert len(observer_before._rules) == 1
    assert observer_before._rules[0].event == "tool.failed"
    assert observer_before._rules[0].matcher == "kolsch/original"

    # Simulate the on-disk edit /reload is supposed to pick up for
    # every OTHER config layer — a different hooks: section entirely.
    new_rule = {
        "event": "session.opened",
        "matcher": "*",
        "command": "false",
        "timeout_seconds": 5,
    }
    _write_platform_yaml(tmp_path, {"hooks": [new_rule]})

    result = await handle_reload(runtime)
    assert result.error is None, result.error

    observer_after = runtime.engine._observer
    # Same object — /reload never rebinds `engine._observer` at all.
    assert observer_after is observer_before
    # Still exactly the rule set built at process start.
    assert len(observer_after._rules) == 1
    assert observer_after._rules[0].event == "tool.failed"
    assert observer_after._rules[0].matcher == "kolsch/original"
    assert observer_after._rules[0].command == "true"

    # Behavioral confirmation, not just data inspection: the NEW rule's
    # event would not match anything the dispatcher would fire, and the
    # OLD rule (still active) matches exactly what it matched before.
    old_matching_event = _make_event(
        EventType.TOOL_FAILED,
        data={
            "tool_call_id": "call_1",
            "tool_name": "kolsch/original",
            "error": {"code": "tool.execution_failed", "message": "boom"},
        },
    )
    assert match(observer_after._rules[0], old_matching_event) is True

    new_rule_would_have_matched_session_opened = _make_event(
        EventType.SESSION_OPENED,
        data={},
    )
    # The active rule set has no rule targeting session.opened at all —
    # the new rule never made it into the dispatcher.
    assert not any(
        match(rule, new_rule_would_have_matched_session_opened)
        for rule in observer_after._rules
    )


async def test_reload_does_not_call_hooks_config_again(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`handle_reload` never calls `AssetManagement.hooks_config()`.

    A more direct structural check alongside the behavioral test above:
    spies on `hooks_config` after bootstrap (which legitimately calls it
    once) and asserts `/reload` triggers zero further calls — confirms
    the isolation isn't an accident of the rule set happening to look
    the same, but that the read path is genuinely never re-entered.
    """
    _write_platform_yaml(
        tmp_path, {"hooks": [{"event": "tool.failed", "command": "true"}]}
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    call_count = 0
    original_hooks_config = runtime.asset_management.hooks_config

    def _spy_hooks_config() -> Any:
        nonlocal call_count
        call_count += 1
        return original_hooks_config()

    monkeypatch.setattr(runtime.asset_management, "hooks_config", _spy_hooks_config)

    _write_platform_yaml(
        tmp_path, {"hooks": [{"event": "session.opened", "command": "false"}]}
    )

    result = await handle_reload(runtime)
    assert result.error is None, result.error
    assert call_count == 0
