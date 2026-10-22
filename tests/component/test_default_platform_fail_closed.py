"""A bare-root bootstrap is fail-closed — bug-157.

`bootstrap_runtime` on a root with no `platform.yaml` auto-generates
one. Its rules must let agents run and deny every tool, in chat too
(a registered prompt callback is never consulted), and the generation
must be loud: one `assets` WARNING plus a header comment in the file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from assets.platform_config import (
    authorization_rule_source_map,
    default_platform_payload,
    load_or_create,
)
from platform_runtime import bootstrap_runtime
from providers import DEFAULT_DENY_RULE_ID, AuthzEffect

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TOOL_ACTIONS = ("kolsch/tools/bash", "tool.bash", "acme/tools/x")


async def test_bare_root_bootstrap_allows_agents_and_denies_tools(
    tmp_path: Path,
) -> None:
    runtime = await bootstrap_runtime(root=tmp_path)
    assert (tmp_path / "platform.yaml").is_file()

    agent = await runtime.authz.allow("user.local", "agent.execute.ag_x")
    assert agent.allowed is True

    for action in _TOOL_ACTIONS:
        decision = await runtime.authz.allow("user.local", action)
        assert decision.allowed is False, action
        assert decision.effect is AuthzEffect.DENY, action
        assert decision.rule_id == DEFAULT_DENY_RULE_ID, action


async def test_bare_root_tool_denied_without_consulting_prompt_callback(
    tmp_path: Path,
) -> None:
    runtime = await bootstrap_runtime(root=tmp_path)
    calls: list[str] = []

    async def _spy(
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
    ) -> AuthzEffect:
        _ = (rule_id, target, caller_chain, tool_call_id)
        calls.append(action)
        return AuthzEffect.ALLOW

    runtime.authz.register_prompt_callback(_spy)
    try:
        for action in _TOOL_ACTIONS:
            decision = await runtime.authz.allow("user.local", action)
            assert decision.allowed is False, action
            assert decision.rule_id == DEFAULT_DENY_RULE_ID, action
    finally:
        runtime.authz.clear_prompt_callback()
    assert calls == []


def test_auto_create_logs_one_warning_naming_path_and_setup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "platform.yaml"
    with caplog.at_level(logging.DEBUG, logger="assets"):
        load_or_create(tmp_path)

    records = [r for r in caplog.records if r.name == "assets"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert str(path) in message
    assert "agents may run; every tool is denied" in message
    assert "kolsch setup" in message

    # A second call reads the existing file and stays quiet.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="assets"):
        load_or_create(tmp_path)
    assert [r for r in caplog.records if r.name == "assets"] == []


def test_generated_file_has_header_and_round_trips(tmp_path: Path) -> None:
    generated = load_or_create(tmp_path)
    text = (tmp_path / "platform.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()

    header = []
    for line in lines:
        if not line.startswith("#"):
            break
        header.append(line)
    assert header, "generated platform.yaml must start with a comment block"
    header_text = "\n".join(header)
    assert "agents may run; every tool is denied" in header_text
    assert "kolsch setup" in header_text

    assert generated == default_platform_payload()
    assert load_or_create(tmp_path) == default_platform_payload()

    # Rule line numbers account for the header: each points at the
    # line holding that rule's `what:` key.
    rules: list[dict[str, Any]] = generated["authorization"]["config"]["rules"]
    source_map = authorization_rule_source_map(tmp_path)
    assert sorted(source_map) == list(range(len(rules)))
    for index, rule in enumerate(rules):
        source_file, line_no = source_map[index]
        assert source_file == str(tmp_path / "platform.yaml")
        assert f"what: {rule['what']}" in lines[line_no - 1] or (
            f"what: '{rule['what']}'" in lines[line_no - 1]
        )
