"""``@node`` decorator — master spec §5.2.

Mirrors the ``@tool`` pattern at :mod:`tools.decorator`. The
decorator attaches a small definition record to the function's
``_kolsch_node`` attribute; the validator (Step 5) reads it at
run-start gate time.

Step 1 only does basic shape validation — ``secrets_needed`` is a
list of strings if present. Signature + return-type introspection
lands in the validator (Step 5) so authored Python modules don't
have to import the validator just to be importable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["NodeDefinition", "get_node_definition", "node"]


@dataclass
class NodeDefinition:
    """The record attached to a ``@node``-decorated function.

    Mirrors :class:`tools.decorator.ToolDefinition` shape-wise but
    is intentionally minimal — type info comes from signature
    annotations (master spec §5.3 / §5.4) and is introspected at
    validation time rather than carried here.

    ``label`` per master spec §5.2 — the node TYPE's human-readable
    label, used by chat capsules and the ``show`` CLI as the
    second-tier fallback (see :func:`workflows.labels.display_label`).
    """

    func: Callable[..., Any]
    label: str | None = None
    secrets_needed: list[str] = field(default_factory=list)


def node(
    *,
    label: str | None = None,
    secrets_needed: list[str] | None = None,
) -> Callable[[Any], Any]:
    """The ``@node`` decorator — master spec §5.2.

    Attaches a :class:`NodeDefinition` to the function's
    ``_kolsch_node`` attribute. The function is returned unchanged
    so it remains directly callable (mirrors the ``@tool`` pattern).

    Args:
        label: optional human-readable type label
            (see :func:`workflows.labels.display_label` for the
            3-step fallback). When omitted, the renderer cleans the
            function name (snake_case → space-separated lowercase).
        secrets_needed: optional list of secret names the engine
            resolves and injects into ``ctx.secrets`` at fire time.
            ``None`` (the default) is normalised to an empty list.

    Raises:
        TypeError: when applied to a non-callable target (e.g., a
            class). Deeper checks (signature shape, return-type
            shape) happen at the run-start gate per master spec
            §5.3 / §5.4.
    """
    # Basic shape check on the args — fail at decoration time so a
    # typo (e.g., ``secrets_needed="api_key"``) doesn't slip into
    # the validator.
    if secrets_needed is not None and (
        not isinstance(secrets_needed, list)
        or not all(isinstance(s, str) for s in secrets_needed)
    ):
        msg = (
            "@node(secrets_needed=...) must be a list[str]; "
            f"got {secrets_needed!r}"
        )
        raise TypeError(msg)
    # mypy's strict typing rules out a non-str ``label`` here; the
    # decorator still accepts ``Any`` at the call site so authors who
    # bypass the type checker get a clean TypeError. The runtime
    # check below catches it.
    if label is not None and not isinstance(label, str):
        msg = f"@node(label=...) must be a str or None; got {label!r}"  # type: ignore[unreachable]
        raise TypeError(msg)

    def decorate(fn: Any) -> Any:  # noqa: ANN401 — decorator validates
        if not callable(fn):
            msg = f"@node can only decorate callables; got {fn!r}"
            raise TypeError(msg)
        defn = NodeDefinition(
            func=fn,
            label=label,
            secrets_needed=list(secrets_needed or []),
        )
        fn._kolsch_node = defn
        return fn

    return decorate


def get_node_definition(fn: Callable[..., Any]) -> NodeDefinition:
    """Return the :class:`NodeDefinition` attached to a ``@node``-decorated function.

    Raises :class:`TypeError` when ``fn`` is not ``@node``-decorated.
    Callers typically filter by ``hasattr(fn, "_kolsch_node")`` during
    discovery instead.
    """
    defn = getattr(fn, "_kolsch_node", None)
    if not isinstance(defn, NodeDefinition):
        msg = f"{fn!r} is not a @node-decorated function"
        raise TypeError(msg)
    return defn
