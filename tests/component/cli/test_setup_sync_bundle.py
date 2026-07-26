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
from typing import TYPE_CHECKING

from cli._doctor import cmd_doctor
from cli._setup import (
    NonInteractivePrompter,
    _SetupIO,
    run_setup,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cli._setup import SetupPrompter


# ---------------------------------------------------------------------------
# Scaffolding shared with tests/component/cli/test_doctor.py
# ---------------------------------------------------------------------------


async def _noop_run_handler(args: argparse.Namespace) -> tuple[int, str | None]:
    _ = args
    return (0, None)


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
# Test — sync on a workspace with no baseline errors gracefully
# ---------------------------------------------------------------------------


async def test_sync_without_baseline_errors_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-093 defensive: sync on a workspace with no baseline exits non-zero cleanly."""
    await _install(tmp_path, monkeypatch)
    (tmp_path / ".kolsch" / "bundle-baseline.json").unlink()

    io_ = _make_setup_io(tmp_path)
    rc = await run_setup(_sync_args(tmp_path, yes=True), io_)
    assert rc != 0
    output = io_.out.getvalue()
    assert "baseline" in output.lower()
