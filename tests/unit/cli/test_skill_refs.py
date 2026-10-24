"""bug-149 — candidate extraction for the skill-body ref rule (steps 1-3).

Pure string tests for ``cli._skill_refs._iter_candidates``; resolution
against the filesystem (steps 4-5) lives in
``tests/component/cli/test_skills_validate.py``.
"""

from __future__ import annotations

import pytest

from cli._skill_refs import _iter_candidates, _RefCandidate


def _texts(body: str) -> list[str]:
    return [c.text for c in _iter_candidates(body)]


@pytest.mark.parametrize(
    "token",
    [
        "<hr/>",
        "<br/>",
        "app.example.so/<slug>/…",
        "https://x.io/a/b",
        "resume-<slug>.yaml",
        "out/{company}_{role}_{date}.*",
        "data/*.yaml",
        "~/Documents/x/INDEX.md",
        "/etc/hosts/x",
        "utils/foo()",
        "$HOME/x/y",
        "a b/c",
        "run_thing",
    ],
)
def test_non_path_code_spans_are_not_candidates(token: str) -> None:
    assert _texts(f"Some prose with `{token}` in it.\n") == []


def test_path_shaped_code_spans_are_candidates_and_dot_slash_stripped() -> None:
    body = "Run `scripts/extract.py`, read `./references/methodology.md`.\n"
    assert _iter_candidates(body) == [
        _RefCandidate(
            text="scripts/extract.py", kind="span", path="scripts/extract.py"
        ),
        _RefCandidate(
            text="references/methodology.md",
            kind="span",
            path="references/methodology.md",
        ),
    ]


def test_link_targets_are_candidates_without_fragment_or_query() -> None:
    body = (
        "See [the forms](FORMS.md#section) and [ref](./references/a.md?x=1).\n"
        'Also ![logo](assets/logo.png "Logo") and [site](https://x.io/a).\n'
        "And [anchor only](#top).\n"
    )
    assert _iter_candidates(body) == [
        _RefCandidate(text="FORMS.md", kind="link", path="FORMS.md"),
        _RefCandidate(text="references/a.md", kind="link", path="references/a.md"),
        _RefCandidate(text="assets/logo.png", kind="link", path="assets/logo.png"),
    ]


def test_fenced_code_blocks_are_not_scanned() -> None:
    body = (
        "Before `scripts/a.py`.\n"
        "```bash\n"
        "python `scripts/in_fence.py`\n"
        "see [x](references/in_fence.md)\n"
        "```\n"
        "~~~~\n"
        "`scripts/tilde_fence.py`\n"
        "~~~\n"
        "`scripts/still_in_fence.py` (a shorter closer does not close)\n"
        "~~~~\n"
        "After `scripts/b.py`.\n"
    )
    assert _texts(body) == ["scripts/a.py", "scripts/b.py"]


def test_link_inside_code_span_is_not_a_link() -> None:
    assert _texts("Write `[x](FORMS.md)` literally.\n") == []


def test_repeats_are_kept_for_the_resolver_to_dedupe() -> None:
    assert _texts("`scripts/a.py` `scripts/a.py`\n") == ["scripts/a.py"] * 2


def test_link_targets_are_percent_decoded_for_resolution_only() -> None:
    assert _iter_candidates("[x](references/with%20space.md)\n") == [
        _RefCandidate(
            text="references/with%20space.md",
            kind="link",
            path="references/with space.md",
        ),
    ]
    # Code spans are literal text, never decoded.
    assert _iter_candidates("`scripts/a%20b.py`\n")[0].path == "scripts/a%20b.py"
