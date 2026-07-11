"""feat-041 — authz introspection: inline hint + /why + /reload tests.

Fifteen tests spanning ACs #1–#8. Each test targets one of the
spec's acceptance criteria; the module-level docstring at the top of
each test names the AC it covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kolsch._yamlio import load_with_positions
from cli.chat.services.authz_introspection import (
    DenialCardData,
    ReloadResult,
    _extract_layer_from_reason,
    _find_correlated_policy,
    _find_target_tool_failed,
    _tildify,
    find_last_denial_via_store,
    format_denial_hint,
    handle_reload,
    render_denial_card,
)
from providers.authz import (
    RuleBasedAuthorizationProvider,
)

# ---------------------------------------------------------------------------
# Helpers — a minimal Event double and a fake runtime for /why + /reload.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEvent:
    """Duck-typed stand-in for `schemas.Event` with just the fields the
    introspection helpers read (``type`` + ``data``)."""

    type: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        # `_event_data` picks up `.data` via `getattr`; make sure it's
        # a dict rather than a pydantic-shaped `_DataModel`.
        assert isinstance(self.data, dict)


class _FakeStoreDirect:
    """Store whose `read_path_to_leaf` IS an async-generator function."""

    def __init__(self, events: list[_FakeEvent]) -> None:
        self._events = events

    async def read_path_to_leaf(self, thread_id: object) -> Any:
        _ = thread_id
        for event in self._events:
            yield event


@dataclass
class _FakeRuntime:
    store: _FakeStoreDirect
    root: Path
    authz: Any = None
    asset_management: Any = None
    tools_subsystem: Any = None


# ---------------------------------------------------------------------------
# AC #1 & AC #3 — Rule carries source metadata from YAML.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_carries_source_metadata_from_yaml(tmp_path: Path) -> None:
    """Provider constructed with a `source_map` stamps each compiled
    rule's `source_file` / `source_line`."""
    source_file = str(tmp_path / "platform.yaml")
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "*", "pattern": "*", "effect": "allow"},
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
                {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
            ],
        },
        source_map={
            0: (source_file, 47),
            1: (source_file, 48),
            2: (source_file, 49),
        },
    )
    rules = await provider.list_rules()
    assert len(rules) == 3
    for idx, rule in enumerate(rules):
        assert rule.source_file == source_file
        assert rule.source_line == 47 + idx
        # Also carries through to model_dump.
        dumped = rule.model_dump()
        assert dumped["source_file"] == source_file
        assert dumped["source_line"] == 47 + idx


# ---------------------------------------------------------------------------
# AC #2 — `load_with_positions` line tracking (incl. anchors + merges).
# ---------------------------------------------------------------------------


def test_load_with_positions_line_tracking() -> None:
    """Each mapping node in a fixture YAML carries a `.lc.line` at its
    authoring line (0-indexed). Anchors + merge keys point at the use
    site, per spec §Design refinements."""
    yaml_text = (
        "authorization:\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: allow}\n"
        "      - {what: kolsch/tools/bash, pattern: '*', effect: deny}\n"
    )
    payload = load_with_positions(yaml_text)
    rules = payload["authorization"]["config"]["rules"]
    # Rule 0 lands on line index 3 (0-indexed).
    assert rules.lc.data[0][0] == 3
    assert rules.lc.data[1][0] == 4

    # Anchor + merge-key fixture — position tracks the USE site.
    yaml_anchor = (
        "defaults: &d\n"
        "  effect: deny\n"
        "rules:\n"
        "  - <<: *d\n"
        "    what: kolsch/tools/bash\n"
        "    pattern: '*'\n"
    )
    payload2 = load_with_positions(yaml_anchor)
    rules_seq = payload2["rules"]
    # The rule's line is where the sequence entry starts (line 3
    # after the `rules:` line at 2).
    assert rules_seq.lc.data[0][0] == 3


# ---------------------------------------------------------------------------
# AC #4 — `policy.decision.made` gains `tool_call_id`, `source_file`, `source_line`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_decision_event_carries_tool_call_id(
    tmp_path: Path,
) -> None:
    """The emit payload picks up `tool_call_id`, `source_file`,
    `source_line` when the caller supplies them + the matched rule
    carries source metadata."""
    from workflows.run_context import (
        WorkflowRunContext,
        reset_workflow_run_context,
        set_workflow_run_context,
    )
    from workflows.run_store import WorkflowRunStore

    source_file = "/tmp/fake-platform.yaml"
    provider = RuleBasedAuthorizationProvider(
        config={
            "audit_level": "all",
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
            ],
        },
        source_map={0: (source_file, 12)},
    )
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    store.append_event(run.run_id, {"kind": "workflow.run.started", "payload": {}})
    token = set_workflow_run_context(
        WorkflowRunContext(
            run_id=run.run_id, trace_id=run.trace_id, store=store,
        )
    )
    try:
        await provider.allow(
            "user.local",
            "kolsch/tools/bash",
            target="/tmp/x",
            tool_call_id="tc_abc123",
        )
    finally:
        reset_workflow_run_context(token)

    events = list(store.read_events(run.run_id))
    policy = next(e for e in events if e["kind"] == "policy.decision.made")
    assert policy["payload"]["tool_call_id"] == "tc_abc123"
    assert policy["payload"]["source_file"] == source_file
    assert policy["payload"]["source_line"] == 12


# ---------------------------------------------------------------------------
# AC #5 — ToolCallBlock denial hint (formatter + widget + OSC-8).
# ---------------------------------------------------------------------------


def test_tool_call_block_denial_hint_rendered() -> None:
    """`format_denial_hint` builds a 2-line hint referencing the
    rule id, effect, source file:line, and pattern."""
    matched = {
        "id": "rl_abc123",
        "effect": "deny",
        "source_file": "/Users/dev/.kolsch/platform.yaml",
        "source_line": 47,
        "what": "kolsch/tools/write_file",
        "pattern": "~/Desktop/*.txt",
    }
    hint = format_denial_hint(matched)
    lines = hint.split("\n")
    assert len(lines) == 2
    assert "denied by rl_abc123" in lines[0]
    assert "(deny)" in lines[0]
    assert "platform.yaml:47" in lines[0]  # tildified path preserved
    assert "pattern ~/Desktop/*.txt" in lines[1]
    assert "/reload to reapply" in lines[1]


def test_tool_call_block_hint_osc8_escape_present() -> None:
    """OSC-8 escape wraps the `<source>:<line>` reference when the
    matched rule carries source metadata."""
    matched = {
        "id": "rl_xyz",
        "effect": "deny",
        "source_file": "/etc/kolsch/rules.yaml",
        "source_line": 12,
        "pattern": "*",
    }
    hint = format_denial_hint(matched)
    assert "\x1b]8;;file:///etc/kolsch/rules.yaml\x07" in hint
    assert "\x1b]8;;\x07" in hint
    # Unknown source path degrades to the "(unknown source)" text.
    hint2 = format_denial_hint({"id": "rl_x", "effect": "deny", "pattern": "*"})
    assert "(unknown source)" in hint2
    assert "\x1b]8;;" not in hint2


# ---------------------------------------------------------------------------
# AC #6 — /reload atomic swap + malformed config preservation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_swaps_provider_atomically(tmp_path: Path) -> None:
    """A successful `/reload` swaps `runtime.authz` for a fresh provider
    reflecting the new YAML on disk."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        "authorization:\n"
        "  variant: rule_based\n"
        "  config:\n"
        "    rules:\n"
        "      - {what: '*', pattern: '*', effect: deny}\n",
        encoding="utf-8",
    )

    class _FakeAssetManagement:
        def catalog(self) -> Any:
            from assets.catalog import AssetCatalog

            return AssetCatalog(bundles={}, assets={})

    class _FakeToolsSubsystem:
        def __init__(self) -> None:
            self._authz: Any = None

    original = RuleBasedAuthorizationProvider(
        config={"rules": [{"what": "*", "pattern": "*", "effect": "allow"}]},
    )
    runtime = _FakeRuntime(
        store=_FakeStoreDirect([]),
        root=tmp_path,
        authz=original,
        asset_management=_FakeAssetManagement(),
        tools_subsystem=_FakeToolsSubsystem(),
    )

    result = await handle_reload(runtime)

    assert result.error is None
    assert result.summary is not None
    assert "reloaded" in result.summary
    assert runtime.authz is not original


@pytest.mark.asyncio
async def test_reload_malformed_config_preserves_old_provider(
    tmp_path: Path,
) -> None:
    """A malformed YAML leaves `runtime.authz` untouched and returns an
    error string."""
    yaml_path = tmp_path / "platform.yaml"
    yaml_path.write_text(
        "authorization:\n  config:\n    rules:\n      - {what: [invalid",
        encoding="utf-8",
    )

    class _FakeAssetManagement:
        def catalog(self) -> Any:
            from assets.catalog import AssetCatalog

            return AssetCatalog(bundles={}, assets={})

    original = RuleBasedAuthorizationProvider(
        config={"rules": [{"what": "*", "pattern": "*", "effect": "allow"}]},
    )
    runtime = _FakeRuntime(
        store=_FakeStoreDirect([]),
        root=tmp_path,
        authz=original,
        asset_management=_FakeAssetManagement(),
    )

    result = await handle_reload(runtime)

    assert result.summary is None
    assert result.error is not None
    assert "Old rules still active" in result.error
    assert runtime.authz is original


# ---------------------------------------------------------------------------
# AC #7 — /why event correlation.
# ---------------------------------------------------------------------------


def _make_tool_failed_event(
    *,
    tool_call_id: str,
    tool_name: str,
    matched_rule: dict[str, Any] | None,
    action: str = "",
) -> _FakeEvent:
    error_detail: dict[str, Any] = {"action": action}
    if matched_rule is not None:
        error_detail["matched_rule"] = matched_rule
    return _FakeEvent(
        type="tool.failed",
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "error": {
                "type": "tool.unauthorized",
                "message": "denied",
                "detail": error_detail,
            },
        },
    )


def _make_policy_event(
    *,
    tool_call_id: str | None,
    action: str,
    rule_id: str = "rl_x",
    effect: str = "deny",
    source_file: str | None = None,
    source_line: int | None = None,
) -> _FakeEvent:
    return _FakeEvent(
        type="policy.decision.made",
        data={
            "tool_call_id": tool_call_id,
            "action": action,
            "effect": effect,
            "rule_id": rule_id,
            "source_file": source_file,
            "source_line": source_line,
        },
    )


@pytest.mark.asyncio
async def test_why_finds_last_denial_via_store(tmp_path: Path) -> None:
    """`find_last_denial_via_store` picks the most recent `tool.failed`
    with a matched_rule and correlates to the paired
    `policy.decision.made`."""
    matched_rule = {
        "id": "rl_bash",
        "effect": "deny",
        "what": "kolsch/tools/bash",
        "pattern": "*",
        "source_file": "/tmp/platform.yaml",
        "source_line": 5,
    }
    events: list[_FakeEvent] = [
        _make_policy_event(
            tool_call_id="tc_1",
            action="kolsch/tools/bash",
            source_file="/tmp/platform.yaml",
            source_line=5,
        ),
        _make_tool_failed_event(
            tool_call_id="tc_1",
            tool_name="bash",
            matched_rule=matched_rule,
            action="kolsch/tools/bash",
        ),
    ]
    runtime = _FakeRuntime(store=_FakeStoreDirect(events), root=tmp_path)
    data = await find_last_denial_via_store(runtime, thread_id="th_x")
    assert data is not None
    assert data.rule_id == "rl_bash"
    assert data.tool_name == "bash"
    assert data.source_file == "/tmp/platform.yaml"
    assert data.source_line == 5


@pytest.mark.asyncio
async def test_why_specific_tool_call_id(tmp_path: Path) -> None:
    """`find_last_denial_via_store(tool_call_id=...)` targets that
    exact call, skipping newer denials."""
    events: list[_FakeEvent] = [
        _make_tool_failed_event(
            tool_call_id="tc_old",
            tool_name="write_file",
            matched_rule={
                "id": "rl_a",
                "effect": "deny",
                "what": "kolsch/tools/write_file",
                "pattern": "*",
            },
        ),
        _make_tool_failed_event(
            tool_call_id="tc_new",
            tool_name="bash",
            matched_rule={
                "id": "rl_b",
                "effect": "deny",
                "what": "kolsch/tools/bash",
                "pattern": "*",
            },
        ),
    ]
    runtime = _FakeRuntime(store=_FakeStoreDirect(events), root=tmp_path)
    data = await find_last_denial_via_store(
        runtime, thread_id="th_x", tool_call_id="tc_old",
    )
    assert data is not None
    assert data.rule_id == "rl_a"


@pytest.mark.asyncio
async def test_why_no_denial_prints_hint(tmp_path: Path) -> None:
    """When no `tool.failed` event exists (or none carry a matched_rule),
    the finder returns `None` so the caller can print the "no recent
    denial" hint."""
    runtime = _FakeRuntime(store=_FakeStoreDirect([]), root=tmp_path)
    data = await find_last_denial_via_store(runtime, thread_id="th_x")
    assert data is None


@pytest.mark.asyncio
async def test_why_orphan_tool_failed_prints_diagnostic(tmp_path: Path) -> None:
    """A `tool.failed` with matched_rule but no correlated
    `policy.decision.made` still assembles card data — the source
    metadata comes from the matched_rule directly."""
    matched_rule = {
        "id": "rl_x",
        "effect": "deny",
        "what": "kolsch/tools/read_file",
        "pattern": "*",
    }
    events = [
        _make_tool_failed_event(
            tool_call_id="tc_x",
            tool_name="read_file",
            matched_rule=matched_rule,
        ),
    ]
    runtime = _FakeRuntime(store=_FakeStoreDirect(events), root=tmp_path)
    data = await find_last_denial_via_store(runtime, thread_id="th_x")
    assert data is not None
    assert data.rule_id == "rl_x"
    # No source metadata — card degrades to `(unknown)`.
    assert data.source_file is None
    assert data.source_line is None


@pytest.mark.asyncio
async def test_why_missing_source_shows_unknown(tmp_path: Path) -> None:
    """A rule without source_file renders with `source: (unknown)` in
    the card."""
    matched_rule = {
        "id": "rl_x",
        "effect": "deny",
        "what": "kolsch/tools/read_file",
        "pattern": "*",
    }
    events = [
        _make_tool_failed_event(
            tool_call_id="tc_x",
            tool_name="read_file",
            matched_rule=matched_rule,
        ),
    ]
    runtime = _FakeRuntime(store=_FakeStoreDirect(events), root=tmp_path)
    data = await find_last_denial_via_store(runtime, thread_id="th_x")
    assert data is not None
    card_text = render_denial_card(data)
    assert "(unknown)" in card_text  # source line renders `(unknown)`


# ---------------------------------------------------------------------------
# AC #8 — `render_denial_card` renderer + edge cases.
# ---------------------------------------------------------------------------


_GOLDEN_CARD_INPUT = DenialCardData(
    tool_name="write_file",
    target="/Users/dev/Desktop/notes.txt",
    time_hhmmss="13:42:07",
    rule_id="rl_abc123",
    effect="deny",
    layer="platform",
    source_file="/Users/dev/.kolsch/platform.yaml",
    source_line=47,
    what="kolsch/tools/write_file",
    who="*",
    pattern="~/Desktop/*.txt",
    reason=(
        "matched deny rule 'rl_abc123' in layer 'platform' — "
        "write_file target ~/Desktop/notes.txt matched pattern ~/Desktop/*.txt"
    ),
)


def test_render_denial_card_matches_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rendered card carries every field from the input and uses
    rounded box-drawing chars per the spec's Mockup 2."""
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/dev"))
    text = render_denial_card(_GOLDEN_CARD_INPUT)
    # Spot-check every field lands + no Textual markup gets embedded.
    assert "╭─ denied" in text
    assert "╰" in text
    assert "write_file" in text
    assert "rl_abc123  (deny)" in text
    assert "platform" in text
    # Path tildified.
    assert "~/.kolsch/platform.yaml:47" in text
    assert "kolsch/tools/write_file" in text
    assert "~/Desktop/*.txt" in text
    assert "matched deny rule" in text
    # Card footer names the file for editing.
    assert "/reload" in text


def test_render_denial_card_paths_outside_home(tmp_path: Path) -> None:
    """An absolute path outside `$HOME` renders as-is (no tildification)."""
    _ = tmp_path
    data = DenialCardData(
        tool_name="bash",
        target=None,
        time_hhmmss="00:00:00",
        rule_id="rl_x",
        effect="deny",
        layer="platform",
        source_file="/etc/kolsch/rules.yaml",
        source_line=12,
        what="kolsch/tools/bash",
        who="*",
        pattern="*",
        reason="matched deny rule 'rl_x' in layer 'platform'",
    )
    text = render_denial_card(data)
    assert "/etc/kolsch/rules.yaml:12" in text
    # No tildify performed on absolute paths outside home.
    assert "~/etc" not in text


def test_render_denial_card_long_reason_wraps() -> None:
    """Reason strings longer than the card's inner width word-wrap
    with a 12-char hanging indent; nothing gets truncated."""
    long_reason = (
        "this is an extremely long reason string that will definitely need "
        "to wrap across many lines because the card's inner width is "
        "sixty characters and this text is far more than sixty"
    )
    data = DenialCardData(
        tool_name="bash",
        target=None,
        time_hhmmss="00:00:00",
        rule_id="rl_x",
        effect="deny",
        layer="platform",
        source_file=None,
        source_line=None,
        what="kolsch/tools/bash",
        who="*",
        pattern="*",
        reason=long_reason,
    )
    text = render_denial_card(data)
    # Every word of the original reason survives.
    for word in long_reason.split():
        assert word in text
    # No line exceeds the card's visible width (60 + 2 borders + 2
    # spaces on each side = 66 visible chars in the body row).
    for line in text.split("\n"):
        # Escape OSC-8 sequences from length counting.
        clean = line
        while "\x1b]8;;" in clean:
            start = clean.find("\x1b]8;;")
            end = clean.find("\x07", start)
            if end == -1:
                break
            clean = clean[:start] + clean[end + 1 :]
        # Also strip the terminator.
        clean = clean.replace("\x1b]8;;\x07", "")
        # Body lines all start with │ and end with │.
        if clean.startswith("│") and clean.endswith("│"):
            assert len(clean) <= 66, (line, len(clean))


def test_render_denial_card_pattern_with_brackets() -> None:
    """A pattern containing `[abc]` renders as literal text (Static
    is `markup=False`)."""
    data = DenialCardData(
        tool_name="bash",
        target=None,
        time_hhmmss="00:00:00",
        rule_id="rl_x",
        effect="deny",
        layer="platform",
        source_file=None,
        source_line=None,
        what="kolsch/tools/bash",
        who="*",
        pattern="ls [abc]",
        reason="pattern with brackets",
    )
    text = render_denial_card(data)
    assert "ls [abc]" in text  # literal — no Textual markup interpretation


# ---------------------------------------------------------------------------
# Additional small unit coverage (internals).
# ---------------------------------------------------------------------------


def test_tildify_home_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_tildify` collapses `$HOME/rest` to `~/rest` and passes other
    paths through unchanged."""
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/dev"))
    assert _tildify("/Users/dev/x.txt") == "~/x.txt"
    assert _tildify("/etc/kolsch/rules.yaml") == "/etc/kolsch/rules.yaml"
    assert _tildify(None) == "(unknown)"


def test_extract_layer_from_reason() -> None:
    """The layer name lifted off `AuthorizationResult.reason`."""
    reason = "matched deny rule 'rl_x' in layer 'bundle' — bash target"
    assert _extract_layer_from_reason(reason) == "bundle"
    assert _extract_layer_from_reason("some other message") is None


def test_find_target_tool_failed_skips_non_authz() -> None:
    """The finder skips `tool.failed` events with no matched_rule
    (crashes / timeouts / non-authz failures)."""
    non_authz = _make_tool_failed_event(
        tool_call_id="tc_crash",
        tool_name="bash",
        matched_rule=None,
    )
    authz = _make_tool_failed_event(
        tool_call_id="tc_deny",
        tool_name="bash",
        matched_rule={"id": "rl_x", "effect": "deny", "pattern": "*"},
    )
    result = _find_target_tool_failed(
        [non_authz, authz],  # type: ignore[list-item]  # _FakeEvent duck-types Event
        tool_call_id=None,
    )
    assert result is authz  # type: ignore[comparison-overlap]


def test_find_correlated_policy_prefers_tool_call_id_match() -> None:
    """`_find_correlated_policy` prefers a `tool_call_id` match over the
    action fallback."""
    tf = _make_tool_failed_event(
        tool_call_id="tc_x",
        tool_name="bash",
        matched_rule={"id": "rl_x", "effect": "deny", "pattern": "*"},
        action="kolsch/tools/bash",
    )
    old_action_match = _make_policy_event(
        tool_call_id=None,
        action="kolsch/tools/bash",
        rule_id="rl_action_match",
    )
    tc_match = _make_policy_event(
        tool_call_id="tc_x",
        action="kolsch/tools/bash",
        rule_id="rl_tc_match",
    )
    events: list[Any] = [old_action_match, tc_match, tf]
    result = _find_correlated_policy(
        events,
        tool_call_id="tc_x",
        action="kolsch/tools/bash",
        before_event=tf,
    )
    assert result is tc_match  # type: ignore[comparison-overlap]


def test_reload_result_shape() -> None:
    """`ReloadResult` exposes exactly the summary / error dial."""
    ok = ReloadResult(summary="ok")
    assert ok.error is None
    err = ReloadResult(error="broken")
    assert err.summary is None
