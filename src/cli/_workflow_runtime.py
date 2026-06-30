"""Shared executor-construction helper for workflow-execution surfaces.

Factored out of `cli.workflows._cmd_run` so the chat `/workflow` slash
handler can spin up the same
[`IterativeExecutor`][workflows.executor.IterativeExecutor] wiring
(same tool definitions, same authz/secrets/sandbox providers, same
builtin runners) without duplicating ~30 lines per call site.

The CLI surface still owns its own `WorkflowRunStore` instantiation
(per-command lifetime, scoped to the command's root); chat builds the
store once per `/workflow` invocation but reuses the chat's already-
bootstrapped [`PlatformRuntime`][platform_runtime.PlatformRuntime] for
everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools.builtins import (
    make_bash,
    make_list_files,
    make_read_file,
    make_write_file,
)
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from platform_runtime import PlatformRuntime
    from workflows.parser import Workflow
    from workflows.run import WorkflowRun
    from workflows.run_store import WorkflowRunStore


__all__ = ["build_workflow_executor"]


def build_workflow_executor(
    *,
    root: Path,
    runtime: PlatformRuntime,
    workflow: Workflow,
    run: WorkflowRun,
    store: WorkflowRunStore,
) -> IterativeExecutor:
    """Construct a fully-wired
    [`IterativeExecutor`][workflows.executor.IterativeExecutor].

    The wiring matches what `cli.workflows._cmd_run` ships: bash +
    file-IO tool definitions, the runtime's authz / secrets / sandbox /
    engine / session-store / config builder plumbed into
    [`register_builtins`][workflows.builtins.register_builtins], and a
    sub-workflow executor factory that defers to this same class.

    Caller-owned: the `WorkflowRunStore` and the `WorkflowRun` are
    passed in. The store's directory layout is created lazily by
    `WorkflowRunStore.create`; the executor's `run` writes the JSONL.
    """
    catalog = runtime.asset_management.catalog()

    bash_fn = make_bash(policy=runtime.path_policy, root=root)
    bash_defn = get_tool_definition(bash_fn)
    read_file_defn = get_tool_definition(
        make_read_file(policy=runtime.path_policy, root=root)
    )
    write_file_defn = get_tool_definition(
        make_write_file(policy=runtime.path_policy, root=root)
    )
    list_files_defn = get_tool_definition(
        make_list_files(policy=runtime.path_policy, root=root)
    )
    registry = RunnerRegistry()

    def _executor_factory(**kwargs: Any) -> IterativeExecutor:
        return IterativeExecutor(**kwargs)

    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=runtime.authz,
        secrets=runtime.secrets,
        sandbox=runtime.sandbox,
        catalog=catalog,
        engine=runtime.engine,
        session_store=runtime.store,
        config_builder=runtime.build_resolved_config,
        read_file_defn=read_file_defn,
        write_file_defn=write_file_defn,
        list_files_defn=list_files_defn,
        workflow_store=store,
        executor_factory=_executor_factory,
    )
    return IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
