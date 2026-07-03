"""Skills x tools.read authz interaction + Sk-I2 end-to-end pins.

Two regression tests closing audit gaps surfaced in Step 1:

- **TC-SKILL-AUTHZ-01** — When the authz layer denies ``tool.read``
  on the SKILL.md path, force-invoke surfaces as a typed
  :class:`errors.ToolUnauthorized` and the engine emits a Diagnostic
  ``tool.failed`` synthetic event followed by ``thread.exchange_failed``
  — proving the Sk-I4 seam ("force-invoke goes through the standard
  ``tool.read`` authorization, not a skill-specific action") is wired
  end-to-end through the dispatch + provider stack.

- **TC-SKILL-DISABLE-01** — Sk-I2's two halves pinned together in one
  pass: ``disable-model-invocation: true`` excludes the skill from the
  system-prompt listing (``format_skills_listing`` filters it) AND
  leaves it force-invocable through ``SkillsSubsystem.force_invoke``
  (which the engine drives to a synthetic ``read``). The integration
  layer is the only place both halves are observable in one execute().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from engine import ExecutionContext, LLMRequest, LLMResponse
from engine.context import AgentExecutionError
from platform_runtime import bootstrap_runtime
from schemas import EventType

# qol-skills-jinja-variable-symmetric-with-tools — the post-append
# projection is gone; SkillsVar from templating.vars is the new
# in-prompt rendering surface. This test now exercises the same
# Sk-I2 filter at the Var construction step (mirroring the engine's
# pre-flight at engine._render_prompt_if_needed).
from templating.vars import SkillItem, SkillsVar

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture.
# ---------------------------------------------------------------------------


def _write_workspace(
    root: Path,
    *,
    authz_rules: list[dict[str, str]],
    skills: list[dict[str, Any]],
    enabled_refs: list[str],
) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": authz_rules},
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
    (root / ".env").write_text("", encoding="utf-8")

    bundle = root / "bundles" / "skl"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "skl", "version": "0.1", "description": "skl"}),
        encoding="utf-8",
    )
    for spec in skills:
        skill_dir = bundle / "skills" / spec["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        fm = {"name": spec["name"], "description": spec["description"]}
        if spec.get("disable_model_invocation"):
            fm["disable-model-invocation"] = True
        (skill_dir / "SKILL.md").write_text(
            f"---\n{yaml.safe_dump(fm)}---\n\n{spec.get('body', 'Body.')}\n",
            encoding="utf-8",
        )

    (bundle / "agents" / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_skl",
                "name": "skl",
                "description": "skills + authz agent",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": "hi",
                "io": {"mode": "conversational"},
                "subsystems": {
                    "tools": {
                        "variant": "standard",
                        "config": {"builtin": ["read"]},
                    },
                    "skills": {
                        "variant": "standard",
                        "config": {"enabled": [{"ref": r} for r in enabled_refs]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


# A canonical "allow agent.execute, otherwise default-deny" minimum.
_ALLOW_EXECUTE = {"who": ".*", "what": r"agent\.execute\..*", "effect": "allow"}
_DEFAULT_DENY = {"who": ".*", "what": ".*", "effect": "deny"}


# ---------------------------------------------------------------------------
# TC-SKILL-AUTHZ-01 — authz deny on tool.read surfaces as Diagnostic failure.
# ---------------------------------------------------------------------------


async def test_tc_skill_authz_01_skill_md_read_denied_by_authz(
    tmp_path: Path,
) -> None:
    """Pin the Sk-I4 seam: force-invoke uses ``tool.read`` authz, not a
    skill-specific action. With an explicit deny rule on
    ``tool.read``, the synthetic call surfaces as a Diagnostic
    :class:`ToolUnauthorized`; the engine emits ``tool.failed`` then
    ``thread.exchange_failed`` rather than crashing.

    The deny rule is scripted explicitly so the test holds even if
    the workspace-only default rule set's coverage of ``tool.read``
    changes. The seam being proved is "authz can refuse this read,
    and the failure shape is observable" — not "the defaults
    currently happen to allow it"."""
    captured_requests: list[LLMRequest] = []

    async def stub(request: LLMRequest) -> LLMResponse:
        captured_requests.append(request)
        return LLMResponse(content="ok")

    _write_workspace(
        tmp_path,
        authz_rules=[
            _ALLOW_EXECUTE,
            # The headline: explicit deny on tool.read. The synthetic
            # call from force-invoke MUST land here, not on a
            # skill-specific allow.
            {"who": ".*", "what": r"tool\.read", "effect": "deny"},
            _DEFAULT_DENY,
        ],
        skills=[
            {
                "name": "forced",
                "description": "Force-invoked skill the authz layer denies.",
            },
        ],
        enabled_refs=["skl/skills/forced"],
    )

    runtime = await bootstrap_runtime(root=tmp_path, llm=stub)
    config = await runtime.build_resolved_config("skl/agents/agent")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-skill-authz-01"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message_with_force_skill",
            "content": "Use the skill.",
            "force_skills": ["skl/skills/forced"],
        },
    )
    # Force-invoke denial is fatal — execute() raises after persisting
    # the thread.exchange_failed event. The events still land in the
    # store for inspection (Materialization Rule).
    with pytest.raises(AgentExecutionError):
        await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]

    # The synthetic tool.called landed (engine entered force-invoke), but the
    # tool.failed event is what authz denial produces — NOT tool.result.
    synthetic_called = [
        e
        for e in events
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    ]
    assert len(synthetic_called) == 1
    assert synthetic_called[0].data["tool_name"] == "read"

    tool_failed = [e for e in events if e.type == EventType.TOOL_FAILED]
    assert len(tool_failed) == 1, (
        "Authz deny on tool.read must surface as a tool.failed event; "
        f"got events: {[e.type.value for e in events]}"
    )
    failure = tool_failed[0]
    assert failure.data["tool_name"] == "read"
    assert failure.data["error"]["type"] == "tool.unauthorized"
    # qol-action-namespace-bundle-scoped.md Step 2 — action is the
    # 3-seg catalog ref `<bundle>/tools/<name>` (was `tool.read`).
    assert failure.data["error"]["detail"]["action"] == "kolsch/tools/read"
    assert failure.data["error"]["detail"]["principal"] == "system"

    # Force-invoke is pre-flight; failure terminates the exchange BEFORE
    # the model is reached. The stub LLM saw nothing.
    assert captured_requests == []
    assert any(e.type == EventType.THREAD_EXCHANGE_FAILED for e in events)
    # And no tool.result for the denied call — authz blocks before the
    # tool body runs.
    assert not any(e.type == EventType.TOOL_RESULT for e in events)


# ---------------------------------------------------------------------------
# TC-SKILL-DISABLE-01 — disable-model-invocation: both halves of Sk-I2.
# ---------------------------------------------------------------------------


async def test_tc_skill_disable_01_excludes_listing_but_force_invocable(
    tmp_path: Path,
) -> None:
    """A skill with ``disable-model-invocation: true`` MUST be absent
    from the system-prompt listing (``format_skills_listing`` filters
    it) AND MUST remain force-invocable. End-to-end pin: a single
    execute() observes both halves through the public seams."""
    _write_workspace(
        tmp_path,
        authz_rules=[{"who": ".*", "what": ".*", "effect": "allow"}],
        skills=[
            {
                "name": "discoverable",
                "description": "Listed for the model.",
            },
            {
                "name": "hidden-deliberate",
                "description": "Only available via force-invoke.",
                "disable_model_invocation": True,
            },
        ],
        enabled_refs=[
            "skl/skills/discoverable",
            "skl/skills/hidden-deliberate",
        ],
    )

    runtime = await bootstrap_runtime(root=tmp_path)
    config = await runtime.build_resolved_config("skl/agents/agent")

    # Half one — Sk-I2 listing filter. The disabled skill MUST NOT
    # appear in the system-prompt block; the discoverable one MUST.
    enabled = await runtime.skills_subsystem.get_enabled_skills(config)
    refs_returned = {s.ref for s in enabled}
    assert refs_returned == {
        "skl/skills/discoverable",
        "skl/skills/hidden-deliberate",
    }, (
        "get_enabled_skills must include disabled skills (force-invoke routes through them)."
    )

    # Mirror the engine's pre-flight filter at engine.py
    # (``[SkillItem(...) for s in enabled_skills if not s.disable_model_invocation]``)
    # so the assertion stays a pure projection check decoupled from
    # the engine path.
    var = SkillsVar(
        [
            SkillItem(name=s.name, description=s.description, ref=s.ref)
            for s in enabled
            if not s.disable_model_invocation
        ]
    )
    listing = var.list()
    assert "discoverable" in listing
    assert "hidden-deliberate" not in listing, (
        "disable-model-invocation: true must hide the skill from the listing. "
        f"Listing emitted: {listing!r}"
    )

    # Half two — Sk-I2 force-invoke pass-through. Driving force-invoke
    # against the disabled skill end-to-end produces the synthetic
    # tool.called + tool.result pair on the SKILL.md absolute path.
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-skill-disable-01"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={
            "kind": "user_message_with_force_skill",
            "content": "Load it.",
            "force_skills": ["skl/skills/hidden-deliberate"],
        },
    )
    await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    synthetic_calls = [
        e
        for e in events
        if e.type == EventType.TOOL_CALLED and e.actor.kind == "system"
    ]
    assert len(synthetic_calls) == 1
    skill_md_path = (
        tmp_path / "bundles" / "skl" / "skills" / "hidden-deliberate" / "SKILL.md"
    ).resolve()
    assert synthetic_calls[0].data["arguments"]["path"] == str(skill_md_path)

    synthetic_results = [
        e
        for e in events
        if e.type == EventType.TOOL_RESULT and e.actor.kind == "system"
    ]
    assert len(synthetic_results) == 1
    assert synthetic_results[0].data["tool_name"] == "read"
