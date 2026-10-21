"""``kolsch skills validate`` — Step 6 SKILL.md linting.

Five cases per the build prompt:

- Valid skill → `[OK]` line, exit 0.
- Missing description → `[FAIL]` with the `missing-description` code.
- Body empty → `[FAIL]` with `body-empty`.
- Broken backticked script ref → `[FAIL]` naming the missing path.
- `--json` shape — structured per-skill rows + exit 1 on any fail.

Plus coverage for: targeted single-ref validation, non-skill ref
rejection, the "no skills to validate" friendly line, and the
``--all`` flag as the explicit form of the bare invocation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from cli import main

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Workspace fixture.
# ---------------------------------------------------------------------------


def _platform_yaml(root: Path) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


def _make_bundle(root: Path, name: str) -> Path:
    bundle = root / "bundles" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1", "description": "x"}),
        encoding="utf-8",
    )
    return bundle


def _write_skill_md(
    root: Path,
    bundle: str,
    name: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    body: str = "# A skill\n\nUseful instructions go here.\n",
    raw_frontmatter: str | None = None,
) -> Path:
    skill_dir = root / "bundles" / bundle / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    if raw_frontmatter is not None:
        fm_block = raw_frontmatter
    else:
        fm = (
            frontmatter
            if frontmatter is not None
            else {
                "name": name,
                "description": "A short description that triggers the skill.",
            }
        )
        fm_block = yaml.safe_dump(fm, sort_keys=False)
    skill_md.write_text(f"---\n{fm_block}---\n\n{body}", encoding="utf-8")
    return skill_md


# ---------------------------------------------------------------------------
# Valid + invalid cases — single-skill validation.
# ---------------------------------------------------------------------------


def test_validate_valid_skill_reports_ok_exit_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A well-formed skill: ``[OK] <ref>`` + exit 0."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(tmp_path, "acme", "good")

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "[OK]   acme/skills/good" in out
    assert "[FAIL]" not in out


def test_validate_missing_description_reports_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A skill whose frontmatter has no ``description`` fires
    ``missing-description``. Description is the model's discovery
    signal per spec §2.3 — its absence is load-bearing, hence a
    hard fail (not just a warning)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(
        tmp_path,
        "acme",
        "no-desc",
        frontmatter={"name": "no-desc"},  # description absent
    )

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 1

    out = capsys.readouterr().out
    assert "[FAIL] acme/skills/no-desc" in out
    assert "missing required `description`" in out


def test_validate_empty_body_reports_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A SKILL.md with only frontmatter (no body) fires
    ``body-empty``. The body IS the skill's content — frontmatter
    alone is just metadata, not a usable skill."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(tmp_path, "acme", "hollow", body="")

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 1

    out = capsys.readouterr().out
    assert "[FAIL] acme/skills/hollow" in out
    assert "body is empty" in out


def test_validate_broken_script_ref_reports_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A backticked path in the body that doesn't resolve under the
    skill's directory fires ``missing-ref:<path>``. Catches skill
    authors who refactored a scripts/ tree without updating the
    body's instructions."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(
        tmp_path,
        "acme",
        "refs",
        body=(
            "# Refs test\n\n"
            "Use the helper at `scripts/extract.py` to pull citations.\n"
            "See `references/methodology.md` for the workflow.\n"
        ),
    )

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 1

    out = capsys.readouterr().out
    assert "[FAIL] acme/skills/refs" in out
    assert "scripts/extract.py" in out
    assert "references/methodology.md" in out


def test_validate_resolved_script_ref_does_not_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the backticked path DOES exist under the skill dir, no
    error fires — the heuristic isn't blindly noisy."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    skill_md = _write_skill_md(
        tmp_path,
        "acme",
        "withref",
        body="See `scripts/extract.py` for the helper.\n",
    )
    (skill_md.parent / "scripts").mkdir()
    (skill_md.parent / "scripts" / "extract.py").write_text(
        "# stub helper\n", encoding="utf-8"
    )

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 0
    assert "[OK]   acme/skills/withref" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# JSON output.
# ---------------------------------------------------------------------------


def test_validate_json_structures_per_skill_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` returns a list with one object per skill carrying
    ``ref``, ``ok``, and ``errors`` (list of stable codes — JSON
    consumers can match on those). Exit 1 when any row fails."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(tmp_path, "acme", "good")
    _write_skill_md(
        tmp_path,
        "acme",
        "broken",
        frontmatter={"name": "broken"},  # missing description
    )

    exit_code = main(["--root", str(tmp_path), "skills", "validate", "--json"])
    assert exit_code == 1

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    by_ref = {row["ref"]: row for row in payload}
    assert by_ref["acme/skills/good"]["ok"] is True
    assert by_ref["acme/skills/good"]["errors"] == []
    assert by_ref["acme/skills/broken"]["ok"] is False
    assert "missing-description" in by_ref["acme/skills/broken"]["errors"]


# ---------------------------------------------------------------------------
# Targeted + edge cases.
# ---------------------------------------------------------------------------


def test_validate_specific_ref_only_checks_that_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``kolsch skills validate <ref>`` checks ONLY that skill —
    handy for tightening a CI gate around one new skill while
    others are still in flux."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(tmp_path, "acme", "good")
    _write_skill_md(
        tmp_path,
        "acme",
        "broken",
        frontmatter={"name": "broken"},
    )

    exit_code = main(
        ["--root", str(tmp_path), "skills", "validate", "acme/skills/good"],
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "[OK]   acme/skills/good" in out
    # Only the targeted skill is reported.
    assert "acme/skills/broken" not in out


def test_validate_unknown_ref_friendly_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Targeted validation with a ref that's not in the catalog
    exits 1 with a friendly stderr (distinct from a validation
    failure — this is a usage error)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "skills",
            "validate",
            "acme/skills/does-not-exist",
        ],
    )
    assert exit_code == 1
    assert "not in catalog" in capsys.readouterr().err


def test_validate_empty_catalog_is_clean_exit_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Workspace with no skills: friendly line, exit 0. Validation
    of an empty set is by definition clean."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")

    exit_code = main(["--root", str(tmp_path), "skills", "validate"])
    assert exit_code == 0
    assert "no skills to validate" in capsys.readouterr().out


def test_validate_all_flag_equivalent_to_bare(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--all`` is documented sugar for the bare invocation — make
    sure they actually behave the same."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(tmp_path, "acme", "good")

    exit_code = main(["--root", str(tmp_path), "skills", "validate", "--all"])
    assert exit_code == 0
    assert "[OK]   acme/skills/good" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# bug-149 — the skill-body ref rule (cli._skill_refs).
# ---------------------------------------------------------------------------


def _validate_json(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> list[dict[str, Any]]:
    main(["--root", str(root), "skills", "validate", "--json"])
    rows: list[dict[str, Any]] = json.loads(capsys.readouterr().out)
    return rows


def test_validate_ref_rule_skips_non_refs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tokens that are HTML, hosts, URLs, placeholders, globs, home or
    absolute paths, code, unanchored prose paths, or inside a fenced
    block never produce ``missing-ref``."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(
        tmp_path,
        "acme",
        "noisy",
        body=(
            "# Noisy\n\n"
            "Separate with `<hr/>` or `<br/>`.\n"
            "Open `app.example.so/<slug>/…` or `docs.example.com/guide`.\n"
            "Fetch `https://x.io/a/b`.\n"
            "Write `resume-<slug>.yaml` to `out/{company}_{role}_{date}.*`.\n"
            "Read every `data/*.yaml` and `~/Documents/x/INDEX.md`.\n"
            "Not `/etc/hosts/x` and not `utils/foo()`.\n"
            "If you pass `resumes/resume-acme.yaml` the PDF lands next to it.\n"
            "```bash\n"
            "python `scripts/only_in_fence.py`\n"
            "```\n"
        ),
    )
    assert _validate_json(tmp_path, capsys) == [
        {"ref": "acme/skills/noisy", "ok": True, "errors": []},
    ]


# (body, normalised ref, setup(skill_dir) creating needed parent dirs)
_ANCHORED_REFS: list[tuple[str, str, str | None]] = [
    ("Run `scripts/extract.py`.", "scripts/extract.py", None),
    ("Read `./references/methodology.md`.", "references/methodology.md", None),
    ("Show `assets/logo.png`.", "assets/logo.png", None),
    ("Then `steps/two.md`.", "steps/two.md", "steps"),
    ("See [the forms](FORMS.md).", "FORMS.md", None),
    (
        "From the workspace root run `bundles/acme/skills/anchored/scripts/run.py`.",
        "bundles/acme/skills/anchored/scripts/run.py",
        None,
    ),
]


@pytest.mark.parametrize(("body", "ref", "parent_dir"), _ANCHORED_REFS)
def test_validate_ref_rule_anchored_refs_checked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
    ref: str,
    parent_dir: str | None,
) -> None:
    """Anchored refs fire exactly one ``missing-ref`` when absent and
    none once the target exists (``bundles/…`` from the workspace
    root, everything else from the skill dir)."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    skill_md = _write_skill_md(tmp_path, "acme", "anchored", body=f"# A\n\n{body}\n")
    if parent_dir is not None:
        (skill_md.parent / parent_dir).mkdir()

    assert _validate_json(tmp_path, capsys)[0]["errors"] == [f"missing-ref:{ref}"]

    base = tmp_path if ref.startswith("bundles/") else skill_md.parent
    target = base / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stub\n", encoding="utf-8")
    assert _validate_json(tmp_path, capsys)[0]["errors"] == []


def test_validate_ref_rule_wording_names_the_base(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The table row says which base a missing ref was resolved against."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(
        tmp_path,
        "acme",
        "x",
        body="Run `scripts/a.py` then `bundles/acme/skills/x/scripts/run.py`.\n",
    )
    assert main(["--root", str(tmp_path), "skills", "validate"]) == 1
    out = capsys.readouterr().out
    assert "`scripts/a.py` not found under the skill directory" in out
    assert (
        "`bundles/acme/skills/x/scripts/run.py` not found under the workspace root"
        in out
    )


def test_validate_ref_rule_dedupes_and_skips_escapes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repeated ref is reported once; ``..`` refs escaping the skill
    dir (or ``<root>/bundles`` for the ``bundles/`` form) are skipped."""
    _platform_yaml(tmp_path)
    _make_bundle(tmp_path, "acme")
    _write_skill_md(
        tmp_path,
        "acme",
        "rep",
        body=(
            "Run `scripts/a.py`, then `scripts/a.py`, then [again](scripts/a.py).\n"
            "Not `scripts/../../other/x.py`, [sibling](../other/SKILL.md),\n"
            "or `bundles/../outside.txt`.\n"
        ),
    )
    assert _validate_json(tmp_path, capsys)[0]["errors"] == ["missing-ref:scripts/a.py"]
