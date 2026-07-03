"""Integration tests — bundle-level ``permissions:`` reach the authz
provider.

qol-tool-permission-discovery-uniform-with-stacking.md Step 2:
``bundle.yaml`` gains a ``permissions:`` field whose rules are
walked between the agent layer and the platform layer at
:meth:`providers.authz.RuleBasedAuthorizationProvider.allow`.

Two end-to-end scenarios against the real runtime bootstrap +
catalog scan + engine tools-subsystem registration:

1. A bundle whose ``bundle.yaml`` carries an
   ``allow``-effect rule for ``tool.bash`` lets a tool invocation
   pass even when the workspace's ``platform.yaml`` has the
   default ``prompt`` rule (which would collapse to deny in a
   non-chat context).

2. A bundle's ``deny`` rule wins over a ``platform.yaml`` allow
   rule — confirming the four-level walk order
   (agent → bundle → platform → no-rule → default-deny).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas import EventType
from tests.fixtures.fake_llm import (
    FakeLLM,
    TextResponse,
    ToolCallResponse,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# Workspace fixture — initialised via ``kolsch setup``
# ---------------------------------------------------------------------------


def _setup_workspace(root: Path) -> None:
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)


def _strip_agent_bash_prompt(root: Path) -> None:
    """Remove the starter ``helper.yaml``'s agent-level
    ``tool.bash → prompt`` rule so the BUNDLE layer drives the
    test instead of the agent layer."""
    helper_path = root / "bundles" / "kolsch" / "agents" / "helper.yaml"
    payload = yaml.safe_load(helper_path.read_text(encoding="utf-8")) or {}
    # qol-action-namespace-bundle-scoped.md Step 2 — helper.yaml's
    # bash rule is now 3-seg ``kolsch/tools/bash``; accept both forms
    # so this fixture works across the migration window.
    payload["permissions"] = [
        r
        for r in (payload.get("permissions") or [])
        if not (
            r.get("what") in (r"tool\.bash", "kolsch/tools/bash")
            and r.get("effect") == "prompt"
        )
    ]
    helper_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _set_bundle_permissions(root: Path, rules: list[dict[str, str]]) -> None:
    """Add a ``permissions:`` block to the kolsch bundle's
    ``bundle.yaml``."""
    bundle_path = root / "bundles" / "kolsch" / "bundle.yaml"
    payload = yaml.safe_load(bundle_path.read_text(encoding="utf-8")) or {}
    payload["permissions"] = rules
    bundle_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _make_fake_llm_callable(
    fake: FakeLLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Adapter matching the one in tests/integration/test_tool_wiring.py."""

    async def _llm(request: LLMRequest) -> LLMResponse:
        context: dict[str, Any] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        response = await fake.agenerate(context)
        if isinstance(response, TextResponse):
            return LLMResponse(content=response.text)
        if isinstance(response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": response.tool_call_id or f"tc_{uuid4()}",
                        "name": response.tool,
                        "arguments": dict(response.args),
                    },
                ],
            )
        msg = f"unexpected FakeLLM response shape: {response!r}"
        raise RuntimeError(msg)

    return _llm


# ---------------------------------------------------------------------------
# TC-PERMDISC-INT-01 — bundle allow rule lets a non-chat tool pass
# ---------------------------------------------------------------------------


async def test_bundle_allow_rule_passes_through_in_non_chat_context(
    tmp_path: Path,
) -> None:
    """A bundle ``allow`` rule for ``tool.bash`` should let a
    FakeLLM-driven invocation succeed even though the workspace's
    starter ``platform.yaml`` carries ``tool.bash → prompt`` (which
    would collapse to deny in a non-chat / no-callback context)."""
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    _strip_agent_bash_prompt(root)
    _set_bundle_permissions(
        root,
        [{"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"}],
    )

    fake = FakeLLM(
        script=[
            ToolCallResponse(
                tool="bash",
                args={"command": "echo bundle-allow-rule-01"},
                tool_call_id="tc_permdisc_01",
            ),
            TextResponse(text="Done — bundle-allow-rule-01"),
        ],
    )
    runtime = await bootstrap_runtime(
        root=root, llm=_make_fake_llm_callable(fake)
    )
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id), name="permdisc-int-01"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message",
            "content": "run echo bundle-allow-rule-01",
        },
    )
    events = [e async for e in runtime.engine.execute_stream(config, ctx)]

    tool_results = [e for e in events if e.type == EventType.TOOL_RESULT]
    failures = [e for e in events if e.type == EventType.THREAD_EXCHANGE_FAILED]
    assert len(tool_results) >= 1, (
        f"expected tool.result with bundle allow rule; got types "
        f"{[e.type for e in events]}; "
        f"failures={[f.data for f in failures]}"
    )


# ---------------------------------------------------------------------------
# TC-PERMDISC-INT-02 — bundle deny wins over platform allow
# ---------------------------------------------------------------------------


async def test_bundle_deny_rule_wins_over_platform_allow(
    tmp_path: Path,
) -> None:
    """Stacking precedence — bundle layer wins over platform.
    Workspace ``platform.yaml`` has ``tool.bash → allow``; the
    bundle's ``permissions:`` block has ``tool.bash → deny``.
    The bash invocation should fail with ``ToolUnauthorized`` (the
    tool.failed event) — confirming bundle layer fired first."""
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    _strip_agent_bash_prompt(root)

    # Flip platform.yaml's bash rule to allow. Match both forms so
    # this fixture works whether the workspace has been migrated yet.
    plat_path = root / "platform.yaml"
    plat = yaml.safe_load(plat_path.read_text(encoding="utf-8")) or {}
    for rule in plat["authorization"]["config"]["rules"]:
        if rule.get("what") in (r"tool\.bash", "kolsch/tools/bash"):
            rule["effect"] = "allow"
            break
    plat_path.write_text(yaml.safe_dump(plat, sort_keys=False), encoding="utf-8")

    _set_bundle_permissions(
        root,
        [{"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"}],
    )

    fake = FakeLLM(
        script=[
            ToolCallResponse(
                tool="bash",
                args={"command": "echo should-be-denied"},
                tool_call_id="tc_permdisc_02",
            ),
            TextResponse(text="should not be reached"),
        ],
    )
    runtime = await bootstrap_runtime(
        root=root, llm=_make_fake_llm_callable(fake)
    )
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id), name="permdisc-int-02"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message",
            "content": "run echo should-be-denied",
        },
    )
    events = [e async for e in runtime.engine.execute_stream(config, ctx)]

    tool_failures = [e for e in events if e.type == EventType.TOOL_FAILED]
    assert len(tool_failures) >= 1, (
        f"expected tool.failed (bundle deny wins); got types "
        f"{[e.type for e in events]}"
    )
    assert tool_failures[0].data["error"]["type"] == "tool.unauthorized"
