"""RFC 7386 (JSON Merge Patch) — spec §3 + Conformance §I2.

Two functions:

- :func:`merge_patch` — the pure RFC 7386 algorithm for one target/patch
  pair. Mirrors the RFC's pseudocode (Appendix A test vectors all pass).
- :func:`resolve` — fold ``merge_patch`` left-to-right across an ordered
  list of partial dicts to produce the resolved dict.

Both functions are deterministic and operate on plain Python types so
the RFC 7386 ``absent`` vs ``null`` distinction is preserved (spec §I3).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def merge_patch(target: Any, patch: Any) -> Any:  # noqa: ANN401 — RFC takes any JSON value
    """Apply an RFC 7386 patch to a target value and return the result.

    Per the RFC:

    - If ``patch`` is not an object, it replaces ``target`` wholesale.
    - If ``patch`` is an object and ``target`` is not, ``target`` is
      treated as ``{}`` (its contents are ignored).
    - For each key/value pair in ``patch`` (when both are objects):
      - ``null`` deletes the key from the result.
      - any other value is recursively merged into the corresponding
        target key.

    The function returns a new value; the inputs are not mutated.
    """
    if not isinstance(patch, dict):
        return deepcopy(patch)
    base: dict[str, Any] = deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = merge_patch(base.get(key), value)
    return base


def resolve(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """RFC 7386 merge of an ordered list of partial dicts — spec §3.1.

    Folds :func:`merge_patch` left-to-right. The empty list returns
    ``{}``. Inputs are not mutated; the returned dict is a fresh deep
    copy so callers can mutate it safely.
    """
    result: dict[str, Any] = {}
    for partial in partials:
        result = merge_patch(result, partial)
    return result
