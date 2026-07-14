"""Shared workflow-inputs scanner (bug-027, AC #13).

Scans a workflow YAML string for ``{{ inputs.<name> }}`` Jinja
references and separates *required* (unguarded) names from *optional*
(``| default(...)``-guarded) ones. Consumed by both
``bundles/kolsch/tools/run_workflow.py`` (input-validation gate,
bug-025) and ``bundles/kolsch/tools/workflow_inspect.py`` (introspection
affordance, bug-027).

The scanner is best-effort: unparseable YAML or unparseable Jinja
returns empty sets rather than raising, so the caller can decide
whether to escalate (the validator surfaces malformed YAML on its own
path — this module is a probe, not a validator).

**Design notes.**

- Zero dependencies on the workflows service layer — takes a raw YAML
  string in, returns pure sets out. Keeps ``workflow_inspect``'s
  service-catalog lookup unaware of scanning internals.
- Uses ``jinja2.Environment().parse`` (no rendering) so no side effects
  and no security surface. ``S701`` is disabled inline because the AST
  never renders.
- Bare ``{{ inputs }}`` (whole-namespace reference) is ignored — there's
  no per-key requirement to name.

bug-025 previously kept ``_extract_required_inputs`` inline on
``bundles/kolsch/tools/run_workflow.py``. bug-027 needed the same
walker for ``workflow_inspect``; extraction was preferred over
duplication so a future Jinja node shape (Getattr on ``inputs`` via a
nested filter, say) doesn't drift between the two consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


__all__ = [
    "InputsScanResult",
    "extract_required_inputs",
    "scan_inputs",
]


@dataclass(frozen=True)
class InputsScanResult:
    """Sorted required/optional input-name lists from a workflow YAML.

    ``required`` and ``optional`` are ``list[str]`` (not ``set``) so the
    caller can render them in a stable order without re-sorting.
    ``optional`` names never appear in ``required`` — a key referenced
    both guarded and unguarded is treated as required (the unguarded
    site would still explode without a value).
    """

    required: list[str]
    optional: list[str]


def scan_inputs(yaml_source: str) -> InputsScanResult:
    """Return the required + optional ``inputs.<name>`` refs.

    Walks the parsed workflow YAML for every string field, parses each
    string as a Jinja template, and collects the first-level attribute
    name off every ``{{ inputs.<name> }}`` chain. A reference wrapped
    in ``| default(...)`` (or ``default(inputs.foo)``) is classified as
    optional; every other unguarded reference is required.

    A workflow that references ``{{ inputs.message | default('hi') }}``
    treats ``message`` as optional (the shipped
    ``kolsch/workflows/hello`` sample follows exactly this pattern).

    Returns an empty ``InputsScanResult`` when the YAML fails to parse
    or the workflow has no inputs references (best-effort probe — bad
    YAML surfaces later via the parser, not here).

    Args:
        yaml_source: The workflow YAML as a raw string.

    Returns:
        Sorted required and optional input-name lists.
    """
    # Local imports keep the module import graph light.
    from jinja2 import Environment  # noqa: PLC0415
    from jinja2.exceptions import TemplateError  # noqa: PLC0415
    from jinja2.nodes import (  # noqa: PLC0415
        Filter,
        Getattr,
        Getitem,
        Name,
    )

    from kolsch._yamlio import load  # noqa: PLC0415

    try:
        payload = load(yaml_source)
    except Exception:  # noqa: BLE001 — best-effort
        return InputsScanResult(required=[], optional=[])
    if not isinstance(payload, dict):
        return InputsScanResult(required=[], optional=[])

    env = Environment()  # noqa: S701 — parse-only, no rendering
    required: set[str] = set()
    optional: set[str] = set()

    def _extract_key(node: Any) -> str | None:  # noqa: ANN401
        """Return the top-level ``inputs.<name>`` key from a Getattr/Getitem chain."""
        # Getattr(node=Name('inputs'), attr='foo') -> 'foo'.
        if isinstance(node, Getattr) and isinstance(node.node, Name) and node.node.name == "inputs":
            return str(node.attr)
        # Getitem(node=Name('inputs'), arg=Const('foo')) -> 'foo'.
        if isinstance(node, Getitem) and isinstance(node.node, Name) and node.node.name == "inputs":
            arg = node.arg
            val = getattr(arg, "value", None)
            if isinstance(val, str) and val:
                return val
        return None

    def _collect_guarded_ids(node: Any, out: set[int]) -> None:  # noqa: ANN401
        """Mark this node id (and descendant inputs chains) as guarded."""
        out.add(id(node))
        # Recurse into common containers so `default(inputs.foo)` and
        # `inputs.foo | default(...) | trim` still mark `inputs.foo`.
        for child in getattr(node, "iter_child_nodes", lambda: [])():
            _collect_guarded_ids(child, out)

    def _walk_string(s: str) -> None:
        try:
            ast = env.parse(s)
        except TemplateError:
            return
        # Collect the top-level `inputs.<name>` chain nodes that live
        # under a `default(...)` filter — those are optional.
        guarded: set[int] = set()
        for filt in ast.find_all(Filter):
            if getattr(filt, "name", None) == "default":
                _collect_guarded_ids(filt.node, guarded)
        # Now collect every `inputs.<name>` reference; classify as
        # optional when guarded, required otherwise.
        for chain in ast.find_all((Getattr, Getitem)):
            key = _extract_key(chain)
            if key is None:
                continue
            if id(chain) in guarded:
                optional.add(key)
            else:
                required.add(key)

    def _walk(node: Any) -> None:  # noqa: ANN401
        if isinstance(node, str):
            _walk_string(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)

    # A key referenced both guarded and unguarded is required (the
    # unguarded reference would still explode without a value).
    optional -= required
    return InputsScanResult(
        required=sorted(required),
        optional=sorted(optional),
    )


def extract_required_inputs(yaml_source: str) -> set[str]:
    """Legacy shim — required-only view over ``scan_inputs``.

    Kept for ``bundles/kolsch/tools/run_workflow.py`` (bug-025), which
    only needs the required set. The set (unsorted) matches the
    pre-bug-027 shape exactly.
    """
    return set(scan_inputs(yaml_source).required)
