"""TC-ENV-* regression suite — Phase 2.2 Step 15.

End-to-end coverage of the agent envelope per
``process/test-catalog.md`` Part 9.10:

- **TC-ENV-04** (plain-text prompt round-trip) — the
  backward-compatibility floor. A Phase 1.x prompt with no
  Jinja markers renders byte-identical.
- **TC-ENV-bug-closure** — the load-bearing test for the bug
  this work closes: an agent declaring ``context_files`` AND
  ``permissions`` AND a Jinja prompt referencing the five
  top-level variables loads through
  :meth:`AssetManagement.build_resolved_config`, runs through
  the engine pre-flight, and produces a rendered prompt with
  the variables substituted.
- **TC-ENV-11** (``agent.template_warning`` emission) — the
  warning fires when a prompt references variable data the
  resolved config doesn't carry.

The per-feature unit tests (under tests/unit/templating /
tests/component/templating / tests/component/authz /
tests/component/tools) cover the standalone contracts
(TC-ENV-01, 02, 05, 06, 07, 08, 09, 10, 12, 13, 14); this file
pins the END-TO-END composition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import yaml

from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from schemas import EventType

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture.
# ---------------------------------------------------------------------------


_DISTINCTIVE_REPLY = "RENDERED_BY_PHASE_2_2_PIPELINE"


def _write_workspace(
    root: Path,
    *,
    prompt: str,
    permissions: list[dict] | None = None,
    context_files: list[dict] | None = None,
) -> None:
    """Lay down a minimal workspace for an agent test."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {
                                "who": ".*",
                                "what": r"agent\.execute\..*",
                                "effect": "allow",
                            },
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
                "workspace_summary": "A Phase 2.2 envelope regression workspace.",
            }
        ),
        encoding="utf-8",
    )
    bundle = root / "bundles" / "demo"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    agent_payload: dict[str, object] = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "agent_id": "ag_demo",
        "name": "demo",
        "description": "Phase 2.2 envelope demo agent.",
        "model": {"provider": "fake", "model": "fake-model"},
        "prompt": prompt,
        "io": {"mode": "conversational"},
    }
    if permissions is not None:
        agent_payload["permissions"] = permissions
    if context_files is not None:
        agent_payload["context_files"] = context_files
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(agent_payload), encoding="utf-8"
    )


async def _stub_llm(request: LLMRequest) -> LLMResponse:
    _ = request
    return LLMResponse(content=_DISTINCTIVE_REPLY)


# ---------------------------------------------------------------------------
# TC-ENV-04 — plain-text prompt round-trips byte-identical.
# ---------------------------------------------------------------------------


async def test_tc_env_04_plain_text_prompt_renders_unchanged(tmp_path: Path) -> None:
    """Backward-compatibility floor: a prompt with no Jinja
    markers must render byte-identical to itself. Pinning so a
    Phase 2.2 regression doesn't break Phase 1.x agents."""
    prompt = (
        "You are a helpful assistant. Use the tools you have. "
        "Be concise. Don't invent reasons to use tools you don't need."
    )
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    config = await runtime.build_resolved_config("demo/agents/demo")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-env-04"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hello"},
    )
    await runtime.engine.execute(config, ctx)

    # Pull the config.set event — that's where the rendered prompt is
    # persisted by pre-flight step 5.6.6.
    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    config_set = next(e for e in events if e.type == EventType.CONFIG_SET)
    rendered_prompt = config_set.data["resolved_config"]["prompt"]
    assert rendered_prompt == prompt


# ---------------------------------------------------------------------------
# TC-ENV-bug-closure — full envelope, end-to-end.
# ---------------------------------------------------------------------------


async def test_envelope_end_to_end_bug_closure(tmp_path: Path) -> None:
    """The load-bearing test for the
    bug-phase2.2-envelope-schema-gap closure.

    An agent declares ``context_files`` AND ``permissions`` AND a
    Jinja prompt referencing every top-level variable. Builds
    cleanly through ``AssetManagement.build_resolved_config``
    (Layer 1 no longer rejects the envelope fields), runs through
    the engine pre-flight, and produces a rendered prompt
    persisted on ``config.set`` with every variable substituted.
    """
    # Write a context file the agent's context_files: will pull in.
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "CONVENTIONS.md").write_text(
        "Conventions for this workspace.\n", encoding="utf-8"
    )
    prompt = (
        "You are {{ agent.name }}, running on {{ agent.model.ref }}.\n\n"
        "{{ workspace }}\n\n"
        "Your tools:\n"
        "{% for t in tools %}- {{ t.name }}: {{ t.summary }}\n{% endfor %}\n"
        "Your docs:\n{{ docs.summary() }}"
    )
    _write_workspace(
        tmp_path,
        prompt=prompt,
        permissions=[
            {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
        ],
        context_files=[
            {
                "path": "workspace:data/CONVENTIONS.md",
                "summary": "Workspace conventions",
                "primary": False,
            },
        ],
    )

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    # The bug repro path — build_resolved_config used to raise
    # ConfigInvalid with extra_forbidden on context_files / permissions.
    config = await runtime.build_resolved_config("demo/agents/demo")
    assert len(config.permissions) == 1
    assert len(config.context_files) == 1
    assert config.permissions[0].effect == "prompt"
    assert config.context_files[0].path == "workspace:data/CONVENTIONS.md"

    session = await runtime.store.create_session(
        str(config.agent_id), name=f"bug-closure-{uuid4()}"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    await runtime.engine.execute(config, ctx)

    # Verify the persisted config.set carries the RENDERED prompt
    # — variables substituted, no literal {{ }} markers.
    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    config_set = next(e for e in events if e.type == EventType.CONFIG_SET)
    rendered = config_set.data["resolved_config"]["prompt"]
    assert "{{ agent.name }}" not in rendered
    assert "demo" in rendered  # agent.name substituted
    assert "Workspace conventions" in rendered  # docs.summary() rendered
    assert "Phase 2.2 envelope regression workspace" in rendered  # workspace_summary


# ---------------------------------------------------------------------------
# TC-ENV-11 — agent.template_warning emission.
# ---------------------------------------------------------------------------


async def test_tc_env_11_template_warning_for_empty_data_reference(
    tmp_path: Path,
) -> None:
    """Spec §9: a prompt that references ``{{ docs }}`` against an
    agent declaring no ``context_files`` emits an
    :event:`agent.template_warning` (Recoverable). The warning
    surfaces in the JSONL for chat / replay consumption."""
    prompt = "I have these docs: {{ docs }}"
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    config = await runtime.build_resolved_config("demo/agents/demo")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-env-11"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    warnings = [e for e in events if e.type == EventType.AGENT_TEMPLATE_WARNING]
    assert len(warnings) >= 1
    assert warnings[0].data["reference"] == "docs"
    assert "context_files" in warnings[0].data["hint"]


# ---------------------------------------------------------------------------
# TC-ENV-12 — qol-skills-jinja-variable-symmetric-with-tools.
# Symmetric warning when {{ skills }} is referenced but agent has none.
# ---------------------------------------------------------------------------


async def test_tc_env_12_template_warning_for_empty_skills_reference(
    tmp_path: Path,
) -> None:
    """A prompt referencing ``{{ skills }}`` against an agent with
    no skills declared emits :event:`agent.template_warning` —
    symmetric with the docs/tools branches."""
    prompt = "I have these skills: {{ skills }}"
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    config = await runtime.build_resolved_config("demo/agents/demo")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-env-12"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    warnings = [
        e
        for e in events
        if e.type == EventType.AGENT_TEMPLATE_WARNING
        and e.data["reference"] == "skills"
    ]
    assert len(warnings) >= 1
    # Hint guides the user to add a skills entry, mirroring the
    # docs/tools hint wording.
    assert "skills" in warnings[0].data["hint"]


async def test_tc_env_13_template_warning_for_skills_declared_but_not_referenced(
    tmp_path: Path,
) -> None:
    """qol-skills-jinja-variable-symmetric-with-tools — the
    migration warning. Pre-QoL, every rendered prompt got an
    auto-appended ``## Available Skills`` block; post-QoL the
    template owns rendering. Agents that declare skills but
    whose template omits ``{{ skills }}`` would silently lose
    the listing, so a Recoverable fires at agent-load."""
    # Lay down the standard workspace with a prompt that does NOT
    # reference {{ skills }} (and doesn't contain the word as
    # substring — the engine's referenced-check matches the literal
    # substring per the existing tools/docs heuristic).
    prompt = "You are a plain agent. Use the tools you have."
    _write_workspace(tmp_path, prompt=prompt)

    # Patch the agent file to declare a skill, and lay down the
    # corresponding SKILL.md so the catalog resolves.
    agent_path = tmp_path / "bundles" / "demo" / "agents" / "demo.yaml"
    agent_payload = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    agent_payload["subsystems"] = {
        "skills": {
            "variant": "standard",
            "config": {"enabled": [{"ref": "demo/skills/literature-review"}]},
        },
    }
    agent_path.write_text(yaml.safe_dump(agent_payload), encoding="utf-8")
    skill_dir = tmp_path / "bundles" / "demo" / "skills" / "literature-review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: literature-review\ndescription: A test skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )

    runtime = await bootstrap_runtime(root=tmp_path, llm=_stub_llm)
    config = await runtime.build_resolved_config("demo/agents/demo")
    session = await runtime.store.create_session(
        str(config.agent_id), name="tc-env-13"
    )
    threads = await runtime.store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hi"},
    )
    await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    warnings = [
        e
        for e in events
        if e.type == EventType.AGENT_TEMPLATE_WARNING
        and e.data["reference"] == "skills"
    ]
    assert len(warnings) >= 1
    # The migration warning's reason names the QoL doc + hints at the fix.
    assert "auto-appended" in warnings[0].data["reason"]
    assert "{{ skills }}" in warnings[0].data["hint"]
