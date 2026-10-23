"""bug-163 — the provider matches file-tool path targets canonically.

A real `RuleBasedAuthorizationProvider` with `path_target=` built from
each raw spelling. Pins the effect rule (allows strict, denies/prompts
loose) for new-shape and legacy rules, the prompt path, unchanged rule
ids, unchanged default rules, and byte-identical decisions for every
non-path target (`path_target=None`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cli._setup import _workspace_only_default_rules
from files.authz_target import PathTarget, normalise_path_target
from providers.authz import (
    DEFAULT_DENY_RULE_ID,
    AllowAllTestAuthorizationProvider,
    AuthzEffect,
    Rule,
    RuleBasedAuthorizationProvider,
    derive_rule_id,
)

if TYPE_CHECKING:
    from pathlib import Path

_WRITE = "kolsch/tools/write_file"
_TAIL: dict[str, str] = {"what": "*", "pattern": "*", "effect": "deny"}


def _provider(rules: list[dict[str, Any]]) -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(config={"rules": rules})


def _id(pattern: str, effect: str, what: str = _WRITE) -> str:
    return derive_rule_id(".*", f"{what}|{pattern}", effect)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = (tmp_path / "ws").resolve()
    (root / "bundles").mkdir(parents=True)
    (root / "data" / "secret").mkdir(parents=True)
    return root


def _pt(raw: str, root: Path, *, ci: bool = False) -> PathTarget:
    return normalise_path_target(raw, root=root, case_insensitive=ci)


async def _decide(
    provider: RuleBasedAuthorizationProvider,
    raw: str,
    root: Path,
    *,
    ci: bool = False,
    action: str = _WRITE,
) -> tuple[AuthzEffect, str | None]:
    pt = _pt(raw.replace("R/", f"{root.as_posix()}/", 1), root, ci=ci)
    result = await provider.allow(
        "user.local", action, target=pt.display, path_target=pt
    )
    return result.effect, result.rule_id


def _probe_rules(root: Path) -> list[dict[str, Any]]:
    return [
        {"what": _WRITE, "pattern": "data/secret/*", "effect": "deny"},
        {"what": _WRITE, "pattern": "bundles/*", "effect": "allow"},
        {"what": _WRITE, "pattern": f"{root.as_posix()}/*", "effect": "allow"},
        _TAIL,
    ]


# --- AC 6: the probe rule set -----------------------------------------------


async def test_traversal_is_matched_by_absolute_allow_not_by_bundles(ws: Path) -> None:
    effect, rule_id = await _decide(
        _provider(_probe_rules(ws)), "bundles/../platform.yaml", ws
    )

    assert effect is AuthzEffect.ALLOW
    assert rule_id == _id(f"{ws.as_posix()}/*", "allow")


@pytest.mark.parametrize(
    "raw", ["R/data/secret/k", "./data/secret/k", "DATA/secret/k", "data//secret/./k"]
)
async def test_every_spelling_hits_the_relative_deny(ws: Path, raw: str) -> None:
    effect, rule_id = await _decide(_provider(_probe_rules(ws)), raw, ws)

    assert effect is AuthzEffect.DENY
    assert rule_id == _id("data/secret/*", "deny")


async def test_escape_from_workspace_hits_no_rule(ws: Path) -> None:
    effect, rule_id = await _decide(_provider(_probe_rules(ws)), "R/../etc/passwd", ws)

    assert effect is AuthzEffect.DENY
    assert rule_id == DEFAULT_DENY_RULE_ID


async def test_relative_spelling_matches_absolute_allow(ws: Path) -> None:
    effect, rule_id = await _decide(_provider(_probe_rules(ws)), "data/x", ws)

    assert effect is AuthzEffect.ALLOW
    assert rule_id == _id(f"{ws.as_posix()}/*", "allow")


async def test_bundles_allow_alone_no_longer_covers_traversal(ws: Path) -> None:
    provider = _provider(
        [{"what": _WRITE, "pattern": "bundles/*", "effect": "allow"}, _TAIL]
    )

    assert (await _decide(provider, "bundles/../platform.yaml", ws))[1] == (
        DEFAULT_DENY_RULE_ID
    )
    assert (await _decide(provider, "./bundles/x", ws))[0] is AuthzEffect.ALLOW
    assert (await _decide(provider, "R/bundles/x", ws))[0] is AuthzEffect.ALLOW


@pytest.mark.parametrize(("ci", "expected"), [(False, "deny"), (True, "allow")])
async def test_allow_case_rule_follows_the_injected_flag(
    ws: Path, *, ci: bool, expected: str
) -> None:
    provider = _provider(
        [{"what": _WRITE, "pattern": "bundles/*", "effect": "allow"}, _TAIL]
    )

    effect, _ = await _decide(provider, "BUNDLES/x", ws, ci=ci)

    assert effect.value == expected


# --- AC 6: legacy regex rules -----------------------------------------------


async def test_legacy_allow_does_not_match_traversal(ws: Path) -> None:
    provider = _provider(
        [{"who": ".*", "what": "^bundles/.*$", "effect": "allow"}, _TAIL]
    )

    effect, rule_id = await _decide(provider, "bundles/../platform.yaml", ws)
    assert (effect, rule_id) == (AuthzEffect.DENY, DEFAULT_DENY_RULE_ID)
    assert (await _decide(provider, "R/bundles/x", ws))[0] is AuthzEffect.ALLOW


@pytest.mark.parametrize("ci", [False, True])
@pytest.mark.parametrize("raw", ["R/data/secret/k", "DATA/secret/k", "data/secret/k/"])
async def test_legacy_deny_matches_loose_forms(ws: Path, raw: str, *, ci: bool) -> None:
    provider = _provider(
        [
            {"who": ".*", "what": "^data/secret/.*$", "effect": "deny"},
            {"what": "*", "pattern": "*", "effect": "allow"},
        ]
    )

    effect, rule_id = await _decide(provider, raw, ws, ci=ci)

    assert effect is AuthzEffect.DENY
    assert rule_id != _id("*", "allow", what="*")


async def test_legacy_rule_still_matches_the_action(ws: Path) -> None:
    provider = _provider(
        [{"who": ".*", "what": "^kolsch/tools/write_file$", "effect": "allow"}, _TAIL]
    )

    effect, _ = await _decide(provider, "bundles/../../etc/x", ws)

    assert effect is AuthzEffect.ALLOW


# --- AC 5 / AC 12: non-path and default rule sets are unchanged ----------------


_NON_PATH_CASES: list[tuple[str, str | None]] = [
    ("kolsch/tools/bash", "ls ../.."),
    ("kolsch/tools/bash", "cat bundles/../platform.yaml"),
    ("kolsch/tools/fetch_url", "https://example.com/../x"),
    ("kolsch/tools/run_workflow", "kolsch/workflows/w"),
    ("kolsch/tools/jev_decide", '{"path": "bundles/../platform.yaml"}'),
    ("agent.execute.kolsch/agents/helper", None),
    (_WRITE, "bundles/../platform.yaml"),
    (_WRITE, "/ws/data/secret/k"),
]


def _raw_glob_rules() -> list[dict[str, Any]]:
    return [
        {"what": _WRITE, "pattern": "data/secret/*", "effect": "deny"},
        {"what": _WRITE, "pattern": "bundles/*", "effect": "allow"},
        {"what": _WRITE, "pattern": "/ws/*", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "cat *", "effect": "allow"},
        {"what": "kolsch/tools/fetch_url", "pattern": "https://*", "effect": "prompt"},
        {"what": "agent.execute.*", "pattern": "*", "effect": "allow"},
        _TAIL,
    ]


_RAW_EXPECTED: dict[tuple[str, str | None], str] = {
    ("kolsch/tools/bash", "ls ../.."): DEFAULT_DENY_RULE_ID,
    ("kolsch/tools/bash", "cat bundles/../platform.yaml"): derive_rule_id(
        ".*", "kolsch/tools/bash|cat *", "allow"
    ),
    ("kolsch/tools/fetch_url", "https://example.com/../x"): derive_rule_id(
        ".*", "kolsch/tools/fetch_url|https://*", "prompt"
    ),
    ("kolsch/tools/run_workflow", "kolsch/workflows/w"): DEFAULT_DENY_RULE_ID,
    ("kolsch/tools/jev_decide", '{"path": "bundles/../platform.yaml"}'): (
        DEFAULT_DENY_RULE_ID
    ),
    ("agent.execute.kolsch/agents/helper", None): derive_rule_id(
        ".*", "agent.execute.*|*", "allow"
    ),
    # path_target=None keeps the raw matcher — the pre-bug-163 answers.
    (_WRITE, "bundles/../platform.yaml"): _id("bundles/*", "allow"),
    (_WRITE, "/ws/data/secret/k"): _id("/ws/*", "allow"),
}


@pytest.mark.parametrize(("action", "target"), _NON_PATH_CASES)
async def test_path_target_none_is_raw_matching(
    action: str, target: str | None
) -> None:
    result = await _provider(_raw_glob_rules()).allow(
        "user.local", action, target=target, path_target=None
    )

    assert result.rule_id == _RAW_EXPECTED[(action, target)]


@pytest.mark.parametrize(
    "raw", ["platform.yaml", "bundles/../platform.yaml", "/etc/passwd", "DATA/x"]
)
async def test_setup_default_rules_decide_the_same(ws: Path, raw: str) -> None:
    rules: list[dict[str, Any]] = list(_workspace_only_default_rules())
    provider = _provider(rules)
    pt = _pt(raw, ws)
    for rule in rules:
        action = rule["what"] if "*" not in rule["what"] else _WRITE
        before = await provider.allow("user.local", action, target=raw)
        after = await provider.allow(
            "user.local", action, target=pt.display, path_target=pt
        )
        assert (after.effect, after.rule_id) == (before.effect, before.rule_id)


async def test_allow_all_test_provider_accepts_path_target(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KOLSCH_TEST_BUILD", "1")
    provider = AllowAllTestAuthorizationProvider(config={})
    pt = _pt("bundles/../x", ws)

    result = await provider.allow(
        "user.local", _WRITE, target=pt.display, path_target=pt
    )

    assert result.allowed is True


# --- AC 10: prompt path -----------------------------------------------------


async def test_prompt_sees_display_form_and_persisted_rule_matches_both(
    ws: Path,
) -> None:
    provider = _provider([{"what": _WRITE, "pattern": "*", "effect": "prompt"}, _TAIL])
    seen: list[str | None] = []

    async def callback(
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
        execution_context: object = None,
    ) -> tuple[AuthzEffect, Rule]:
        _ = rule_id, caller_chain, execution_context
        seen.append(target)
        assert target is not None
        return AuthzEffect.ALLOW, Rule(what=action, pattern=target, effect="allow")

    pt = _pt("bundles/../data/x.txt", ws)
    first = await provider.allow(
        "user.local", _WRITE, target=pt.display, path_target=pt, callback=callback
    )

    assert first.allowed is True
    assert seen == ["data/x.txt"]
    persisted = _id("data/x.txt", "allow")
    for raw in ("bundles/../data/x.txt", "data/x.txt", "./data/x.txt"):
        effect, rule_id = await _decide(provider, raw, ws)
        assert (effect, rule_id) == (AuthzEffect.ALLOW, persisted)
    assert seen == ["data/x.txt"]


# --- AC 11: stored patterns and ids are unchanged -------------------------------


@pytest.mark.parametrize("pattern", ["./data/*", "/abs/x/*", "bundles/*", "*"])
async def test_rule_patterns_and_ids_are_verbatim(pattern: str) -> None:
    provider = _provider(
        [{"what": _WRITE, "pattern": pattern, "effect": "allow"}, _TAIL]
    )

    first = (await provider.list_rules())[0]

    assert first.pattern == pattern
    assert first.id == _id(pattern, "allow")


async def test_dotslash_pattern_now_matches_its_canonical_target(ws: Path) -> None:
    provider = _provider(
        [{"what": _WRITE, "pattern": "./data/*", "effect": "allow"}, _TAIL]
    )

    effect, rule_id = await _decide(provider, "data/k", ws)

    assert (effect, rule_id) == (AuthzEffect.ALLOW, _id("./data/*", "allow"))
