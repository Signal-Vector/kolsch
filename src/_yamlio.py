"""ruamel.yaml-based load/dump helper — Phase 3.0 Step 0 prereq.

Single chokepoint for YAML serialization across kolsch. Migrating off
PyYAML fixes the Norway-bug (unquoted ``no``/``yes``/``on``/``off``
were coerced to booleans) and unblocks the Phase 3.0 workflow parser's
need for line-info-aware loading (Step 2).

This module is the ONLY site in ``src/`` that imports ``ruamel.yaml``;
every other module imports :func:`load` / :func:`dump` (and, when it
needs to catch parse errors, :class:`YAMLError`) from here.

The module name is ``_yamlio`` rather than ``_yaml`` because PyYAML
itself ships a compatibility stub package literally named ``_yaml``
(``site-packages/_yaml/__init__.py`` — a back-compat shim for the
old standalone libyaml C extension). PyYAML remains a transitive
dependency of crewai / chromadb / kubernetes / huggingface-hub /
pre-commit, so the stub package can't be removed, and Python's
package finder hits site-packages before this file. ``_yamlio``
sidesteps that collision while keeping the spec's intent (one small
underscore-prefixed helper at the src root).
"""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

__all__ = ["YAMLError", "dump", "load"]

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


def load(text: str) -> Any:  # noqa: ANN401
    """Parse YAML ``text`` and return the corresponding Python object."""
    return _yaml.load(text)


def dump(data: Any) -> str:  # noqa: ANN401
    """Serialize ``data`` to a YAML string in block style, key-order preserved."""
    buf = StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()
