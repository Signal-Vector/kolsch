"""bug-146 Phase 3 — remembered `[k]eep`, and the re-run notice / doctor / dry-run agree.

- An interactive `[k]eep` is recorded in `.kolsch/bundle-kept.json`
  against the file's and the source's sha256: not asked again, not
  counted by the `kolsch setup` notice or as doctor drift (doctor lists
  it as "Kept local", informational) — until the file or the shipped
  version changes. `--yes` / `--no` never record it.
- The re-run notice counts everything sync would act on, including
  retired-tool-ref rewrites (the ses_0e54 class) and workspace-only
  files sync would ask about; the same numbers come out of
  `--sync-bundle --dry-run` and `kolsch doctor`.
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
from typing import TYPE_CHECKING

import cli._setup as setup_mod
from cli._bundle_sync import plan_workspace_upgrade
from cli._doctor import cmd_doctor
from cli._setup import NonInteractivePrompter, _SetupIO, _TestRunResult, run_setup

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cli._setup import SetupPrompter


async def _noop_run_handler(args: argparse.Namespace) -> _TestRunResult:
    _ = args
    return _TestRunResult(rc=0)


def _connect_io_factory(
    _root: object, _provider: str, _api_key: str | None = None
) -> object:
    raise NotImplementedError


def _io(root: Path, prompter: SetupPrompter | None = None) -> _SetupIO:
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=prompter if prompter is not None else NonInteractivePrompter(),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )


def _sync(
    root: Path, *, dry_run: bool = False, yes: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        non_interactive=False,
        sync_bundle=True,
        dry_run=dry_run,
        yes=yes,
        no=False,
    )


class _Recorder(NonInteractivePrompter):
    """Default every answer (keep / keep-missing) and record the questions."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    def choose(self, question: str, options: list[str], default: str) -> str:
        self.questions.append(question)
        return default


async def _install(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install from a private copy of the template (so a test can change it)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    template = root.parent / f"{root.name}-template" / "kolsch"
    shutil.copytree(setup_mod._STARTER_BUNDLE_TEMPLATE_DIR, template)
    monkeypatch.setattr(setup_mod, "_STARTER_BUNDLE_TEMPLATE_DIR", template)
    root.mkdir(exist_ok=True)
    io_ = _io(root)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=root), io_) == 0
    )
    return template


def _edit(path: Path, line: str = "# local edit\n") -> None:
    path.write_text(path.read_text(encoding="utf-8") + line, encoding="utf-8")


async def _doctor(root: Path) -> tuple[int, str]:
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=root)
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# Remembered [k]eep
# ---------------------------------------------------------------------------


async def test_kept_file_is_not_asked_again_or_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    bash = root / "bundles" / "kolsch" / "tools" / "bash.py"
    _edit(bash)

    first = _Recorder()
    assert await run_setup(_sync(root), _io(root, first)) == 1  # kept this run
    assert any("tools/bash.py" in q for q in first.questions)
    assert (root / ".kolsch" / "bundle-kept.json").is_file()

    second = _Recorder()
    io2 = _io(root, second)
    assert await run_setup(_sync(root), io2) == 0, io2.out.getvalue()
    assert not any("tools/bash.py" in q for q in second.questions)
    assert "Kept local (1)" in io2.out.getvalue()
    assert bash.read_text(encoding="utf-8").endswith("# local edit\n")

    assert not (await plan_workspace_upgrade(root)).actionable
    rc, text = await _doctor(root)
    assert rc == 0, text
    assert "Kept local: 1" in text
    assert "· tools/bash.py" in text


async def test_kept_file_is_asked_again_when_the_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    template = await _install(root, monkeypatch)
    _edit(root / "bundles" / "kolsch" / "tools" / "bash.py")
    await run_setup(_sync(root), _io(root, _Recorder()))

    _edit(template / "tools" / "bash.py", "# new shipped version\n")
    assert (await plan_workspace_upgrade(root)).modified == 1
    again = _Recorder()
    assert await run_setup(_sync(root), _io(root, again)) == 1
    assert any("tools/bash.py" in q for q in again.questions)


async def test_kept_file_is_asked_again_when_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    bash = root / "bundles" / "kolsch" / "tools" / "bash.py"
    _edit(bash)
    await run_setup(_sync(root), _io(root, _Recorder()))

    _edit(bash, "# another edit\n")
    rc, text = await _doctor(root)
    assert rc == 1, text
    assert "Kept local" not in text
    again = _Recorder()
    assert await run_setup(_sync(root), _io(root, again)) == 1
    assert any("tools/bash.py" in q for q in again.questions)


async def test_kept_retired_user_file_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    mine = root / "bundles" / "kolsch" / "tools" / "my_tool.py"
    mine.write_text("# mine\n", encoding="utf-8")

    assert (await plan_workspace_upgrade(root)).modified == 1  # asked about
    await run_setup(_sync(root), _io(root, _Recorder()))
    second = _Recorder()
    assert await run_setup(_sync(root), _io(root, second)) == 0
    assert not any("my_tool.py" in q for q in second.questions)
    assert mine.is_file()
    assert not (await plan_workspace_upgrade(root)).actionable


async def test_yes_never_records_a_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    _edit(root / "bundles" / "kolsch" / "tools" / "bash.py")
    for _ in range(2):
        assert await run_setup(_sync(root, yes=True), _io(root)) == 1
    assert not (root / ".kolsch" / "bundle-kept.json").exists()


async def test_overwrite_forgets_an_earlier_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    template = await _install(root, monkeypatch)
    bash = root / "bundles" / "kolsch" / "tools" / "bash.py"
    _edit(bash)
    await run_setup(_sync(root), _io(root, _Recorder()))
    _edit(template / "tools" / "bash.py", "# new shipped version\n")

    class _Overwrite(NonInteractivePrompter):
        def choose(self, question: str, options: list[str], default: str) -> str:
            return "o" if "tools/bash.py" in question else default

    assert await run_setup(_sync(root), _io(root, _Overwrite())) == 0
    assert bash.read_bytes() == (template / "tools" / "bash.py").read_bytes()
    from cli._bundle_kept import read_kept

    assert "tools/bash.py" not in read_kept(root)


# ---------------------------------------------------------------------------
# P3-B1 / P3-D2 — the notice counts everything; the three agree
# ---------------------------------------------------------------------------


async def test_notice_reports_a_retired_tool_ref_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ses_0e54 class: only a retired ref in platform.yaml is pending."""
    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace(
            "what: kolsch/tools/fetch_url", "what: kolsch/tools/fetch_website", 1
        ),
        encoding="utf-8",
    )
    plan = await plan_workspace_upgrade(root)
    assert plan.rewrites == 1
    io_ = _io(root)
    await run_setup(argparse.Namespace(non_interactive=True, root=root), io_)
    assert "rewrite retired tool refs in 1 file(s)" in io_.out.getvalue()


def _dry_run_counts(report: str) -> dict[str, int]:
    actions = re.findall(r"^  [✓·] ([a-z-]+(?: \([^)]*\))?):", report, flags=re.M)
    return {a: actions.count(a) for a in set(actions)}


async def test_notice_doctor_and_dry_run_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.permissions import RemoveRule, mutate_platform_rules
    from providers.authz import Rule

    root = tmp_path / "ws"
    await _install(root, monkeypatch)
    bundle = root / "bundles" / "kolsch"
    (root / ".kolsch" / "default-rules.json").unlink()
    mutate_platform_rules(
        root / "platform.yaml",
        RemoveRule(
            match=Rule(what="kolsch/tools/jev_decide", pattern="*", effect="prompt")
        ),
        catalog=None,
    )
    (bundle / "tools" / "jev_decide.py").unlink()  # copy in
    (bundle / "nodes" / "echo.py").unlink()  # copy in
    _edit(bundle / "tools" / "bash.py")  # ask about
    (bundle / "tools" / "my_tool.py").write_text("# mine\n", encoding="utf-8")  # ask
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(  # rewrite
        yaml_path.read_text(encoding="utf-8").replace(
            "what: kolsch/tools/fetch_url", "what: kolsch/tools/fetch_website", 1
        ),
        encoding="utf-8",
    )

    plan = await plan_workspace_upgrade(root)
    assert (plan.added, plan.modified, plan.rewrites) == (2, 2, 1)
    assert [w for w, _e, _r in plan.rule_proposals] == ["kolsch/tools/jev_decide"]

    dry = _io(root)
    await run_setup(_sync(root, dry_run=True, yes=True), dry)
    counts = _dry_run_counts(dry.out.getvalue())
    assert counts.get("copied", 0) == plan.added
    assert counts.get("refreshed", 0) == plan.outdated
    assert counts.get("deleted", 0) == plan.removable
    assert counts.get("rewrote", 0) == plan.rewrites
    assert (
        counts.get("kept-local", 0)
        + counts.get("kept-retired (not shipped by kolsch, or edited)", 0)
        == plan.modified
    )
    assert "+ kolsch/tools/jev_decide → prompt" in dry.out.getvalue()

    rc, text = await _doctor(root)
    assert rc == 1
    assert text.count("\n  + tools/") + text.count("\n  + nodes/") == plan.added
    assert "~ tools/bash.py" in text
    assert "- tools/my_tool.py" in text
    assert "kolsch/tools/fetch_website" in text
    assert "+ kolsch/tools/jev_decide → prompt" in text
