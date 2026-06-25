"""The :class:`ToolsSubsystem` facade — Module API Reference §9.

Composes the registry, dispatch, builtins, and MCP lifecycle behind
one object the Agent Engine consumes. Tools the engine receives are
already authorized + secret-bound + sandbox-wrapped at invoke time.

Phase 1 simplifications:
- The Module API Reference's ``register_tools_for_agent(config,
  framework_registry)`` signature is split here into ``register_for_agent``
  taking explicit ``tools`` / ``builtin_names`` / ``mcp_servers`` lists.
  The engine's CrewAI integration (Phase 1 follow-up) wraps this with
  the ResolvedAgentConfig dispatch.
- Declared-from-file loading (importing user .py modules at
  registration time) is a follow-up — for now callers pass already-
  imported ``@tool``-decorated functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from errors import (
    PlatformError,
    SecretNotFound,
    ToolNotFound,
    ToolSetupFailed,
)
from tools.decorator import (
    ToolContext,
    ToolDefinition,
    ToolInfo,
    ToolResult,
    get_tool_definition,
)
from tools.discovery import discover_tools_in_file
from tools.dispatch import dispatch_tool
from tools.registry import ToolRegistry, _MCPRegistration

from providers.authz import PermissionLayer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from files import PathPolicy
    from providers import (
        AuthorizationProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from providers.authz import Rule
    from schemas import AgentId
    from tools.mcp import MCPServer


class ToolsSubsystem:
    """The platform's Tools subsystem.

    Constructor takes the three providers (secrets / sandbox / authz)
    and optionally the path policy + platform root used by the
    file-touching builtins. Per-agent registration happens via
    :meth:`register_for_agent`; per-invocation dispatch via
    :meth:`invoke_tool`.
    """

    def __init__(
        self,
        *,
        secrets: SecretsProvider,
        sandbox: SandboxProvider,
        authz: AuthorizationProvider,
        path_policy: PathPolicy | None = None,
        root: Path | None = None,
    ) -> None:
        self._secrets = secrets
        self._sandbox = sandbox
        self._authz = authz
        self._path_policy = path_policy
        self._root = root
        self._registry = ToolRegistry()

    async def register_for_agent(
        self,
        *,
        agent_id: AgentId,
        tools: list[Callable[..., Awaitable[Any]]] | None = None,
        builtin_names: list[str] | None = None,
        declared_paths: list[Path] | None = None,
        mcp_servers: list[tuple[MCPServer, Literal["lazy", "eager"]]] | None = None,
        agent_permissions: list[Rule] | None = None,
        bundle_permissions: list[Rule] | None = None,
    ) -> list[ToolInfo]:
        """Register the given tool set for ``agent_id``.

        Per spec §6.1:
        - ``tools`` — already-imported ``@tool``-decorated functions
          (test helper / programmatic registration).
        - ``declared_paths`` — paths to ``.py`` files. Spec §6.1 step
          1: scan + import here, NOT in Asset Management (Am-I10 keeps
          asset discovery read-only of Python). Each file's
          ``@tool``-decorated functions are extracted via
          :func:`discover_tools_in_file` and registered.
        - For each tool (regardless of source): verify declared
          secrets via :meth:`SecretsProvider.exists`; raise
          :class:`ToolSetupFailed` with the underlying
          :class:`SecretNotFound` chained if any are missing.
        - For each eager MCP server: call :meth:`MCPServer.start` now.
        - For each lazy MCP server: register as a placeholder; the
          subsystem starts it on first invoke (T-I8).
        """
        registration = self._registry.ensure_agent(agent_id)

        for fn in tools or []:
            defn = get_tool_definition(fn)
            await self._verify_secrets(defn)
            registration.tools[defn.name] = defn

        for path in declared_paths or []:
            for fn in discover_tools_in_file(path):
                defn = get_tool_definition(fn)
                await self._verify_secrets(defn)
                registration.tools[defn.name] = defn

        for name in builtin_names or []:
            fn = self._make_builtin(name)
            defn = get_tool_definition(fn)
            registration.tools[defn.name] = defn

        for server, lifecycle in mcp_servers or []:
            reg = _MCPRegistration(server=server, lifecycle=lifecycle)
            if lifecycle == "eager":
                await server.start()
                reg.started = True
                # Populate the published tool list eagerly so the model
                # sees them at agent setup (T-I8 mirrors this for lazy
                # by registering the placeholder).
                listed = await server.list_tools()
                reg.tools = [str(t.get("name", "")) for t in listed]
            registration.mcp_servers[server.name] = reg

        # Pin the agent / bundle permission layers for the dispatcher.
        # Both are no-ops when the caller doesn't supply them — the
        # provider's existing two-layer (platform → default-deny)
        # behaviour stays intact for Phase 1 programmatic tests.
        registration.agent_permissions = list(agent_permissions or [])
        registration.bundle_permissions = list(bundle_permissions or [])

        return [defn.to_info() for defn in registration.tools.values()]

    async def invoke_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Run the full dispatch — Module API Reference §9.1.

        Supports both regular tools (looked up by name in the
        registry) and MCP invocations of the form
        ``mcp.<server>.<tool>`` (forwarded to the registered MCP
        server, starting it lazily if needed per spec §5.1).

        Phase 2.0 (TOOL-R7): enriches the caller's ``ToolContext``
        with the subsystem's ``workspace_root`` and ``path_policy``
        so bundle tools at ``<workspace>/bundles/kolsch/tools/`` can
        reach them without holding a closure (the way the Phase 1
        in-engine factories do). Fields the caller already set
        (non-None) are preserved.
        """
        agent_id = context.agent_id

        # Enrich the context with workspace_root + path_policy when
        # the subsystem knows them and the caller hasn't already
        # supplied them. Phase 1 callers / tests that construct
        # ToolContext directly stay byte-identical.
        updates: dict[str, Any] = {}
        if context.workspace_root is None and self._root is not None:
            updates["workspace_root"] = self._root
        if context.path_policy is None and self._path_policy is not None:
            updates["path_policy"] = self._path_policy
        enriched = context.model_copy(update=updates) if updates else context

        registration = self._registry.ensure_agent(agent_id)

        mcp_match = self._registry.lookup_mcp(agent_id, tool_name)
        if mcp_match is not None:
            return await self._invoke_mcp(
                mcp_match,
                tool_name,
                args,
                enriched,
                agent_permissions=registration.agent_permissions,
                bundle_permissions=registration.bundle_permissions,
            )

        defn = self._registry.get_tool(agent_id, tool_name)
        if defn is None:
            raise ToolNotFound(tool_name=tool_name, agent_id=str(agent_id))

        result = await dispatch_tool(
            defn=defn,
            args=args,
            context=enriched,
            authz=self._authz,
            secrets=self._secrets,
            sandbox=self._sandbox,
            agent_permissions=registration.agent_permissions,
            bundle_permissions=registration.bundle_permissions,
        )
        return ToolResult(result=result, metadata={})

    def list_registered_tools(self, agent_id: AgentId) -> list[ToolInfo]:
        return [d.to_info() for d in self._registry.list_tools(agent_id)]

    # --- internal --------------------------------------------------------

    async def _verify_secrets(self, defn: ToolDefinition) -> None:
        """Spec §6.1 step 3 — call ``secrets.exists`` for every
        declared secret. The first missing one raises ToolSetupFailed
        with the SecretNotFound chained as ``__cause__``."""
        for name in defn.secrets_needed:
            if await self._secrets.exists(name):
                continue
            try:
                # Use ``resolve`` so we get a real SecretNotFound
                # instance to chain (the catalog code on it is what
                # the operator sees in the cause envelope).
                await self._secrets.resolve(name)
            except SecretNotFound as exc:
                raise ToolSetupFailed(
                    tool_name=defn.name,
                    reason=f"required secret {name!r} is missing",
                ) from exc

    def _make_builtin(
        self,
        name: str,
    ) -> Callable[..., Awaitable[Any]]:
        # Late import keeps `builtins` from loading at module import
        # for callers that don't use them.
        from tools.builtins import make_builtin  # noqa: PLC0415

        if self._path_policy is None or self._root is None:
            msg = (
                f"cannot register builtin {name!r}: "
                "ToolsSubsystem was constructed without a path_policy/root"
            )
            raise RuntimeError(msg)
        return make_builtin(name, policy=self._path_policy, root=self._root)

    async def _invoke_mcp(
        self,
        match: tuple[_MCPRegistration, str],
        invocation_name: str,
        args: dict[str, Any],
        context: ToolContext,
        *,
        agent_permissions: list[Rule] | None = None,
        bundle_permissions: list[Rule] | None = None,
    ) -> ToolResult:
        reg, mcp_tool_name = match

        # Authorization happens at the MCP action namespace per spec
        # §5.4 — `mcp.<server>.<tool>`. The dispatch order below
        # mirrors the standard tool flow as closely as the spec allows.
        action = invocation_name  # already in the right shape
        # Phase 3.0 Step 4 — assemble the layers tuple from the
        # registration's agent + bundle permission lists.
        layers: tuple[PermissionLayer, ...] = ()
        if agent_permissions:
            layers = (
                *layers,
                PermissionLayer(name="agent", rules=tuple(agent_permissions)),
            )
        if bundle_permissions:
            layers = (
                *layers,
                PermissionLayer(name="bundle", rules=tuple(bundle_permissions)),
            )
        auth = await self._authz.allow(
            context.principal,
            action,
            layers=layers,
        )
        if not auth.allowed:
            from errors import ToolUnauthorized  # noqa: PLC0415

            raise ToolUnauthorized(
                tool_name=mcp_tool_name,
                action=action,
                principal=context.principal,
                matched_rule=(
                    auth.matched_rule.model_dump() if auth.matched_rule else None
                ),
            )

        # Lazy start — T-I8: subprocess starts on first use.
        if not reg.started:
            await reg.server.start()
            reg.started = True

        try:
            result = await reg.server.invoke(mcp_tool_name, args)
        except PlatformError:
            raise
        except Exception as exc:
            from errors import McpUpstreamError  # noqa: PLC0415

            raise McpUpstreamError(
                server_name=reg.server.name,
                tool=mcp_tool_name,
                response=str(exc),
            ) from exc
        return ToolResult(result=result, metadata={"source": "mcp"})
