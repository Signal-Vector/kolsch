"""bug-021 Test 2 — `PromptsImpl` merges `metadata` into the payload.

Covers AC #3:

- `metadata` keys land at the TOP LEVEL of the emitted
  ``kolsch.prompt.requested`` payload (not nested under a ``metadata``
  sub-dict), matching the reader shape in
  ``EventAdapter._handle_prompt_requested``.
- Reserved payload keys (``prompt_id`` / ``kind`` / ``prompt`` /
  ``options`` / ``placeholder`` / ``source``) MUST NOT be overwritten
  by a caller-supplied metadata dict.
- ``metadata=None`` (or missing) produces the pre-bug-021 payload
  shape byte-identically.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.services.prompts_impl import PromptsImpl
from schemas.events import EventType
from schemas.identifiers import ThreadId
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend
from tools.services.prompts import PromptSource


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


async def _await_first_requested_payload(
    store: SessionStore,
    thread_id: ThreadId,
    *,
    timeout: float = 1.0,
) -> dict[str, Any]:
    """Poll the store for the first `WIRE_PROMPT_REQUESTED` payload."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async for ev in store.read_events(thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") == WIRE_PROMPT_REQUESTED:
                payload = data.get("payload")
                assert isinstance(payload, dict)
                return payload
        await asyncio.sleep(0.01)
    msg = "no WIRE_PROMPT_REQUESTED event landed in the store"
    raise AssertionError(msg)


async def _run_choice_and_capture_payload(
    store: SessionStore,
    *,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fire a choice(), cancel the pending future once emitted, return payload."""
    session = await store.create_session("ag_test", name=f"m_{uuid4().hex[:6]}")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id
    impl = PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
    )
    src: PromptSource = {"kind": "tool", "id": "t1"}

    # Fire choice() in a task; we don't want it to block forever.
    task = asyncio.create_task(
        impl.choice(
            "pick",
            ["y", "n"],
            source=src,
            **({"metadata": metadata} if metadata is not None else {}),
        ),
    )
    try:
        payload = await _await_first_requested_payload(store, thread_id)
    finally:
        # Cancel the still-pending future so the choice() coroutine unwinds
        # and we don't leak the task at test teardown.
        await impl.aclose()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return payload


# --------------------------------------------------------------------
# AC #3 — top-level merge, reserved-key precedence, `None` back-compat.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_keys_merged_top_level(store: SessionStore) -> None:
    """`rule_id` / `action` / `target` land at the top of the payload."""
    metadata: dict[str, Any] = {
        "rule_id": "rl_x",
        "action": "bash",
        "target": "ls /tmp",
    }
    payload = await _run_choice_and_capture_payload(store, metadata=metadata)
    assert payload["rule_id"] == "rl_x"
    assert payload["action"] == "bash"
    assert payload["target"] == "ls /tmp"


@pytest.mark.asyncio
async def test_metadata_does_not_overwrite_reserved_keys(
    store: SessionStore,
) -> None:
    """A metadata dict whose keys collide with reserved names loses."""
    # Try to clobber every reserved key.
    metadata: dict[str, Any] = {
        "prompt_id": "evil",
        "kind": "evil",
        "prompt": "evil",
        "options": ["evil"],
        "placeholder": "evil",
        "source": {"kind": "evil", "id": "evil"},
        # Also a legit metadata key alongside — the legit one still wins.
        "rule_id": "rl_legit",
    }
    payload = await _run_choice_and_capture_payload(store, metadata=metadata)
    # Reserved keys carry the impl-supplied values, not the metadata.
    assert payload["kind"] == "choice"
    assert payload["prompt"] == "pick"
    assert payload["options"] == ["y", "n"]
    assert payload["source"]["kind"] == "tool"
    assert payload["source"]["id"] == "t1"
    # prompt_id is a fresh, impl-generated id — NOT the string "evil".
    assert payload["prompt_id"] != "evil"
    assert isinstance(payload["prompt_id"], str)
    # Non-reserved metadata still passes through.
    assert payload["rule_id"] == "rl_legit"


@pytest.mark.asyncio
async def test_metadata_none_produces_legacy_payload(
    store: SessionStore,
) -> None:
    """Omitting `metadata` yields the pre-bug-021 payload shape."""
    payload = await _run_choice_and_capture_payload(store, metadata=None)
    assert set(payload.keys()) == {
        "prompt_id",
        "kind",
        "prompt",
        "options",
        "source",
    }
