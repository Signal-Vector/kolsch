"""Authored Python `@node` runner — generic dispatch for catalog refs.

Routes every `type: <bundle>/nodes/<name>` ref that is NOT a
reserved built-in slot through this runner. The catalog entry's
on-disk `.py` is imported via `importlib.util`, the `@node`
sentinel is verified, the run function's first parameter is
introspected via `inspect.get_type_hints` to extract the Pydantic
input model, `bound_inputs` is validated against it, and the
result is parsed into one of three canonical return shapes:

- `pydantic.BaseModel` → `result.model_dump()`.
- `bool` → `{"value": result}`.
- `tuple[bool, BaseModel]` → `{"value": result[0],
  **result[1].model_dump()}`.

Module isolation is the executor's responsibility (it wraps the
worklist walk with `snapshot_sys_modules` /
`cleanup_bundle_imports`) — this runner only imports and invokes.
Per-run `sys.modules` namespacing: when the active
[`WorkflowRunContext`][workflows.run_context.WorkflowRunContext]
carries a
[`RunModuleNamespace`][workflows.module_isolation.RunModuleNamespace],
the import key is wrapped with that namespace's prefix so two
concurrent runs loading the same module observe distinct module
instances.
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
    """Generic dispatcher for authored Python `@node` modules.

    Provider-light. Construction takes the workflow catalog so the
    runner can resolve the on-disk path for a given `type:` ref;
    the actual import is performed lazily on the first call for
    that ref and cached on the runner instance. The executor's
    module-isolation discipline still cleans these imports up at
    run end via `cleanup_bundle_imports`.
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
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — NodeRunner protocol
        ctx: Any,
    ) -> dict[str, Any]:
        """Import the authored module, validate inputs, and dispatch `run`.

        Returns the result coerced into the canonical captured-dict
        shape. Catalog drift (missing entry, missing on-disk path,
        builtin slot reaching this runner) surfaces as
        [`WorkflowNodeImportFailed`][workflows.errors.WorkflowNodeImportFailed]
        or
        [`NodeTypeNotImplemented`][workflows.errors.NodeTypeNotImplemented];
        Pydantic input validation propagates `ValidationError`.
        """
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
        """Return the `@node`'s introspected `Inputs(BaseModel)`.

        Returns `None` when the catalog entry can't be resolved or
        its `run` signature isn't typed with a Pydantic model.
        Invoked by
        [`workflows._inputs_models.inputs_model_for`][workflows._inputs_models.inputs_model_for]
        so the executor's strict-mode bind-validation hook covers
        authored Python receivers too. Caches via `_resolve`'s
        import cache — repeat lookups for the same ref don't
        re-import.
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
        """Import the module, validate the `run` shape, return the triple.

        Returns `(module, run_fn, inputs_model)` and caches by
        `type_ref` so repeat invocations within one executor
        instance don't re-import. The executor's module-isolation
        cleanup at run end removes the bundle modules from
        `sys.modules` so a subsequent run picks up fresh code. When
        the active `WorkflowRunContext` carries a `module_ns`, the
        import key is wrapped with the run-specific prefix so
        concurrent runs each see a distinct module instance.
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


def _import_module(*, type_ref: str, entry_path: str) -> Any:
    """Load the authored node module via `importlib.util`.

    The synthetic module name embeds the ref so concurrent runs
    targeting different bundles never share a key. The active run's
    [`RunModuleNamespace`][workflows.module_isolation.RunModuleNamespace]
    prefix (when one is published on the contextvar) wraps the key
    so two concurrent runs loading the SAME ref each see a distinct
    module instance. The executor's bundle-prefix cleanup +
    per-run prefix cleanup both strip these on terminal state.
    """
    from workflows.run_context import (  # noqa: PLC0415
        current_workflow_run_context,
    )

    canonical = f"_kolsch_node_{type_ref.replace('/', '_')}"
    run_ctx = current_workflow_run_context()
    if run_ctx is not None and run_ctx.module_ns is not None:
        module_name = run_ctx.module_ns.wrap_module_name(canonical)
    else:
        # Fallback for test paths that exercise the runner directly
        # without the executor's lifecycle — preserves baseline
        # behavior byte-for-byte.
        module_name = canonical
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
    run_fn: Any,
    type_ref: str,
) -> type[BaseModel] | None:
    """Inspect `run_fn` and return its `inputs` parameter's Pydantic class.

    Returns `None` when the parameter has no annotation OR is not a
    `BaseModel` subclass — both cases mean `bound_inputs` is passed
    through verbatim. The run-start validator already rejects
    non-conforming signatures; this runtime check is defensive.
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


def _coerce_return(*, type_ref: str, result: Any) -> dict[str, Any]:
    """Parse the run function's return into the canonical captured dict.

    Three accepted return shapes:

    - `BaseModel` → `result.model_dump()`.
    - `bool` → `{"value": result}`.
    - `tuple[bool, BaseModel]` → `{"value": result[0],
      **result[1].model_dump()}`.

    Any other shape raises
    [`WorkflowNodeReturnTypeInvalid`][workflows.errors.WorkflowNodeReturnTypeInvalid].
    """
    if isinstance(result, bool):
        # `bool` must be checked BEFORE `int` since `bool` is a
        # subclass of `int` in Python.
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
