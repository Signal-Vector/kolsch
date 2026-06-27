"""Phase 3.1 + Phase 3.2 built-in runtime node implementations.

Public surface:

- :class:`BashRunner` — routes through :func:`tools.dispatch.dispatch_tool`.
- :class:`NoopRunner` — returns ``{}``.
- :class:`AgentRunner` — invokes a catalogued agent via
  :class:`engine.AgentEngine` (Phase 3.2).
- :class:`CatalogNodeRunner` — generic dispatch for authored Python
  ``@node`` modules (Step 3).
- :func:`with_timeout` — wraps a runner with an
  :func:`asyncio.wait_for` deadline.
- :func:`with_retry` — wraps a runner with a flat retry loop.
- :func:`register_builtins` — convenience wiring for the Step 5 CLI
  ``_cmd_run`` handler; registers ``BashRunner`` + ``NoopRunner``
  against a :class:`workflows.executor.RunnerRegistry`. Phase 3.2 adds
  an opt-in :class:`AgentRunner` registration when the caller supplies
  the engine + store + config-builder triple; absent the triple the
  ``kolsch/nodes/agent`` slot keeps raising
  :class:`workflows.errors.NodeTypeNotImplemented(phase="3.2")` so
  callers that don't yet wire agents (bash-only tests, scripts) fail
  fast and visibly.

Built-in slots beyond the registered set continue to raise
:class:`workflows.errors.NodeTypeNotImplemented` via the registry's
phase-numbered default lookup; Steps 3.3-3.7 register their runners
as they ship.
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
from workflows.builtins.noop import NoopRunner
from workflows.builtins.read_file import ReadFileRunner
from workflows.builtins.retry import with_retry
from workflows.builtins.set_state import SetStateRunner
from workflows.builtins.timeout import with_timeout
from workflows.builtins.user_input import UserInputRunner
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
    from workflows.executor import RunnerRegistry

__all__ = [
    "AgentRunner",
    "BashRunner",
    "BranchRunner",
    "CatalogNodeRunner",
    "ComputeRunner",
    "DeleteStateRunner",
    "FilterRunner",
    "ListFilesRunner",
    "NoopRunner",
    "ReadFileRunner",
    "SetStateRunner",
    "UserInputRunner",
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
) -> None:
    """Register the Phase 3.1 + 3.2 runtime node types against ``registry``.

    Built-in slots (``kolsch/nodes/bash``, ``kolsch/nodes/noop``) get
    explicit registrations. When ``catalog`` is supplied (Step 3 of
    Phase 3.1), :class:`CatalogNodeRunner` installs as the registry's
    fallback dispatch so any non-built-in 3-seg ref routes through the
    authored-Python pipeline.

    Phase 3.2: when ``engine`` + ``session_store`` + ``config_builder``
    are ALL supplied, :class:`AgentRunner` registers against
    ``kolsch/nodes/agent`` (and ``catalog`` MUST also be supplied —
    the runner needs catalog access for the defensive ref lookup). The
    triple gate is intentional — callers that don't yet have an
    engine wired (e.g. bash-only tests) should not silently install a
    half-built agent runner; instead the slot keeps raising
    :class:`workflows.errors.NodeTypeNotImplemented(phase="3.2")` so
    the gap is visible.
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
    # Phase 3.4 — ``user_input`` has no constructor dependencies; the
    # runner reads the per-node Jinja renderer through ``ctx`` and
    # raises ``WorkflowPausedForInput`` which the executor's worklist
    # catches separately from the failure path.
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    # Phase 3.5 Step 1 — state-mutation runners. Both are dependency-
    # free (they read ``ctx`` for the executor's render/emit hooks);
    # registering them here means a workflow using ``set_state`` /
    # ``delete_state`` works as soon as ``register_builtins`` runs,
    # without the caller threading more wiring (master spec §4.3).
    registry.register("kolsch/nodes/set_state", SetStateRunner())
    registry.register("kolsch/nodes/delete_state", DeleteStateRunner())
    # Phase 3.6 Step 0 — branch / filter / compute runners. All three
    # are dependency-free (pure Jinja evaluation against the
    # executor's render hook); registered unconditionally so a
    # workflow using any of these types resolves immediately. The
    # branch runner is the second branchable source after bash; its
    # ``{value: bool}`` capture is read by
    # :func:`workflows.branching.compute_branch_value`.
    registry.register("kolsch/nodes/branch", BranchRunner())
    registry.register("kolsch/nodes/filter", FilterRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    # Phase 3.5 Step 2 — file-IO runners. Each requires a
    # :class:`ToolDefinition` built from the in-engine
    # ``tools.builtins.make_<n>`` factory (closure over policy + root
    # so the tool checks file access without ToolContext carrying
    # path_policy — matches the bash runner's wiring). Each kwarg
    # defaults to ``None`` so bash-only test callers stay
    # byte-identical; absent the kwarg the slot keeps raising
    # :class:`workflows.errors.NodeTypeNotImplemented(phase="3.5")`.
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
