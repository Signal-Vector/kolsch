"""Authored Python ``@node`` runtime — Phase 3.1 Step 3.

Routes every ``type: <bundle>/nodes/<name>`` ref that is NOT a
reserved built-in slot through this runner. The catalog entry's
on-disk ``.py`` is imported via :mod:`importlib.util`, the
``@node`` sentinel is verified, the run function's first parameter
is introspected via :func:`inspect.get_type_hints` to extract the
Pydantic input model, ``bound_inputs`` is validated against it,
and the result is parsed per master spec §5.4's three return
shapes:

- :class:`pydantic.BaseModel` → ``result.model_dump()``.
- ``bool`` → ``{"value": result}``.
- ``tuple[bool, BaseModel]`` → ``{"value": result[0],
  **result[1].model_dump()}``.

Module isolation is the executor's responsibility (Step 3 wires
``snapshot_sys_modules`` / ``cleanup_bundle_imports`` around the
worklist walk) — this runner only imports + invokes.
"""

from __future__ import annotations

import importlib.util
import inspect
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from workflows.errors import (
    NodeTypeNotImplemented,
    WorkflowNodeImportFailed,
    WorkflowNodeMissingRun,
    WorkflowNodeNotDecorated,
    WorkflowNodeReturnTypeInvalid,
    WorkflowNodeSignatureInvalid,
)

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog

__all__ = ["CatalogNodeRunner"]


class CatalogNodeRunner:
    """Generic dispatcher for authored Python ``@node`` modules.

    Phase 3.1 Step 3 — provider-light. Construction takes the
    workflow catalog so the runner can resolve the on-disk path for
    a given ``type:`` ref; the actual import is performed lazily on
    the first call for that ref and cached on the runner instance.
    The executor's module-isolation discipline still cleans these
    imports up at run end via :func:`cleanup_bundle_imports`.
    """

    def __init__(self, *, catalog: AssetCatalog) -> None:
        self._catalog = catalog
        # ref → resolved (module, run_callable, inputs_model) tuple
        # cached so the second invocation of the same node in a run
        # doesn't re-import. The cache lives for the lifetime of the
        # runner instance; the executor constructs a fresh runner
        # per run, so module-isolation discipline holds.
        self._cache: dict[str, tuple[Any, Any, type[BaseModel] | None]] = {}

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,  # noqa: ANN401 — CatalogNodeDef
        bound_inputs: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — NodeRunner protocol
        ctx: Any,  # noqa: ANN401 — NodeContext
    ) -> dict[str, Any]:
        type_ref = getattr(node_def, "type", None)
        if not isinstance(type_ref, str):
            msg = (
                "CatalogNodeRunner requires node_def.type; got "
                f"{type_ref!r}"
            )
            raise NodeTypeNotImplemented(
                node=node_name, type_ref="<unknown>", phase="3.1"
            ) from ValueError(msg)

        entry = self._catalog.lookup(type_ref, expected_type="node")
        if entry is None:
            raise WorkflowNodeImportFailed(
                file="<workflow>",
                line=0,
                ref=type_ref,
                reason=f"node {type_ref!r} is not in the catalog",
            )
        if entry.metadata.get("builtin") is True:
            # The dispatcher should have routed built-ins to their
            # dedicated runners upstream. Reaching this branch is a
            # registry-wiring bug; surface it with the
            # already-typed exception so the caller's diagnostics
            # match other "node-type missing" failures.
            raise NodeTypeNotImplemented(
                node=node_name,
                type_ref=type_ref,
                phase="3.1",
            )
        if not entry.path:
            raise WorkflowNodeImportFailed(
                file="<workflow>",
                line=0,
                ref=type_ref,
                reason=f"node {type_ref!r} has no on-disk path",
            )

        module, run_fn, inputs_model = self._resolve(
            type_ref=type_ref, entry_path=entry.path
        )
        _ = module  # the import side-effect IS what we wanted

        # Validate inputs against the run function's Pydantic input
        # model. Pydantic's own ValidationError propagates if the
        # author's @node declared a Pydantic input class and the
        # rendered binds don't fit.
        validated_inputs: Any = bound_inputs
        if inputs_model is not None:
            validated_inputs = inputs_model.model_validate(bound_inputs)

        result = run_fn(inputs=validated_inputs, ctx=ctx)
        if inspect.iscoroutine(result):
            result = await result

        return _coerce_return(type_ref=type_ref, result=result)

    def inputs_model_for(self, type_ref: str) -> type[BaseModel] | None:
        """Return the ``@node``'s introspected ``Inputs(BaseModel)``,
        or ``None`` when the catalog entry can't be resolved or its
        ``run`` signature isn't typed with a Pydantic model.

        Phase 3.3 Step 1 — invoked by
        :func:`workflows._inputs_models.inputs_model_for` for
        authored Python receivers so the executor's strict-mode
        bind-validation hook (master spec §6.4.4) covers them too.
        Caches via ``_resolve``'s import cache — repeat lookups for
        the same ref don't re-import.
        """
        entry = self._catalog.lookup(type_ref, expected_type="node")
        if entry is None or not entry.path:
            return None
        if entry.metadata.get("builtin") is True:
            return None
        try:
            _, _, inputs_model = self._resolve(
                type_ref=type_ref, entry_path=entry.path
            )
        except Exception:  # noqa: BLE001 — defensive; raise path runs at dispatch
            return None
        return inputs_model

    def _resolve(
        self,
        *,
        type_ref: str,
        entry_path: str,
    ) -> tuple[Any, Any, type[BaseModel] | None]:
        """Import the module, validate the ``run`` shape, return
        ``(module, run_fn, inputs_model)``.

        Caches by ``type_ref`` so repeat invocations within one
        executor instance don't re-import. The executor's
        module-isolation cleanup at run end (Step 3) removes the
        bundle modules from ``sys.modules`` so a subsequent run
        picks up fresh code.
        """
        cached = self._cache.get(type_ref)
        if cached is not None:
            return cached

        module = _import_module(type_ref=type_ref, entry_path=entry_path)
        run_fn = getattr(module, "run", None)
        if run_fn is None:
            raise WorkflowNodeMissingRun(
                file="<workflow>", line=0, ref=type_ref
            )
        if getattr(run_fn, "_kolsch_node", None) is None:
            raise WorkflowNodeNotDecorated(
                file="<workflow>", line=0, ref=type_ref
            )
        inputs_model = _extract_inputs_model(
            run_fn=run_fn, type_ref=type_ref
        )
        self._cache[type_ref] = (module, run_fn, inputs_model)
        return self._cache[type_ref]


def _import_module(*, type_ref: str, entry_path: str) -> Any:  # noqa: ANN401
    """Load the authored node module via :mod:`importlib.util`.

    The synthetic module name embeds the ref so concurrent runs
    targeting different bundles never share a key. The executor's
    bundle-prefix cleanup strips these on terminal state.
    """
    module_name = f"_kolsch_node_{type_ref.replace('/', '_')}"
    spec = importlib.util.spec_from_file_location(
        module_name, Path(entry_path)
    )
    if spec is None or spec.loader is None:
        raise WorkflowNodeImportFailed(
            file="<workflow>",
            line=0,
            ref=type_ref,
            reason=f"could not build import spec for {entry_path}",
        )
    module = importlib.util.module_from_spec(spec)
    import sys  # noqa: PLC0415

    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise WorkflowNodeImportFailed(
            file="<workflow>",
            line=0,
            ref=type_ref,
            reason=str(exc),
        ) from exc
    return module


def _extract_inputs_model(
    *,
    run_fn: Any,  # noqa: ANN401
    type_ref: str,
) -> type[BaseModel] | None:
    """Inspect ``run_fn`` and return the Pydantic class annotated as
    the ``inputs`` parameter.

    Returns ``None`` when the parameter has no annotation OR is not
    a :class:`pydantic.BaseModel` subclass — both cases mean
    ``bound_inputs`` is passed through verbatim. The Phase 3.0 Step 5
    validator already rejects non-conforming signatures at run-start;
    this runtime check is defensive.
    """
    try:
        signature = inspect.signature(run_fn)
        hints = typing.get_type_hints(run_fn)
    except Exception as exc:
        raise WorkflowNodeSignatureInvalid(
            file="<workflow>",
            line=0,
            ref=type_ref,
            reason=f"could not introspect signature: {exc}",
        ) from exc
    params = list(signature.parameters.values())
    if not params:
        raise WorkflowNodeSignatureInvalid(
            file="<workflow>",
            line=0,
            ref=type_ref,
            reason="run() takes no parameters; expected 'inputs' + 'ctx'",
        )
    # The first parameter (positional or keyword) is ``inputs``. The
    # author may name it differently; we look at position, not name.
    inputs_param = params[0]
    annotation = hints.get(inputs_param.name)
    if annotation is None:
        return None
    if not (
        isinstance(annotation, type) and issubclass(annotation, BaseModel)
    ):
        return None
    return annotation


def _coerce_return(*, type_ref: str, result: Any) -> dict[str, Any]:  # noqa: ANN401
    """Parse the run function's return into the canonical captured dict.

    Master spec §5.4 — three return shapes:

    - :class:`BaseModel` → its model_dump().
    - ``bool`` → ``{"value": result}``.
    - ``tuple[bool, BaseModel]`` → ``{"value": result[0],
      **result[1].model_dump()}``.

    Any other shape raises :class:`WorkflowNodeReturnTypeInvalid`.
    """
    if isinstance(result, bool):
        # ``bool`` must be checked BEFORE ``int`` since ``bool`` is
        # a subclass of ``int`` in Python.
        return {"value": result}
    if isinstance(result, BaseModel):
        return dict(result.model_dump())
    if isinstance(result, tuple) and len(result) == 2:  # noqa: PLR2004 — (flag, payload) tuple shape
        flag, payload = result
        if isinstance(flag, bool) and isinstance(payload, BaseModel):
            return {"value": flag, **payload.model_dump()}
    raise WorkflowNodeReturnTypeInvalid(
        file="<workflow>",
        line=0,
        ref=type_ref,
        return_type=type(result).__name__,
    )
