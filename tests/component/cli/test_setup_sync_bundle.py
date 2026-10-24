"""qol-093 — `kolsch setup --sync-bundle` write path.

Covers the acceptance criteria for the repair half of the bundle-drift
work (qol-089 shipped the detection foundation; qol-093 layers the
sync-write path on top):

- **Test 1 — ses_0e54 repair.** Given a workspace that mimics the
  ses_0e549428 shape (fetch_website tool file + retired refs in
  helper.yaml + platform.yaml), `--sync-bundle --yes` produces the
  current source's file set, rewrites the retired refs, refreshes the
  baseline, exits 0.
- **Test 2 — locally-modified preserved.** A user's edit to a shipped
  tool file is NOT overwritten by `--yes`; the file is listed as
  "kept-local" and sync exits non-zero.
- **Test 3 — `--dry-run` writes nothing.** Directory-hash before/after
  identical; baseline unchanged; exit code parity with the wet-run.
- **Test 4 — baseline refreshed after successful sync.** Running
  `kolsch doctor` after `--sync-bundle` produces the clean output.
- **Test 5 — user-owned files untouched.** `bundles/autoply/**` retired
  refs are reported, not rewritten.
- **Test 6 — interactive `[k]` / `[o]` / `[d]iff`.** Scripted prompter
  drives each answer path; `[d]` prints a diff and re-prompts.
- **Test 7 — partial-success non-zero exit.** Any skipped drift item
  triggers non-zero exit even when the sync also applied changes.
- **Test 8 — first-run install byte-preserved.** Regression guard —
  `kolsch setup --non-interactive` (no `--sync-bundle`) still ships
  the same file set as before qol-093.

The fixture mirrors `tests/component/cli/test_doctor.py`'s scaffolding
so the two suites share intuition around the setup surface.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
from typing import TYPE_CHECKING

from cli._doctor import cmd_doctor
from cli._setup import (
    NonInteractivePrompter,
    _SetupIO,
    _TestRunResult,
    run_setup,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cli._setup import SetupPrompter


# ---------------------------------------------------------------------------
# Scaffolding shared with tests/component/cli/test_doctor.py
# ---------------------------------------------------------------------------


async def _noop_run_handler(args: argparse.Namespace) -> _TestRunResult:
    _ = args
    return _TestRunResult(rc=0)


def _connect_io_factory(
    _root: object,
    _provider: str,
    _api_key: str | None = None,
) -> object:
    raise NotImplementedError("no provider auto-connect in this test")


def _make_setup_io(root: Path, prompter: SetupPrompter | None = None) -> _SetupIO:
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=prompter if prompter is not None else NonInteractivePrompter(),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )


async def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SetupIO:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 0, io_.out.getvalue()
    return io_


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _dir_hash(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        out[str(path.relative_to(root))] = _sha256(path)
    return out


def _sync_args(
    root: Path, *, dry_run: bool = False, yes: bool = True
) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        non_interactive=False,
        sync_bundle=True,
        dry_run=dry_run,
        yes=yes,
        no=False,
    )


# ---------------------------------------------------------------------------
# Test 1 — ses_0e54-shape repair end-to-end
# ---------------------------------------------------------------------------


async def test_sync_repairs_ses_0e54_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-1: `--sync-bundle --yes` repairs retired refs + retired tool file."""
    await _install(tmp_path, monkeypatch)

    # Simulate the ses_0e54 aftermath: a workspace that had
    # fetch_website.py + retired refs in kolsch-owned YAMLs.
    stray_tool = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray_tool.write_text("# legacy tool\n", encoding="utf-8")

    helper = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    helper_text = helper.read_text(encoding="utf-8")
    helper.write_text(
        helper_text.replace(
            "- ref: kolsch/tools/bash",
            "- ref: kolsch/tools/bash\n        - ref: kolsch/tools/fetch_website",
        ),
        encoding="utf-8",
    )

    platform_yaml = tmp_path / "platform.yaml"
    platform_text = platform_yaml.read_text(encoding="utf-8")
    # Inject a retired-ref allow-rule into the EXISTING
    # `authorization.config.rules` block. YAML has a single top-level
    # `authorization:` key already; adding a second would be a
    # DuplicateKeyError. Splice a new list entry ahead of the first
    # rule (the stock config uses 4-space indent for list entries).
    injected = platform_text.replace(
        "    rules:\n    - what:",
        "    rules:\n    - what: kolsch/tools/fetch_website\n"
        '      pattern: "*"\n      effect: allow\n    - what:',
        1,
    )
    assert injected != platform_text, (
        "platform.yaml did not carry the expected authz.rules block"
    )
    platform_yaml.write_text(injected, encoding="utf-8")

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()

    assert rc == 0, output
    # Retired tool file is gone.
    assert not stray_tool.exists(), "retired-at-source file should be deleted"
    # Retired ref rewritten in helper.
    helper_after = helper.read_text(encoding="utf-8")
    assert "kolsch/tools/fetch_website" not in helper_after
    assert "kolsch/tools/fetch_url" in helper_after
    assert "kolsch/tools/browse_url" in helper_after
    # Retired ref rewritten in platform.yaml.
    platform_after = platform_yaml.read_text(encoding="utf-8")
    assert "kolsch/tools/fetch_website" not in platform_after
    assert "kolsch/tools/fetch_url" in platform_after
    # Baseline refreshed.
    assert "baseline refreshed" in output


# ---------------------------------------------------------------------------
# Test 2 — locally-modified file preserved under --yes
# ---------------------------------------------------------------------------


async def test_locally_modified_preserved_under_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-2: `--yes` never silently clobbers a modified file."""
    await _install(tmp_path, monkeypatch)
    bash_path = tmp_path / "bundles" / "kolsch" / "tools" / "bash.py"
    original = bash_path.read_text(encoding="utf-8")
    bash_path.write_text(original + "\n# local edit\n", encoding="utf-8")

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)

    output = io_.out.getvalue()
    assert rc != 0, output  # partial-success: skipped a drift item
    assert bash_path.read_text(encoding="utf-8") == original + "\n# local edit\n"
    assert "kept-local" in output
    assert "tools/bash.py" in output


# ---------------------------------------------------------------------------
# Test 3 — --dry-run writes nothing
# ---------------------------------------------------------------------------


async def test_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-3: --dry-run produces the same report but no writes."""
    await _install(tmp_path, monkeypatch)
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# legacy\n", encoding="utf-8")

    hash_before = _dir_hash(tmp_path)

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, dry_run=True, yes=True), io_)

    output = io_.out.getvalue()
    assert rc == 0, output
    assert "dry-run" in output.lower()
    hash_after = _dir_hash(tmp_path)
    assert hash_before == hash_after, "dry-run mutated files on disk"


# ---------------------------------------------------------------------------
# Test 4 — baseline refresh after successful sync → doctor clean
# ---------------------------------------------------------------------------


async def test_baseline_refreshed_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-4: post-sync doctor exits clean."""
    await _install(tmp_path, monkeypatch)
    # Induce one clean drift item (added file — an easy fix).
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# legacy\n", encoding="utf-8")

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    assert rc == 0, io_.out.getvalue()

    out = io.StringIO()
    doctor_rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert doctor_rc == 0, out.getvalue()
    assert "no drift detected" in out.getvalue()


# ---------------------------------------------------------------------------
# Test 5 — user-owned files untouched
# ---------------------------------------------------------------------------


async def test_user_owned_files_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-5: `bundles/<other>/**` retired refs are reported, not rewritten."""
    await _install(tmp_path, monkeypatch)
    autoply = tmp_path / "bundles" / "autoply" / "workflows" / "job.yaml"
    autoply.parent.mkdir(parents=True)
    autoply.write_text(
        "nodes:\n  fetch:\n    kind: agent\n    config:\n"
        "      policy:\n        allow:\n"
        "          - kolsch/tools/fetch_website\n",
        encoding="utf-8",
    )
    autoply_before = autoply.read_text(encoding="utf-8")

    io_ = _make_setup_io(tmp_path)
    await run_setup(_sync_args(tmp_path, yes=True), io_)

    assert autoply.read_text(encoding="utf-8") == autoply_before
    output = io_.out.getvalue()
    assert "autoply/workflows/job.yaml" in output
    assert "kolsch/tools/fetch_website" in output
    assert "user-owned" in output.lower() or "does not rewrite" in output.lower()


# ---------------------------------------------------------------------------
# Test 6 — interactive [k] / [o] / [d] paths
# ---------------------------------------------------------------------------


class _ScriptedPrompter:
    """Returns the next scripted answer per call to `.choose(...)`."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.questions: list[str] = []

    def choose(self, question: str, options: list[str], default: str) -> str:
        _ = options, default
        self.questions.append(question)
        return self._answers.pop(0) if self._answers else default

    def text(self, question: str, default: str = "") -> str:
        _ = question
        return default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        _ = question
        return default


async def test_interactive_overwrite_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-6: interactive `[o]` overwrites the modified file."""
    await _install(tmp_path, monkeypatch)
    bash_path = tmp_path / "bundles" / "kolsch" / "tools" / "bash.py"
    original = bash_path.read_text(encoding="utf-8")
    bash_path.write_text(original + "\n# local edit\n", encoding="utf-8")

    prompter = _ScriptedPrompter(["o"])
    io_ = _make_setup_io(tmp_path, prompter=prompter)
    args = argparse.Namespace(
        root=tmp_path,
        non_interactive=False,
        sync_bundle=True,
        dry_run=False,
        yes=False,
        no=False,
    )
    rc = await run_setup(args, io_)
    assert rc == 0, io_.out.getvalue()
    # bash.py restored to source content.
    assert bash_path.read_text(encoding="utf-8") == original


async def test_interactive_diff_then_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-6: `[d]iff` prints a diff and re-prompts; `[k]` keeps."""
    await _install(tmp_path, monkeypatch)
    bash_path = tmp_path / "bundles" / "kolsch" / "tools" / "bash.py"
    original = bash_path.read_text(encoding="utf-8")
    bash_path.write_text(original + "\n# local edit\n", encoding="utf-8")

    prompter = _ScriptedPrompter(["d", "k"])
    io_ = _make_setup_io(tmp_path, prompter=prompter)
    args = argparse.Namespace(
        root=tmp_path,
        non_interactive=False,
        sync_bundle=True,
        dry_run=False,
        yes=False,
        no=False,
    )
    rc = await run_setup(args, io_)
    output = io_.out.getvalue()
    # Partial-success — user kept their edit.
    assert rc != 0, output
    # Diff was printed (unified-diff markers appear).
    assert "---" in output and "+++" in output
    # File preserved.
    assert bash_path.read_text(encoding="utf-8") == original + "\n# local edit\n"
    # The [k]/[o]/[d] question was asked TWICE (once for the diff,
    # once for the final decision).
    assert len(prompter.questions) == 2


# ---------------------------------------------------------------------------
# Test 7 — partial-success exit code (already covered by Test 2, +
# an explicit "mixed: applied + skipped" case here).
# ---------------------------------------------------------------------------


async def test_partial_success_when_applied_and_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-7: even if some items applied, a single skip → non-zero exit."""
    await _install(tmp_path, monkeypatch)
    # Retired-at-source file — will be prompted; --yes safe default = keep.
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# legacy\n", encoding="utf-8")
    # Modified file — --yes safe default = keep-local.
    bash_path = tmp_path / "bundles" / "kolsch" / "tools" / "bash.py"
    bash_path.write_text(
        bash_path.read_text(encoding="utf-8") + "\n# local\n", encoding="utf-8"
    )

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert rc != 0, output
    # `--yes` deletes retired files (safe: not user edits) but keeps
    # modified files (never clobber user work). The delete lands in
    # `applied`; the modified skip lands in `skipped`. The skip alone
    # is enough to trigger the partial-success exit.
    assert "deleted" in output
    assert "kept-local" in output


# ---------------------------------------------------------------------------
# Test 8 — first-run install byte-preserved (regression guard)
# ---------------------------------------------------------------------------


async def test_first_run_install_unchanged_by_qol_093(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 AC-8: --sync-bundle wiring doesn't perturb the plain install path.

    `kolsch setup --non-interactive` on a fresh directory ships the
    same file set as before qol-093 — the sync branch is entirely
    dormant when --sync-bundle isn't passed.
    """
    await _install(tmp_path, monkeypatch)
    from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR

    workspace_bundle = tmp_path / "bundles" / "kolsch"
    assert _dir_hash(workspace_bundle) == _dir_hash(_STARTER_BUNDLE_TEMPLATE_DIR)


# ---------------------------------------------------------------------------
# bug-146 — sync on a workspace with no baseline bootstraps one
# ---------------------------------------------------------------------------


def _baseline_files(root: Path) -> dict[str, str]:
    payload = json.loads(
        (root / ".kolsch" / "bundle-baseline.json").read_text(encoding="utf-8")
    )
    files = payload["files"]
    assert isinstance(files, dict)
    return files


def _make_pre_baseline_workspace(root: Path) -> Path:
    """Turn a fresh install into the bug-146 Reproduce shape (file part).

    No baseline, no `tools/jev_decide.py`, no `workflows/`, one comment
    line added to `agents/helper.yaml`, plus the junk a real workspace
    carries (`__pycache__/` bytecode, a Finder `.DS_Store`). Returns
    the edited helper path.
    """
    bundle = root / "bundles" / "kolsch"
    (root / ".kolsch" / "bundle-baseline.json").unlink()
    (bundle / "tools" / "jev_decide.py").unlink()
    shutil.rmtree(bundle / "workflows")
    helper = bundle / "agents" / "helper.yaml"
    helper.write_text(
        "# my local note\n" + helper.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (bundle / "tools" / "__pycache__").mkdir()
    (bundle / "tools" / "__pycache__" / "bash.cpython-313.pyc").write_bytes(b"\0pyc")
    (bundle / ".DS_Store").write_bytes(b"\0finder")
    return helper


async def test_sync_without_baseline_bootstraps_and_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-1/3/4 (Test 1): no baseline → classify vs source, repair, record.

    Contract change: qol-093 refused a baseline-less workspace; it now
    classifies against the current source and writes a baseline.
    """
    await _install(tmp_path, monkeypatch)
    from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR

    source = _STARTER_BUNDLE_TEMPLATE_DIR
    helper = _make_pre_baseline_workspace(tmp_path)
    helper_before = helper.read_bytes()
    bundle = tmp_path / "bundles" / "kolsch"

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()

    assert rc == 1, output  # partial: helper.yaml kept local
    assert "no baseline recorded" in output
    assert "Delete `bundles/kolsch/`" not in output
    # Missing files copied in, byte-identical to source.
    copied = ["tools/jev_decide.py"] + [
        f"workflows/{p.name}" for p in (source / "workflows").iterdir()
    ]
    for rel in copied:
        assert (bundle / rel).read_bytes() == (source / rel).read_bytes(), rel
    assert "kept-local: agents/helper.yaml" in output
    assert helper.read_bytes() == helper_before
    # Junk is not "retired at source": never deleted, never recorded.
    assert (bundle / ".DS_Store").is_file()
    assert (bundle / "tools" / "__pycache__" / "bash.cpython-313.pyc").is_file()

    files = _baseline_files(tmp_path)
    assert "agents/helper.yaml" not in files
    assert not any("__pycache__" in k or k.endswith(".DS_Store") for k in files)
    expected = {
        p.relative_to(bundle).as_posix()
        for p in bundle.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.name != ".DS_Store"
        and p != helper
    }
    assert set(files) == expected
    for rel, digest in files.items():
        assert digest == _sha256(bundle / rel), rel

    # Second run: only the kept-local helper comes back.
    io2 = _make_setup_io(tmp_path)
    rc2 = await run_setup(_sync_args(tmp_path, yes=True), io2)
    output2 = io2.out.getvalue()
    assert rc2 == 1, output2
    assert "no baseline recorded" not in output2
    assert "Applied" not in output2
    assert "Skipped (1):" in output2
    assert "kept-local: agents/helper.yaml" in output2


async def test_sync_without_baseline_on_synced_workspace_records_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-1/2: a baseline-less but in-sync workspace exits 0 and gains one."""
    await _install(tmp_path, monkeypatch)
    baseline = tmp_path / ".kolsch" / "bundle-baseline.json"
    recorded = _baseline_files(tmp_path)
    baseline.unlink()

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert rc == 0, output
    assert "baseline refreshed" in output
    assert "no drift" in output
    assert _baseline_files(tmp_path) == recorded


async def test_identical_to_source_file_is_unchanged_without_baseline_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-2 (Test 2): a missing baseline entry no longer means "modified"."""
    await _install(tmp_path, monkeypatch)
    baseline = tmp_path / ".kolsch" / "bundle-baseline.json"
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    del payload["files"]["tools/bash.py"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    output = io_.out.getvalue()
    assert rc == 0, output
    assert "kept-local" not in output
    # The entry is restored.
    assert "tools/bash.py" in _baseline_files(tmp_path)


async def test_partial_sync_with_baseline_records_copied_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-3: a partial run still records what it copied.

    Before bug-146 a partial run left the baseline untouched, so a file
    it copied in had no entry and came back as "modified" next run.
    """
    await _install(tmp_path, monkeypatch)
    bundle = tmp_path / "bundles" / "kolsch"
    (bundle / "tools" / "jev_decide.py").unlink()
    payload_path = tmp_path / ".kolsch" / "bundle-baseline.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    del payload["files"]["tools/jev_decide.py"]
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    bash_path = bundle / "tools" / "bash.py"
    bash_path.write_text(
        bash_path.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8"
    )
    bash_entry = _baseline_files(tmp_path)["tools/bash.py"]

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    assert rc == 1, io_.out.getvalue()
    files = _baseline_files(tmp_path)
    assert files["tools/jev_decide.py"] == _sha256(bundle / "tools" / "jev_decide.py")
    # The kept-local file keeps its prior entry.
    assert files["tools/bash.py"] == bash_entry

    io2 = _make_setup_io(tmp_path)
    rc2 = await run_setup(_sync_args(tmp_path, yes=True), io2)
    output2 = io2.out.getvalue()
    assert rc2 == 1, output2
    assert "jev_decide" not in output2
    assert "kept-local: tools/bash.py" in output2


async def test_sync_dry_run_without_baseline_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-10 (file part): `--dry-run` records no baseline either."""
    await _install(tmp_path, monkeypatch)
    _make_pre_baseline_workspace(tmp_path)
    before = _dir_hash(tmp_path)

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, dry_run=True, yes=True), io_)
    output = io_.out.getvalue()
    assert rc == 1, output
    assert "copied: tools/jev_decide.py" in output
    assert _dir_hash(tmp_path) == before
    assert not (tmp_path / ".kolsch" / "bundle-baseline.json").exists()


async def test_interactive_prompt_without_baseline_entry_names_the_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146: with no baseline entry the prompt can't claim "modified vs baseline"."""
    await _install(tmp_path, monkeypatch)
    helper = _make_pre_baseline_workspace(tmp_path)
    questions: list[str] = []

    class _Recorder(NonInteractivePrompter):
        def choose(self, question: str, options: list[str], default: str) -> str:
            questions.append(question)
            return "o" if "helper.yaml" in question else default

    io_ = _make_setup_io(tmp_path, prompter=_Recorder())
    rc = await run_setup(_sync_args(tmp_path, yes=False), io_)
    assert rc == 0, io_.out.getvalue()
    helper_q = [q for q in questions if "agents/helper.yaml" in q]
    assert helper_q == [
        "[sync] agents/helper.yaml: differs from source (no baseline entry — "
        "a local edit or an older shipped version)."
    ]
    # Overwritten → identical to source → recorded.
    assert _baseline_files(tmp_path)["agents/helper.yaml"] == _sha256(helper)


async def test_fresh_install_skips_pycache_and_ds_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-4: install never copies or hashes bytecode / Finder junk."""
    import cli._setup as setup_mod

    template = tmp_path / "template" / "kolsch"
    shutil.copytree(setup_mod._STARTER_BUNDLE_TEMPLATE_DIR, template)
    (template / "tools" / "__pycache__").mkdir()
    (template / "tools" / "__pycache__" / "bash.cpython-313.pyc").write_bytes(b"x")
    (template / ".DS_Store").write_bytes(b"x")
    monkeypatch.setattr(setup_mod, "_STARTER_BUNDLE_TEMPLATE_DIR", template)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    await _install(workspace, monkeypatch)
    bundle = workspace / "bundles" / "kolsch"
    assert not (bundle / "tools" / "__pycache__").exists()
    assert not (bundle / ".DS_Store").exists()
    files = _baseline_files(workspace)
    assert set(files) == {
        p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()
    }
    assert not any("__pycache__" in k or ".DS_Store" in k for k in files)
