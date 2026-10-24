"""bug-146 Tests 4 and 5 — `--sync-bundle`'s default-rule pass and doctor's rule section.

A workspace set up before a starter rule shipped never gets it: these
tests remove `kolsch/tools/jev_decide` (+ the ledger) from a fresh
install to model that, then check the upgrade adds it once, with
consent, before the default-deny tail (or at the end), never as
`allow`, never touching any other rule — and records every decision in
`.kolsch/default-rules.json`. Also covers the bug-146 locking of the
other `platform.yaml` rule writers: a chat "always" that lands while
setup / sync is mid-way survives.
"""

from __future__ import annotations

import argparse
import io
import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import yaml

from cli._doctor import cmd_doctor
from cli._setup import (
    NonInteractivePrompter,
    _SetupIO,
    _TestRunResult,
    run_setup,
)
from cli.permissions import RemoveRule, UpsertRule, mutate_platform_rules
from providers.authz import Rule

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cli._setup import SetupPrompter

_JEV = {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"}
_TAIL = {"what": "*", "pattern": "*", "effect": "deny"}
_CHAT_ALWAYS = Rule(what="kolsch/tools/bash", pattern="git status", effect="allow")


async def _noop_run_handler(args: argparse.Namespace) -> _TestRunResult:
    _ = args
    return _TestRunResult(rc=0)


def _connect_io_factory(
    _root: object, _provider: str, _api_key: str | None = None
) -> object:
    raise NotImplementedError("no provider auto-connect in this test")


def _io(root: Path, prompter: SetupPrompter | None = None) -> _SetupIO:
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=prompter if prompter is not None else NonInteractivePrompter(),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )


def _sync_args(
    root: Path, *, dry_run: bool = False, yes: bool = False, no: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        non_interactive=False,
        sync_bundle=True,
        dry_run=dry_run,
        yes=yes,
        no=no,
    )


async def _install(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _io(root)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=root), io_)
    assert rc == 0, io_.out.getvalue()


async def _legacy_workspace(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, tail: bool = True
) -> Path:
    """A fresh install minus the `jev_decide` rule and the ledger.

    Adds an unknown `llm_providers.opencode.request_policy` key and a
    comment above the rules, so the tests can check both survive.
    """
    await _install(root, monkeypatch)
    yaml_path = root / "platform.yaml"
    mutate_platform_rules(yaml_path, RemoveRule(match=Rule(**_JEV)), catalog=None)
    if not tail:
        text = yaml_path.read_text(encoding="utf-8")
        tail_text = "    - what: '*'\n      pattern: '*'\n      effect: deny\n"
        assert tail_text in text, text
        yaml_path.write_text(text.replace(tail_text, "", 1), encoding="utf-8")
    text = yaml_path.read_text(encoding="utf-8")
    text = text.replace("authorization:\n", "# my authz notes\nauthorization:\n", 1) + (
        "llm_providers:\n  opencode:\n    request_policy:\n      session_header: x\n"
    )
    yaml_path.write_text(text, encoding="utf-8")
    (root / ".kolsch" / "default-rules.json").unlink()
    return yaml_path


def _rules(yaml_path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    rules = data["authorization"]["config"]["rules"]
    assert isinstance(rules, list)
    return rules


def _ledger(root: Path) -> dict[str, str]:
    payload = json.loads(
        (root / ".kolsch" / "default-rules.json").read_text(encoding="utf-8")
    )
    return {key: value["decision"] for key, value in payload["rules"].items()}


def _assert_only_additions(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every old rule survives in order; return the added ones (never `allow`)."""
    added = Counter(map(repr, after)) - Counter(map(repr, before))
    assert Counter(map(repr, before)) - Counter(map(repr, after)) == Counter()
    assert [r for r in after if repr(r) not in added] == before
    added_rules = [r for r in after if repr(r) in added]
    assert all(r["effect"] in {"prompt", "deny"} for r in added_rules), added_rules
    return added_rules


class _Answers(NonInteractivePrompter):
    """Answer `choose` for the rules prompt; default everything else."""

    def __init__(self, rules_answer: str, confirms: list[bool] | None = None) -> None:
        self.rules_answer = rules_answer
        self.confirms = list(confirms or [])
        self.questions: list[str] = []

    def choose(self, question: str, options: list[str], default: str) -> str:
        self.questions.append(question)
        if "default permission rules" in question:
            return self.rules_answer
        return default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        self.questions.append(question)
        return self.confirms.pop(0) if self.confirms else default


# ---------------------------------------------------------------------------
# Test 4 — placement, consent, ledger, idempotence
# ---------------------------------------------------------------------------


async def test_rule_interactive_accept_inserts_directly_before_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    before = _rules(yaml_path)
    prompter = _Answers("a")

    io_ = _io(tmp_path, prompter)
    rc = await run_setup(_sync_args(tmp_path), io_)
    output = io_.out.getvalue()
    assert rc == 0, output

    after = _rules(yaml_path)
    assert _assert_only_additions(before, after) == [_JEV]
    assert after[-2:] == [_JEV, _TAIL]
    assert any(
        "sends workspace content to a third party" in q for q in prompter.questions
    )
    assert "✓ added rl_" in output
    assert "directly before the default-deny tail" in output
    # N3: the auto-approve caveat is spelled out.
    assert "is an allow under `kolsch chat --auto-approve`" in output
    assert list((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    text = yaml_path.read_text(encoding="utf-8")
    assert "# my authz notes" in text
    assert "session_header: x" in text
    assert "# hooks:" in text  # trailing comment block
    assert _ledger(tmp_path)["jev_decide"] == "added"

    # Re-run: nothing proposed, no duplicate.
    io2 = _io(tmp_path, _Answers("a"))
    assert await run_setup(_sync_args(tmp_path), io2) == 0
    assert "new default rule" not in io2.out.getvalue()
    assert _rules(yaml_path) == after


async def test_rule_tail_less_workspace_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch, tail=False)
    before = _rules(yaml_path)

    io_ = _io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert rc == 0, output
    after = _rules(yaml_path)
    assert _assert_only_additions(before, after) == [_JEV]
    assert after[-1] == _JEV
    assert _TAIL not in after  # never adds the tail (D4)
    assert "no default-deny tail" in output


async def test_rule_interactive_skip_is_declined_and_never_reoffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    before_text = yaml_path.read_text(encoding="utf-8")

    io_ = _io(tmp_path, _Answers("s"))
    assert await run_setup(_sync_args(tmp_path), io_) == 0
    assert "recorded as declined" in io_.out.getvalue()
    assert yaml_path.read_text(encoding="utf-8") == before_text
    assert _ledger(tmp_path)["jev_decide"] == "declined"

    for args in (_sync_args(tmp_path, yes=True), _sync_args(tmp_path)):
        io2 = _io(tmp_path, _Answers("a"))
        assert await run_setup(args, io2) == 0
        assert "jev_decide" not in io2.out.getvalue()
    assert yaml_path.read_text(encoding="utf-8") == before_text


async def test_rule_interactive_choose_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    mutate_platform_rules(
        yaml_path,
        RemoveRule(
            match=Rule(what="kolsch/tools/run_workflow", pattern="*", effect="prompt")
        ),
        catalog=None,
    )
    before = _rules(yaml_path)
    prompter = _Answers("c", confirms=[False, True])  # jev no, run_workflow yes

    io_ = _io(tmp_path, prompter)
    assert await run_setup(_sync_args(tmp_path), io_) == 0
    # N7: the per-rule question says what "no" means.
    assert any(
        "kolsch/tools/jev_decide" in q and "(no = don't offer again)" in q
        for q in prompter.questions
    )
    added = _assert_only_additions(before, _rules(yaml_path))
    assert [r["what"] for r in added] == ["kolsch/tools/run_workflow"]
    ledger = _ledger(tmp_path)
    assert ledger["jev_decide"] == "declined"
    assert ledger["run_workflow"] == "added"


async def test_rule_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _legacy_workspace(tmp_path, monkeypatch)
    # Also no baseline + a missing tool file, so the preview counts the
    # tool it would copy in.
    (tmp_path / ".kolsch" / "bundle-baseline.json").unlink()
    (tmp_path / "bundles" / "kolsch" / "tools" / "jev_decide.py").unlink()
    watched = [tmp_path / "platform.yaml", tmp_path / ".kolsch"]

    def snapshot() -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for path in watched:
            files = [path] if path.is_file() else sorted(path.rglob("*"))
            for f in files:
                if f.is_file():
                    out[str(f)] = f.read_bytes()
        return out

    before = snapshot()
    io_ = _io(tmp_path)
    await run_setup(_sync_args(tmp_path, dry_run=True, yes=True), io_)
    output = io_.out.getvalue()
    assert "would add" in output
    assert "kolsch/tools/jev_decide → prompt" in output
    assert snapshot() == before
    assert not (tmp_path / ".platform").exists() or not list(
        (tmp_path / ".platform").glob("platform.yaml.bak.*")
    )


async def test_rule_no_writes_no_rule_and_offers_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    before_text = yaml_path.read_text(encoding="utf-8")

    io_ = _io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, no=True), io_)
    output = io_.out.getvalue()
    assert "not added (--no)" in output
    assert yaml_path.read_text(encoding="utf-8") == before_text
    # N1: `--no` changes nothing, the ledger included (not even `present`).
    assert not (tmp_path / ".kolsch" / "default-rules.json").exists()
    # N2: an offered rule left out is unresolved, like a kept file.
    assert rc == 1, output
    assert "1 item(s) left unresolved — exit 1 (partial)." in output
    assert "without --yes/--no" in output

    io2 = _io(tmp_path)
    await run_setup(_sync_args(tmp_path, dry_run=True), io2)
    assert "kolsch/tools/jev_decide → prompt" in io2.out.getvalue()


async def test_rule_yes_adds_and_never_writes_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: `--yes` adds prompt/deny defaults; D3: a missing allow is report-only."""
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    mutate_platform_rules(
        yaml_path,
        RemoveRule(
            match=Rule(what="kolsch/tools/workflow_status", pattern="*", effect="allow")
        ),
        catalog=None,
    )
    before = _rules(yaml_path)

    io_ = _io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert rc == 0, output
    assert _assert_only_additions(before, _rules(yaml_path)) == [_JEV]
    assert "missing default `allow`" in output
    assert (
        "kolsch permissions add --what kolsch/tools/workflow_status "
        "--pattern '*' --effect allow" in output
    )
    ledger = _ledger(tmp_path)
    assert ledger["jev_decide"] == "added"
    assert "workflow_status" not in ledger  # reported, not decided
    assert ledger["bash"] == "present"


async def test_fresh_install_ledger_has_every_key_and_sync_proposes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assets.default_rules import STARTER_RULES

    await _install(tmp_path, monkeypatch)
    assert _ledger(tmp_path) == {r.key: "installed" for r in STARTER_RULES}
    before = (tmp_path / "platform.yaml").read_bytes()

    io_ = _io(tmp_path)
    assert await run_setup(_sync_args(tmp_path, yes=True), io_) == 0
    output = io_.out.getvalue()
    assert "Default permission rules" not in output
    assert "no drift" in output
    assert (tmp_path / "platform.yaml").read_bytes() == before


async def test_rule_for_uninstalled_tool_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule naming an uncatalogued tool would break authz loading."""
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    (tmp_path / "bundles" / "kolsch" / "tools" / "jev_decide.py").unlink()
    before_text = yaml_path.read_text(encoding="utf-8")

    io_ = _io(tmp_path)
    await run_setup(_sync_args(tmp_path, no=True), io_)  # --no: don't copy it in
    assert "not offered — the tool isn't installed" in io_.out.getvalue()
    assert yaml_path.read_text(encoding="utf-8") == before_text


async def test_corrupt_ledger_reads_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    (tmp_path / ".kolsch" / "default-rules.json").write_text("{nope", encoding="utf-8")

    io_ = _io(tmp_path)
    assert await run_setup(_sync_args(tmp_path, yes=True), io_) == 0
    assert _rules(yaml_path)[-2:] == [_JEV, _TAIL]
    assert _ledger(tmp_path)["jev_decide"] == "added"


async def test_migrator_does_not_rewrite_a_file_it_just_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA (b): a retired agent YAML with a retired ref stays deleted."""
    await _install(tmp_path, monkeypatch)
    old_agent = tmp_path / "bundles" / "kolsch" / "agents" / "old_helper.yaml"
    old_agent.write_text(
        "agent_id: ag_old\nname: old_helper\nsubsystems:\n  tools:\n"
        "    config:\n      declared:\n        - ref: kolsch/tools/fetch_website\n",
        encoding="utf-8",
    )
    baseline = tmp_path / ".kolsch" / "bundle-baseline.json"
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    from cli._bundle_baseline import sha256_file

    payload["files"]["agents/old_helper.yaml"] = sha256_file(old_agent)
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    io_ = _io(tmp_path)
    await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert "deleted: agents/old_helper.yaml" in output
    assert "rewrote: bundles/kolsch/agents/old_helper.yaml" not in output
    assert not old_agent.exists()


async def test_sync_copies_leave_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _install(tmp_path, monkeypatch)
    bundle = tmp_path / "bundles" / "kolsch"
    (bundle / "tools" / "jev_decide.py").unlink()
    await run_setup(_sync_args(tmp_path, yes=True), _io(tmp_path))
    assert (bundle / "tools" / "jev_decide.py").is_file()
    assert not list(bundle.rglob("*.sync-tmp"))


# ---------------------------------------------------------------------------
# bug-146 — every rules writer under `platform_yaml_lock`; a chat
# "always" that lands mid-way survives.
# ---------------------------------------------------------------------------


def _chat_always(yaml_path: Path) -> None:
    """What a chat "always" answer persists (bug-162's writer)."""
    mutate_platform_rules(yaml_path, UpsertRule(_CHAT_ALWAYS), catalog=None)


def _has_chat_always(yaml_path: Path) -> bool:
    return any(
        r.get("what") == _CHAT_ALWAYS.what and r.get("pattern") == _CHAT_ALWAYS.pattern
        for r in _rules(yaml_path)
    )


async def test_chat_always_during_setup_schema_migration_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The qol-schema migration re-reads under the lock after its catalog build."""
    await _install(tmp_path, monkeypatch)
    yaml_path = tmp_path / "platform.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    data["authorization"]["config"]["rules"].insert(
        0,
        {
            "id": "rl_legacy",
            "who": ".*",
            "what": r"^tool\.read_file$",
            "effect": "allow",
        },
    )
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    import assets

    real_initialize = assets.AssetManagement.initialize
    fired: list[bool] = []

    async def initialize_then_chat(self: Any) -> None:
        await real_initialize(self)
        if not fired:  # the migration's own catalog build — between read and write
            fired.append(True)
            _chat_always(yaml_path)

    monkeypatch.setattr(assets.AssetManagement, "initialize", initialize_then_chat)
    io_ = _io(tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert fired
    assert "migrated 1 authz rule(s)" in io_.out.getvalue()
    assert _has_chat_always(yaml_path)
    assert not any(r.get("who") for r in _rules(yaml_path))


async def test_chat_always_during_sync_platform_rewrite_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migrator's platform.yaml rewrite re-plans under the lock."""
    await _install(tmp_path, monkeypatch)
    yaml_path = tmp_path / "platform.yaml"
    mutate_platform_rules(
        yaml_path,
        UpsertRule(Rule(what="kolsch/tools/fetch_url", pattern="*", effect="allow")),
        catalog=None,
    )
    text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        text.replace(
            "what: kolsch/tools/fetch_url", "what: kolsch/tools/fetch_website"
        ),
        encoding="utf-8",
    )
    import cli._bundle_sync as sync_mod

    real_migrate = sync_mod.migrate_tool_refs

    def migrate_then_chat(root: Path) -> Any:
        result = real_migrate(root)
        _chat_always(yaml_path)  # lands after the plan read, before the write
        return result

    monkeypatch.setattr(sync_mod, "migrate_tool_refs", migrate_then_chat)
    io_ = _io(tmp_path)
    await run_setup(_sync_args(tmp_path, yes=True), io_)
    assert "rewrote: platform.yaml" in io_.out.getvalue()
    assert "kolsch/tools/fetch_website" not in yaml_path.read_text(encoding="utf-8")
    assert _has_chat_always(yaml_path)


async def test_rules_writes_hold_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every platform.yaml write in setup's re-run happens with the lock held."""
    import fcntl

    import cli.permissions as permissions_mod
    from assets.platform_config import platform_yaml_lock_path

    await _install(tmp_path, monkeypatch)
    yaml_path = tmp_path / "platform.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    rules = data["authorization"]["config"]["rules"]
    data["authorization"]["config"]["rules"] = [
        {
            "id": "rl_legacy",
            "who": ".*",
            "what": r"^tool\.read_file$",
            "effect": "allow",
        },
        *[r for r in rules if r["what"] != "agent.execute.*"],
    ]
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    real_write = permissions_mod._atomic_write_text
    probes: list[bool] = []

    def probing_write(path: Path, text: str) -> None:
        with platform_yaml_lock_path(tmp_path).open("ab") as fd:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                probes.append(True)
            else:
                probes.append(False)
        real_write(path, text)

    monkeypatch.setattr(permissions_mod, "_atomic_write_text", probing_write)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path), _io(tmp_path)
    )
    # schema migration + agent.execute insert
    assert probes == [True, True], probes


# ---------------------------------------------------------------------------
# Amendment 2b — the agent.execute.* allow is named, and added once
# ---------------------------------------------------------------------------


async def test_agent_execute_allow_is_named_and_added_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _install(tmp_path, monkeypatch)
    yaml_path = tmp_path / "platform.yaml"
    mutate_platform_rules(
        yaml_path,
        RemoveRule(match=Rule(what="agent.execute.*", pattern="*", effect="allow")),
        catalog=None,
    )
    io1 = _io(tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io1)
    out1 = io1.out.getvalue()
    assert "✓ added rule rl_" in out1
    assert "(`agent.execute.*`, pattern `*` → allow) as rule 1" in out1
    rules = _rules(yaml_path)
    assert rules[0] == {"what": "agent.execute.*", "pattern": "*", "effect": "allow"}

    io2 = _io(tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io2)
    assert "agent.execute" not in io2.out.getvalue()
    assert _rules(yaml_path) == rules


# ---------------------------------------------------------------------------
# Test 5 — doctor's "Missing default permission rules" section
# ---------------------------------------------------------------------------


async def test_doctor_rule_section_lists_missing_rule_as_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _legacy_workspace(tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1, text
    assert "Missing default permission rules" in text
    assert "+ kolsch/tools/jev_decide → prompt" in text
    assert after_equal(tmp_path, before)


def after_equal(root: Path, before: dict[str, bytes]) -> bool:
    return before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def test_doctor_rule_report_only_items_do_not_change_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing allow default and a missing tail are informational."""
    yaml_path = await _legacy_workspace(tmp_path, monkeypatch, tail=False)
    mutate_platform_rules(
        yaml_path,
        RemoveRule(
            match=Rule(what="kolsch/tools/workflow_status", pattern="*", effect="allow")
        ),
        catalog=None,
    )
    from cli._default_rules_ledger import record_decisions

    record_decisions(tmp_path, {"jev_decide": "declined"})
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert "kolsch/tools/workflow_status → allow" in text
    assert "informational" in text
    # The missing tail is bug-152's Authorization finding — not repeated
    # in the rules section.
    section = text.split("Missing default permission rules", 1)[1].split("\n\n", 1)[0]
    assert "tail" not in section
    assert "has no default-deny tail" in text


async def test_doctor_fresh_workspace_has_no_rule_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _install(tmp_path, monkeypatch)
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0, out.getvalue()
    assert "Missing default permission rules" not in out.getvalue()


async def test_doctor_counts_a_tool_sync_would_copy_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner shape: tool file AND rule missing → doctor proposes the rule, like sync."""
    await _legacy_workspace(tmp_path, monkeypatch)
    (tmp_path / "bundles" / "kolsch" / "tools" / "jev_decide.py").unlink()
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1, text
    assert "+ kolsch/tools/jev_decide → prompt" in text
    assert "not installed" not in text


async def test_sync_interactive_without_terminal_refuses_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4: no `--yes`/`--no` and stdin isn't a TTY → a clear error, no EOFError."""
    import sys

    from cli._setup import StdinSetupPrompter

    yaml_path = await _legacy_workspace(tmp_path, monkeypatch)
    before = yaml_path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    out = io.StringIO()
    io_ = _SetupIO(
        root=tmp_path,
        out=out,
        err=io.StringIO(),
        prompter=StdinSetupPrompter(out),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )
    for args in (_sync_args(tmp_path), _sync_args(tmp_path, dry_run=True)):
        assert await run_setup(args, io_) == 2
    text = out.getvalue()
    assert "error: `kolsch setup --sync-bundle` asks before each change" in text
    assert "--yes" in text and "--no" in text
    assert yaml_path.read_bytes() == before
