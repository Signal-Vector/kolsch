"""Inline ``PromptCallback`` — routes approvals through `PromptsImpl`.

The chat lifecycle registers this callable as the authz provider's
prompt callback via ``authz.register_prompt_callback`` (see
``src/providers/authz.py``). When the callback fires, it derives a
[`PromptSource`][tools.services.prompts.PromptSource] from the
supplied `ExecutionContext` (workflow-scoped when the executor set
`workflow_run_id`; tool-scoped otherwise), calls
[`Prompts.choice`][tools.services.prompts.Prompts.choice] on the
injected impl, then translates the response string back into a
`PromptCallbackReturn` for the authz provider.

The widget mount is no longer this module's concern: the
[`EventAdapter`][cli.chat.event_adapter.EventAdapter] listens for the
`kolsch.prompt.requested` event this callback triggers (via
`PromptsImpl.choice`) and mounts `ApprovalBlock` from its dispatch
path. This module never imports the widget — that separation is the
spec-004 refactor of the pre-spec-004 direct-mount shape.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from providers.authz import AuthzEffect, Rule

if TYPE_CHECKING:
    from cli.chat.app import ChatApp
    from engine.context import ExecutionContext
    from tools.services.prompts import Prompts, PromptSource


_LOGGER = logging.getLogger(__name__)


PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


# The four keys the approval choice pane emits. Kept in sync with
# `ApprovalChoicePane.Chosen` (see
# `src/cli/chat/widgets/approval_choice_pane.py`). The refactor
# routes through the event bus, but the vocabulary is identical.
_APPROVAL_OPTIONS: list[str] = ["y", "a", "n", "d"]


class InlineApprovalCallback:
    """Route approvals through the `Prompts` service.

    Constructed once per chat session (in `ChatApp.on_mount`) with a
    live `PromptsImpl` handle. The callback is called with keyword
    args ``rule_id``, ``action``, ``target``, ``caller_chain`` (per
    the `PromptCallback` Protocol at `src/providers/authz.py`), plus
    the spec-004 r-review r1 ``execution_context`` passthrough. It
    returns a `PromptCallbackReturn`.
    """

    def __init__(self, app: ChatApp, prompts: Prompts) -> None:
        self._app = app
        self._prompts = prompts

    async def __call__(
        self,
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
        execution_context: ExecutionContext | None = None,
    ) -> PromptCallbackReturn:
        """Emit `kolsch.prompt.requested`; translate the response.

        `caller_chain` is accepted for the `PromptCallback` Protocol
        signature but not consumed here — the widget's frozen footer
        is enough for the user; the chain lives on the JSONL for
        later analysis.

        `execution_context` is the spec-004 r-review r1 workflow-
        context passthrough. When
        `execution_context.workflow_run_id` is set (executor-side —
        see spec-011), the emitted event's `PromptSource` is
        workflow-scoped so a downstream renderer can group approvals
        by run. Non-workflow callers pass `None`; the source is
        tool-scoped.
        """
        # r-review r1: `get_running_loop()` replaces the pre-existing
        # `get_event_loop()` violation at line 64. The callback fires
        # inside the Textual app's event loop; the running-loop
        # accessor is the correct API when a loop is already active.
        loop = asyncio.get_running_loop()
        _ = loop
        _ = caller_chain

        source = self._derive_source(execution_context)
        question = _format_question(rule_id, action, target)
        try:
            response = await self._prompts.choice(
                question,
                _APPROVAL_OPTIONS,
                source=source,
            )
        finally:
            # The composer freezes while awaiting approval (mounted
            # by `ChatApp.mount_approval_block` via the adapter's
            # dispatch); unfreeze on any exit path, including
            # `CancelledError` from a `WIRE_PROMPT_CANCELLED` event.
            try:
                self._app.clear_awaiting_approval()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                _LOGGER.debug(
                    "InlineApprovalCallback: clear_awaiting_approval raised",
                    exc_info=True,
                )
        return _translate_response(response)

    @staticmethod
    def _derive_source(
        execution_context: ExecutionContext | None,
    ) -> PromptSource:
        """Build a `PromptSource` from the caller's context.

        Returns a workflow-scoped source when
        `execution_context.workflow_run_id` is set (executor-side —
        see spec-011); tool-scoped otherwise. The `run_chain`
        breadcrumb stays empty here; spec-017 populates it for
        nested workflows.
        """
        if (
            execution_context is not None
            and execution_context.workflow_run_id is not None
        ):
            source: PromptSource = {
                "kind": "workflow",
                "id": str(execution_context.workflow_run_id),
                "run_id": str(execution_context.workflow_run_id),
                "node_name": execution_context.workflow_node_name or "",
                "run_chain": [],
            }
            return source
        return {"kind": "tool", "id": ""}


def _format_question(
    rule_id: str | None,
    action: str,
    target: str | None,
) -> str:
    """Build the human-facing question string for the approval prompt.

    The widget renders its own richer header from the payload's
    `rule_id` / `action` / `target`; this string is what the
    Prompts.choice contract accepts. Kept lean so a non-widget
    consumer (a test double, a headless surface) still gets a legible
    prompt.
    """
    rule_label = rule_id or "(no matching rule)"
    target_label = target or ""
    return f"Tool approval — {action} {target_label} (rule: {rule_label})"


def _translate_response(response: str) -> PromptCallbackReturn:
    """Map an `ApprovalChoicePane` key back to a `PromptCallbackReturn`.

    - ``y`` → allow-once (`AuthzEffect.ALLOW`).
    - ``n`` (or an empty / unknown response — treated as deny) →
      deny-once (`AuthzEffect.DENY`).
    - ``a`` / ``d`` are pattern-persist paths that go through the
      widget's multi-step flow. Today the `Prompts` service returns
      only the final resolved key; the widget-side pattern-persist
      round-trip is not yet expressed on the event surface (spec-006
      lands `CardsImpl` and the widget retirement). Until then, `a`
      resolves to allow-once and `d` to deny-once — safe defaults
      that match the widget's fast-path behaviour.

    The return contract is the `PromptCallback` Protocol's
    `AuthzEffect | tuple[AuthzEffect, Rule]`; only the effect leg is
    exercised here.
    """
    if response == "y" or response == "a":
        return AuthzEffect.ALLOW
    return AuthzEffect.DENY


__all__ = ["InlineApprovalCallback", "PromptCallbackReturn"]
