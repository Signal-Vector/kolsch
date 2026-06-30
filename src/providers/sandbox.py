"""Sandbox provider — execute work coroutines under a policy.

The v1 `native` variant is a pass-through of the work coroutine; the
Protocol plus registry seam lets future isolation variants (containers,
resource limits) drop in without changing call sites. For the full
contract — result/exception transparency, policy validation, async
shape — see the [secrets-sandbox-authz
contract](../../docs/explanation/secrets-sandbox-authz-contract.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from errors import ConfigInvalid

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

T = TypeVar("T")


class SandboxProvider(Protocol):
    """The single-method contract every sandbox variant honours."""

    async def execute(
        self,
        work: Callable[[], Awaitable[T]],
        policy: dict[str, Any],
    ) -> T:
        """Execute `work` under `policy`; return what `work` returns.

        For `native`, `policy` is ignored. Real variants validate the
        policy and raise `SandboxPolicyInvalid` for shapes they don't
        support.
        """


# ---------------------------------------------------------------------------
# Native variant
# ---------------------------------------------------------------------------


class NativeSandboxProvider:
    """Pass-through sandbox — literally `await work()`.

    Accepted for v1 because the platform is local-first and
    single-user; the tools the platform runs are the tools the user
    installed and trusts. Real isolation variants arrive when
    multi-tenancy, downloaded bundles, or untrusted sources land.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # Native variant has no configuration; accept and ignore so the
        # factory can hand whatever shape arrived in platform.yaml.
        _ = config

    async def execute(
        self,
        work: Callable[[], Awaitable[T]],
        policy: dict[str, Any],  # noqa: ARG002 — contract-mandated, ignored here
    ) -> T:
        """Run `work` directly; result and exceptions propagate unchanged."""
        return await work()


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Callable[..., SandboxProvider]] = {}


def register_sandbox_variant(
    name: str,
    factory: Callable[..., SandboxProvider],
) -> None:
    """Register a variant factory under `name`."""
    _REGISTRY[name] = factory


def make_sandbox_provider(*, config: dict[str, Any]) -> SandboxProvider:
    """Dispatch by `config['variant']` per `platform.yaml`'s `sandbox` block.

    Raises:
        ConfigInvalid: `variant` is unset or names no registered factory.
    """
    variant = config.get("variant")
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant or "")
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.sandbox",
                    "path": "/sandbox/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown sandbox variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.sandbox.make_sandbox_provider",
        )
    return factory(config=body)


register_sandbox_variant("native", NativeSandboxProvider)
