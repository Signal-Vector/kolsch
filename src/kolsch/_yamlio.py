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

from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

__all__ = [
    "YAMLError",
    "dump",
    "dump_round_trip",
    "load",
    "load_round_trip",
    "load_with_positions",
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
