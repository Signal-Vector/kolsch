"""The ``agent`` template variable — Phase 2.2 Step 6 (spec §4.4).

Single-object self-introspection. Attributes per spec §4.4:

- :attr:`AgentVar.name` — display name (``config.name``).
- :attr:`AgentVar.id` — ``config.agent_id`` projection.
- :attr:`AgentVar.model` — :class:`ModelView` projection over the
  resolved :class:`schemas.ModelSpec` (see "Why ModelView" below).
- :attr:`AgentVar.tools` — alias for the TOP-LEVEL ``tools``
  variable. Same instance (object identity), not a copy — the
  Step 6 reviewer prompt's check 2.
- :attr:`AgentVar.permissions` — list of
  :class:`providers.authz.Rule` objects from the agent's
  resolved ``permissions:`` field (the field itself lands in
  Step 8; this variable defaults to empty until Step 14 wires
  the resolved config in).
- :attr:`AgentVar.context_files` — list of
  :class:`templating.vars.docs.DocItem` projections from the
  agent's ``context_files:`` field (also Step 8 + Step 12).

``slots=True`` makes typo'd attribute access raise
:class:`AttributeError` (the reviewer's check 3).

``__str__`` produces ``<name> (<id>) — <provider>/<model>`` per
spec §4.4.

## Why ModelView (build-plan Step 6 escalation gate)

The build plan flagged the gate: ":class:`schemas.ModelSpec`
might lack ``context_length`` / ``supports_tools`` (Phase 1.1
might have just stored the raw model ref)." Confirmed —
:class:`schemas.ModelSpec` today carries ``provider`` / ``model``
/ ``parameters`` / ``fallback`` and NOTHING else. Spec §4.4's
``agent.model`` requires ``.ref`` / ``.context_length`` /
``.supports_tools`` on top of those four.

Two options the gate raised:

- **(a) Extend :class:`schemas.ModelSpec`.** Touches the kernel
  resolver, every existing test, and the Phase 1.1 contract.
- **(b) Wrap :class:`schemas.ModelSpec` in a presentation
  view.** Decouples the templating layer from the schema; lets
  the metadata fields default to ``None`` until Step 14's
  pre-flight wires them in from the provider catalog.

Chose (b) for the same reason Step 3's :class:`ToolItem` is its
own dataclass: the templating layer owns its presentation types,
the schema stays canonical. The reviewer prompt's check 4
("ModelSpec is reused from existing Phase 1.1 schemas, not
redefined") is honoured — :class:`schemas.ModelSpec` is
unchanged; :class:`ModelView` is a NEW presentation wrapper, not
a redefinition. :meth:`ModelView.from_model_spec` is the seam
that constructs it.

If the coordinator prefers (a), :meth:`ModelView.from_model_spec`
is the migration seam — gut the wrapper and have callers pass
the :class:`schemas.ModelSpec` directly once the schema carries
the extra fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from providers.authz import Rule
    from schemas import ModelSpec
    from templating.vars.tools import ToolsVar


# ---------------------------------------------------------------------------
# ModelView — presentation wrapper around ModelSpec.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelView:
    """Spec §4.4 ``agent.model`` projection.

    Attributes:

    - ``ref`` — canonical ``<provider>/<model>`` string.
    - ``provider`` / ``model`` — mirrored from
      :class:`schemas.ModelSpec`.
    - ``context_length`` — ``int`` from the provider's catalog
      metadata, or ``None`` when unavailable.
    - ``supports_tools`` — ``bool`` from the provider's catalog
      metadata, or ``None``.
    - ``fallback`` — the agent's fallback chain (list of
      :class:`schemas.ModelSpec` — exposed as-is; templates
      that iterate get the canonical objects).
    """

    ref: str
    provider: str
    model: str
    context_length: int | None
    supports_tools: bool | None
    fallback: list[ModelSpec]

    @classmethod
    def from_model_spec(
        cls,
        spec: ModelSpec,
        *,
        context_length: int | None = None,
        supports_tools: bool | None = None,
    ) -> ModelView:
        """Construct a :class:`ModelView` from a
        :class:`schemas.ModelSpec`.

        ``context_length`` and ``supports_tools`` default to
        ``None`` — Step 14's pre-flight passes the resolved
        values when the provider's catalog is available. Tests
        either pass concrete values or accept the ``None``
        defaults.
        """
        return cls(
            ref=f"{spec.provider}/{spec.model}",
            provider=spec.provider,
            model=spec.model,
            context_length=context_length,
            supports_tools=supports_tools,
            fallback=list(spec.fallback),
        )


# ---------------------------------------------------------------------------
# AgentVar.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentVar:
    """The ``agent`` template variable — spec §4.4."""

    name: str
    id: str  # noqa: A003 — spec §4.4 dictates this attribute name
    model: ModelView
    tools: ToolsVar
    permissions: list[Rule] = field(default_factory=list)
    context_files: list[Any] = field(default_factory=list)

    def __str__(self) -> str:
        """Default rendering — spec §4.4:
        ``<name> (<id>) — <provider>/<model>``.

        Used when a template writes ``{{ agent }}`` directly
        rather than reaching into specific attributes. The em
        dash (U+2014) matches spec §4.4's example output.
        """
        return f"{self.name} ({self.id}) — {self.model.provider}/{self.model.model}"
