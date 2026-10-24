"""bug-162 Phase 3 — `/reload` keeps the chat's approval registration.

`handle_reload` builds a fresh `RuleBasedAuthorizationProvider` from
disk and swaps it into the runtime. Before the fix it never carried
over the chat's registered prompt callback or `on_rule_added` hook, so
after `/reload` every `prompt` rule collapsed to deny and uncovered
actions hit default deny: no approval ever appeared again and "always"
was impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

from cli.chat.approval import RulePersistHook
from cli.chat.services.authz_introspection import handle_reload
from platform_runtime import bootstrap_runtime
from providers import AuthzEffect, Rule, RuleBasedAuthorizationProvider

if TYPE_CHECKING:
    from pathlib import Path

    from platform_runtime import PlatformRuntime


def _seed(root: Path, rules: list[dict[str, str]]) -> None:
    from assets.platform_config import default_platform_payload

    tools = root / "bundles" / "kolsch" / "tools"
    tools.mkdir(parents=True)
    (root / "bundles" / "kolsch" / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "kolsch", "version": "0.1.0", "description": "x"}),
        encoding="utf-8",
    )
    for name in ("bash", "read_file", "list_files"):
        (tools / f"{name}.py").write_text(f"# stub for {name}\n", encoding="utf-8")
    payload = default_platform_payload()
    payload["authorization"] = {"variant": "rule_based", "config": {"rules": rules}}
    payload["defaults"] = {}
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _disk_keys(root: Path) -> list[tuple[str, str, str]]:
    payload = yaml.safe_load((root / "platform.yaml").read_text(encoding="utf-8"))
    return [
        (r["what"], r.get("pattern", "*"), r["effect"])
        for r in payload["authorization"]["config"]["rules"]
    ]


async def _register(runtime: PlatformRuntime) -> tuple[Any, Any, list[str]]:
    calls: list[str] = []

    async def modal(**kwargs: Any) -> tuple[AuthzEffect, Rule]:
        calls.append(str(kwargs["action"]))
        return (
            AuthzEffect.ALLOW,
            Rule(what=str(kwargs["action"]), pattern="*", effect="allow"),
        )

    hook = RulePersistHook(
        yaml_path=runtime.root / "platform.yaml",
        authz=lambda: runtime.authz,
        catalog=runtime.asset_management.catalog,
        notify=lambda *_a, **_kw: None,
    )
    assert isinstance(runtime.authz, RuleBasedAuthorizationProvider)
    runtime.authz.register_prompt_callback(modal, on_rule_added=hook)
    return modal, hook, calls


async def test_reload_keeps_prompt_callback_and_rule_hook(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [{"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"}],
    )
    runtime = await bootstrap_runtime(root=tmp_path)
    old = runtime.authz
    modal, hook, calls = await _register(runtime)

    result = await handle_reload(runtime)

    assert result.error is None, result.error
    new = runtime.authz
    assert new is not old
    assert isinstance(new, RuleBasedAuthorizationProvider)
    assert new.registered_prompt_callback is modal
    assert new.rule_added_hook is hook

    # An uncovered action prompts on the NEW provider and "always"
    # persists through the mutator.
    decision = await new.allow("agent.x", "kolsch/tools/list_files", target=".")
    assert decision.allowed
    assert calls == ["kolsch/tools/list_files"]
    assert ("kolsch/tools/list_files", "*", "allow") in _disk_keys(tmp_path)


async def test_failed_reload_keeps_old_provider_and_registration(
    tmp_path: Path,
) -> None:
    _seed(
        tmp_path,
        [{"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"}],
    )
    runtime = await bootstrap_runtime(root=tmp_path)
    old = runtime.authz
    modal, hook, _calls = await _register(runtime)
    (tmp_path / "platform.yaml").write_text(
        "authorization: [unclosed\n", encoding="utf-8"
    )

    result = await handle_reload(runtime)

    assert result.error is not None
    assert runtime.authz is old
    assert isinstance(old, RuleBasedAuthorizationProvider)
    assert old.registered_prompt_callback is modal
    assert old.rule_added_hook is hook


async def test_reload_without_registration_leaves_new_provider_unregistered(
    tmp_path: Path,
) -> None:
    """`kolsch run`-style runtimes (no chat) must not grow a callback."""
    _seed(
        tmp_path,
        [{"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"}],
    )
    runtime = await bootstrap_runtime(root=tmp_path)

    result = await handle_reload(runtime)

    assert result.error is None, result.error
    assert isinstance(runtime.authz, RuleBasedAuthorizationProvider)
    assert runtime.authz.registered_prompt_callback is None
    assert runtime.authz.rule_added_hook is None
