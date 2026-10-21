"""Setup re-run with a broken skill — Step 6 migration hook.

Pins the warning-only contract the build prompt calls out:

- A SKILL.md that fails validation surfaces a one-line warning
  inside the existing setup output, and re-run completes with
  exit 0 (the user might be mid-editing a skill — setup must
  not be the thing that blocks them).
- A re-run with all-clean skills prints a positive "validated N"
  line, again exit 0.
- A re-run with no skills at all is silent on the validation
  front — no noise on fresh workspaces.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from typing import TYPE_CHECKING

import yaml

from cli import main
from cli._setup import NonInteractivePrompter, _SetupIO, _TestRunResult, run_setup

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def _noop_run_handler(_args: argparse.Namespace) -> _TestRunResult:
    """``run_handler`` stand-in — Step E uses it for the test exchange.

    Returns ``_TestRunResult(rc=0)`` so Step E records SUCCESS and the success
    banner fires, mirroring what the existing setup tests use when
    they don't need to exercise the real engine path."""
    return _TestRunResult(rc=0)


def _make_test_io(root: Path) -> _SetupIO:
    """Minimal _SetupIO for the validation-hook tests.

    Doesn't need the connect-IO factory (no provider wiring in
    these tests) — passes ``None`` for the factory and a no-op
    run_handler so Step E + Step C are quiet stand-ins."""
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=lambda _root, _provider, _api_key: None,  # type: ignore[return-value]  # no LLM path exercised
        run_handler=_noop_run_handler,
    )


def _make_broken_skill(root: Path) -> None:
    """Write a SKILL.md missing the required ``description`` field
    under the kolsch bundle that setup creates."""
    skill_dir = root / "bundles" / "kolsch" / "skills" / "incomplete"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: incomplete\n---\n\n# incomplete\n\nBody is here.\n",
        encoding="utf-8",
    )


def _make_clean_skill(root: Path) -> None:
    skill_dir = root / "bundles" / "kolsch" / "skills" / "ok-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "name": "ok-skill",
                "description": "Triggers on cues; well-formed.",
            },
            sort_keys=False,
        )
        + "---\n\n# ok-skill\n\nUseful body content.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_setup_rerun_with_broken_skill_warns_and_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline contract: a broken SKILL.md surfaces as a setup-
    time warning on re-run, but setup itself still completes (exit
    0). The build prompt is explicit about this — a user mid-editing
    a skill should not be locked out of finishing setup."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    # First run — initializes the workspace.
    first_io = _make_test_io(tmp_path)
    first_rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        first_io,
    )
    assert first_rc == 0

    # Author a broken skill BETWEEN the two runs (simulating the
    # user mid-edit case).
    _make_broken_skill(tmp_path)

    # Re-run — should warn, NOT block.
    second_io = _make_test_io(tmp_path)
    second_rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        second_io,
    )
    assert second_rc == 0, (
        "Setup MUST complete even when a SKILL.md fails validation "
        "(Step 6 design: a mid-edit skill should not block setup)."
    )

    captured = second_io.out.getvalue()
    # Warning surface — the count + the broken ref + the actionable
    # hint pointing at the standalone CLI verb.
    assert "skill validation:" in captured
    assert "kolsch/skills/incomplete" in captured
    assert "missing-description" in captured
    assert "`kolsch skills validate`" in captured


async def test_setup_rerun_with_clean_skill_prints_validated_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive-confirmation surface: when every catalogued
    skill passes, setup prints a ``✓ validated N skill(s)`` line.
    Gives the user visible feedback that the hook ran and was
    silent for a reason.

    Phase 2.4 Step 9 — the kolsch starter bundle now ships 3
    canonical skills, so a fresh workspace + one user-added clean
    skill totals 4. The assertion checks the validated-count line
    is present and reads as a plural "skill(s)" surface.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    # Bootstrap, then author one clean skill, then re-run.
    first_io = _make_test_io(tmp_path)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        first_io,
    )
    _make_clean_skill(tmp_path)

    second_io = _make_test_io(tmp_path)
    rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        second_io,
    )
    assert rc == 0

    captured = second_io.out.getvalue()
    assert "✓ validated " in captured and "skill" in captured
    # No warning line when everything is clean.
    assert "⚠ skill validation" not in captured


async def test_setup_rerun_with_only_canonical_skills_prints_validated_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2.4 Step 9 — the kolsch starter bundle now ships
    canonical skills (3 today). A workspace with NO user-authored
    skills still has those, so the validation hook prints the
    positive ``✓ validated N skill(s)`` line rather than being
    silent. Before Step 9 this case was silent (no skills in the
    catalog at all); the regression test name is updated to reflect
    the new baseline.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    first_io = _make_test_io(tmp_path)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        first_io,
    )

    second_io = _make_test_io(tmp_path)
    rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        second_io,
    )
    assert rc == 0

    captured = second_io.out.getvalue()
    assert "✓ validated " in captured and "skill" in captured
    # All canonical skills clean — no warning surface.
    assert "⚠ skill validation" not in captured


async def test_setup_rerun_resolves_bundles_refs_from_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-149 — the setup hook passes the workspace root, so a
    ``bundles/…`` body ref that exists is clean and one that doesn't
    warns ``missing-ref:bundles/…``."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_test_io(tmp_path),
    )
    skill_dir = tmp_path / "bundles" / "kolsch" / "skills" / "ws-ref"
    (skill_dir / "scripts").mkdir(parents=True)
    script = skill_dir / "scripts" / "run.py"
    script.write_text("# stub\n", encoding="utf-8")
    ref = "bundles/kolsch/skills/ws-ref/scripts/run.py"
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ws-ref\ndescription: Runs a helper.\n---\n\n"
        f"From the workspace root run `{ref}`.\n",
        encoding="utf-8",
    )

    clean_io = _make_test_io(tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path), clean_io
        )
        == 0
    )
    assert "⚠ skill validation" not in clean_io.out.getvalue()

    script.unlink()
    broken_io = _make_test_io(tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path), broken_io
        )
        == 0
    )
    assert f"kolsch/skills/ws-ref: missing-ref:{ref}" in broken_io.out.getvalue()


async def test_setup_rerun_shows_frontmatter_detail_in_parentheses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bug-149 — frontmatter codes carry their detail on the setup
    advisory line; other codes stay compact."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_test_io(tmp_path),
    )
    skills = tmp_path / "bundles" / "kolsch" / "skills"
    (skills / "bad-yaml").mkdir(parents=True)
    (skills / "bad-yaml" / "SKILL.md").write_text(
        '---\nname: bad-yaml\ndescription: Use it. Triggers on: "x".\n---\n\n'
        "Run `scripts/gone.py`.\n",
        encoding="utf-8",
    )
    (skills / "no-fm").mkdir()
    (skills / "no-fm" / "SKILL.md").write_text("# Skill\n\nBody.\n", encoding="utf-8")

    io_ = _make_test_io(tmp_path)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
        == 0
    )
    out = io_.out.getvalue()
    assert (
        "kolsch/skills/bad-yaml: frontmatter-unparseable (SKILL.md line 3, column "
    ) in out
    assert "must be quoted), missing-ref:scripts/gone.py\n" in out
    assert (
        "kolsch/skills/no-fm: frontmatter-missing (no YAML frontmatter; "
        "SKILL.md must start with a `---` line)\n"
    ) in out


def test_starter_bundle_skills_validate_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """bug-149 — every skill the starter bundle ships validates clean
    in a freshly set-up workspace (guards the ref rule against shipped
    content, e.g. a ``bundles/…`` mention)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    # Sync test: ``cli.main`` runs its own event loop.
    setup_rc = asyncio.run(
        run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            _make_test_io(tmp_path),
        )
    )
    assert setup_rc == 0

    exit_code = main(["--root", str(tmp_path), "skills", "validate", "--json"])
    rows = json.loads(capsys.readouterr().out)
    starter = [r for r in rows if r["ref"].startswith("kolsch/skills/")]
    assert starter, "the starter bundle ships skills"
    assert [r for r in starter if not r["ok"]] == []
    assert exit_code == 0
