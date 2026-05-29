"""TC-API-01..06 — component tests for the FastAPI surface.

Uses :class:`fastapi.testclient.TestClient` against the in-process
app (no network, no uvicorn). The platform runtime is built per-test
with a tmp_path root and the EchoLLM default so the suite runs
without external services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from fastapi.testclient import TestClient

from api import build_app
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_platform_root(tmp_path: Path) -> Path:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {"who": ".*", "what": ".*", "effect": "allow"},
                        ],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    bundle = tmp_path / "bundles" / "acme"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents").mkdir(exist_ok=True)
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "Demo agent.",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    root = _make_platform_root(tmp_path)
    runtime = await bootstrap_runtime(root=root)
    app = build_app(runtime)
    return TestClient(app)


# ===========================================================================
# TC-API-01 — health probe
# ===========================================================================


def test_tc_api_01_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ===========================================================================
# TC-API-02 — create/list session
# ===========================================================================


def test_tc_api_02_create_and_list_session(client: TestClient) -> None:
    resp = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "from-api"},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["agent_id"] == "ag_demo"
    assert created["name"] == "from-api"
    assert created["session_id"].startswith("ses_")

    resp = client.get("/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert any(s["session_id"] == created["session_id"] for s in sessions)


# ===========================================================================
# TC-API-03 — execute agent (sync) end-to-end
# ===========================================================================


def test_tc_api_03_execute_agent_sync(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "exec-test"},
    ).json()

    resp = client.post(
        "/agents/acme/agents/demo/execute",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "user_message",
            "content": "hello",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_event_type"] == "agent.message"
    assert body["data"]["content"]["text"] == "Echo: hello"


# ===========================================================================
# TC-API-04 — execute_stream emits SSE in engine order
# ===========================================================================


def test_tc_api_04_execute_stream_yields_sse(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "stream-test"},
    ).json()

    with client.stream(
        "POST",
        "/agents/acme/agents/demo/execute_stream",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "user_message",
            "content": "stream",
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes()).decode("utf-8")

    # SSE messages look like: "event: <name>\ndata: <json>\n\n".
    events = []
    for raw_block in body.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        event_line, _sep, _data_line = block.partition("\n")
        if event_line.startswith("event: "):
            events.append(event_line[len("event: ") :])
    # The engine yields exchange_started → ... → exchange_completed,
    # then our generator appends a synthetic "done" terminator.
    assert "thread.exchange_started" in events
    assert "agent.message" in events
    assert "thread.exchange_completed" in events
    assert events[-1] == "done"


# ===========================================================================
# TC-API-05 — fetch session events
# ===========================================================================


def test_tc_api_05_get_session_events(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "events-test"},
    ).json()
    client.post(
        "/agents/acme/agents/demo/execute",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "user_message",
            "content": "trigger",
        },
    )

    resp = client.get(f"/sessions/{session['session_id']}/events")
    assert resp.status_code == 200
    events = resp.json()
    types = [e["type"] for e in events]
    assert "session.opened" in types
    assert "agent.message" in types


# ===========================================================================
# TC-API-06 — file_ref validation at the API boundary
# ===========================================================================


def test_tc_api_06_file_ref_input_outside_policy_rejected(
    client: TestClient,
) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "fileref-test"},
    ).json()

    # `something_path` triggers the file_ref heuristic in routes.py;
    # /etc/passwd resolves outside the default allowed_paths
    # (data/, bundles/) and gets a 400 at the boundary, NOT a 500
    # from the engine.
    resp = client.post(
        "/agents/acme/agents/demo/execute",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "invocation",
            "inputs": {"secret_path": "/etc/passwd"},
        },
    )
    assert resp.status_code == 400


# ===========================================================================
# TC-API-07 — unknown agent ref → 404
# ===========================================================================


def test_tc_api_07_unknown_agent_returns_404(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "missing-agent"},
    ).json()
    resp = client.post(
        "/agents/acme/agents/does_not_exist/execute",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "user_message",
            "content": "x",
        },
    )
    assert resp.status_code == 404


# ===========================================================================
# TC-API-08 — PATCH /sessions/{id} updates UI-state fields
# ===========================================================================


def test_tc_api_08_patch_session(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "before"},
    ).json()
    sid = session["session_id"]

    resp = client.patch(
        f"/sessions/{sid}",
        json={"name": "after", "starred": True},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["name"] == "after"
    assert updated["starred"] is True
    assert updated["archived"] is False  # unchanged


# ===========================================================================
# TC-API-09 — DELETE /sessions/{id} removes the entity + JSONL
# ===========================================================================


def test_tc_api_09_delete_session(client: TestClient) -> None:
    session = client.post(
        "/sessions",
        json={"agent_id": "ag_demo", "name": "to-delete"},
    ).json()
    sid = session["session_id"]

    resp = client.delete(f"/sessions/{sid}")
    assert resp.status_code == 204

    # GET events on the deleted session → 404 (session_not_found).
    resp = client.get(f"/sessions/{sid}/events")
    assert resp.status_code == 404


# ===========================================================================
# TC-API-10 — Layer 3 validator wired: wildcard write_tag rejected
# ===========================================================================


def test_tc_api_10_layer3_validator_fires(tmp_path: Path) -> None:
    """Step 13 review #1 — Layer 3 validators MUST fire on the
    production execute paths. Author an agent with an invalid memory
    config (wildcard write_tag, which MEM I5 forbids); the execute
    request must surface ConfigInvalid as 400, NOT silently flow into
    the engine."""
    import asyncio  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    from api import build_app  # noqa: PLC0415
    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    root = _make_platform_root(tmp_path)
    # Author a SECOND agent with a broken memory subsystem config —
    # write_tag contains the wildcard `*` which MEM I5 forbids.
    bad_agent = root / "bundles" / "acme" / "agents" / "broken_memory.yaml"
    bad_agent.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_broken",
                "name": "broken",
                "description": "broken memory config.",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "You are broken.",
                "io": {"mode": "conversational"},
                "subsystems": {
                    "memory": {
                        "variant": "recent_buffer",
                        "config": {"write_tag": "bad/*"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    runtime = asyncio.run(bootstrap_runtime(root=root))
    app = build_app(runtime)
    client = TestClient(app)

    session = client.post(
        "/sessions",
        json={"agent_id": "ag_broken", "name": "broken-test"},
    ).json()

    resp = client.post(
        "/agents/acme/agents/broken_memory/execute",
        json={
            "session_id": session["session_id"],
            "trigger_kind": "user_message",
            "content": "hi",
        },
    )
    # With the Layer 3 wire-up, ConfigInvalid fires inside
    # runtime.build_resolved_config and surfaces as 400 BEFORE the
    # engine sees the config — proving the validators run on the
    # production path (Step 13 review #1).
    assert resp.status_code == 400
    detail = resp.json()["detail"]["error"]
    assert detail["type"] == "config.invalid"
    validation_errors = detail["detail"]["validation_errors"]
    assert any(e["code"] == "wildcard_in_write_tag" for e in validation_errors)
