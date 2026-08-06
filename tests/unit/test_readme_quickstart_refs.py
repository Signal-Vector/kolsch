"""pub-003 — README quickstart agent refs must actually resolve.

Guards against the drift that motivated this test: the README's
one-shot example read `kolsch run starter/agents/helper`, but the
starter bundle installs under the `kolsch/` namespace — there is no
`starter/` bundle, so the copy-pasted command would fail on a fresh
`kolsch setup`. This scans every fenced ```bash block in README.md
for `kolsch run <ref>` / `kolsch chat <ref>` invocations and checks
each ref is well-formed and, when it names the `kolsch` bundle,
actually present in the packaged starter bundle template.
"""

from __future__ import annotations

import re
from pathlib import Path

from assets.refs import is_three_segment, parse_three_segment

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_STARTER_BUNDLE_DIR = _REPO_ROOT / "src" / "cli" / "_starter_bundle" / "kolsch"

# Matches `kolsch run <ref>` / `kolsch chat <ref>` at the start of a
# shell line inside a fenced bash block — the ref is the first
# whitespace-delimited token after the subcommand.
_INVOCATION_RE = re.compile(r"^kolsch (?:run|chat) (\S+)", re.MULTILINE)


def _bash_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    lines = markdown.splitlines()
    in_block = False
    current: list[str] = []
    for line in lines:
        if line.strip() == "```bash":
            in_block = True
            current = []
            continue
        if line.strip() == "```" and in_block:
            in_block = False
            blocks.append("\n".join(current))
            continue
        if in_block:
            current.append(line)
    return blocks


def _quickstart_refs() -> list[str]:
    markdown = _README.read_text(encoding="utf-8")
    refs: list[str] = []
    for block in _bash_blocks(markdown):
        refs.extend(_INVOCATION_RE.findall(block))
    return refs


def test_readme_has_at_least_one_quickstart_ref() -> None:
    """A regex silently matching nothing would defeat this whole test."""
    assert _quickstart_refs(), (
        "no `kolsch run`/`kolsch chat` invocations found in README.md's "
        "fenced bash blocks — did the quickstart move or get reworded?"
    )


def test_readme_quickstart_refs_are_well_formed() -> None:
    for ref in _quickstart_refs():
        assert is_three_segment(ref), (
            f"README quickstart ref {ref!r} is not a canonical "
            "<bundle>/<agents|workflows|...>/<name> ref"
        )


def test_readme_quickstart_refs_exist_in_starter_bundle() -> None:
    """Every quickstart ref names the shipped starter agent.

    The README's quickstart only ever exercises `kolsch setup`'s
    packaged starter agent — there is no other bundle a fresh
    workspace could resolve. A ref naming any other bundle (e.g. the
    `starter/` typo this test was written to catch) is exactly the
    class of bug this guards: assert the bundle segment itself,
    don't just skip refs that get it wrong.
    """
    for ref in _quickstart_refs():
        bundle, segment, name = parse_three_segment(ref)
        assert bundle == "kolsch", (
            f"README quickstart ref {ref!r} names bundle {bundle!r}, but "
            "kolsch setup only ships the 'kolsch' starter bundle — a fresh "
            "workspace cannot resolve this ref"
        )
        asset_path = _STARTER_BUNDLE_DIR / segment / f"{name}.yaml"
        assert asset_path.exists(), (
            f"README quickstart ref {ref!r} points at {asset_path}, which "
            "the packaged starter bundle does not ship"
        )
