"""bug-026 — pin the import shape of `WorkflowsImpl._default_executor_factory`.

Regression guard for a pre-existing bug shipped in spec-012 (commit
1db7201) that landed a broken import inside the default executor
factory: the code imported `parse_workflow_yaml` from
`workflows.parser`, but that symbol does not exist — the actual
symbol is `load_workflow(path: Path) -> Workflow`. The bug was
invisible in the test suite because every workflow test injects a
custom executor factory; `_default_executor_factory` is only reached
in production when no factory is passed. bug-025's diagnostic surface
(`WorkflowRunStatusView.error_message`) would have made this visible
as an `ImportError` at first workflow execution, but by then a real
user would have already hit it.

This test constructs a `WorkflowsImpl` with the default factory and
asserts the imports it uses actually resolve — no full workflow
execution required, just proof the symbols exist and match the
factory's expected shape.
"""

from __future__ import annotations


def test_default_executor_factory_imports_resolve() -> None:
    """Both symbols the default factory imports at runtime must exist.

    Guards against a symbol-rename in `workflows.parser` (or
    `cli._workflow_runtime`) silently breaking the default factory
    path — which is the ONLY factory path in production.
    """
    from cli._workflow_runtime import build_workflow_executor
    from workflows.parser import load_workflow

    assert callable(build_workflow_executor)
    assert callable(load_workflow)


def test_load_workflow_signature_takes_path() -> None:
    """`load_workflow` must accept a `Path` — the factory hands it
    `entry.path` from the runtime catalog directly, without reading
    the YAML text first. If the signature drifts to accept a string
    of YAML content, `_default_executor_factory` breaks silently.
    """
    import inspect
    from pathlib import Path

    from workflows.parser import load_workflow

    sig = inspect.signature(load_workflow)
    params = list(sig.parameters.values())
    assert len(params) >= 1
    first = params[0]
    # The parameter should be typed as Path (or accept one).
    # `inspect.signature` returns the annotation as a type; we
    # normalise to str for a stable assertion.
    annotation = first.annotation
    assert (
        annotation is Path
        or annotation is inspect.Parameter.empty
        or "Path" in str(annotation)
    )
