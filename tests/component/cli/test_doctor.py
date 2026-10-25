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
  with no baseline → doctor still runs, compares shared files against
  the current source instead, with a one-line note (bug-146).

qol-093 layers the *repair* half (`kolsch setup --sync-bundle`) on top
of this detection foundation; its tests live in
``test_setup_sync_bundle.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from cli._doctor import cmd_doctor
from cli._setup import (
    NonInteractivePrompter,
    _SetupIO,
    _TestRunResult,
    run_setup,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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
    """qol-089 AC-6: older workspace w/o baseline → note it, still clean.

    Sim: install, then delete ``.kolsch/bundle-baseline.json``. Doctor
    should still run, note the missing baseline (bug-146: section (b)
    compares against source instead), run the other sections normally.
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


async def test_doctor_exit_hint_points_at_sync_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-11: the exit hint names the shipped repair command.

    Contract change: qol-089's hint said `--sync-bundle` was "planned in
    qol-093 (not yet shipped)"; qol-093 shipped, so the hint now points
    at it (and `--dry-run`).
    """
    await _run_install(tmp_path, monkeypatch)
    # Induce one drift so the exit hint fires at all.
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# retired stub\n", encoding="utf-8")

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 1
    text = out.getvalue()
    assert "Run `kolsch setup --sync-bundle` to review and repair" in text
    assert "--dry-run" in text
    assert "not yet shipped" not in text
    assert "qol-093" not in text


async def test_doctor_without_baseline_lists_modified_vs_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-11 (Test 2): no baseline → files that differ from source are listed.

    Mirrors the owner's pre-qol-089 workspace: no baseline, a missing
    shipped tool, one edited shipped file, plus `__pycache__/` and
    `.DS_Store` junk (which must not show as "retired at source").
    Doctor stays read-only.
    """
    await _run_install(tmp_path, monkeypatch)
    bundle = tmp_path / "bundles" / "kolsch"
    (tmp_path / ".kolsch" / "bundle-baseline.json").unlink()
    (bundle / "tools" / "jev_decide.py").unlink()
    helper = bundle / "agents" / "helper.yaml"
    helper.write_text("# note\n" + helper.read_text(encoding="utf-8"), encoding="utf-8")
    (bundle / "tools" / "__pycache__").mkdir()
    (bundle / "tools" / "__pycache__" / "bash.cpython-313.pyc").write_bytes(b"x")
    (bundle / ".DS_Store").write_bytes(b"x")
    before = {
        str(p.relative_to(tmp_path)): _sha256(p)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1, text
    assert "Modified vs source" in text
    assert "  ~ agents/helper.yaml" in text
    # Identical-to-source files are never listed as modified.
    assert "~ tools/bash.py" not in text
    assert "  + tools/jev_decide.py" in text
    assert ".DS_Store" not in text
    assert "__pycache__" not in text
    assert "kolsch setup --sync-bundle" in text
    assert "run `kolsch setup` to record" not in text
    assert "not yet shipped" not in text

    after = {
        str(p.relative_to(tmp_path)): _sha256(p)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before == after, "doctor must be strictly read-only"


async def test_doctor_identical_file_without_baseline_entry_is_not_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-146 AC-2: a baseline that lacks an entry doesn't make the file "modified"."""
    await _run_install(tmp_path, monkeypatch)
    baseline = tmp_path / ".kolsch" / "bundle-baseline.json"
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    del payload["files"]["tools/bash.py"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0, out.getvalue()
    assert "no drift detected" in out.getvalue()


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


# ---------------------------------------------------------------------------
# bug-152 — advisory Authorization section (default-deny tail)
# ---------------------------------------------------------------------------

_TAIL_FIX = "kolsch permissions add --what '*' --pattern '*' --effect deny"


def _edit_rules(root: Path, edit: Callable[[list[Any]], list[Any] | None]) -> None:
    """Rewrite ``authorization.config.rules`` in ``root/platform.yaml``."""
    path = root / "platform.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["authorization"]["config"]["rules"] = edit(
        data["authorization"]["config"]["rules"]
    )
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


async def test_doctor_fresh_install_has_no_authorization_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0
    assert "Authorization" not in out.getvalue()


async def test_doctor_tail_removed_reports_authorization_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    _edit_rules(tmp_path, lambda rules: rules[:-1])
    hash_before = _dir_hash(tmp_path)

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text  # advisory: same rc as the clean bundle
    assert "no drift detected" in text
    assert "Authorization (platform.yaml)" in text
    assert "no default-deny tail" in text
    assert _TAIL_FIX in text
    assert _dir_hash(tmp_path) == hash_before


async def test_doctor_tail_not_last_names_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    _edit_rules(tmp_path, lambda rules: [rules[-1], *rules[:-1]])

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert "Authorization (platform.yaml)" in text
    assert "is at position 1 of" in text


async def test_doctor_null_rules_reports_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same normalization as the runtime: null rules == empty enforced set.
    await _run_install(tmp_path, monkeypatch)
    _edit_rules(tmp_path, lambda _rules: None)

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0
    assert "no default-deny tail" in out.getvalue()


async def test_doctor_authorization_does_not_change_drift_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# retired stub\n", encoding="utf-8")
    _edit_rules(tmp_path, lambda rules: rules[:-1])

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1  # bundle drift, exactly as without the authz finding
    assert "Bundle drift report" in text
    assert "Authorization (platform.yaml)" in text
    assert text.index("Bundle drift report") < text.index(
        "Authorization (platform.yaml)"
    )


async def test_doctor_pre_setup_workspace_reports_authorization(
    tmp_path: Path,
) -> None:
    payload = {
        "authorization": {
            "variant": "rule_based",
            "config": {
                "rules": [{"what": "a/tools/x", "pattern": "*", "effect": "allow"}]
            },
        }
    }
    (tmp_path / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    hash_before = _dir_hash(tmp_path)

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert "run `kolsch setup`" in text
    assert "Authorization (platform.yaml)" in text
    assert _TAIL_FIX in text
    assert _dir_hash(tmp_path) == hash_before


async def test_doctor_malformed_platform_yaml_skips_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    (tmp_path / "platform.yaml").write_text(
        "not: valid: yaml:\n  - [unclosed\n", encoding="utf-8"
    )

    out = io.StringIO()
    await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert "Authorization" not in out.getvalue()


async def test_doctor_non_mapping_platform_yaml_skips_authorization(
    tmp_path: Path,
) -> None:
    (tmp_path / "platform.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    assert rc == 0
    assert "Authorization" not in out.getvalue()


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("storage: {}\n", "`authorization` section is missing"),
        ("authorization:\n", "`authorization` is null, not a mapping"),
        (
            "authorization:\n  config: off\n",
            "`authorization.config` is a string, not a mapping",
        ),
        ("authorization: 3\n", "`authorization` is a number, not a mapping"),
        (
            "authorization:\n  config: [1]\n",
            "`authorization.config` is a list, not a mapping",
        ),
        (
            "authorization:\n  config:\n    rules: 5\n",
            "`authorization.config.rules` is a number, not a list",
        ),
    ],
)
async def test_doctor_unreadable_authorization_shape_is_advisory(
    tmp_path: Path, content: str, reason: str
) -> None:
    (tmp_path / "platform.yaml").write_text(content, encoding="utf-8")
    hash_before = _dir_hash(tmp_path)
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text  # advisory
    assert "Authorization (platform.yaml)" in text
    assert f"couldn't check the default-deny tail: {reason}." in text
    assert _dir_hash(tmp_path) == hash_before


async def test_doctor_unreadable_authorization_shape_installed_keeps_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    path = tmp_path / "platform.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["authorization"]["config"]["rules"] = "oops"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert "no drift detected" in text
    assert "couldn't check the default-deny tail" in text


# ---------------------------------------------------------------------------
# bug-161 — advisory "Agent write access" section
# ---------------------------------------------------------------------------

_WRITE_ACCESS = "Agent write access — advisory, exit code unchanged:"
_FLOOR_LINE = (
    "agent file tools can never write: platform.yaml, .kolsch/, .env, .platform/"
)
_ALL_CLEAR = (
    "✓ no rule allows `bash`, and no rule allows `write_file` under "
    "`bundles/` or with a glob pattern (`*`, `?`, `[`), without asking"
)
_BASH_LINE = "allow `bash` without asking: agents can run shell commands"
_BUNDLES_LINE = "allow `write_file` under `bundles/` without asking"
_WILDCARD_LINE = "allow `write_file` on a glob pattern without asking"


def _rid(rule: dict[str, Any]) -> str:
    from providers.authz import derive_rule_id

    return derive_rule_id(
        ".*", f"{rule['what']}|{rule.get('pattern', '*')}", rule["effect"]
    )


async def _doctor_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[dict[str, Any]]
) -> str:
    await _run_install(tmp_path, monkeypatch)
    _edit_rules(tmp_path, lambda rules: [*extra, *rules])
    hash_before = _dir_hash(tmp_path)
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text  # advisory: never changes the exit code
    assert _dir_hash(tmp_path) == hash_before  # read-only
    assert _WRITE_ACCESS in text
    assert _FLOOR_LINE in text
    return text


def _wf(pattern: str) -> dict[str, Any]:
    return {"what": "kolsch/tools/write_file", "pattern": pattern, "effect": "allow"}


async def test_agent_write_access_setup_defaults_have_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = await _doctor_with(tmp_path, monkeypatch, [])
    assert _ALL_CLEAR in text
    assert "--auto-approve" in text
    assert "  ! " not in text


async def test_agent_write_access_owner_shaped_rules_are_grouped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's workspace shape: absolute-root `*`, `bundles/*`,
    `bundles/<name>/*`, and an absolute allow outside the workspace."""
    owner = [
        _wf(f"{tmp_path}/*"),
        _wf("bundles/*"),
        _wf("bundles/andre/*"),
        _wf(f"{tmp_path.parent}/Resumes/*"),
    ]
    text = await _doctor_with(tmp_path, monkeypatch, owner)
    bundles = [line for line in text.splitlines() if _BUNDLES_LINE in line]
    assert bundles == [
        f"  ! rules 1 ({_rid(owner[0])}), 2 ({_rid(owner[1])}), "
        f"3 ({_rid(owner[2])}) allow `write_file` under `bundles/` without "
        "asking: agents can change bundle code and permission YAML "
        "(equivalent to running code)."
    ]
    wildcard = [line for line in text.splitlines() if _WILDCARD_LINE in line]
    assert len(wildcard) == 1
    assert wildcard[0].startswith(f"  ! rule 4 ({_rid(owner[3])}) allow")
    assert "also match `/` and `.`" in wildcard[0]
    assert "kolsch permissions list" in text
    assert _ALL_CLEAR not in text


@pytest.mark.parametrize(
    "pattern",
    [
        "bundles/*",
        "bundles/mine/*",
        "bundles/mine/nodes/n.py",
        "./bundles/mine/*",
        "data/../bundles/x/nodes/n.py",
        "Bundles/mine/*",
        "bundles",
        "{root}/bundles/mine/*",
        "*",
    ],
)
async def test_agent_write_access_bundles_reach_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    text = await _doctor_with(
        tmp_path, monkeypatch, [_wf(pattern.replace("{root}", str(tmp_path)))]
    )
    assert _BUNDLES_LINE in text, text
    assert _WILDCARD_LINE not in text
    assert _ALL_CLEAR not in text


@pytest.mark.parametrize(
    "pattern",
    [
        "data/*",
        "logs/*.txt",
        "/elsewhere/Resumes/*",
        "data/../logs/*",
        # `?` and `[...]` match any one char, including `/` and `.` (R3-2).
        "data/?",
        "?undles/mine/nodes/n.py",
        "[b]undles/mine/nodes/n.py",
        "data/??/bundles/mine/nodes/n.py",
    ],
)
async def test_agent_write_access_glob_pattern_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    """Glob characters match `/` and `.` against the raw path, so `data/*`
    allows `data/../bundles/x/nodes/n.py` and `?undles/…` allows
    `bundles/…` (spec F1, QA R3-2)."""
    from fnmatch import fnmatchcase

    if pattern == "data/*":
        assert fnmatchcase("data/../bundles/x/nodes/n.py", pattern)
    if pattern.endswith("nodes/n.py"):
        reach = (
            "data/../bundles/mine/nodes/n.py"
            if "??" in pattern
            else "bundles/mine/nodes/n.py"
        )
        assert fnmatchcase(reach, pattern)
    text = await _doctor_with(tmp_path, monkeypatch, [_wf(pattern)])
    assert _WILDCARD_LINE in text, text
    assert _BUNDLES_LINE not in text
    assert _ALL_CLEAR not in text


@pytest.mark.parametrize(
    "rule",
    [
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/bash", "pattern": "git *", "effect": "allow"},
        {"what": "kolsch/tools/*", "pattern": "*", "effect": "allow"},
        {"who": ".*", "what": "tool\\.bash", "effect": "allow"},
    ],
)
async def test_agent_write_access_bash_allow_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: dict[str, Any]
) -> None:
    text = await _doctor_with(tmp_path, monkeypatch, [rule])
    assert _BASH_LINE in text, text
    assert "bypasses every file protection, including this one" in text
    assert "also matches `; <any command>`" in text
    assert _ALL_CLEAR not in text


@pytest.mark.parametrize(
    "rule",
    [
        {"what": "kolsch/tools/write_file", "pattern": "bundles/*", "effect": "prompt"},
        {
            "what": "kolsch/tools/write_file",
            "pattern": "data/notes.md",
            "effect": "allow",
        },
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
        {"what": "kolsch/tools/read_file", "pattern": "bundles/*", "effect": "allow"},
        {"who": ".*", "what": "[unclosed", "effect": "allow"},
    ],
)
async def test_agent_write_access_non_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: dict[str, Any]
) -> None:
    text = await _doctor_with(tmp_path, monkeypatch, [rule])
    assert _ALL_CLEAR in text, text
    assert "  ! " not in text


async def test_agent_write_access_catch_all_allow_flags_bash_and_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = await _doctor_with(
        tmp_path,
        monkeypatch,
        [{"id": "rl_wide", "what": "*", "pattern": "*", "effect": "allow"}],
    )
    assert "  ! rule 1 (rl_wide) allow `bash` without asking" in text
    assert "  ! rule 1 (rl_wide) allow `write_file` under `bundles/`" in text


async def test_agent_write_access_legacy_write_allow_counts_as_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = await _doctor_with(
        tmp_path,
        monkeypatch,
        [{"who": ".*", "what": "tool\\.write_file", "effect": "allow"}],
    )
    assert _BUNDLES_LINE in text


async def test_agent_write_access_rule_numbers_skip_non_mapping_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numbers are positions in the raw list, so a stray non-mapping
    entry doesn't shift them."""
    text = await _doctor_with(tmp_path, monkeypatch, ["junk", _wf("bundles/*")])  # type: ignore[list-item]
    assert f"  ! rule 2 ({_rid(_wf('bundles/*'))}) allow `write_file`" in text


async def test_agent_write_access_does_not_change_drift_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_install(tmp_path, monkeypatch)
    stray = tmp_path / "bundles" / "kolsch" / "tools" / "fetch_website.py"
    stray.write_text("# retired stub\n", encoding="utf-8")
    _edit_rules(
        tmp_path,
        lambda rules: [
            {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
            *rules,
        ],
    )
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1  # bundle drift, exactly as without the finding
    assert text.index("Bundle drift report") < text.index(_WRITE_ACCESS)
    assert _BASH_LINE in text


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "platform.yaml not found"),
        (
            "not: valid: yaml:\n  - [unclosed\n",
            "platform.yaml couldn't be read or parsed",
        ),
        ("- just\n- a list\n", "platform.yaml is not a mapping"),
        (
            "authorization:\n  config:\n    rules: 5\n",
            "`authorization.config.rules` is a number, not a list",
        ),
    ],
)
async def test_agent_write_access_unreadable_says_couldnt_check(
    tmp_path: Path, content: str | None, reason: str
) -> None:
    if content is not None:
        (tmp_path / "platform.yaml").write_text(content, encoding="utf-8")
    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 0, text
    assert _FLOOR_LINE in text
    assert f"couldn't check agent write access: {reason}." in text
    assert _ALL_CLEAR not in text


async def test_doctor_labels_unrecorded_modified_files_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA N3: a differing file with no baseline entry isn't "sha256 mismatch"."""
    await _run_install(tmp_path, monkeypatch)
    bundle = tmp_path / "bundles" / "kolsch"
    for rel in ("tools/bash.py", "tools/read_file.py"):
        path = bundle / rel
        path.write_text(path.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
    baseline = tmp_path / ".kolsch" / "bundle-baseline.json"
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    del payload["files"]["tools/read_file.py"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    out = io.StringIO()
    rc = await cmd_doctor(argparse.Namespace(), out=out, root=tmp_path)
    text = out.getvalue()
    assert rc == 1, text
    mismatch, _, rest = text.partition(
        "Modified — differs from source, no baseline entry"
    )
    assert "  ~ tools/bash.py" in mismatch
    assert "tools/read_file.py" not in mismatch
    assert "  ? tools/read_file.py" in rest
    assert "run `kolsch setup --sync-bundle` interactively" in text
