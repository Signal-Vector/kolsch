"""`display_label` — single source of truth for human-readable node labels.

The 3-step fallback:

1. **Per-instance `description:`** — if the node carries a non-empty
   `description`, that wins.
2. **Type-level label** — when the caller passes `type_label` (e.g.,
   `@node(label="...")` for authored Python nodes, or one of the
   built-in labels), that wins.
3. **Cleaned class/function name** — final fallback: convert
   `BashNodeDef` → `"bash"` and `MyAuthoredNode` → `"my authored
   node"` (camel/snake → space-separated lowercase with the trailing
   `Node`/`NodeDef` suffix stripped).

All chat capsules, CLI table renderers (`kolsch workflows show`,
`kolsch nodes list`), and error-message label formatters route through
this helper rather than re-implementing the fallback.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["display_label"]

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
_SNAKE_SEPARATOR = re.compile(r"[_\-]+")


def _clean_name(raw: str) -> str:
    """Convert a class or function name to a human-readable label.

    - Strip a trailing `NodeDef` / `Node` suffix when present (so
      `BashNodeDef` → `bash` instead of `bash node def`).
    - Split camelCase / PascalCase boundaries.
    - Replace `_` / `-` with spaces.
    - Lowercase the result.
    """
    name = raw
    for suffix in ("NodeDef", "Node"):
        if name.endswith(suffix) and name != suffix:
            name = name[: -len(suffix)]
            break
    # camel → spaces
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    # snake → spaces
    spaced = _SNAKE_SEPARATOR.sub(" ", spaced)
    return spaced.strip().lower()


def display_label(node: Any, type_label: str | None = None) -> str:  # noqa: ANN401
    """Resolve the 3-step fallback for a node's human-readable label.

    Args:
        node: Any object representing the node — typically a Pydantic
            `NodeDef` instance or a Python `@node` function. The
            helper consults `getattr(node, "description", None)`
            first; if that's empty or missing, falls back to
            `type_label`, and finally to a cleaned form of the class
            name (`type(node).__name__`) or, for callables,
            `node.__name__`.
        type_label: Optional caller-supplied type-level label
            (typically from `@node(label="...")` or the per-built-in
            label table).

    Returns:
        A non-empty human-readable string.
    """
    # Per-instance description wins.
    description = getattr(node, "description", None)
    if isinstance(description, str) and description.strip():
        return description.strip()

    # Type-level label.
    if type_label is not None and type_label.strip():
        return type_label.strip()

    # Cleaned class or function name.
    raw = getattr(node, "__name__", None)
    if not isinstance(raw, str) or not raw:
        raw = type(node).__name__
    return _clean_name(raw)
