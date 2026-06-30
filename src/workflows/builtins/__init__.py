"""Built-in workflow node runners and runner-wrapping decorators.

The runners that implement the platform's stock node types
(`kolsch/nodes/bash`, `kolsch/nodes/noop`, `kolsch/nodes/agent`,
`kolsch/nodes/workflow`, etc.) — one runner class per node type —
plus [`with_timeout`][workflows.builtins.with_timeout] and
[`with_retry`][workflows.builtins.with_retry], the per-invocation
wrappers the executor layers onto any runner. The
[`CatalogNodeRunner`][workflows.builtins.CatalogNodeRunner] is the
generic dispatch for authored Python `@node` modules.

[`register_builtins`][workflows.builtins.register_builtins] is the
single-call wiring surface — it slots each runner against the
executor's [`RunnerRegistry`][workflows.executor.RunnerRegistry].
Runners whose dependencies aren't supplied stay unregistered so the
slot keeps raising
[`NodeTypeNotImplemented`][workflows.errors.NodeTypeNotImplemented]
and the gap is visible at run-start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflows.builtins.agent import AgentRunner
from workflows.builtins.bash import BashRunner
from workflows.builtins.branch import BranchRunner
from workflows.builtins.catalog_node_runner import CatalogNodeRunner
from workflows.builtins.compute import ComputeRunner
from workflows.builtins.delete_state import DeleteStateRunner
from workflows.builtins.filter import FilterRunner
from workflows.builtins.list_files import ListFilesRunner
from workflows.builtins.map import MapRunner
from workflows.builtins.noop import NoopRunner
from workflows.builtins.read_file import ReadFileRunner
from workflows.builtins.retry import with_retry
from workflows.builtins.set_state import SetStateRunner
from workflows.builtins.timeout import with_timeout
from workflows.builtins.user_input import UserInputRunner
from workflows.builtins.workflow_sub import WorkflowSubRunner
from workflows.builtins.write_file import WriteFileRunner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from assets.catalog import AssetCatalog
    from engine import AgentEngine
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from schemas import ResolvedAgentConfig
    from store import SessionStore
    from tools.decorator import ToolDefinition
    from workflows.executor import IterativeExecutor, RunnerRegistry
    from workflows.run_store import WorkflowRunStore

__all__ = [
    "AgentRunner",
    "BashRunner",
    "BranchRunner",
    "CatalogNodeRunner",
    "ComputeRunner",
    "DeleteStateRunner",
    "FilterRunner",
    "ListFilesRunner",
    "MapRunner",
    "NoopRunner",
    "ReadFileRunner",
    "SetStateRunner",
    "UserInputRunner",
    "WorkflowSubRunner",
    "WriteFileRunner",
    "register_builtins",
    "with_retry",
    "with_timeout",
]


def register_builtins(  # noqa: PLR0913 — wide-by-design wiring surface; see docstring
    registry: RunnerRegistry,
    *,
    bash_defn: ToolDefinition,
    authz: AuthorizationProvider,
    secrets: SecretsProvider,
    sandbox: SandboxProvider,
    catalog: AssetCatalog | None = None,
    engine: AgentEngine | None = None,
    session_store: SessionStore | None = None,
    config_builder: (
        Callable[[str], Awaitable[ResolvedAgentConfig]] | None
    ) = None,
    read_file_defn: ToolDefinition | None = None,
    write_file_defn: ToolDefinition | None = None,
    list_files_defn: ToolDefinition | None = None,
    workflow_store: WorkflowRunStore | None = None,
    executor_factory: (
        Callable[..., IterativeExecutor] | None
    ) = None,
) -> None:
    """Install the built-in runners against `registry`.

    Always wires the dependency-light core (`bash`, `noop`,
    `user_input`, `set_state`, `delete_state`, `branch`, `filter`,
    `compute`, `map`). Slots whose runners need extra wiring stay
    unregistered when the relevant kwargs are absent, leaving the
    registry to raise
    [`NodeTypeNotImplemented`][workflows.errors.NodeTypeNotImplemented]
    on dispatch — the gap is visible rather than silently filled.

    The slot-by-slot gating:

    - `kolsch/nodes/agent` — registers only when `engine`,
      `session_store`, `config_builder`, AND `catalog` are all
      supplied. The triple gate is intentional; a half-wired agent
      runner would silently misbehave.
    - `kolsch/nodes/workflow` — registers only when `catalog`,
      `workflow_store`, AND `executor_factory` are all supplied. The
      factory seam exists to avoid a circular import between this
      package and [`workflows.executor`][workflows.executor].
    - `kolsch/nodes/read_file` / `write_file` / `list_files` — each
      registers only when its `ToolDefinition` is supplied.
    - The catalog-driven authored-`@node` dispatch (the
      [`CatalogNodeRunner`][workflows.builtins.CatalogNodeRunner]
      fallback) installs when `catalog` is supplied.

    Args:
        registry: The executor's runner registry; mutated in place.
        bash_defn: The `kolsch/bash` tool definition the bash runner
            dispatches against.
        authz: Authorization provider forwarded into every
            tool-backed runner's dispatch context.
        secrets: Secrets provider forwarded into every tool-backed
            runner's dispatch context.
        sandbox: Sandbox provider forwarded into every tool-backed
            runner's dispatch context.
        catalog: The workflow asset catalog. Required for the
            authored-`@node` fallback, the agent runner, and the
            sub-workflow runner.
        engine: The agent engine — required for the agent runner.
        session_store: The session store — required for the agent
            runner.
        config_builder: Async callable resolving an agent ref into a
            `ResolvedAgentConfig` — required for the agent runner.
        read_file_defn: The `read_file` tool definition.
        write_file_defn: The `write_file` tool definition.
        list_files_defn: The `list_files` tool definition.
        workflow_store: The workflow run store — required for the
            sub-workflow runner.
        executor_factory: Callable returning an
            [`IterativeExecutor`][workflows.executor.IterativeExecutor]
            for nested child runs. Required for the sub-workflow runner.
    """
    registry.register(
        "kolsch/nodes/bash",
        BashRunner(
            bash_defn=bash_defn,
            authz=authz,
            secrets=secrets,
            sandbox=sandbox,
        ),
    )
    registry.register("kolsch/nodes/noop", NoopRunner())
    # `user_input` has no constructor dependencies; the runner reads
    # the per-node Jinja renderer through `ctx` and raises
    # `WorkflowPausedForInput` which the executor's worklist catches
    # separately from the failure path.
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    # State-mutation runners. Both are dependency-free (they read
    # `ctx` for the executor's render/emit hooks); registering them
    # here means a workflow using `set_state` / `delete_state` works
    # as soon as `register_builtins` runs, without the caller
    # threading more wiring.
    registry.register("kolsch/nodes/set_state", SetStateRunner())
    registry.register("kolsch/nodes/delete_state", DeleteStateRunner())
    # Branch / filter / compute runners. All three are
    # dependency-free (pure Jinja evaluation against the executor's
    # render hook); registered unconditionally so a workflow using
    # any of these types resolves immediately. The branch runner is
    # the second branchable source after bash; its `{value: bool}`
    # capture is read by
    # `workflows.branching.compute_branch_value`.
    registry.register("kolsch/nodes/branch", BranchRunner())
    registry.register("kolsch/nodes/filter", FilterRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    # Map runner. Dependency-free at registration (the registry
    # reference is captured so the runner can dispatch its worker's
    # runner per iteration). Registered unconditionally so a
    # workflow using `kolsch/nodes/map` resolves immediately.
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))
    # File-IO runners. Each requires a `ToolDefinition` built from
    # the in-engine `tools.builtins.make_<n>` factory (closure over
    # policy + root so the tool checks file access without
    # `ToolContext` carrying `path_policy` — matches the bash
    # runner's wiring). Each kwarg defaults to `None` so bash-only
    # test callers stay byte-identical; absent the kwarg the slot
    # keeps raising `NodeTypeNotImplemented`.
    if read_file_defn is not None:
        registry.register(
            "kolsch/nodes/read_file",
            ReadFileRunner(
                tool_defn=read_file_defn,
                authz=authz,
                secrets=secrets,
                sandbox=sandbox,
            ),
        )
    if write_file_defn is not None:
        registry.register(
            "kolsch/nodes/write_file",
            WriteFileRunner(
                tool_defn=write_file_defn,
                authz=authz,
                secrets=secrets,
                sandbox=sandbox,
            ),
        )
    if list_files_defn is not None:
        registry.register(
            "kolsch/nodes/list_files",
            ListFilesRunner(
                tool_defn=list_files_defn,
                authz=authz,
                secrets=secrets,
                sandbox=sandbox,
            ),
        )
    if catalog is not None:
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
    if (
        catalog is not None
        and engine is not None
        and session_store is not None
        and config_builder is not None
    ):
        registry.register(
            "kolsch/nodes/agent",
            AgentRunner(
                catalog=catalog,
                session_store=session_store,
                config_builder=config_builder,
                engine=engine,
            ),
        )
    # Sub-workflow runner. Needs the catalog (child-ref resolution),
    # the workflow run store (child run creation), an executor
    # factory (nested executor construction — the lambda seam exists
    # to avoid a circular import between `builtins/` and
    # `executor.py`), and the registry itself (passed back into the
    # nested executor). Absent the full quartet the slot keeps
    # raising `NodeTypeNotImplemented` so callers that don't yet
    # wire composition fail fast.
    if (
        catalog is not None
        and workflow_store is not None
        and executor_factory is not None
    ):
        registry.register(
            "kolsch/nodes/workflow",
            WorkflowSubRunner(
                catalog=catalog,
                store=workflow_store,
                executor_factory=executor_factory,
                registry=registry,
            ),
        )
