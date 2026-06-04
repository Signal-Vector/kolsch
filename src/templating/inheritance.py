"""Agent inheritance — Phase 2.2 Step 13 (spec §8).

Two responsibilities:

1. **Chain resolution** — :func:`resolve_extends_chain` walks an
   agent's ``extends:`` chain from leaf to root, detecting
   cycles via a visited set. Returns the chain as a list of raw
   YAML dicts (leaf-first); the caller composes them via the
   kernel's RFC 7386 merge.
2. **Prompt rendering with parent binding** —
   :func:`render_with_parent` is a render-harness variant that
   pre-loads the parent's prompt source as a template named
   ``"parent"`` so the child's ``{% extends parent %}`` resolves.
   Required-block enforcement happens naturally via Jinja 3.1's
   :class:`jinja2.exceptions.TemplateAssertionError`; we
   re-raise as :class:`errors.ConfigRequiredBlockMissing`.

## E-2.2-001 A5 — loader strategy (DictLoader-with-preprocessing)

Three options the build-plan escalation flagged:

- **(a) DictLoader-with-preprocessing** (chosen) — pre-load the
  parent's prompt source as a template named ``"parent"`` in a
  :class:`jinja2.DictLoader`. Inject ``parent = "parent"`` into
  the render context so ``{% extends parent %}`` evaluates the
  variable, gets ``"parent"``, and finds the pre-loaded
  template. Simple; no custom :class:`jinja2.BaseLoader`
  subclass needed.
- **(b) Custom BaseLoader** — bind the parent template source
  to the loader's ``get_source`` via an instance-scoped dict.
  More boilerplate; same observable behaviour.
- **(c) Pre-process the child's template** — string-replace
  ``{% extends parent %}`` with ``{% extends "parent" %}``
  before passing to Jinja. Most fragile (false positives on
  string literals).

Going with (a). The implementation is ~10 lines plus
required-block trapping. If the coordinator prefers a different
strategy, this module is the only seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from jinja2 import DictLoader, Environment, StrictUndefined
from jinja2.exceptions import (
    TemplateAssertionError,
    TemplateError,
    TemplateRuntimeError,
)

from assets.refs import resolve_shorthand
from errors import (
    AssetsInvalidRef,
    AssetsNotFound,
    ConfigAgentInheritanceCycle,
    ConfigParentAgentNotFound,
    ConfigRequiredBlockMissing,
    ConfigTemplateRenderFailed,
)
from templating.context_files import resolve_path

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog


def _resolve_extends_to_path(
    extends_ref: str,
    catalog: AssetCatalog | None,
    workspace_root: Path,
) -> Path:
    """Resolve an ``extends:`` ref to a parent-agent YAML path.

    Order (per qol-prefix-defaults-and-unified-refs.md):

    1. **Prefixed file paths** (contain ``:``) — ``kolsch:`` and
       ``workspace:`` route through
       :func:`templating.context_files.resolve_path`.
    2. **Catalog refs** — 1-segment ``<name>`` or 2-segment
       ``<bundle>/<name>`` — when ``catalog`` is provided, try
       resolving via :func:`assets.refs.resolve_shorthand` against
       ``asset_type="agent"``; success returns the catalog entry's
       file path.
    3. **Plain paths** — anything else falls back to
       ``resolve_path`` (workspace-rooted file path).

    ``catalog`` is optional so legacy callers (and unit tests that
    don't need catalog resolution) still work; without it the
    function behaves as plain ``resolve_path``.
    """
    # Prefixed file path branch wins first (contains `:`).
    if ":" in extends_ref:
        return resolve_path(extends_ref, workspace_root)

    # Catalog-ref branch — try to resolve when the input shape
    # looks like a catalog ref AND we have a catalog to query.
    if catalog is not None:
        try:
            canonical = resolve_shorthand(catalog, extends_ref, "agent")
        except (AssetsInvalidRef, AssetsNotFound):
            canonical = None
        if canonical is not None:
            entry = catalog.assets.get(canonical)
            if entry is not None:
                return Path(entry.path)

    # Fallback — plain path under the workspace root.
    return resolve_path(extends_ref, workspace_root)


# ---------------------------------------------------------------------------
# Chain resolution.
# ---------------------------------------------------------------------------


def resolve_extends_chain(
    agent_yaml_path: Path,
    workspace_root: Path,
    catalog: AssetCatalog | None = None,
) -> list[dict[str, Any]]:
    """Walk the ``extends:`` chain from the given agent YAML
    (leaf) up to the root.

    Returns a list of raw YAML dicts, leaf-first. The first
    entry is the leaf (the agent passed in); subsequent entries
    are each parent in turn. Composition (apply_inheritance) is
    the caller's job — typically via the kernel's RFC 7386
    merge.

    ``catalog`` enables catalog-ref resolution for ``extends:``
    values (e.g. ``extends: kolsch/helper`` → catalog lookup for
    agent ``kolsch/agents/helper``). When omitted, only the
    file-path forms (``kolsch:`` / ``workspace:`` / plain) are
    supported.

    :raises ConfigParentAgentNotFound: when an ``extends:``
        reference points at a missing file.
    :raises ConfigAgentInheritanceCycle: when the chain loops.
    """
    chain: list[dict[str, Any]] = []
    visited: set[Path] = set()
    cycle_trail: list[str] = []

    current_path = agent_yaml_path.resolve()
    while True:
        if current_path in visited:
            cycle_trail.append(str(current_path))
            raise ConfigAgentInheritanceCycle(cycle=cycle_trail)
        visited.add(current_path)
        cycle_trail.append(str(current_path))

        try:
            text = current_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigParentAgentNotFound(
                path=str(current_path),
                resolved=str(current_path),
            ) from exc
        payload: dict[str, Any] = yaml.safe_load(text) or {}
        chain.append(payload)

        extends_ref = payload.get("extends")
        if not extends_ref:
            return chain
        parent_path = _resolve_extends_to_path(
            extends_ref, catalog, workspace_root
        )
        if not parent_path.is_file():
            raise ConfigParentAgentNotFound(
                path=extends_ref,
                resolved=str(parent_path),
            )
        current_path = parent_path


def apply_inheritance(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """Compose a chain (leaf-first) into a single resolved dict.

    Walks the chain from ROOT toward LEAF, applying each later
    layer's wholesale-per-field override. Honours the
    composition order from Schemas §A.11.2: ``extends`` chain
    layers — leaf wins. Defaults / agent merging at the
    kernel-resolver layer is a separate step (Step 14's
    pre-flight).

    Strips the ``extends:`` key from each layer after composition
    so the resulting dict carries only the resolved agent
    envelope.
    """
    composed: dict[str, Any] = {}
    for layer in reversed(chain):
        cleaned = {k: v for k, v in layer.items() if k != "extends"}
        composed.update(cleaned)
    return composed


# ---------------------------------------------------------------------------
# Prompt rendering with parent binding.
# ---------------------------------------------------------------------------


_PARENT_TEMPLATE_NAME = "parent"


def render_with_parent(
    child_template_string: str,
    parent_template_string: str,
    context: dict[str, Any],
    *,
    template_name: str = "<inline>",
) -> str:
    """Render a child template that may use ``{% extends parent %}``.

    The parent's source is pre-loaded into a
    :class:`jinja2.DictLoader` under the name ``"parent"``; the
    render context gets ``parent = "parent"`` injected so the
    variable form ``{% extends parent %}`` resolves naturally.

    :raises ConfigRequiredBlockMissing: when the parent declares
        a ``{% block X required %}`` and the child does not
        override it (Jinja 3.1's
        :class:`jinja2.exceptions.TemplateAssertionError` is
        re-raised in the project's envelope).
    :raises ConfigTemplateRenderFailed: for any other render-time
        Jinja error.
    """
    env = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        loader=DictLoader({_PARENT_TEMPLATE_NAME: parent_template_string}),
    )
    enriched_context = {**context, "parent": _PARENT_TEMPLATE_NAME}
    try:
        template = env.from_string(child_template_string)
        return template.render(enriched_context)
    except (TemplateAssertionError, TemplateRuntimeError) as exc:
        # Jinja raises ``TemplateRuntimeError`` (NOT
        # ``TemplateAssertionError``) for required-block-missing
        # at render time; both subclasses share the
        # "Required block 'X' not found" message shape.
        message = str(exc)
        if "Required block" in message:
            raise ConfigRequiredBlockMissing(
                block_name=_extract_block_name(message),
                agent_path=template_name,
            ) from exc
        raise ConfigTemplateRenderFailed(
            template=template_name,
            reason=message,
        ) from exc
    except TemplateError as exc:
        raise ConfigTemplateRenderFailed(
            template=template_name,
            reason=str(exc),
        ) from exc


def _extract_block_name(message: str) -> str:
    """Pull the required-block name out of Jinja's error message
    ("Required block 'role' not found"). Returns ``"<unknown>"``
    when the pattern doesn't match — keeps the error surface
    robust against Jinja message-text drift."""
    marker = "Required block "
    if marker in message:
        tail = message.split(marker, 1)[1]
        # Format: "Required block 'role' not found" — strip quotes.
        end = tail.find("'", 1)
        if end > 1 and tail.startswith("'"):
            return tail[1:end]
    return "<unknown>"
