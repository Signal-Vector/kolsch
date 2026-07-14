"""Factory fixtures — sensible-default builders for test data.

Per handbook §8.5. Bodies are stubs that depend on types not yet
built; signatures and return-shape contracts are pinned now so tests
can compose against them. When the real types land, the stub bodies
are filled in without changing call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def make_agent_config(**overrides: Any) -> dict[str, Any]:
    """Return a valid ResolvedAgentConfig dict with sensible defaults.

    Currently returns a plain dict; the real `ResolvedAgentConfig`
    Pydantic model arrives with the kernel implementation. When it does,
    this builder constructs the model and returns it; call sites that
    use it as a dict can adapt by dropping `.model_dump()` or similar.
    """
    defaults: dict[str, Any] = {
        "schema_version": "0.1",
        "assembly_version": "0.1",
        "agent_id": "ag_test",
        "name": "test-agent",
        "description": "A scaffolding-time placeholder agent.",
        "model": {
            "provider": "mock",
            "model": "mock-1",
            "parameters": {},
            "fallback": [],
        },
        "prompt": "You are a test agent.",
        "io": {"mode": "conversational", "inputs": {}, "outputs": {}},
        "subsystems": {},
        "runtime": {},
    }
    defaults.update(overrides)
    return defaults


def make_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """Return a well-formed event dict for the given type.

    Envelope keys (`event_id`, `parent_event_id`, `thread_id`,
    `session_id`, `actor`, `created_at`) may be supplied as keyword
    arguments; anything else lands in the `data` payload. Defaults
    match Module API Reference §13.2 so tests can already key by
    `type`, `data`, etc.

    Currently returns a plain dict; the canonical `Event` Pydantic
    model lands with the store/engine.
    """
    event_id = fields.pop("event_id", "ev_stub")
    parent_event_id = fields.pop("parent_event_id", None)
    thread_id = fields.pop("thread_id", "th_stub")
    session_id = fields.pop("session_id", "ses_stub")
    actor = fields.pop("actor", {"kind": "system"})
    created_at = fields.pop("created_at", "1970-01-01T00:00:00Z")
    # Whatever remains is the type-specific payload. Copy so the
    # returned event is independent of the caller's local state.
    payload = dict(fields)
    return {
        "event_id": event_id,
        "parent_event_id": parent_event_id,
        "thread_id": thread_id,
        "session_id": session_id,
        "type": event_type,
        "data": payload,
        "actor": actor,
        "created_at": created_at,
    }


def make_session(**overrides: Any) -> dict[str, Any]:
    """Return a Session entity dict with a temp JSONL placeholder.

    Real `Session` Pydantic model lands with the store. Tests using
    this builder for the entity-record fields keep working; tests that
    need the on-disk JSONL should compose with `temp_platform_root`.
    """
    defaults: dict[str, Any] = {
        "session_id": "ses_stub",
        "agent_id": "ag_test",
        "name": "stub session",
        "starred": False,
        "archived": False,
        "created_at": "1970-01-01T00:00:00Z",
        "updated_at": "1970-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def temp_platform_root(tmp_path: Path) -> Path:
    """Return a fresh platform-root temp directory with the standard layout.

    Standard layout (per file-handling spec): `sessions/`, `content/`,
    `logs/`, plus a placeholder `platform.yaml`. Tests that need a
    different layout override after the fixture returns. Cleanup is
    handled by pytest's `tmp_path` (the parent fixture).
    """
    root = tmp_path / "platform-root"
    root.mkdir()
    (root / "sessions").mkdir()
    (root / "content").mkdir()
    (root / "logs").mkdir()
    # Minimal platform.yaml placeholder — the real bootstrap auto-fills
    # this on first run (TC-ASM-12). Keeping it empty here so tests
    # opt in to whatever they need.
    (root / "platform.yaml").write_text("", encoding="utf-8")
    return root
