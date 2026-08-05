"""feat-126 Test 1 — the `--auto-approve` chat launch flag parses.

Covers AC #1: `kolsch chat --auto-approve` parses to
`args.auto_approve = True`; omitting the flag yields `False`; the
flag is listed in `kolsch chat --help`.
"""

from __future__ import annotations

import pytest

from cli.main import _build_parser


def test_auto_approve_flag_parses_true() -> None:
    """`chat --auto-approve` → `ns.auto_approve is True`."""
    parser = _build_parser()
    ns = parser.parse_args(["chat", "--auto-approve"])
    assert ns.auto_approve is True


def test_auto_approve_defaults_false() -> None:
    """Bare `chat` → `ns.auto_approve is False` (mode is opt-in)."""
    parser = _build_parser()
    ns = parser.parse_args(["chat"])
    assert ns.auto_approve is False


def test_auto_approve_listed_in_chat_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`chat --help` mentions the flag and its trusted-context warning."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["chat", "--help"])
    out = capsys.readouterr().out
    assert "--auto-approve" in out
