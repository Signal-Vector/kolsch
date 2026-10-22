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
from typing import TYPE_CHECKING, Any

from providers.authz import AuthzEffect, Rule

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from assets.catalog import AssetCatalog
    from cli.chat.app import ChatApp
    from engine.context import ExecutionContext
    from providers.authz import RulePersistRequest
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
        # bug-021 Symptom 1: forward the raw fields into `metadata` so
        # `PromptsImpl.choice` merges them into the `WIRE_PROMPT_REQUESTED`
        # payload at the top level, where
        # `EventAdapter._handle_prompt_requested` already reads them.
        # Pre-bug-021 the payload only carried the composite question
        # string, so the ApprovalBlock's `Tool` / `Args` / `Rule`
        # context lines rendered empty. `metadata` is a superset —
        # existing headless callers keep the composite `prompt` string
        # verbatim for their own rendering.
        metadata: dict[str, Any] = {
            "rule_id": rule_id,
            "action": action,
            "target": target,
        }
        try:
            response = await self._prompts.choice(
                question,
                _APPROVAL_OPTIONS,
                source=source,
                metadata=metadata,
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

        spec-014 r-review r1: workflow branch carries `tool_call_id`
        alongside `run_id` / `node_name` so downstream renderers can
        correlate the approval back to the invoking tool call even
        when it fires inside a workflow node. `ExecutionContext` today
        doesn't surface `tool_call_id` (the field is on
        `ToolInvocationContext` which is a different envelope), so this
        helper falls back to an empty string — the workflow-scoped
        renderer routes by `run_id` + `node_name` and doesn't require
        the call id.

        bug-107: prefer the explicit `execution_context` parameter (kept
        for test surface); when `None`, fall back to the engine-set
        `_current_execution_context` ContextVar (production wiring —
        `_Exchange.run` publishes `self.context` there). Without this
        fallback the parameter is always `None` in production (no caller
        in `providers/authz.py::_resolve_prompt` / `_resolve_no_rule`
        threads it through), so the workflow branch below is dead code
        and every workflow-nested approval ships as `source.kind="tool"`.
        """
        if execution_context is None:
            from engine.engine import current_execution_context  # noqa: PLC0415

            execution_context = current_execution_context()
        raw_call_id = (
            getattr(execution_context, "tool_call_id", None)
            if execution_context is not None
            else None
        )
        tool_call_id = str(raw_call_id) if raw_call_id is not None else ""
        if (
            execution_context is not None
            and execution_context.workflow_run_id is not None
        ):
            source: PromptSource = {
                "kind": "workflow",
                "id": str(execution_context.workflow_run_id),
                "run_id": str(execution_context.workflow_run_id),
                "node_name": execution_context.workflow_node_name or "",
                "tool_call_id": tool_call_id,
                "run_chain": [],
            }
            return source
        return {
            "kind": "tool",
            "id": tool_call_id,
            "tool_call_id": tool_call_id,
        }


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

    Fast paths:

    - ``y`` → allow-once (`AuthzEffect.ALLOW`).
    - ``n`` (or an empty / unknown response — treated as deny) →
      deny-once (`AuthzEffect.DENY`).

    bug-025 (closes bug-022) — Rule persistence round-trip:

    - ``a`` (allow-persist) / ``d`` (deny-persist) with a rebuilt
      widget-side ``Rule`` return the tuple form
      ``(AuthzEffect.ALLOW|DENY, Rule)``. The persist branch at
      ``RuleBasedAuthorizationProvider._resolve_prompt`` fires and the
      ``_rule_added_hook`` inserts that one rule into the rules
      currently in ``platform.yaml`` (bug-162).
    - ``a`` / ``d`` WITHOUT a rebuilt Rule (e.g. the wire event
      dropped the ``rule`` payload — a pre-bug-025 caller or a
      partial-mount degraded path) fall back to allow-once / deny-once
      so the flow still resolves, matching the pre-bug-025 behaviour.

    The Rule is carried via the
    [`PromptResponse`][cli.chat.services.prompts_impl.PromptResponse]
    `str` subclass — reading `.rule` on any bare ``str`` returns
    ``None`` via the ``isinstance`` guard so non-widget callers
    (headless surfaces, existing tests using ``AsyncMock`` on
    ``Prompts.choice``) stay byte-identical.

    The return contract is the `PromptCallback` Protocol's
    `AuthzEffect | tuple[AuthzEffect, Rule]`.
    """
    # bug-025: local import to avoid touching module-import ordering.
    from cli.chat.services.prompts_impl import PromptResponse  # noqa: PLC0415

    rule_dict: dict[str, Any] | None = None
    if isinstance(response, PromptResponse):
        rule_dict = response.rule

    if response == "y":
        return AuthzEffect.ALLOW
    if response == "a":
        if rule_dict is not None:
            try:
                return (AuthzEffect.ALLOW, Rule(**rule_dict))
            except Exception:
                _LOGGER.exception(
                    "InlineApprovalCallback: Rule(**rule_dict) failed; "
                    "collapsing to allow-once",
                )
        return AuthzEffect.ALLOW
    if response == "d":
        if rule_dict is not None:
            try:
                return (AuthzEffect.DENY, Rule(**rule_dict))
            except Exception:
                _LOGGER.exception(
                    "InlineApprovalCallback: Rule(**rule_dict) failed; "
                    "collapsing to deny-once",
                )
        return AuthzEffect.DENY
    return AuthzEffect.DENY


__all__ = ["InlineApprovalCallback", "PromptCallbackReturn"]


class RulePersistHook:
    """The chat's `on_rule_added` hook: save one "always" rule (bug-162).

    Registered with
    [`register_prompt_callback`][providers.authz.RuleBasedAuthorizationProvider.register_prompt_callback]
    and awaited by the provider after its in-memory insert. Runs
    [`persist_rule_request`][cli.permissions.persist_rule_request] OFF
    the event loop (`asyncio.to_thread`; a write is ~25 ms, up to the
    5 s lock wait under contention), inserting the one rule into the
    rules **currently** in `platform.yaml` — never the provider's
    startup-snapshot list.

    Never raises. On any failure the rule stays live for this chat
    only, a WARNING is logged and `notify` gets a warning naming the
    path, the reason and the `kolsch permissions add …` command that
    saves it. On success, when the disk rules (before the insert)
    differ from the chat's loaded rules — both sides minus every rule
    this hook saved, is saving, or failed to save — `notify`
    gets a one-time warning per distinct difference: those edits are
    kept on disk but not enforced until `/reload` (D1 — the in-memory
    rules are never rebuilt from disk here).

    Args:
        yaml_path: The workspace `platform.yaml`.
        authz: Returns the live provider (read at call time, so it
            follows a `/reload` swap).
        catalog: Returns the live asset catalog for rule validation.
        notify: `notify(text, kind)` — schedules a transcript notice;
            must be safe to call from the event loop.
    """

    def __init__(
        self,
        *,
        yaml_path: Path,
        authz: Callable[[], object],
        catalog: Callable[[], AssetCatalog | None],
        notify: Callable[..., None],
    ) -> None:
        self._yaml_path = yaml_path
        self._authz = authz
        self._catalog = catalog
        self._notify = notify
        self._drift_noticed: set[tuple[Any, ...]] = set()
        # Rules this hook is saving right now (another "always" answer
        # can land while a save is in flight), rules it saved, and rules
        # whose save failed (session-only). All are left out of the
        # drift comparison — they differ from (or sit at a different
        # position on) disk because of this chat, not because someone
        # edited the file. Tracked by object identity.
        self._in_flight: list[Rule] = []
        self._saved: list[Rule] = []
        self._unsaved: list[Rule] = []

    async def __call__(self, request: RulePersistRequest) -> None:
        """Persist `request.rule`; report failures and disk drift via `notify`."""
        from cli.permissions import (  # noqa: PLC0415
            permissions_add_command,
            persist_rule_request,
            rule_identity,
        )
        from errors import AuthzRulesPersistFailed  # noqa: PLC0415
        from providers.authz import RuleBasedAuthorizationProvider  # noqa: PLC0415

        rule = request.rule
        self._in_flight.append(rule)
        try:
            result = await asyncio.to_thread(
                persist_rule_request,
                self._yaml_path,
                request,
                catalog=self._catalog(),
            )
        except Exception as exc:  # noqa: BLE001 — the hook must never raise
            self._unsaved.append(rule)
            if isinstance(exc, AuthzRulesPersistFailed):
                reason = f"{exc.detail['reason']}: {exc.detail['detail']}"
                if exc.detail["reason"] == "invalid_rules":
                    reason += (
                        " (if a bundle was installed since this chat started, "
                        "run /reload first)"
                    )
            else:
                reason = f"{type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "chat: 'always' rule %r %r %s not saved to %s: %s",
                rule.what,
                rule.pattern,
                rule.effect,
                self._yaml_path,
                reason,
            )
            self._notify(
                f"Rule `{rule.what} {rule.pattern} {rule.effect}` is active for "
                f"this chat only — not saved to {self._yaml_path}: {reason}. "
                f"Save it later with `{permissions_add_command(rule)}`.",
                kind="warning",
            )
            return
        finally:
            self._in_flight = [r for r in self._in_flight if r is not rule]
        if result.inserted:
            self._saved.append(rule)
        authz = self._authz()
        if not isinstance(authz, RuleBasedAuthorizationProvider):
            return
        skip = [rule, *self._in_flight, *self._saved, *self._unsaved]
        live = [
            rule_identity(r)
            for r in authz.rules_snapshot()
            if not any(r is s for s in skip)
        ]
        # This chat's other saves (done, or in flight and maybe already
        # on disk) are dropped from the disk side too — one copy each.
        disk = [rule_identity(r) for r in result.disk_rules_before]
        for own in (*self._saved, *self._in_flight):
            if own is rule:
                continue
            key = rule_identity(own)
            if key in disk:
                disk.remove(key)
        if live == disk:
            return
        drift = _rule_drift_signature(live, disk)
        if drift in self._drift_noticed:
            return
        self._drift_noticed.add(drift)
        self._notify(
            f"{self._yaml_path.name}'s rules changed on disk since this chat "
            "loaded them; those changes are not enforced yet. Review them "
            "(`kolsch permissions list`), then run `/reload`.",
            kind="warning",
        )


def _rule_drift_signature(
    live: list[tuple[Any, ...]], disk: list[tuple[Any, ...]]
) -> tuple[Any, ...]:
    """Summarize how the disk rules differ from the chat's loaded rules.

    The drift-notice key: the rules only on disk and only in memory
    (multiset difference), or `("reordered",)` when only the order
    differs. Rules the chat itself added since appear on both sides and
    cancel out, so a later "always" answer doesn't re-notice the same
    unapplied edit.
    """
    from collections import Counter  # noqa: PLC0415

    live_c, disk_c = Counter(live), Counter(disk)
    only_disk = tuple(sorted((disk_c - live_c).elements(), key=repr))
    only_live = tuple(sorted((live_c - disk_c).elements(), key=repr))
    if not only_disk and not only_live:
        return ("reordered",)
    return (only_disk, only_live)
