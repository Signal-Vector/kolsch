"""Phase 3.1 built-in runtime node implementations.

Public surface:

- :class:`BashRunner` — routes through :func:`tools.dispatch.dispatch_tool`.
- :class:`NoopRunner` — returns ``{}``.
- :class:`CatalogNodeRunner` — generic dispatch for authored Python
  ``@node`` modules (Step 3).
- :func:`with_timeout` — wraps a runner with an
  :func:`asyncio.wait_for` deadline.
- :func:`with_retry` — wraps a runner with a flat retry loop.
- :func:`register_builtins` — convenience wiring for the Step 5 CLI
  ``_cmd_run`` handler; registers ``BashRunner`` + ``NoopRunner``
  against a :class:`workflows.executor.RunnerRegistry` and optionally
  installs the :class:`CatalogNodeRunner` as the authored-Python
  fallback when a catalog is supplied.

Built-in slots beyond ``bash`` + ``noop`` continue to raise
:class:`workflows.errors.NodeTypeNotImplemented` via the registry's
phase-numbered default lookup; Steps 3.2–3.7 register their runners
as they ship.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflows.builtins.bash import BashRunner
from workflows.builtins.catalog_node_runner import CatalogNodeRunner
from workflows.builtins.noop import NoopRunner
from workflows.builtins.retry import with_retry
from workflows.builtins.timeout import with_timeout

if TYPE_CHECKING:
    from assets.catalog import AssetCatalog
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition
    from workflows.executor import RunnerRegistry

__all__ = [
    "BashRunner",
    "CatalogNodeRunner",
    "NoopRunner",
    "register_builtins",
    "with_retry",
    "with_timeout",
]


def register_builtins(
    registry: RunnerRegistry,
    *,
    bash_defn: ToolDefinition,
    authz: AuthorizationProvider,
    secrets: SecretsProvider,
    sandbox: SandboxProvider,
    catalog: AssetCatalog | None = None,
) -> None:
    """Register the Phase 3.1 runtime node types against ``registry``.

    Built-in slots (``kolsch/nodes/bash``, ``kolsch/nodes/noop``) get
    explicit registrations. When ``catalog`` is supplied (Step 3),
    :class:`CatalogNodeRunner` is installed as the registry's
    fallback dispatch so any non-built-in 3-seg ref routes through
    the authored-Python pipeline. Tests can omit ``catalog`` to
    keep the registry built-in-only.
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
    if catalog is not None:
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
