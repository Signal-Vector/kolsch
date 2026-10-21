"""qol-089 — `kolsch doctor` (read-only bundle-drift report) + baseline write.

Covers acceptance criteria 4, 5, 6, 7 (spec §Acceptance Criteria):

- Test 5 (:func:`test_install_writes_baseline`) — first-run install
  writes ``.kolsch/bundle-baseline.json`` with sha256 per shipped file.
- Test 6 (:func:`test_install_byte_preserved`) — first-run install
  still produces the same tool + agent file set as pre-qol.
- Test 2 (:func:`test_doctor_clean_workspace`) — a workspace where
  install just ran → doctor exits 0, prints "no drift detected".
- Test 3 (:func:`test_doctor_reports_drift`) — modified shipped file
  + retired-at-source file + user-owned retired ref → doctor exits 1
  and names all three; the workspace is byte-identical after.
- Test 4 (:func:`test_doctor_without_baseline`) — older workspace
  with no baseline → doctor still runs, skips modified-vs-baseline
  section with a one-line note.

qol-093 will layer the *repair* half (`kolsch setup --sync-bundle`)
on top of this detection foundation; those component tests live in
that qol's shipment.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
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


# ---------------------------------------------------------------------------
# Helpers — light shims that reuse the existing setup test infrastructure
# ---------------------------------------------------------------------------


async def _noop_run_handler(args: argparse.Namespace) -> _TestRunResult:
    _ = args
    return _TestRunResult(rc=0)


def _connect_io_factory(
    _root: object,
    _provider: str,
    _api_key: str | None = None,
) -> object:
    """Setup only touches this when auto-connecting via env vars — we don't."""
    raise NotImplementedError("no provider auto-connect in this test")


def _make_setup_io(root: Path) -> _SetupIO:
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )


async def _run_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SetupIO:
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
    """Return {relpath: sha256} for every file under ``root`` (skipping __pycache__)."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        out[str(path.relative_to(root))] = _sha256(path)
    return out


# ---------------------------------------------------------------------------
# Test 5 — install writes .kolsch/bundle-baseline.json
# ---------------------------------------------------------------------------


async def test_install_writes_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 AC-4: install records a valid baseline for every shipped file."""
    await _run_install(tmp_path, monkeypatch)

    baseline_path = tmp_path / ".kolsch" / "bundle-baseline.json"
    assert baseline_path.is_file(), (
        f"expected {baseline_path.relative_to(tmp_path)} after install"
    )
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert isinstance(payload["shipped_at"], str) and payload["shipped_at"]
    assert isinstance(payload["kolsch_version"], str)
    files = payload["files"]
    assert isinstance(files, dict) and files, "baseline files map must be non-empty"

    # Every recorded sha256 must match the file actually on disk.
    bundle_dir = tmp_path / "bundles" / "kolsch"
    for rel, recorded in files.items():
        assert recorded == _sha256(bundle_dir / rel), (
            f"baseline hash for {rel} doesn't match on-disk file"
        )

    # Every shipped tool file appears in the baseline (spot-check the
    # canonical set).
    for expected in ("agents/helper.yaml", "bundle.yaml", "tools/bash.py"):
        assert expected in files, f"baseline missing {expected}"


# ---------------------------------------------------------------------------
# Test 6 — install byte-preserved (regression guard for qol-089's refactor)
# ---------------------------------------------------------------------------


async def test_install_byte_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 AC-7: install still ships the exact source file set.

    The refactor added a baseline write; it must not change what
    ``shutil.copytree`` lands. Compare the workspace's ``bundles/kolsch/``
    content against the packaged template — sha256 per file must match.
    """
    await _run_install(tmp_path, monkeypatch)

    from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR

    workspace_bundle = tmp_path / "bundles" / "kolsch"
    workspace_hashes = _dir_hash(workspace_bundle)
    source_hashes = _dir_hash(_STARTER_BUNDLE_TEMPLATE_DIR)
    assert workspace_hashes == source_hashes, (
        "workspace bundle diverges from source template — install path changed"
    )


# ---------------------------------------------------------------------------
# Test 2 — doctor on a clean workspace exits 0
# ---------------------------------------------------------------------------


async def test_doctor_clean_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 AC-5: doctor on a just-installed workspace → no drift, exit 0."""
    await _run_install(tmp_path, monkeypatch)

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0, out.getvalue()
    assert "no drift detected" in out.getvalue()


# ---------------------------------------------------------------------------
# Test 3 — doctor reports drift + writes nothing
# ---------------------------------------------------------------------------


async def test_doctor_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 AC-5 + AC-2: doctor names each drift kind, exits 1, no writes.

    Fixture creates three drift categories in one workspace:

    - **Modified vs baseline** — mutate ``tools/bash.py`` after install.
    - **Retired at source** — drop a ``fetch_website.py`` stub into
      ``bundles/kolsch/tools/`` (source no longer ships it).
    - **User-owned retired ref** — add a
      ``bundles/autoply/workflows/job.yaml`` referencing
      ``kolsch/tools/fetch_website`` (the migrator manifest's seeded
      entry).
    """
    await _run_install(tmp_path, monkeypatch)

    # (a) modified vs baseline
    bash_path = tmp_path / "bundles" / "kolsch" / "tools" / "bash.py"
    original_bash = bash_path.read_text(encoding="utf-8")
    bash_path.write_text(original_bash + "\n# local edit\n", encoding="utf-8")

    # (b) retired-at-source — a file not in the current starter bundle.
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# retired stub\n", encoding="utf-8")

    # (c) user-owned retired ref.
    autoply_wf = tmp_path / "bundles" / "autoply" / "workflows" / "job.yaml"
    autoply_wf.parent.mkdir(parents=True)
    autoply_wf.write_text(
        "nodes:\n  fetch:\n    kind: agent\n    config:\n"
        "      policy:\n        allow:\n"
        "          - kolsch/tools/fetch_website\n",
        encoding="utf-8",
    )

    hash_before = _dir_hash(tmp_path)

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 1, out.getvalue()

    text = out.getvalue()
    # Each drift kind must appear in its section.
    assert "tools/bash.py" in text
    assert "fetch_website.py" in text
    assert "autoply/workflows/job.yaml" in text
    assert "kolsch/tools/fetch_website" in text
    assert "kolsch setup --sync-bundle" in text  # pointer to qol-093

    # AC-2 invariant — doctor is read-only.
    hash_after = _dir_hash(tmp_path)
    assert hash_before == hash_after, (
        "doctor modified files on disk; must be strictly read-only"
    )


# ---------------------------------------------------------------------------
# Test 4 — doctor gracefully handles missing baseline
# ---------------------------------------------------------------------------


async def test_doctor_without_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 AC-6: older workspace w/o baseline → skip section (b), note it.

    Sim: install, then delete ``.kolsch/bundle-baseline.json``. Doctor
    should still run, skip the modified-vs-baseline section with a
    one-line note, run the other sections normally.
    """
    await _run_install(tmp_path, monkeypatch)
    (tmp_path / ".kolsch" / "bundle-baseline.json").unlink()

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)

    text = out.getvalue()
    # The "baseline not recorded" note must fire...
    assert "baseline not recorded" in text
    # ...and the workspace is otherwise clean, so exit 0.
    assert rc == 0, text


# ---------------------------------------------------------------------------
# Reviewer #2 findings — regression guards
# ---------------------------------------------------------------------------


async def test_doctor_on_pre_setup_workspace_hints_run_setup(
    tmp_path: Path,
) -> None:
    """qol-089 Reviewer #2 finding #3: no `bundles/kolsch/` → friendly hint, exit 0.

    A brand-new workspace directory (no `bundles/` at all) previously
    exited 1 and dumped the entire source manifest as "missing" with
    an unshipped `--sync-bundle` pointer. Now it emits a one-line
    "run `kolsch setup` to install" hint and exits 0.
    """
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert "kolsch setup" in text
    assert "install" in text
    # Must NOT dump the source manifest.
    assert "Missing" not in text
    # Must NOT reference the unshipped --sync-bundle flag.
    assert "--sync-bundle" not in text


async def test_doctor_exit_hint_does_not_reference_unshipped_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 Reviewer #2 finding #2: no exit-hint pointer at unshipped flag.

    The pre-fix hint said "Run `kolsch setup --sync-bundle` to
    repair"; the flag doesn't exist yet (qol-093). Reworded to make
    the not-yet-shipped state explicit.
    """
    await _run_install(tmp_path, monkeypatch)
    # Induce one drift so the exit hint fires at all.
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# retired stub\n", encoding="utf-8")

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 1
    text = out.getvalue()
    # The hint MUST mention qol-093 as the future automation home;
    # it MUST NOT tell the user to run an unshipped flag as the
    # primary action.
    assert "qol-093" in text
    assert "not yet shipped" in text


async def test_doctor_surfaces_malformed_yaml_as_report_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 Reviewer #2 finding #1: malformed YAML → report line, not crash.

    Corrupt ``bundles/kolsch/agents/helper.yaml`` after install. The
    migrator's ``_yaml_load`` used to raise a ``YAMLError`` up through
    doctor as a traceback; now it lands in ``parse_failures`` and
    gets a dedicated report section.
    """
    await _run_install(tmp_path, monkeypatch)
    helper = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    helper.write_text(
        "not: valid: yaml:\n  - [unclosed\n  key without value\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    # Doctor must not crash; must exit 1 (parse failure is drift).
    assert rc == 1, text
    assert "couldn't parse" in text
    assert "helper.yaml" in text


async def test_retired_kolsch_owned_ref_shows_replacement_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qol-089 Reviewer #2 finding #4: Retired.replacement is surfaced.

    Uses a monkey-patched manifest carrying a ``Retired`` action with
    a ``replacement=`` pointer; render output should include the
    ``consider: `<ref>``` hint.
    """
    await _run_install(tmp_path, monkeypatch)
    # helper declares kolsch/tools/bash — retire it with a replacement
    # pointer to prove the render surfaces the manifest hint.
    from cli._migrations import tools as tools_mod

    monkeypatch.setattr(
        tools_mod,
        "TOOL_MIGRATIONS",
        {
            "kolsch/tools/bash": tools_mod.Retired(
                reason="test — retired for the render-hint check",
                replacement="kolsch/tools/shell",
            )
        },
    )

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1, text
    assert "remove `kolsch/tools/bash`" in text
    assert "consider: `kolsch/tools/shell`" in text
