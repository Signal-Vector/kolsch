"""bug-159 — shipped rules are canonical; author_workflow's bash prompt is live.

The starter `author_workflow` agent used to ship a legacy
`{who: ".*", what: "tool\\.bash", effect: prompt}` rule. Engine tool
actions are `<bundle>/tools/<name>`, so that regex never matched and a
workspace that widened `kolsch/tools/bash` to `allow` got no prompt for
this meta-agent — the rule's stated intent. Guard: every `permissions:`
/ `policy:` rule shipped in the starter bundle or repo `bundles/` is in
the canonical (no `who`) shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kolsch._yamlio import load
from providers.authz import (
    AuthzEffect,
    PermissionLayer,
    Rule,
    RuleBasedAuthorizationProvider,
)
from schemas.config import PermissionRule

_REPO = Path(__file__).resolve().parents[2]
_STARTER = _REPO / "src" / "cli" / "_starter_bundle"
_SHIPPED_ROOTS = (_STARTER, _REPO / "bundles")
_AUTHOR_WORKFLOW = _STARTER / "kolsch" / "agents" / "author_workflow.yaml"


def _shipped_rule_lists() -> list[tuple[str, list[Any]]]:
    found: list[tuple[str, list[Any]]] = []
    for root in _SHIPPED_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted([*root.rglob("*.yaml"), *root.rglob("*.yml")]):
            payload = load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            for key in ("permissions", "policy"):
                rules = payload.get(key)
                if isinstance(rules, list):
                    found.append((f"{path.relative_to(_REPO)}:{key}", rules))
    return found


def test_shipped_rule_lists_are_found() -> None:
    """Sanity: the scan sees the starter agents' `permissions:` blocks."""
    sources = [source for source, _ in _shipped_rule_lists()]
    assert any("author_workflow.yaml:permissions" in s for s in sources)
    assert any("helper.yaml:permissions" in s for s in sources)


@pytest.mark.parametrize(
    ("source", "rules"),
    _shipped_rule_lists(),
    ids=[source for source, _ in _shipped_rule_lists()],
)
def test_shipped_rules_are_canonical(source: str, rules: list[Any]) -> None:
    legacy = [r for r in rules if isinstance(r, dict) and "who" in r]
    assert legacy == [], f"{source} ships legacy-shape rules: {legacy}"


async def test_author_workflow_agent_layer_prompts_for_bash() -> None:
    """A workspace-wide bash `allow` must not bypass this agent's prompt."""
    payload = load(_AUTHOR_WORKFLOW.read_text(encoding="utf-8"))
    rules = tuple(
        Rule.model_validate(
            PermissionRule.model_validate(raw).model_dump(by_alias=True)
        )
        for raw in payload["permissions"]
    )
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ]
        }
    )
    result = await provider.allow(
        "user.local",
        "kolsch/tools/bash",
        target="kolsch workflows validate x.yaml",
        layers=(PermissionLayer(name="agent", rules=rules),),
        caller_chain=("kolsch/agents/author_workflow",),
    )
    assert result.effect is AuthzEffect.PROMPT
    assert "who_chain=agent" in result.reason
