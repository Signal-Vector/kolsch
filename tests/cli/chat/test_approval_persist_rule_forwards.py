"""bug-025 Test 2 — Rule dict round-trips through the wire boundary.

Covers AC #1, #3.

1. Bridge task in ``EventAdapter._bridge_widget_resolution`` — when
   the widget resolves via ``(AuthzEffect, Rule)``, the emitted
   ``WIRE_PROMPT_ANSWERED`` payload carries ``rule`` = the dict form
   of the Rule.
2. ``InlineApprovalCallback._translate_response`` — a
   ``PromptResponse(key="a", rule={...})`` becomes
   ``(AuthzEffect.ALLOW, Rule(...))``; a fast-path
   ``PromptResponse(key="y", rule=None)`` stays as bare
   ``AuthzEffect.ALLOW``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.approval import _translate_response
from cli.chat.event_adapter import EventAdapter
from cli.chat.services.prompts_impl import PromptResponse
from providers.authz import AuthzEffect, Rule
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend


class _MockBlock:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._result: Any = None

    def set_result(self, result: Any) -> None:
        self._result = result
        self._event.set()

    async def resolved(self) -> Any:
        await self._event.wait()
        return self._result


class _MockApp:
    def __init__(self, *, block: _MockBlock) -> None:
        self._block = block

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
        *,
        parent_hint: Any = None,
    ) -> Any:
        _ = rule_id, action, target, parent_hint
        return self._block


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


def _mk_requested(session_id: Any, thread_id: Any, prompt_id: str) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_REQUESTED,
            "payload": {
                "prompt_id": prompt_id,
                "kind": "choice",
                "prompt": "pick",
                "options": ["y", "a", "n", "d"],
                "source": {"kind": "tool", "id": "t1"},
                "rule_id": "rl_x",
                "action": "kolsch/tools/bash",
                "target": "ls",
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _await_answered(
    store: SessionStore,
    thread_id: Any,
    prompt_id: str,
) -> Event:
    for _ in range(100):
        async for ev in store.read_events(thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") != WIRE_PROMPT_ANSWERED:
                continue
            payload = data.get("payload")
            if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
                return ev
        await asyncio.sleep(0.02)
    raise AssertionError("WIRE_PROMPT_ANSWERED never landed")


# ----------------------------------------------------------------------
# AC #1 — bridge serialises the Rule dict.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_serializes_rule_dict(store: SessionStore) -> None:
    """Widget resolves with (ALLOW, Rule) → payload carries `rule`."""
    session = await store.create_session("ag_test", name="rule_fwd")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )

    prompt_id = f"prm_{uuid4().hex[:8]}"
    await adapter.handle(
        _mk_requested(session.session_id, thread_id, prompt_id),
    )

    rule = Rule(what="kolsch/tools/bash", pattern="*", effect="allow")
    block.set_result((AuthzEffect.ALLOW, rule))

    answered = await _await_answered(store, thread_id, prompt_id)
    payload = answered.data["payload"]
    assert payload["response"] == "a"
    assert "rule" in payload
    assert payload["rule"]["what"] == "kolsch/tools/bash"
    assert payload["rule"]["pattern"] == "*"
    assert payload["rule"]["effect"] == "allow"


@pytest.mark.asyncio
async def test_bridge_fast_path_omits_rule_field(store: SessionStore) -> None:
    """Widget resolves with bare ALLOW → payload has NO `rule` key.

    Backward-compat with the pre-bug-025 shape — WIRE_PROMPT_ANSWERED
    payload consumers that only read `prompt_id` + `response` see
    the exact same field set they did before.
    """
    session = await store.create_session("ag_test", name="rule_bare")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    block = _MockBlock()
    app = _MockApp(block=block)
    adapter = EventAdapter(
        app,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    prompt_id = f"prm_{uuid4().hex[:8]}"
    await adapter.handle(
        _mk_requested(session.session_id, thread_id, prompt_id),
    )
    block.set_result(AuthzEffect.ALLOW)  # fast path — bare effect

    answered = await _await_answered(store, thread_id, prompt_id)
    payload = answered.data["payload"]
    assert payload["response"] == "y"
    assert "rule" not in payload


# ----------------------------------------------------------------------
# AC #3 — `_translate_response` rebuilds the tuple.
# ----------------------------------------------------------------------


def test_translate_response_rebuilds_tuple() -> None:
    """PromptResponse(key='a', rule={...}) → (ALLOW, Rule)."""
    response = PromptResponse(
        "a",
        rule={"what": "kolsch/tools/bash", "pattern": "ls *", "effect": "allow"},
    )
    result = _translate_response(response)
    assert isinstance(result, tuple)
    effect, rule = result
    assert effect is AuthzEffect.ALLOW
    assert rule.what == "kolsch/tools/bash"
    assert rule.pattern == "ls *"
    assert rule.effect == "allow"


def test_translate_response_rebuilds_deny_tuple() -> None:
    """PromptResponse(key='d', rule={...}) → (DENY, Rule)."""
    response = PromptResponse(
        "d",
        rule={"what": "kolsch/tools/bash", "pattern": "rm *", "effect": "deny"},
    )
    result = _translate_response(response)
    assert isinstance(result, tuple)
    effect, rule = result
    assert effect is AuthzEffect.DENY
    assert rule.pattern == "rm *"


def test_translate_response_bare_effect_when_no_rule() -> None:
    """PromptResponse(key='y', rule=None) → bare ALLOW (fast path)."""
    response = PromptResponse("y", rule=None)
    result = _translate_response(response)
    assert result is AuthzEffect.ALLOW

    response_n = PromptResponse("n", rule=None)
    assert _translate_response(response_n) is AuthzEffect.DENY


def test_translate_response_plain_string_still_works() -> None:
    """A plain ``str`` (headless / test-mock caller) round-trips unchanged."""
    # No isinstance(PromptResponse) → falls to the fast path.
    assert _translate_response("y") is AuthzEffect.ALLOW
    assert _translate_response("a") is AuthzEffect.ALLOW  # no rule → fast fallback
    assert _translate_response("n") is AuthzEffect.DENY
    assert _translate_response("d") is AuthzEffect.DENY


def test_translate_response_a_without_rule_falls_back_to_allow() -> None:
    """`a` with no rule dict falls back to allow-once (widget bridge dropped rule)."""
    response = PromptResponse("a", rule=None)
    assert _translate_response(response) is AuthzEffect.ALLOW
