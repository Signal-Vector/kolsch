"""Phase 3.1 built-in runtime node implementations.

Public surface:

- :class:`BashRunner` — routes through :func:`tools.dispatch.dispatch_tool`.
- :class:`NoopRunner` — returns ``{}``.
- :func:`with_timeout` — wraps a runner with an
  :func:`asyncio.wait_for` deadline.
- :func:`with_retry` — wraps a runner with a flat retry loop.
- :func:`register_builtins` — convenience wiring for the Step 5 CLI
  ``_cmd_run`` handler; registers ``BashRunner`` + ``NoopRunner``
  against a :class:`workflows.executor.RunnerRegistry`.

Only the two runtime types Phase 3.1 ships register here. Every
other built-in slot continues to raise
:class:`workflows.errors.NodeTypeNotImplemented` via the registry's
default lookup (Step 1 wired the phase-numbered defaults).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflows.builtins.bash import BashRunner
from workflows.builtins.noop import NoopRunner
from workflows.builtins.retry import with_retry
from workflows.builtins.timeout import with_timeout

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition
    from workflows.executor import RunnerRegistry

__all__ = [
    "BashRunner",
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
) -> None:
    """Register the Phase 3.1 runtime node types against ``registry``.

    The CLI ``_cmd_run`` handler (Step 5) is the canonical caller —
    it resolves ``bash_defn`` from the workspace's
    ``bundles/kolsch/tools/bash.py`` and forwards the platform's
    already-bootstrapped providers.

    Tests can install a stub bash :class:`ToolDefinition` for
    in-process verification without needing the starter bundle on
    disk (see ``tests/component/test_workflow_builtins.py``).
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
