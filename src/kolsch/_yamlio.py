"""ruamel.yaml-based load/dump helper — Phase 3.0 Step 0 prereq.

Single chokepoint for YAML serialization across kolsch. Migrating off
PyYAML fixes the Norway-bug (unquoted ``no``/``yes``/``on``/``off``
were coerced to booleans) and unblocks the Phase 3.0 workflow parser's
need for line-info-aware loading (Step 2).

This module is the ONLY site in ``src/`` that imports ``ruamel.yaml``;
every other module imports :func:`load` / :func:`dump` (and, when it
needs to catch parse errors, :class:`YAMLError`) from here via
``from kolsch._yamlio import ...``.

The module lives INSIDE the ``kolsch`` package (rather than at the
``src/`` root) because top-level src-root modules require a
``pyproject.toml`` force-include to land in a built wheel — and that
force-include creates a physical copy at ``site-packages/_yamlio.py``
alongside the editable ``.pth``-added ``src/`` entry. Python's finder
resolves top-level modules from ``site-packages`` before ``.pth``-added
paths, so the shadow copy wins and any drift between the two (e.g. a
new export added to the src tree without a wheel reinstall) surfaces
as ``ImportError`` at CLI startup. See bug-045 (regression guard) and
bug-049 (root fix — the move into ``kolsch``) for the failure-mode
history. Living inside a package that's already in ``[tool.hatch.
build.targets.wheel] packages`` sidesteps the force-include entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.tokens import CommentToken

__all__ = [
    "RoundTripLayout",
    "YAMLError",
    "dump",
    "dump_preserving_trailing_comment_block",
    "dump_round_trip",
    "dump_round_trip_with_layout",
    "extract_trailing_comment_block",
    "load",
    "load_round_trip",
    "load_round_trip_with_layout",
    "load_with_positions",
    "seq_delete_preserving_comments",
    "seq_insert_preserving_comments",
]

# Module-level singleton — cheap to share and sufficient for our
# single-threaded CLI / setup use cases. ``typ='safe'`` mirrors
# PyYAML's safe loader (no arbitrary Python object construction).
# ``default_flow_style = False`` keeps block-style output (matches
# PyYAML's default). ``sort_base_mapping_type_on_output = False``
# preserves insertion order on dump — every prior call site used
# ``sort_keys=False`` explicitly, so the helper bakes that in.
_yaml = YAML(typ="safe")
_yaml.default_flow_style = False
# ruamel.yaml's stub types ``sort_base_mapping_type_on_output`` as
# ``None`` but the runtime accepts plain booleans — the ignore
# narrows scope to that single well-understood stub gap.
_yaml.sort_base_mapping_type_on_output = False  # type: ignore[assignment]


# feat-041 — a second singleton in round-trip mode. `typ='rt'`
# builds `CommentedMap` / `CommentedSeq` nodes that carry a `.lc`
# line-column tracker; the authz introspection features
# (`/why`, inline denial hint) need the source line of each rule to
# emit `<source_file>:<line>` breadcrumbs. Kept SEPARATE from `_yaml`
# so existing `load()` callers don't accidentally start receiving
# `CommentedMap` instances (which mostly quack like dicts but
# `isinstance(x, dict)` in downstream code varies).
_yaml_rt = YAML(typ="rt")
_yaml_rt.default_flow_style = False
_yaml_rt.sort_base_mapping_type_on_output = False  # type: ignore[assignment]


def load(text: str) -> Any:  # noqa: ANN401
    """Parse YAML ``text`` and return the corresponding Python object."""
    return _yaml.load(text)


def load_with_positions(text: str) -> Any:  # noqa: ANN401
    """Parse YAML ``text`` in round-trip mode and preserve source lines.

    Returns a ``CommentedMap``/``CommentedSeq`` tree; each mapping /
    sequence node carries a ``.lc`` (line-column) tracker. For a
    mapping ``m``, ``m.lc.line`` is the 0-indexed line where the
    node's first key begins. For a sequence element ``s[i]``,
    ``s.lc.data[i][0]`` is the 0-indexed line where that element
    starts.

    feat-041 uses this to record ``(source_file, source_line)`` on
    each authz rule so the ``/why`` card and inline denial hint can
    display a ``file:line`` breadcrumb.

    Anchors + merge keys are supported (``.lc.line`` points to the
    use site, which is what the feature wants). Custom ``!tag`` types
    raise :class:`ruamel.yaml.constructor.ConstructorError`; callers
    that anticipate them (config loaders) should catch and fall back
    to positional metadata of ``None``.
    """
    return _yaml_rt.load(text)


def dump(data: Any) -> str:  # noqa: ANN401
    """Serialize ``data`` to a YAML string in block style, key-order preserved."""
    buf = StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


# qol-133 — generic "trailing scaffold" preservation for the plain
# load()/dump() pair.
#
# Several independent call sites across the CLI (``cli._setup``'s
# migration helpers, ``cli.llm_commands``'s ``_write_platform_yaml_
# atomic`` — shared by ``cmd_connect``/``cmd_disconnect``/``cmd_
# refresh`` — and ``cli.permissions``'s ``_write_back``) all follow
# the same "read platform.yaml as a plain dict, mutate it, dump() it
# back" pattern. Plain ``load()``/``dump()`` is safe-mode: it has no
# comment-preservation machinery at all (that's what the separate
# ``load_round_trip``/``dump_round_trip`` pair is for), so every one
# of those call sites silently drops any trailing, fully-commented,
# inert content that isn't live YAML — e.g. qol-133's fresh-install
# ``hooks:`` scaffold example. Migrating every write site to the
# round-trip pair is out of scope (it would require passing
# ``CommentedMap`` payloads through every caller); instead, these two
# helpers let a caller keep using plain dicts while still not
# clobbering a purely-decorative trailing block.
#
# The mechanism is intentionally generic — it does not know about
# ``hooks:`` or any specific scaffold content. It only recognizes
# "the tail of the file, from the first line of a contiguous
# comment/blank-line run that reaches EOF without any more live YAML
# after it". That block, by construction, contributes nothing to
# ``load()``'s parsed result (comments and blank lines are not
# data), so re-appending it verbatim after a fresh ``dump()`` is safe
# for ANY such trailing block, not just this spec's.
def _trailing_block_start_index(lines: list[str]) -> int | None:
    """Return the confirmed decorative block's first line index, or ``None``.

    Two-phase: find the maximal trailing run of blank/``#`` lines,
    then require that run's own first line be blank — that blank
    line is the separator between live content and the block, and is
    excluded from the returned index (the block starts right after
    it). See :func:`extract_trailing_comment_block` for the full
    rationale (the docstring lives there since that's the public
    entry point).
    """
    run_start = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            run_start = idx
            continue
        break

    no_trailing_run = run_start == len(lines)
    whole_file_is_the_run = run_start == 0
    if no_trailing_run or whole_file_is_the_run:
        return None

    separator_present = not lines[run_start].strip()
    if not separator_present:
        # The run's first line is a `#` line glued directly onto live
        # content, no blank between — the adversarial case: a
        # `#`-shaped tail of a real multi-line scalar, not a
        # decorative block.
        return None

    first_comment_idx = run_start + 1
    has_content_after_separator = (
        first_comment_idx < len(lines) and "".join(lines[first_comment_idx:]).strip()
    )
    return first_comment_idx if has_content_after_separator else None


def extract_trailing_comment_block(text: str) -> str | None:
    """Return the trailing all-comment/blank-line block of ``text``, if any.

    Scans lines from the end of ``text`` backwards; as long as each
    line is blank or starts with ``#`` (after stripping leading
    whitespace), it's part of the candidate trailing run. Stops at
    the first line that has live content — that run (start index
    through EOF) is the *candidate* block.

    The candidate is only recognized as a genuine decorative block
    when it is separated from the preceding live content by at least
    one BLANK line — i.e. the line immediately above the candidate's
    first line must itself be blank (after stripping). Without this
    requirement, a `#`-shaped physical line that's actually the tail
    of a real multi-line scalar (e.g. a `hooks:` rule's
    ``command: |`` field whose last line happens to start with ``#``,
    such as a shell script ending in ``echo '# done'``) would be
    misidentified as decorative and get orphaned as a duplicated
    fragment after a rewrite — the scalar's *parsed value* stays
    correct either way (comments inside a block scalar are literal
    content, not YAML syntax), but the fragment would still wrongly
    reappear as junk text past EOF. Every known call site writes a
    real blank-line separator before the scaffold (``dump(payload)``
    followed by a blank line and then the block), so requiring one
    here costs nothing for the legitimate case while closing the
    misattribution.

    A blank line INSIDE the candidate run (not just at its start) is
    fine and stays part of the returned block — only the boundary
    between real content and the run's first line matters.

    Returns ``None`` when the file has no trailing comment block
    (e.g. the last line is real YAML), when the run isn't preceded by
    a blank-line separator (the adversarial case above), when the
    candidate run is only blank lines (nothing worth preserving), or
    when ``text`` is empty.

    The returned string starts at the first ``#`` line of the run
    (the blank separator line itself is not included — callers
    re-add their own separator) and preserves everything verbatim
    through EOF, including the original line endings inside the
    block.

    Known narrow gap (non-blocking, cosmetic only): this heuristic is
    line-level, not indentation-aware. A block scalar containing an
    embedded blank line immediately followed by one more *indented*
    line that strips to a ``#``-prefix, as the very last line of the
    file, can still be misattributed as decorative — the scalar's
    parsed value stays correct (comments inside a block scalar are
    literal content), but a duplicated, harmless junk fragment can
    reappear past EOF after a rewrite. Not reachable via any
    documented hook-writing pattern (`docs/how-to/event-hooks.md`
    steers users toward single-line commands), and none of this
    module's real call sites' generated content can trigger it on
    its own — only a hand-authored, unusually-shaped multi-line
    ``command:`` block could. Flagged here rather than fixed because
    closing it fully would require real indentation-tracking, not a
    line-level heuristic.
    """
    if not text:
        return None
    lines = text.splitlines(keepends=True)
    start = _trailing_block_start_index(lines)
    return None if start is None else "".join(lines[start:])


def dump_preserving_trailing_comment_block(
    previous_text: str | None,
    data: Any,  # noqa: ANN401
) -> str:
    """Like :func:`dump`, but re-appends ``previous_text``'s trailing comment block.

    Re-appends the trailing comment block (if any) after the
    freshly-dumped content.

    Callers that read `platform.yaml` (or any similarly-scaffolded
    file) as a plain dict, mutate it, and write it back via ``dump()``
    should use this instead whenever the *previous* on-disk text is
    available — it makes the previously-described class of bug
    (silently dropping a trailing, purely-decorative comment block on
    any load-dict-dump-write round-trip) structurally impossible at
    every call site that adopts it, rather than requiring each site
    to hand-roll its own detect-and-reappend check.

    ``previous_text`` is the file's content *before* this write (pass
    ``None`` or ``""`` when there is no previous file, e.g. first
    creation — the return value is then identical to ``dump(data)``).
    """
    new_text = dump(data)
    if not previous_text:
        return new_text
    trailing = extract_trailing_comment_block(previous_text)
    if trailing is None:
        return new_text
    return new_text + "\n" + trailing


# Round-trip pair: ``load_round_trip`` + ``dump_round_trip`` preserve
# comments, blank lines, and key order end-to-end. qol-089's tool-refs
# migrator uses these so ``platform.yaml``'s heavy comment structure
# survives a rewrite. Prefer these over the plain ``load``/``dump``
# pair whenever a caller reads a user-authored YAML, mutates it, and
# writes it back — the plain pair is safe-mode and DROPS comments on
# dump.
def load_round_trip(text: str) -> Any:  # noqa: ANN401
    """Parse ``text`` in round-trip mode (comments preserved).

    Alias for :func:`load_with_positions` — the same singleton, named
    for callers whose intent is "read → mutate → dump" rather than
    "read source-line positions". Either name is fine.
    """
    return _yaml_rt.load(text)


def dump_round_trip(data: Any) -> str:  # noqa: ANN401
    """Serialize ``data`` in round-trip mode (comments/blank lines preserved).

    Only meaningful when ``data`` was itself loaded via
    :func:`load_round_trip` / :func:`load_with_positions` — comment and
    blank-line metadata rides on the ``CommentedMap``/``CommentedSeq``
    nodes those loaders return.
    """
    buf = StringIO()
    _yaml_rt.dump(data, buf)
    return buf.getvalue()


# bug-162 — layout-preserving round-trip + single-entry list edits.
#
# `cli.permissions.mutate_platform_rules` inserts or removes ONE entry
# of `authorization.config.rules` and must leave every other line of a
# user-authored `platform.yaml` alone. ruamel keeps comments but not
# indentation (it re-indents with its configured defaults), and it
# stores a comment that sits between two list entries as a post-comment
# on the *previous* entry's last key. The helpers below guess the
# file's indentation and move those between-entry comments so they
# stay above the entry they precede.


@dataclass(frozen=True, slots=True)
class RoundTripLayout:
    """Block indentation of a YAML text, in ruamel's `indent()` terms."""

    mapping: int = 2
    sequence: int = 2
    offset: int = 0


def _guess_layout(text: str) -> RoundTripLayout:
    """Guess mapping / block-sequence indentation from the first examples.

    Same heuristic as `ruamel.yaml.util.load_yaml_guess_indent`, but
    also returns the nested-mapping indent. Falls back to ruamel's
    defaults (2 / 2 / 0) for anything it can't see.
    """
    mapping: int | None = None
    sequence: int | None = None
    offset: int | None = None
    key_indent = 0
    key_only_at: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip(" "))
        if stripped.startswith("- ") and sequence is None:
            rest = line[lead + 1 :]
            content = lead + 1 + (len(rest) - len(rest.lstrip(" ")))
            if not line[content:].startswith("#"):
                offset = lead - key_indent
                sequence = content - key_indent
        elif mapping is None and key_only_at is not None and lead > key_only_at:
            mapping = lead - key_only_at
        if stripped.endswith(":") and not stripped.startswith("- "):
            key_indent = key_only_at = lead
        else:
            key_only_at = None
        if mapping is not None and sequence is not None:
            break
    default = RoundTripLayout()
    seq = sequence if sequence is not None else default.sequence
    off = offset if offset is not None else default.offset
    if off >= seq:  # unrepresentable (dash past content); use defaults
        seq, off = default.sequence, default.offset
    return RoundTripLayout(
        mapping=mapping if mapping is not None else default.mapping,
        sequence=seq,
        offset=off,
    )


def load_round_trip_with_layout(text: str) -> tuple[Any, RoundTripLayout]:
    """Round-trip-load ``text`` and guess its block indentation.

    Pair with :func:`dump_round_trip_with_layout` so an unchanged node
    dumps back with the file's own indentation.
    """
    return _yaml_rt.load(text), _guess_layout(text)


def dump_round_trip_with_layout(data: Any, layout: RoundTripLayout) -> str:  # noqa: ANN401
    """Like :func:`dump_round_trip`, indenting blocks per ``layout``."""
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.sort_base_mapping_type_on_output = False  # type: ignore[assignment]
    yaml.width = 4096  # never re-wrap a user's long lines
    yaml.indent(mapping=layout.mapping, sequence=layout.sequence, offset=layout.offset)
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def _post_comment_slot(item: Any) -> tuple[Any, Any] | None:  # noqa: ANN401
    """Return `(comment_items, last_key)` of a flat mapping list entry.

    The slot holding the comment that follows the entry (inline on its
    last line, then any full-line comments before the next entry).
    `None` for anything but a `CommentedMap` whose last value is a
    scalar (nested values carry the comment deeper — left alone).
    """
    if not isinstance(item, CommentedMap) or not item:
        return None
    last_key = list(item.keys())[-1]
    if isinstance(item[last_key], Mapping | list):
        return None
    return item.ca.items, last_key


def _take_following_comment(item: Any) -> str:  # noqa: ANN401
    """Detach and return the full-line comment block that follows ``item``.

    Keeps ``item``'s own inline comment (on its last line) in place.
    Returns the detached text (each line with its own indentation),
    or ``""`` when there is none.
    """
    slot = _post_comment_slot(item)
    if slot is None:
        return ""
    items, key = slot
    entry = items.get(key)
    token = entry[2] if entry and len(entry) > 2 else None  # noqa: PLR2004
    if token is None:
        return ""
    value: str = token.value
    first, sep, following = value.partition("\n")
    if not sep or not following:
        return ""
    if first.strip():
        token.value = first + "\n"  # keep the inline comment
    else:
        entry[2] = None
    return following


def _append_following_comment(item: Any, following: str) -> bool:  # noqa: ANN401
    """Attach a full-line comment block after ``item``; `False` if impossible."""
    if not following:
        return True
    slot = _post_comment_slot(item)
    if slot is None:
        return False
    items, key = slot
    entry = items.setdefault(key, [None, None, None, None])
    while len(entry) < 3:  # noqa: PLR2004
        entry.append(None)
    token = entry[2]
    if token is None:
        entry[2] = CommentToken("\n" + following, CommentMark(0), None)
    else:
        token.value = token.value + following
    return True


def seq_insert_preserving_comments(
    seq: Any,  # noqa: ANN401 — a ruamel CommentedSeq
    index: int,
    item: Mapping[str, Any],
) -> None:
    """Insert mapping ``item`` at ``index`` of a round-trip list.

    A full-line comment block that sat directly above the old
    ``seq[index]`` (stored by ruamel on ``seq[index - 1]``) is moved
    onto the new entry, so it stays directly above the entry it
    described. Every comment is kept.
    """
    new = CommentedMap(item)
    if index > 0 and index < len(seq) + 1:
        following = _take_following_comment(seq[index - 1])
        if following and not _append_following_comment(new, following):
            _append_following_comment(seq[index - 1], following)
    seq.insert(index, new)


def seq_delete_preserving_comments(seq: Any, index: int) -> None:  # noqa: ANN401
    """Delete ``seq[index]`` of a round-trip list, keeping following comments.

    The full-line comment block after the removed entry (i.e. above the
    next entry) is re-attached to the previous entry, or turned into
    the list's leading comment when the first entry is removed. The
    removed entry's own inline comments go with it.
    """
    following = _take_following_comment(seq[index])
    del seq[index]
    if not following:
        return
    if index > 0 and _append_following_comment(seq[index - 1], following):
        return
    if index == 0:
        comment = seq.ca.comment
        if comment is None:
            seq.ca.comment = [None, []]
            comment = seq.ca.comment
        while len(comment) < 2:  # noqa: PLR2004
            comment.append([])
        if comment[1] is None:
            comment[1] = []
        for line in following.splitlines(keepends=True):
            if line.strip():
                col = len(line) - len(line.lstrip(" "))
                comment[1].append(
                    CommentToken(line.lstrip(" "), CommentMark(col), None)
                )
