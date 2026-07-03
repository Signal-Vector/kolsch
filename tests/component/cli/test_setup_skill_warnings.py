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
import io
from typing import TYPE_CHECKING

import yaml

from cli._setup import NonInteractivePrompter, _SetupIO, run_setup

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def _noop_run_handler(_args: argparse.Namespace) -> tuple[int, str | None]:
    """``run_handler`` stand-in — Step E uses it for the test exchange.

    Returns ``(0, None)`` so Step E records SUCCESS and the success
    banner fires, mirroring what the existing setup tests use when
    they don't need to exercise the real engine path."""
    return 0, None


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
        connect_io_factory=lambda _root, _provider, _api_key: None,
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
