"""The [`ToolsSubsystem`][tools.subsystem.ToolsSubsystem] facade.

Composes the registry, dispatch, builtins, and MCP lifecycle behind
one object the Agent Engine consumes. Tools the engine receives are
already authorized, secret-bound, and sandbox-wrapped at invoke time.
See the [tools subsystem](../explanation/tools-subsystem.md)
contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from errors import (
    PlatformError,
    SecretNotFound,
    ToolNotFound,
    ToolSetupFailed,
)
from providers.authz import PermissionLayer
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from cli.chat.service_registry import ChatServiceRegistry
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
    """The platform's Tools subsystem facade.

    Constructor takes the three providers (secrets, sandbox, authz)
    and optionally the path policy and platform root used by the
    file-touching builtins. Per-agent registration happens via
    [`register_for_agent`][tools.subsystem.ToolsSubsystem.register_for_agent];
    per-invocation dispatch via
    [`invoke_tool`][tools.subsystem.ToolsSubsystem.invoke_tool].
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
        # bug-020: the chat runtime injects its `ChatServiceRegistry`
        # sidecar here via `set_chat_services`. When present,
        # `invoke_tool` forwards it into every `dispatch_tool` call so
        # tools declaring `requires=[...]` see the corresponding
        # service handles on `ToolContext`. Default `None` preserves
        # headless callers (workflow runner, one-shot CLI, tests) —
        # every service field on the resulting `ToolContext` stays
        # `None` and `require_services(ctx, [...])` raises
        # `ToolNotAvailable` for any tool that asked for one.
        self._chat_services: ChatServiceRegistry | None = None

    def set_chat_services(
        self,
        registry: ChatServiceRegistry | None,
    ) -> None:
        """Register the chat runtime's `ChatServiceRegistry` sidecar.

        bug-020: `ChatApp.on_mount` calls this immediately after
        building `CardsImpl` / `PromptsImpl` / `WorkflowsImpl` so a
        tool declaring `requires=["cards", "workflows"]` sees a
        populated `ToolContext` at dispatch time. Passing `None`
        detaches (used by test teardown or a headless caller that
        rebooted the sidecar).

        The registry lands on `self._chat_services` and is forwarded
        into every subsequent `dispatch_tool` call as the
        `chat_services=` kwarg. Headless callers that never call this
        setter see the default `None` and the dispatcher's
        `_populate_service_fields` short-circuits — every service
        field on `ToolContext` stays `None`.

        Args:
            registry: The chat runtime's `ChatServiceRegistry`, or
                `None` to detach.
        """
        self._chat_services = registry

    async def register_for_agent(
        self,
        *,
        agent_id: AgentId,
        tools: list[Callable[..., Awaitable[Any]]] | None = None,
        builtin_names: list[str] | None = None,
        declared_paths: list[Path] | None = None,
        declared_owner_bundles: dict[Path, str] | None = None,
        mcp_servers: list[tuple[MCPServer, Literal["lazy", "eager"]]] | None = None,
        agent_permissions: list[Rule] | None = None,
        bundle_permissions: list[Rule] | None = None,
        home_bundle: str | None = None,
    ) -> list[ToolInfo]:
        """Register the given tool set for `agent_id`.

        Three intake channels with one common post-step. `tools` is
        already-imported `@tool`-decorated functions (test helper or
        programmatic registration). `declared_paths` is paths to
        `.py` files — scanned and imported here, not in Asset
        Management; each file's `@tool` functions are extracted via
        [`discover_tools_in_file`][tools.discovery.discover_tools_in_file].
        `builtin_names` selects from the platform's canonical
        builtins. For every tool (regardless of source), declared
        secrets are verified via `SecretsProvider.exists` and
        `ToolSetupFailed` is raised with the underlying
        `SecretNotFound` chained if any are missing.
        MCP servers register the same way: eager lifecycle starts
        the server now, lazy registers a placeholder and starts on
        first invoke.
        `home_bundle` and `declared_owner_bundles` together stamp
        every registered
        [`ToolDefinition`][tools.decorator.ToolDefinition] so its
        `action_name` resolves to the catalog ref
        `<home_bundle>/tools/<name>`:

        - Builtins are always attributed to `kolsch` (canonical
          set has no ref of its own).
        - Declared tools are stamped from
          `declared_owner_bundles[path]` when the map is supplied
          (the ref's authoritative owner bundle); otherwise fall
          back to `home_bundle` (preserves programmatic /
          test-driven callers).
        - Programmatic (`tools=`) callers stamp from `home_bundle`.

        When both `declared_owner_bundles[path]` and `home_bundle`
        are absent for a given tool, `action_name` falls back to
        the legacy `tool.<name>` form so existing test paths stay
        byte-identical.

        The declared-owner map is what fixes
        bug-authz-rule-caller-bundle-mismatch: a butler agent in
        `andre` extending `kolsch/agents/helper` inherits declared
        refs like `kolsch/tools/read_file`. Before this parameter,
        the single `home_bundle` kwarg (the caller agent's bundle)
        was stamped onto every declared tool, so `defn.action_name`
        returned `andre/tools/read_file` and modal-granted rules
        landed under the wrong bundle. The map preserves each
        ref's authoritative bundle.

        Args:
            agent_id: The agent the registration is scoped to.
            tools: Already-imported `@tool` functions.
            builtin_names: Names of canonical builtins to register.
            declared_paths: Paths to `.py` files declaring tools.
            declared_owner_bundles: Optional per-path owner-bundle
                map. When supplied, stamps each declared tool's
                `home_bundle` from `declared_owner_bundles[path]`
                (the ref's leading segment). When absent, declared
                tools fall through to `home_bundle`.
            mcp_servers: `(server, lifecycle)` pairs to register.
            agent_permissions: The agent's resolved permission rules.
            bundle_permissions: The bundle's resolved permission rules.
            home_bundle: Bundle slug stamped on programmatic tools
                and on declared tools not covered by
                `declared_owner_bundles`. Serves as the DEFAULT for
                the two channels that don't carry per-tool owner
                info; the engine's setup path always provides
                `declared_owner_bundles`, so this only applies to
                programmatic-tools callers there.

        Returns:
            Metadata-only views of every tool registered for the agent.

        Raises:
            ToolSetupFailed: A declared secret is missing or a tool
                file fails to import.
        """
        registration = self._registry.ensure_agent(agent_id)

        for fn in tools or []:
            defn = get_tool_definition(fn)
            await self._verify_secrets(defn)
            if home_bundle is not None and defn.home_bundle is None:
                defn.home_bundle = home_bundle
            registration.tools[defn.name] = defn

        for path in declared_paths or []:
            # Per-path owner bundle (from the declared ref's leading
            # segment) is the AUTHORITATIVE home for tools resolved
            # through the declared:-refs channel. Falls back to the
            # caller-supplied `home_bundle` when the map is absent
            # (programmatic / test callers that only pass raw
            # `declared_paths=[Path, ...]`).
            path_owner: str | None = None
            if declared_owner_bundles is not None:
                path_owner = declared_owner_bundles.get(path)
            stamp = path_owner if path_owner is not None else home_bundle
            for fn in discover_tools_in_file(path):
                defn = get_tool_definition(fn)
                await self._verify_secrets(defn)
                if stamp is not None and defn.home_bundle is None:
                    defn.home_bundle = stamp
                registration.tools[defn.name] = defn

        for name in builtin_names or []:
            fn = self._make_builtin(name)
            defn = get_tool_definition(fn)
            # Builtins are always attributed to the kolsch canonical
            # bundle. Stamp unconditionally so the authz action
            # string is correct regardless of whether the caller
            # supplied a ``home_bundle`` (engine path) or not (test
            # path).
            if defn.home_bundle is None:
                defn.home_bundle = "kolsch"
            registration.tools[defn.name] = defn

        for server, lifecycle in mcp_servers or []:
            reg = _MCPRegistration(server=server, lifecycle=lifecycle)
            if lifecycle == "eager":
                await server.start()
                reg.started = True
                # Populate the published tool list eagerly so the
                # model sees them at agent setup; the lazy path
                # mirrors this by registering a placeholder.
                listed = await server.list_tools()
                reg.tools = [str(t.get("name", "")) for t in listed]
            registration.mcp_servers[server.name] = reg

        # Pin the agent / bundle permission layers for the dispatcher.
        # Both are no-ops when the caller doesn't supply them — the
        # provider's existing two-layer (platform → default-deny)
        # behaviour stays intact for programmatic tests.
        registration.agent_permissions = list(agent_permissions or [])
        registration.bundle_permissions = list(bundle_permissions or [])

        return [defn.to_info() for defn in registration.tools.values()]

    async def invoke_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Run the full dispatch for one tool call.

        Supports both regular tools (looked up by name in the
        registry) and MCP invocations of the form
        `mcp.<server>.<tool>` (forwarded to the registered MCP
        server, starting it lazily if needed).

        Enriches the caller's [`ToolContext`][tools.decorator.ToolContext]
        with the subsystem's `workspace_root` and `path_policy` so
        bundle tools at `<workspace>/bundles/kolsch/tools/` can reach
        them without holding a closure. Fields the caller already set
        (non-`None`) are preserved.

        Args:
            tool_name: Name as registered, or `mcp.<server>.<tool>`.
            args: Raw arguments passed by the framework.
            context: The runtime context handed to the tool body.

        Returns:
            A [`ToolResult`][tools.decorator.ToolResult] wrapping the
            tool function's return value.

        Raises:
            ToolNotFound: No tool of that name is registered for the
                context's agent.
        """
        agent_id = context.agent_id

        # Enrich the context with workspace_root + path_policy when
        # the subsystem knows them and the caller hasn't already
        # supplied them. Callers and tests that construct
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
            # bug-020: forward the chat runtime's `ChatServiceRegistry`
            # so tools declaring `requires=[...]` see populated
            # service fields on `ToolContext`. `None` in headless
            # mode; dispatch's `_populate_service_fields` short-
            # circuits and every service field stays `None`.
            chat_services=self._chat_services,
        )
        return ToolResult(result=result, metadata={})

    def list_registered_tools(self, agent_id: AgentId) -> list[ToolInfo]:
        """Return metadata-only views of every tool registered for `agent_id`."""
        return [d.to_info() for d in self._registry.list_tools(agent_id)]

    # --- internal --------------------------------------------------------

    async def _verify_secrets(self, defn: ToolDefinition) -> None:
        """Call ``secrets.exists`` for every declared secret on ``defn``.

        The first missing one raises ``ToolSetupFailed`` with the
        underlying ``SecretNotFound`` chained as ``__cause__``.
        """
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

        # Authorization happens at the MCP authz action string
        # `mcp.<server>.<tool>`. The dispatch order below mirrors
        # the standard tool flow as closely as the contract allows.
        action = invocation_name  # already in the right shape
        # Assemble the layers tuple from the registration's
        # agent + bundle permission lists.
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

        # Lazy start — the subprocess starts on first use.
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
