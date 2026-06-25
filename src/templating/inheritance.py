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

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import DictLoader, Environment, StrictUndefined
from jinja2.exceptions import (
    TemplateAssertionError,
    TemplateError,
    TemplateRuntimeError,
)
from jinja2.nodes import Extends

from _yamlio import load
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
        payload: dict[str, Any] = load(text) or {}
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


def resolve_parent_chain_from_extends(
    extends_ref: str,
    workspace_root: Path,
    catalog: AssetCatalog | None = None,
) -> list[dict[str, Any]]:
    """Walk the parent chain starting from an ``extends:`` ref.

    Unlike :func:`resolve_extends_chain`, this entry point takes
    the leaf's ``extends:`` value directly rather than the leaf
    YAML's on-disk path. Used at engine render time (see
    bug-engine-extends-parent-not-rendered.md Step 2): the engine
    has ``self.config.extends`` but no easy handle on the leaf
    YAML path post-resolution.

    Returns a list of raw YAML payloads, immediate-parent first
    (i.e. excludes the leaf). Single-parent chain → length 1;
    multi-level → length N.

    :raises ConfigAgentInheritanceCycle: when the chain loops.
    :raises ConfigParentAgentNotFound: when an ``extends:``
        reference points at a missing file.
    """
    chain: list[dict[str, Any]] = []
    visited: set[Path] = set()
    cycle_trail: list[str] = []
    current_ref: str | None = extends_ref
    while current_ref:
        parent_path = _resolve_extends_to_path(
            current_ref, catalog, workspace_root
        )
        if not parent_path.is_file():
            raise ConfigParentAgentNotFound(
                path=current_ref,
                resolved=str(parent_path),
            )
        resolved_path = parent_path.resolve()
        if resolved_path in visited:
            cycle_trail.append(str(resolved_path))
            raise ConfigAgentInheritanceCycle(cycle=cycle_trail)
        visited.add(resolved_path)
        cycle_trail.append(str(resolved_path))
        payload: dict[str, Any] = load(
            resolved_path.read_text(encoding="utf-8")
        ) or {}
        chain.append(payload)
        current_ref = payload.get("extends")
    return chain


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


# ---------------------------------------------------------------------------
# Chain rendering (multi-level extends).
# ---------------------------------------------------------------------------


_PARENT_NAME_PREFIX = "__kolsch_parent_"
_EXTENDS_PARENT_DIRECTIVE = re.compile(r"\{%\s*extends\s+parent\s*%\}")


def render_with_parents(
    child_template_string: str,
    parent_chain: list[str],
    context: dict[str, Any],
    *,
    template_name: str = "<inline>",
) -> str:
    """Render a child template against an ordered ancestor chain.

    ``parent_chain[0]`` is the immediate parent, ``parent_chain[1]``
    is the grandparent, etc., root last. Each ancestor may itself
    contain ``{% extends parent %}``; per-layer the directive is
    rewritten to ``{% extends "__kolsch_parent_<n+1>" %}`` at load
    time so Jinja resolves it against the next ancestor up. The
    leaf gets ``parent = "__kolsch_parent_0"`` injected into its
    render context so ``{% extends parent %}`` in the leaf
    resolves to the immediate parent.

    The single-parent case (``len(parent_chain) == 1``) is
    behaviourally identical to :func:`render_with_parent`; passing
    a single-element list keeps callers free to use this function
    for the simple case as well.

    The synthetic loader name prefix ``__kolsch_parent_`` is
    reserved internally — agent authors can't collide because the
    rewrite uses the literal-string form of ``{% extends %}``,
    which doesn't consult the ``parent`` variable at all once
    rewritten.

    :raises ConfigRequiredBlockMissing: when an ancestor declares
        a ``{% block X required %}`` that the leaf does not override.
    :raises ConfigTemplateRenderFailed: for any other render-time
        Jinja error.
    """
    if not parent_chain:
        msg = "render_with_parents requires at least one parent"
        raise ValueError(msg)

    loader_map: dict[str, str] = {}
    for i, src in enumerate(parent_chain):
        name = f"{_PARENT_NAME_PREFIX}{i}"
        if i + 1 < len(parent_chain):
            next_name = f"{_PARENT_NAME_PREFIX}{i + 1}"
            src = _EXTENDS_PARENT_DIRECTIVE.sub(
                f'{{% extends "{next_name}" %}}',
                src,
                count=1,
            )
        loader_map[name] = src

    env = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        loader=DictLoader(loader_map),
    )
    enriched_context = {**context, "parent": f"{_PARENT_NAME_PREFIX}0"}
    try:
        template = env.from_string(child_template_string)
        return template.render(enriched_context)
    except (TemplateAssertionError, TemplateRuntimeError) as exc:
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


def prompt_uses_extends_directive(prompt: str) -> bool:
    """Return ``True`` when ``prompt`` syntactically contains a
    ``{% extends ... %}`` directive at the top of its AST.

    Used by the engine's orphan-check at the dispatch site
    (``_render_prompt_if_needed``): an author who wrote
    ``{% extends parent %}`` but forgot the ``extends:`` field on
    the YAML gets a typed
    :class:`errors.ConfigExtendsTemplateWithoutInheritance`
    instead of the loader-less ``render_prompt`` env's
    unhelpful ``TypeError: no loader for this environment``.

    Implementation note: we parse with a bare
    :class:`jinja2.Environment` (no loader). Parsing builds the
    AST without triggering the loader's ``get_source`` lookup;
    the lookup only fires at template **load** time (Jinja
    inheritance is resolved when a template is loaded, not
    parsed). Comment forms like ``{# {% extends parent %} #}``
    don't false-trigger because Jinja strips comments during
    parse — a comment node yields no :class:`Extends` node.

    Returns ``False`` on any parse error: malformed templates
    surface their own error later via ``render_prompt``'s
    StrictUndefined path; this detector is purely a binary
    "is the extends directive present" probe.
    """
    try:
        ast = Environment().parse(prompt)
    except TemplateError:
        return False
    # ``ast.find_all(Extends)`` is the canonical Jinja walk.
    return any(True for _ in ast.find_all(Extends))


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
