"""bug-agent-inputs-template-scope.md — end-to-end check.

Pins that ``build_resolved_config`` raises
``ConfigInputsInAgentTemplate`` at agent-load when the agent's
prompt template references ``{{ inputs.* }}``. Before the fix,
this shape silently reached first-invocation and raised
``ConfigTemplateRenderFailed`` with a generic ``'inputs' is
undefined`` reason — hard to attribute to the root cause without
reading the render harness's source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from errors import ConfigInputsInAgentTemplate
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path


def _write_workspace(root: Path, *, prompt: str) -> None:
    """Lay down a minimal workspace with one agent using ``prompt``."""
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
    (bundle / "agents" / "probe.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_probe",
                "name": "probe",
                "description": "bug-agent-inputs-template-scope repro agent.",
                "model": {"provider": "fake", "model": "fake-model"},
                "prompt": prompt,
                "io": {"mode": "single_shot"},
            }
        ),
        encoding="utf-8",
    )


async def test_agent_prompt_with_inputs_fails_at_agent_load(
    tmp_path: Path,
) -> None:
    """The spec's Steps to reproduce: agent's prompt references
    ``{{ inputs.job_description }}``; build_resolved_config raises
    ``ConfigInputsInAgentTemplate`` with the reference named."""
    prompt = (
        "You are {{ agent.name }}.\n"
        "## Input\n"
        "{{ inputs.job_description }}\n"
        "## Task\n"
        "Reply with 'OK'."
    )
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path)
    with pytest.raises(ConfigInputsInAgentTemplate) as excinfo:
        await runtime.build_resolved_config("demo/agents/probe")

    exc = excinfo.value
    assert exc.code == "config.inputs_in_agent_template"
    assert "demo/agents/probe" in str(exc)
    assert "{{ inputs.job_description }}" in str(exc)
    assert exc.detail["references"] == ["inputs.job_description"]


async def test_agent_prompt_with_multiple_inputs_lists_them_all(
    tmp_path: Path,
) -> None:
    """When the prompt references several ``inputs.*`` fields, all
    show up in the error's ``references`` list."""
    prompt = (
        "Task: {{ inputs.job_description }}\n"
        "Rubric: {{ inputs.rubric }}\n"
        'Reply format: {{ inputs["reply_format"] }}\n'
    )
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path)
    with pytest.raises(ConfigInputsInAgentTemplate) as excinfo:
        await runtime.build_resolved_config("demo/agents/probe")

    exc = excinfo.value
    assert exc.detail["references"] == [
        "inputs.job_description",
        "inputs.rubric",
        'inputs["reply_format"]',
    ]


async def test_agent_prompt_without_inputs_still_loads(tmp_path: Path) -> None:
    """The classifier-shape pattern (no ``{{ inputs.* }}`` in prompt)
    loads cleanly. Pinning that the fix doesn't over-fire."""
    prompt = (
        "You are {{ agent.name }}.\n"
        "The user message contains a `job_description` field. "
        "Read it and reply with 'OK'."
    )
    _write_workspace(tmp_path, prompt=prompt)

    runtime = await bootstrap_runtime(root=tmp_path)
    config = await runtime.build_resolved_config("demo/agents/probe")
    assert config.name == "probe"
