"""qol-133 — direct unit tests for the trailing-comment-block primitive.

Covers ``kolsch._yamlio.extract_trailing_comment_block`` and
``dump_preserving_trailing_comment_block`` in isolation. These back
every ``platform.yaml`` write site that reads the file as a plain
dict, mutates it, and writes it back (``cli._setup``'s migration
helpers, ``cli.llm_commands._write_platform_yaml_atomic`` — shared by
``cmd_connect``/``cmd_disconnect``/``cmd_refresh``; the rules writer
``cli.permissions.mutate_platform_rules`` uses full round-trip
instead since bug-162) so a trailing, purely-decorative
comment block (e.g. the fresh-install ``hooks:`` scaffold) survives
every rewrite instead of being silently dropped by plain
``dump()``'s lack of comment-preservation.

Prior to this file, only indirect integration coverage existed via
``tests/component/test_setup.py``'s scaffold-survival tests — this
file exercises the primitive itself, including an adversarial case a
reviewer found: a REAL, live multi-line scalar (e.g. a ``hooks:``
rule's ``command: |`` field) whose last physical line happens to
start with ``#`` must NOT be misattributed as a decorative trailing
block, since it's glued directly to live content with no blank-line
separator — the hallmark that distinguishes an intentionally
decorative block from a coincidentally comment-shaped line inside
real YAML.
"""

from __future__ import annotations

from kolsch._yamlio import (
    dump,
    dump_preserving_trailing_comment_block,
    extract_trailing_comment_block,
    load,
)

# ===========================================================================
# extract_trailing_comment_block — legitimate decorative blocks
# ===========================================================================


def test_extract_returns_none_when_no_trailing_comment() -> None:
    """The last line is live YAML — nothing to extract."""
    text = "secrets:\n  variant: dotenv\n"
    assert extract_trailing_comment_block(text) is None


def test_extract_finds_block_after_single_blank_separator() -> None:
    """The common shape every write site produces:
    `dump(payload) + "\\n" + block` — one blank line, then comments."""
    text = (
        "secrets:\n"
        "  variant: dotenv\n"
        "\n"
        "# Example: notify yourself\n"
        "# hooks:\n"
        '#   - event: "x"\n'
    )
    assert extract_trailing_comment_block(text) == (
        '# Example: notify yourself\n# hooks:\n#   - event: "x"\n'
    )


def test_extract_preserves_blank_line_embedded_inside_the_block() -> None:
    """A blank line INSIDE the decorative block (not just at its
    boundary) is legitimate content of the block and must be
    preserved — only the boundary between live content and the
    block's first line is load-bearing, not blanks further in."""
    text = (
        "secrets:\n"
        "  variant: dotenv\n"
        "\n"
        "# Example: notify yourself\n"
        "\n"
        "# hooks:\n"
        '#   - event: "x"\n'
    )
    assert extract_trailing_comment_block(text) == (
        '# Example: notify yourself\n\n# hooks:\n#   - event: "x"\n'
    )


def test_extract_none_when_trailing_run_is_only_blank_lines() -> None:
    """Trailing blank lines with no `#` content anywhere in the run —
    nothing decorative to preserve."""
    text = "secrets:\n  variant: dotenv\n\n\n"
    assert extract_trailing_comment_block(text) is None


def test_extract_none_when_entire_file_is_comments() -> None:
    """No live content precedes the run at all — not a "trailing
    block after content" case."""
    text = "# just a comment\n# another\n"
    assert extract_trailing_comment_block(text) is None


def test_extract_none_for_empty_string() -> None:
    assert extract_trailing_comment_block("") is None


def test_extract_round_trips_the_real_hooks_example_block() -> None:
    """The exact block `cli._setup._HOOKS_EXAMPLE_BLOCK` ships —
    round-tripped byte-for-byte through extraction."""
    hooks_example_block = (
        "# Example: notify yourself when kolsch is about to ask for approval.\n"
        "# Uncomment and edit `command` for your platform (macOS shown below;\n"
        "# Linux: notify-send, or any shell command you'd run by hand).\n"
        "# See docs/how-to/event-hooks.md for the full guide.\n"
        "#\n"
        "# hooks:\n"
        '#   - event: "kolsch.prompt.requested"\n'
        '#     matcher: "*"\n'
        '#     command: "terminal-notifier -title kolsch -message '
        "'waiting on your approval'\"\n"
        "#     timeout_seconds: 10\n"
    )
    origin_text = dump({"a": 1}) + "\n" + hooks_example_block
    assert extract_trailing_comment_block(origin_text) == hooks_example_block


# ===========================================================================
# extract_trailing_comment_block — the adversarial multi-line-scalar case
# ===========================================================================


def test_extract_none_when_comment_shaped_line_is_real_scalar_tail() -> None:
    """Reviewer-found gap: a REAL, live `hooks:` rule whose `command:
    |` block scalar's last physical line happens to start with `#`
    (e.g. a shell script ending in `echo '# done'`) must NOT be
    misattributed as a decorative trailing block — it's glued
    directly onto live YAML with no blank-line separator, which is
    exactly what distinguishes it from an intentional scaffold."""
    text = (
        "hooks:\n"
        '  - event: "e"\n'
        '    matcher: "*"\n'
        "    command: |-\n"
        "      real line\n"
        "      # not a comment, this is literally printed by echo\n"
    )
    assert extract_trailing_comment_block(text) is None

    # The scalar's PARSED value is unaffected either way — comments
    # inside a block scalar are literal content, not YAML syntax.
    # This assertion documents that the fix is about not
    # misappropriating a duplicate fragment after EOF, not about a
    # parsing correctness issue.
    parsed = load(text)
    assert parsed["hooks"][0]["command"] == (
        "real line\n# not a comment, this is literally printed by echo"
    )


def test_extract_treats_comment_shaped_scalar_tail_as_block_when_blank_separated() -> (
    None
):
    """Edge of the heuristic, by design: if a blank line genuinely
    separates real content from a trailing `#`-shaped run, it's
    treated as decorative — this is indistinguishable from a real
    scaffold by any purely textual heuristic, and every real write
    site always inserts exactly this separator, so the tradeoff costs
    nothing in practice."""
    text = (
        "hooks:\n"
        '  - event: "e"\n'
        "    command: |-\n"
        "      real line\n"
        "\n"
        "# hooks:\n"
        '#   - event: "x"\n'
    )
    assert extract_trailing_comment_block(text) == ('# hooks:\n#   - event: "x"\n')


# ===========================================================================
# dump_preserving_trailing_comment_block
# ===========================================================================


def test_dump_preserving_appends_trailing_block_from_previous_text() -> None:
    previous_text = (
        'secrets:\n  variant: dotenv\n\n# Example\n# hooks:\n#   - event: "x"\n'
    )
    new_text = dump_preserving_trailing_comment_block(
        previous_text, {"secrets": {"variant": "dotenv"}, "extra": 1}
    )
    assert new_text == (
        'secrets:\n  variant: dotenv\nextra: 1\n\n# Example\n# hooks:\n#   - event: "x"\n'
    )
    parsed = load(new_text)
    assert "hooks" not in parsed
    assert parsed["extra"] == 1


def test_dump_preserving_none_previous_text_behaves_like_plain_dump() -> None:
    data = {"a": 1}
    assert dump_preserving_trailing_comment_block(None, data) == dump(data)


def test_dump_preserving_empty_previous_text_behaves_like_plain_dump() -> None:
    data = {"a": 1}
    assert dump_preserving_trailing_comment_block("", data) == dump(data)


def test_dump_preserving_no_op_when_previous_text_has_no_trailing_block() -> None:
    previous_text = "secrets:\n  variant: dotenv\n"
    data = {"secrets": {"variant": "dotenv"}, "extra": 1}
    assert dump_preserving_trailing_comment_block(previous_text, data) == dump(data)


def test_dump_preserving_does_not_resurrect_adversarial_scalar_tail() -> None:
    """The reviewer's exact repro run through the write helper: a
    real `hooks:` rule's block-scalar tail must not be duplicated as
    an orphaned fragment after a rewrite."""
    previous_text = (
        "hooks:\n"
        '  - event: "e"\n'
        '    matcher: "*"\n'
        "    command: |-\n"
        "      real line\n"
        "      # not a comment, this is literally printed by echo\n"
    )
    payload = load(previous_text)
    payload["extra"] = 1
    new_text = dump_preserving_trailing_comment_block(previous_text, payload)

    # No orphaned duplicate fragment past the real, re-dumped content.
    assert extract_trailing_comment_block(new_text) is None
    reparsed = load(new_text)
    assert reparsed["hooks"][0]["command"] == (
        "real line\n# not a comment, this is literally printed by echo"
    )
    assert reparsed["extra"] == 1


def test_dump_preserving_idempotent_across_chained_rewrites() -> None:
    """Multiple rewrites in a row (mirrors Step B -> C -> D -> several
    `_maybe_*` migration helpers all firing against the same
    workspace in one `kolsch setup` run) must not drift or
    duplicate the block."""
    hooks_example_block = (
        "# Example: notify yourself when kolsch is about to ask for approval.\n"
        "# See docs/how-to/event-hooks.md for the full guide.\n"
        "#\n"
        "# hooks:\n"
        '#   - event: "kolsch.prompt.requested"\n'
        "#     timeout_seconds: 10\n"
    )
    payload: dict[str, object] = {"a": 1}
    text = dump(payload) + "\n" + hooks_example_block

    for i in range(5):
        payload[f"k{i}"] = i
        text = dump_preserving_trailing_comment_block(text, payload)

    assert extract_trailing_comment_block(text) == hooks_example_block
    parsed = load(text)
    assert "hooks" not in parsed
    assert parsed == {"a": 1, "k0": 0, "k1": 1, "k2": 2, "k3": 3, "k4": 4}
