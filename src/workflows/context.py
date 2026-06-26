"""``NodeContext`` — runtime handle passed to every authored ``@node`` ``run``.

Master spec §5.5 locks the contract. Step 1 only DECLARES the
dataclass + the ``emit`` method + the ``for_test`` classmethod; the
Phase 3.1+ executor binds ``emit_fn`` (and wraps it to prepend
``node.<bundle>.`` to subkinds) and supplies the live state /
secrets mappings wrapped in :class:`types.MappingProxyType`.

Public fields (post-R7-L7-3 renames): the underscore-prefixed
``_bundle`` / ``_emit_fn`` are GONE — both are public test seams,
named ``bundle`` and ``emit_fn``. ``frozen=True`` keeps them
read-only at runtime; tests that need to override them construct
via the :meth:`NodeContext.for_test` classmethod or pass them as
keyword args.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from schemas import EventId, SessionId
    from workflows.ids import TraceId

__all__ = ["NodeContext"]


# A minimal structural-logger protocol: anything with the standard
# logging methods (``info`` / ``warning`` / ``error`` / ``debug``)
# satisfies the contract. The Phase 3.1+ executor binds a structured
# logger here; the for_test default uses the stdlib logger so unit
# tests can construct a ``NodeContext`` without standing up the
# observability stack.
StructLogger = logging.Logger


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Runtime context for an authored ``@node`` ``run`` (master spec §5.5).

    Fields are populated by the engine at construction time. Authored
    code reads these fields — no field on this dataclass is mutable
    from inside an authored node (``frozen=True`` enforces it; the
    read-only mapping wrappers on ``secrets`` / ``state`` enforce
    deep immutability).

    Spoof-resistance (R6 S4 + R7 L7-3): ``bundle`` is set by the
    engine from the catalog-resolved invocation file's bundle, NOT
    from ``inspect``-ing the calling ``@node``'s ``__module__``.
    Re-exporting a node from another bundle cannot fake its bundle
    of origin — the engine tracks the resolution path that led to
    this invocation, and the wire prefix follows the catalog.
    """

    workflow_run_id: str
    invocation_id: str
    workspace_root: Path
    log: StructLogger
    secrets: Mapping[str, str]
    state: Mapping[str, Any]
    bundle: str
    emit_fn: Callable[[str, str, Mapping[str, Any]], None] = field(repr=False)
    # Phase 3.2 — the executor-emitted ``workflow.node.started`` id for
    # this invocation. Runners that spawn child entities (the agent
    # runner) thread it into their parent-pointer payloads
    # (``FromWorkflowRun.event_id`` per master spec §11.6.1). ``None``
    # only when a runner constructs a NodeContext outside the executor's
    # worklist (e.g. unit tests via :meth:`for_test`).
    parent_event_id: EventId | None = None
    # Phase 3.2 — the workflow run's trace_id (master spec §11.6.4).
    # Inherited UNCHANGED by every spawned session's events so the two
    # JSONLs cross-correlate exactly. ``None`` only outside the
    # executor's worklist.
    trace_id: TraceId | None = None
    # Phase 3.2 — the workflow run's caller_chain (master spec §13.x).
    # The agent runner prepends the agent's ref before passing into the
    # engine so authz audit events record the workflow→agent frame.
    # Proximate first; default ``()`` for root-level invocations.
    caller_chain: tuple[str, ...] = field(default_factory=tuple)
    # Phase 3.2 — set by ``AgentRunner`` to the session it spawned, read
    # by the executor's ``workflow.node.completed`` payload builder
    # (master spec §11.6.3). Frozen dataclass — runners mutate via
    # ``object.__setattr__`` so the "authored nodes cannot mutate
    # context" invariant still holds for the public surface.
    spawned_session_id: SessionId | None = None

    def emit(self, subkind: str, payload: Mapping[str, Any]) -> None:
        """Emit a custom workflow event.

        ``subkind`` is the FREEFORM author-supplied suffix (no regex
        enforcement). The engine auto-prepends ``node.<bundle>.``
        based on ``self.bundle`` (set by the engine at construction
        time from the catalog-resolved invocation file's bundle, per
        R6 S4 + R7 L7-3), so the wire ``kind`` written to the JSONL
        becomes ``node.<bundle>.<subkind>``.

        Per R6-R1: the "author" segment from R5 R1's
        ``node.author.<bundle>.<subkind>`` is DROPPED — engine event
        kinds all start with ``workflow.``, ``policy.``, ``model.``,
        or ``tool.``, so ``node.*`` is unambiguously the author
        namespace and no disambiguating segment is needed.

        Authors NEVER write the ``node.<bundle>.`` prefix themselves;
        the auto-prepend is the bundle-namespacing guarantee.
        """
        self.emit_fn(self.bundle, subkind, payload)

    @classmethod
    def for_test(
        cls,
        *,
        workflow_run_id: str = "wrk_test",
        invocation_id: str = "inv_test",
        bundle: str = "test",
        workspace_root: Path | None = None,
        log: StructLogger | None = None,
        emit_fn: Callable[[str, str, Mapping[str, Any]], None] | None = None,
        secrets: Mapping[str, str] | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> NodeContext:
        """Round 7 A7-1 — sensible-defaults constructor for unit tests.

        Tests of authored Python nodes construct via
        ``NodeContext.for_test(...)`` instead of hand-rolling all
        eight fields. Override any field by keyword. Amends
        R6 J3 D ("no helper at all").

        Defaults:
        - ``workflow_run_id`` / ``invocation_id`` — fixed sentinel
          strings (``"wrk_test"`` / ``"inv_test"``).
        - ``bundle`` — ``"test"``.
        - ``workspace_root`` — a fresh ``tempfile.mkdtemp()`` path
          (so file-touching nodes have a real directory).
        - ``log`` — the ``"workflows.test"`` stdlib logger.
        - ``emit_fn`` — a no-op (``lambda *_: None``).
        - ``secrets`` / ``state`` — empty dicts (``Mapping``
          accepts plain dicts; production wraps in
          ``MappingProxyType``).
        """
        if workspace_root is None:
            workspace_root = Path(tempfile.mkdtemp(prefix="kolsch-nodectx-"))
        if log is None:
            log = logging.getLogger("workflows.test")
        if emit_fn is None:

            def _noop_emit(
                _bundle: str,
                _subkind: str,
                _payload: Mapping[str, Any],
            ) -> None:
                return None

            emit_fn = _noop_emit
        return cls(
            workflow_run_id=workflow_run_id,
            invocation_id=invocation_id,
            workspace_root=workspace_root,
            log=log,
            secrets=secrets if secrets is not None else {},
            state=state if state is not None else {},
            bundle=bundle,
            emit_fn=emit_fn,
        )
