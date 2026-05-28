"""Sandbox provider — spec Module 2 + Module API Reference §7.

v1 ships the ``native`` variant: pass-through of the work coroutine.
The Protocol + registry are in place so future variants (containers,
resource limits, isolation) drop in without changing call sites.

Conformance invariants:
- Sb-I1 / Sb-I2 — result + exception transparency for ``native``.
- Sb-I3 — variants MUST honour their policy. ``native``'s policy is
  empty by design.
- Sb-I4 — invalid policy MUST raise :class:`SandboxPolicyInvalid`
  before the work runs. ``native`` has nothing to validate.
- Sb-I5 — all methods are ``async``.
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
        """Execute ``work`` under ``policy``; return what ``work`` returns.

        For ``native``, ``policy`` is ignored. Real variants validate
        the policy and raise :class:`SandboxPolicyInvalid` for shapes
        they don't support (Sb-I4).
        """


# ---------------------------------------------------------------------------
# Native variant
# ---------------------------------------------------------------------------


class NativeSandboxProvider:
    """Spec §2.3 — literally ``await work()``.

    Accepted for v1 because the platform is local-first and
    single-user; the tools the platform runs are the tools the user
    installed and trusts. Sandboxing arrives in Phase 2+ when
    multi-tenancy / downloaded bundles / untrusted sources appear.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # Native variant has no configuration; accept and ignore so the
        # factory can hand whatever shape arrived in platform.yaml.
        _ = config

    async def execute(
        self,
        work: Callable[[], Awaitable[T]],
        policy: dict[str, Any],  # noqa: ARG002 — spec-mandated, ignored here
    ) -> T:
        # Pass-through: result and exceptions propagate unchanged
        # (Sb-I1 / Sb-I2).
        return await work()


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Callable[..., SandboxProvider]] = {}


def register_sandbox_variant(
    name: str,
    factory: Callable[..., SandboxProvider],
) -> None:
    _REGISTRY[name] = factory


def make_sandbox_provider(*, config: dict[str, Any]) -> SandboxProvider:
    """Dispatch by ``config['variant']`` per platform.yaml §sandbox."""
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
