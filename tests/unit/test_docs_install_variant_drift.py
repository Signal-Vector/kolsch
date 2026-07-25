"""qol-090 Test 7 — docs install-variant consolidation.

Guards against the drift that surfaced this qol: the
`uv tool install 'kolsch[browse]'` command was scattered across five
places (`pyproject.toml` + four docs files), each subtly different,
and none mentioned the source-run variant. qol-090 consolidated to
one include at `docs/_includes/browse-install.md`; every other doc
page references it.

If a new page needs to explain the install, it MUST include the
snippet — not re-inline the command. This grep test fails when a
page adds a fresh inline variant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_PATHS = {
    _REPO_ROOT / "docs" / "_includes" / "browse-install.md",
    _REPO_ROOT / "pyproject.toml",
}


def test_no_inline_install_variant_drift() -> None:
    """`uv tool install 'kolsch[browse]'` appears only in the include + pyproject."""
    # grep -rn --binary-files=without-match is the simplest, most portable
    # way to enumerate matches with paths — subprocess keeps this test
    # independent of any project-wide fixture.
    result = subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.md",
            "--include=*.toml",
            "uv tool install 'kolsch[browse]'",
            str(_REPO_ROOT / "docs"),
            str(_REPO_ROOT / "pyproject.toml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # grep exits 1 on no matches; 0 on matches; 2 on error.
    assert result.returncode in {0, 1}, f"grep errored: {result.stderr}"

    hit_paths: set[Path] = set()
    for line in result.stdout.splitlines():
        # `<path>:<lineno>:<content>` — take the path.
        path_str = line.split(":", 1)[0]
        hit_paths.add(Path(path_str).resolve())

    stray = hit_paths - _ALLOWED_PATHS
    assert not stray, (
        f"`uv tool install 'kolsch[browse]'` appears in {sorted(stray)} — "
        "include `docs/_includes/browse-install.md` instead of re-inlining."
    )
