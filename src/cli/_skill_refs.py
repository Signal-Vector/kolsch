"""The skill-body reference rule used by ``kolsch skills validate``.

One place decides what counts as a file reference in a SKILL.md
body and where it resolves. ``find_missing_skill_refs`` states the
rule; ``cli.skills.validate_skill_entry`` turns its result into
``missing-ref:<ref>`` codes. Pure apart from
``exists()`` checks on the filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

if TYPE_CHECKING:
    from pathlib import Path

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINK_TARGET_RE = re.compile(r"\[[^\]\n]*\]\(\s*([^)\s]+)(?:\s+[^)]*)?\)")
_PATH_SAFE_RE = re.compile(r"[\w.\-+@%/]+")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

_ANCHOR_DIRS = frozenset({"scripts", "references", "assets"})
"""Agent Skills standard sub-directories — a code span under one of
these is a reference even when the directory doesn't exist yet."""

_BUNDLES_DIR = "bundles"
"""First segment that marks a workspace-root-relative ref."""


@dataclass(frozen=True)
class _RefCandidate:
    text: str
    """The ref as written (after step 2) — what ``missing-ref:`` reports."""
    kind: Literal["span", "link"]
    path: str
    """The filesystem path to check: ``text``, percent-decoded for links."""


def _iter_candidates(body: str) -> list[_RefCandidate]:
    """Rule steps 1-3: scan, normalise, and drop non-path tokens."""
    found: list[_RefCandidate] = []
    fence: str | None = None
    for line in body.splitlines():
        marker = _FENCE_RE.match(line)
        if fence is not None:
            # Close on the same fence char at least as long as the opener.
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
            ):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            continue
        raw: list[tuple[str, Literal["span", "link"]]] = [
            (m.strip(), "span") for m in _CODE_SPAN_RE.findall(line)
        ]
        # A link inside a code span is code, not a link.
        prose = _CODE_SPAN_RE.sub(" ", line)
        raw.extend(
            (re.split(r"[#?]", m, maxsplit=1)[0], "link")
            for m in _LINK_TARGET_RE.findall(prose)
        )
        for token, kind in raw:
            text = token.removeprefix("./")
            if not _PATH_SAFE_RE.fullmatch(text) or text.startswith("/"):
                continue
            if kind == "span" and "/" not in text:
                continue
            path = unquote(text) if kind == "link" else text
            found.append(_RefCandidate(text=text, kind=kind, path=path))
    return found


def find_missing_skill_refs(
    body: str, *, skill_dir: Path, workspace_root: Path
) -> list[str]:
    r"""Return the body's file references that don't exist, in first-seen order.

    The rule (normative; bug-149):

    1. **Candidates** — inline code spans (single line) and Markdown
       link targets (``[text](target)``, ``#fragment``/``?query``
       dropped). Lines inside fenced code blocks (`` ``` `` / ``~~~``)
       are not scanned; a link inside a code span is not a link.
    2. **Normalise** — strip one leading ``./``. Link targets are
       percent-decoded (``with%20space.md``) for resolution only;
       steps 3 and 5 use the target as written.
    3. **Skip** anything that isn't fully path-safe
       (``^[\w.\-+@%/]+$``: no scheme ``:``, HTML/placeholders
       ``<>{}[]$``, globs ``*?``, ``…``, ``~``, spaces, ``foo()``),
       anything starting with ``/``, and code spans with no ``/``.
    4. **Anchor and base** — a ref whose first segment is ``bundles``
       resolves against ``workspace_root`` and must stay under
       ``<workspace_root>/bundles``. Any other ref resolves against
       ``skill_dir`` and must stay under it, and is only checked
       when it is a link target, or a code span whose first segment
       is ``scripts``/``references``/``assets`` or names an entry
       that exists in ``skill_dir``. Other code spans are prose
       (example paths, host names) and are skipped.
    5. **Report** — refs whose resolved path doesn't exist,
       deduplicated.

    A token the filesystem can't even probe (``OSError`` such as a
    segment longer than ``NAME_MAX``, or ``ValueError`` for an
    embedded NUL) is treated as not a ref, so malformed content never
    makes validation raise.
    """
    skill_base = skill_dir.resolve()
    bundles_base = (workspace_root / _BUNDLES_DIR).resolve()
    missing: list[str] = []
    seen: set[str] = set()
    for cand in _iter_candidates(body):
        try:
            is_missing = _is_missing(
                cand,
                skill_dir=skill_dir,
                skill_base=skill_base,
                workspace_root=workspace_root,
                bundles_base=bundles_base,
            )
        except (OSError, ValueError):
            continue
        if is_missing and cand.text not in seen:
            seen.add(cand.text)
            missing.append(cand.text)
    return missing


def _is_missing(
    cand: _RefCandidate,
    *,
    skill_dir: Path,
    skill_base: Path,
    workspace_root: Path,
    bundles_base: Path,
) -> bool:
    """Rule step 4 for one candidate: ``True`` iff it's a ref and absent."""
    first = cand.path.split("/", 1)[0]
    if first == _BUNDLES_DIR:
        base, resolved = bundles_base, (workspace_root / cand.path).resolve()
    elif cand.kind == "link" or first in _ANCHOR_DIRS or (skill_dir / first).exists():
        base, resolved = skill_base, (skill_dir / cand.path).resolve()
    else:
        return False
    return resolved.is_relative_to(base) and not resolved.exists()
