"""Authorization provider — spec Module 3 + Module API Reference §8.

The ``rule_based`` variant is a real ACL implementation from day one
(spec §3 — not a placeholder). Rules are ``{id, who, what, effect}``;
regexes are compiled at init with ``re.fullmatch`` semantics;
evaluation is first-match-wins with default-deny.

Phase 2.0 extensions (TOOL-R3 / TOOL-R7):

- ``Rule.id`` — stable identifier of form ``rl_<sha256(who+what+effect)[:8]>``
  auto-assigned at parse time. The default-deny tail keeps the
  fixed id :data:`DEFAULT_DENY_RULE_ID` (``rl_default_deny``) and
  is removal-protected.
- ``effect: prompt`` — third value alongside allow/deny. The
  provider classifies; the caller dispatches per
  ``secrets-sandbox-authz-contract.md`` §3.7.2.
- ``AuthorizationResult.effect`` — the three-value classification;
  ``allowed`` collapses to a bool for callers that only need that.
- ``allow(principal, action, target=None, callback=None)`` —
  the optional ``target`` matches against the rule's ``what``
  (for file tools the resolved path; for ``bash`` the command
  string). The optional ``callback`` is the in-chat prompt
  resolver — when omitted, ``prompt`` collapses to ``deny`` so
  unattended callers never hang.
- ``add_rule`` / ``remove_rule`` / ``get_rule`` — live-edit API
  consumed by ``kolsch permissions`` and the in-chat modal.
- ``allow_all_test`` variant — registered ONLY when the
  ``KOLSCH_TEST_BUILD=1`` env var is set; production refuses
  to load it (TOOL-R10 hard rule).

Conformance invariants:
- A-I1 — ``re.fullmatch`` ONLY (not ``match``/``search``).
- A-I2 — first-match-wins; no "most-specific-match" heuristic.
- A-I3 — default deny when no rule matches.
- A-I4 — pure decision; no I/O. (Live-edit methods are NOT pure;
  they are documented separately and not invoked during ``allow``.)
- A-I5 — result carries the matched rule (or ``None`` for default
  deny) for explainability.
- A-I6 — all methods are ``async``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from errors import AuthzMalformedRule, AuthzRegexInvalid, ConfigInvalid

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Phase 2.0 constants
# ---------------------------------------------------------------------------


DEFAULT_DENY_RULE_ID = "rl_default_deny"
"""The reserved id of the default-deny tail.

Set at parse time when a rule with the matching shape is detected;
:meth:`RuleBasedAuthorizationProvider.remove_rule` refuses to remove
this id (TOOL-R10 hard rule). The shape that earns this id is
``who=".*"`` AND ``what=".*"`` AND ``effect="deny"``."""


KOLSCH_TEST_BUILD_ENV_VAR = "KOLSCH_TEST_BUILD"
"""Env var that, set to ``"1"``, registers the ``allow_all_test``
variant. Production builds never set it; setting it on a non-test
runtime is a configuration error."""


# ---------------------------------------------------------------------------
# Types — Module API Reference §8.2 +
# ---------------------------------------------------------------------------


class AuthzEffect(StrEnum):
    """The three classifications a rule can return."""

    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


class Rule(BaseModel):
    """One ACL rule.

    Phase 2.0 (TOOL-R3): ``id`` is auto-derived from ``who+what+effect``
    when absent; ``effect`` is one of ``allow`` / ``deny`` / ``prompt``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    who: str
    what: str
    effect: Literal["allow", "deny", "prompt"]


def derive_rule_id(who: str, what: str, effect: str) -> str:
    """Deterministic id of form ``rl_<sha256(who+what+effect)[:8]>``.

    Stable across reorders and reloads — two rules with identical
    content always produce the same id. The default-deny shape
    (``who=".*"``, ``what=".*"``, ``effect="deny"``) returns the
    reserved :data:`DEFAULT_DENY_RULE_ID` instead so the tail is
    referenced by a fixed name.
    """
    if who == ".*" and what == ".*" and effect == "deny":
        return DEFAULT_DENY_RULE_ID
    payload = f"{who}|{what}|{effect}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"rl_{digest}"


class AuthorizationResult(BaseModel):
    """The structured outcome of one ``allow`` call.

    Carries the matched rule (or ``None`` for default-deny) so
    callers can explain the decision — Module API Reference §8.1
    + spec A-I5.

    Phase 2.0 (TOOL-R3): ``effect`` carries the three-value
    classification; ``allowed`` collapses ``ALLOW`` to ``True`` and
    both ``DENY`` and ``PROMPT`` to ``False`` for callers that only
    need the bool. ``rule_id`` is a convenience accessor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    effect: AuthzEffect = AuthzEffect.DENY
    matched_rule: Rule | None = None
    # Always populated — both the matched-rule and default-deny paths
    # in :meth:`RuleBasedAuthorizationProvider.allow` set a non-None
    # string. Typed as `str` (not `str | None`) so callers don't have
    # to defend against a None they will never see.
    reason: str

    @property
    def rule_id(self) -> str | None:
        """Convenience accessor for ``matched_rule.id`` (or None)."""
        return self.matched_rule.id if self.matched_rule is not None else None


# ---------------------------------------------------------------------------
# Prompt callback
# ---------------------------------------------------------------------------


if TYPE_CHECKING:

    class PromptCallback(Protocol):
        """The hook chat surfaces inject to resolve ``prompt`` effects.

        Receives the matched rule's id, the action (``tool.<name>``),
        and the optional target (resolved path / command string).
        Returns either:

        - A single :class:`AuthzEffect` (``ALLOW`` or ``DENY``) — the
          resolution for this single call; no rule is persisted.
        - A tuple ``(effect, rule)`` — the resolution AND a new rule
          to append to the rule list. The provider writes the new
          rule via :meth:`add_rule` before this call returns so the
          next check in the same exchange sees it.
        """

        async def __call__(
            self,
            *,
            rule_id: str | None,
            action: str,
            target: str | None,
        ) -> AuthzEffect | tuple[AuthzEffect, Rule]: ...


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AuthorizationProvider(Protocol):
    """Three-method contract per spec §3.3 +

    Phase 2.0 (TOOL-R3): :meth:`allow` accepts optional ``target``
    and ``callback`` parameters. ``target`` is matched against the
    rule's ``what`` (for file tools the resolved path; for ``bash``
    the command string). ``callback`` is the chat-surface prompt
    resolver — when omitted, ``prompt`` collapses to ``deny`` so
    unattended callers never hang.
    """

    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        agent_permissions: list[Rule] | None = None,
    ) -> AuthorizationResult: ...

    async def list_rules(self) -> list[Rule]: ...


# ---------------------------------------------------------------------------
# Rule-based variant
# ---------------------------------------------------------------------------


class _CompiledRule:
    """A rule with its regexes already compiled for fast matching.

    Held privately; the public surface is :class:`Rule` (the raw,
    serialisable record) returned through :class:`AuthorizationResult`
    and :meth:`list_rules`.
    """

    __slots__ = ("effect", "raw", "what_re", "who_re")

    def __init__(
        self,
        who_re: re.Pattern[str],
        what_re: re.Pattern[str],
        effect: Literal["allow", "deny", "prompt"],
        raw: Rule,
    ) -> None:
        self.who_re = who_re
        self.what_re = what_re
        self.effect = effect
        self.raw = raw


class RuleBasedAuthorizationProvider:
    """Spec §3.4 — the v1 default authz variant.

    Phase 2.0 (TOOL-R3 / TOOL-R7): supports the third ``prompt``
    effect, optional callback-driven prompt resolution on
    :meth:`allow`, and a live-edit API (:meth:`add_rule`,
    :meth:`remove_rule`, :meth:`get_rule`). The default-deny tail
    keeps the fixed id :data:`DEFAULT_DENY_RULE_ID` and refuses
    removal.
    """

    def __init__(self, *, config: dict[str, Any]) -> None:
        raw_rules = config.get("rules", []) or []
        if not isinstance(raw_rules, list):
            raise AuthzMalformedRule(
                rule_index=-1,
                error=(f"`rules` must be a list; got {type(raw_rules).__name__}"),
            )
        self._rules: list[_CompiledRule] = [
            self._compile(index, entry) for index, entry in enumerate(raw_rules)
        ]
        # Phase 2.1 (24d) — chat-injected callback resolved by
        # _resolve_prompt when `allow(callback=)` is omitted. The
        # chat lifecycle registers on startup + clears on exit so
        # subsequent in-process commands never see a leaked modal
        # surface.
        self._registered_prompt_callback: PromptCallback | None = None
        # Phase 2.1 (24d) — fires after a prompt callback persists
        # a rule via `(effect, Rule)` tuple. The chat wires this to
        # an atomic ``platform.yaml`` rewriter so the modal NEVER
        # writes the YAML directly; this hook is the documented
        # extension point.
        self._rule_added_hook: Callable[[list[Rule]], None] | None = None

    @staticmethod
    def _compile(index: int, entry: object) -> _CompiledRule:
        if not isinstance(entry, dict):
            raise AuthzMalformedRule(
                rule_index=index,
                error=f"rule must be a mapping; got {type(entry).__name__}",
            )
        for required in ("who", "what", "effect"):
            if required not in entry:
                raise AuthzMalformedRule(
                    rule_index=index,
                    error=f"missing required field {required!r}",
                )
        who = entry["who"]
        what = entry["what"]
        effect = entry["effect"]
        if effect not in ("allow", "deny", "prompt"):
            raise AuthzMalformedRule(
                rule_index=index,
                error=(f"effect must be 'allow', 'deny', or 'prompt'; got {effect!r}"),
            )
        try:
            who_re = re.compile(who)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=index,
                field="who",
                regex=str(who),
                error=str(exc),
            ) from exc
        try:
            what_re = re.compile(what)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=index,
                field="what",
                regex=str(what),
                error=str(exc),
            ) from exc
        # Phase 2.0: preserve a caller-supplied id if present,
        # otherwise derive deterministically. This lets pre-existing
        # rule lists round-trip stably through `list_rules` + reload.
        supplied_id = entry.get("id")
        rule_id = (
            supplied_id
            if isinstance(supplied_id, str) and supplied_id
            else derive_rule_id(who, what, effect)
        )
        return _CompiledRule(
            who_re=who_re,
            what_re=what_re,
            effect=effect,
            raw=Rule(id=rule_id, who=who, what=what, effect=effect),
        )

    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        agent_permissions: list[Rule] | None = None,
    ) -> AuthorizationResult:
        """Evaluate ``(principal, action[, target])`` against the rule list.

        Phase 2.0 (TOOL-R3):

        - ``target`` is matched against the rule's ``what`` when
          present; falls back to ``action`` otherwise. For file tools
          ``target`` is the resolved absolute path; for ``bash`` it's
          the command string.
        - ``callback`` is the chat-surface prompt resolver. When
          ``None`` (the default — used by ``kolsch run``, the server,
          tests), ``prompt`` collapses to ``deny`` so unattended
          callers never hang. When supplied, the callback receives
          ``(rule_id, action, target)`` and returns either a final
          :class:`AuthzEffect` or ``(effect, persist_rule)`` — in the
          tuple form the new rule is appended to the in-memory list
          *before* this call returns so the next check in the same
          exchange sees it.

        Phase 2.2 (ENV §3.8 / Step 9):

        - ``agent_permissions`` (keyword-only) is the resolved
          agent's per-agent rule list — composed agent-first,
          platform-second, default-deny tail. ``None`` or ``[]``
          is identical to the pre-Phase-2.2 surface (every existing
          caller continues to work unchanged). When non-empty,
          each :class:`Rule` is compiled in-place and walked
          first-match-wins; if a match is found the rule returns
          directly (PROMPT effects still resolve through the
          callback). The :class:`AuthorizationResult` carries
          a ``who_chain`` detail in its ``reason`` so debugging
          can identify which layer matched.
        """
        # Phase 2.2 — walk agent permissions first. Per-call compile is
        # acceptable because agent_permissions is small (single-digit
        # rules typically) and the engine calls allow() once per tool
        # invocation, not on a hot loop.
        if agent_permissions:
            agent_match = await self._match_against_rules(
                rules=[self._compile_rule_record(r) for r in agent_permissions],
                principal=principal,
                action=action,
                target=target,
                callback=callback,
                layer="agent",
            )
            if agent_match is not None:
                return agent_match

        # Platform rules — same matching logic as Phase 2.0.
        platform_match = await self._match_against_rules(
            rules=self._rules,
            principal=principal,
            action=action,
            target=target,
            callback=callback,
            layer="platform",
        )
        if platform_match is not None:
            return platform_match

        # A-I3 default deny.
        return AuthorizationResult(
            allowed=False,
            effect=AuthzEffect.DENY,
            matched_rule=None,
            reason="no rule matched; default deny (who_chain=default)",
        )

    async def _match_against_rules(
        self,
        *,
        rules: list[_CompiledRule],
        principal: str,
        action: str,
        target: str | None,
        callback: PromptCallback | None,
        layer: str,
    ) -> AuthorizationResult | None:
        """Walk one layer's compiled rules first-match-wins. Returns
        :class:`AuthorizationResult` on match (PROMPT resolved
        through :meth:`_resolve_prompt`); returns ``None`` when no
        rule fires so the caller can fall through to the next
        layer.

        ``layer`` (``"agent"`` / ``"platform"``) is folded into the
        result's ``reason`` (and into PROMPT's resolved reason via
        :meth:`_resolve_prompt`) so debugging output identifies
        which layer matched."""
        # A-I1 fullmatch + A-I2 first-match-wins. Same target-OR-action
        # matching as the Phase 2.0 path.
        for compiled in rules:
            if not compiled.who_re.fullmatch(principal):
                continue
            target_match = target is not None and compiled.what_re.fullmatch(target)
            action_match = compiled.what_re.fullmatch(action)
            if target_match or action_match:
                effect = AuthzEffect(compiled.effect)
                if effect is AuthzEffect.PROMPT:
                    return await self._resolve_prompt(
                        compiled=compiled,
                        action=action,
                        target=target,
                        callback=callback,
                        layer=layer,
                    )
                return AuthorizationResult(
                    allowed=effect is AuthzEffect.ALLOW,
                    effect=effect,
                    matched_rule=compiled.raw,
                    reason=(
                        f"matched rule {compiled.raw.id}: who={compiled.raw.who!r} "
                        f"what={compiled.raw.what!r} "
                        f"effect={compiled.effect!r} (who_chain={layer})"
                    ),
                )
        return None

    async def _resolve_prompt(
        self,
        *,
        compiled: _CompiledRule,
        action: str,
        target: str | None,
        callback: PromptCallback | None,
        layer: str = "platform",
    ) -> AuthorizationResult:
        # Phase 2.1 (24d) — when the per-call callback is absent,
        # fall back to the chat-injected registered callback. This
        # keeps the dispatcher (tools/dispatch.py) callback-agnostic:
        # it never has to thread the modal through; it just calls
        # ``allow()`` and the provider resolves the right
        # callback for the active surface.
        effective_callback = callback or self._registered_prompt_callback
        if effective_callback is None:
            # Non-chat callers (kolsch run, server, tests) collapse
            # `prompt` to `deny`. The dispatcher emits
            # authz.prompt_unattended at the call site so replay
            # shows what would have been asked.
            return AuthorizationResult(
                allowed=False,
                effect=AuthzEffect.PROMPT,
                matched_rule=compiled.raw,
                reason=(
                    f"prompt rule {compiled.raw.id} matched without "
                    f"a callback; collapsed to deny (who_chain={layer})"
                ),
            )
        answer = await effective_callback(
            rule_id=compiled.raw.id,
            action=action,
            target=target,
        )
        if isinstance(answer, tuple):
            resolved_effect, persist_rule = answer
            # Insert the persisted rule before the matched prompt rule
            # so future calls with the same target hit it first
            # ( "always allow/deny" semantics).
            self._insert_rule_before(persist_rule, anchor_id=compiled.raw.id)
            # Phase 2.1 (24d) — fire the rule-added hook AFTER the
            # in-memory insert so the chat's atomic-yaml-write
            # hook sees the updated rule list. The hook is
            # best-effort: failures don't undo the in-memory
            # insert (the rule is already live for the current
            # exchange — which is the load-bearing guarantee).
            if self._rule_added_hook is not None:
                # Diagnostic-level: hook failures don't undo the
                # in-memory insert. The rule is already live for
                # the current exchange — which is the load-bearing
                # guarantee. A failed YAML write means the rule
                # vanishes on chat exit; the user can re-add via
                # `kolsch permissions add` or pick [a] again next
                # time. We swallow to keep the modal's flow simple;
                # the YAML write itself logs on failure.
                with contextlib.suppress(Exception):
                    self._rule_added_hook([c.raw for c in self._rules])
        else:
            resolved_effect = answer
        if resolved_effect not in (AuthzEffect.ALLOW, AuthzEffect.DENY):
            msg = (
                "prompt callback must resolve to ALLOW or DENY; "
                f"got {resolved_effect!r}"
            )
            raise ValueError(msg)
        return AuthorizationResult(
            allowed=resolved_effect is AuthzEffect.ALLOW,
            effect=resolved_effect,
            matched_rule=compiled.raw,
            reason=(
                f"prompt rule {compiled.raw.id} resolved by callback "
                f"to {resolved_effect.value!r} (who_chain={layer})"
            ),
        )

    async def list_rules(self) -> list[Rule]:
        return [c.raw for c in self._rules]

    # -- Phase 2.1 (24d) chat-callback registration --------------------

    def register_prompt_callback(
        self,
        callback: PromptCallback,
        *,
        on_rule_added: Callable[[list[Rule]], None] | None = None,
    ) -> None:
        """Register a chat-injected prompt resolver + rule-persist hook.

        The chat lifecycle calls this once on startup (after
        bootstrap_runtime, before the input loop) and pairs it with
        :meth:`clear_prompt_callback` in a finally so a subsequent
        in-process command never sees the modal surface. See
        ``src/cli/chat.py`` for the canonical registration.

        :param callback: the modal callable. Receives keyword args
            ``(rule_id, action, target)`` and returns either an
            :class:`AuthzEffect` or a tuple
            ``(effect, persisted_rule)``.
        :param on_rule_added: optional hook fired AFTER the in-memory
            rule insert (only in the tuple-return path). Receives
            the updated rule list as
            ``list[Rule]`` so the hook can atomically rewrite
            ``platform.yaml``. The chat wires this to
            ``cli.permissions._write_back``.

        Calling :meth:`register_prompt_callback` twice replaces the
        prior registration — no implicit chaining. Production
        chat lifecycles only ever register once per process; the
        replace semantics are for test setups that want a fresh
        modal across cases.
        """
        self._registered_prompt_callback = callback
        self._rule_added_hook = on_rule_added

    def clear_prompt_callback(self) -> None:
        """Remove the registered modal callback + rule-added hook.

        Called by the chat lifecycle in a finally so a subsequent
        in-process ``kolsch run`` against the same authz provider
        does not see the modal — which would either hang (no
        modal-stdin available) or worse, ALLOW silently. This is
        load-bearing for tests that bootstrap then exercise more
        than one command against the same runtime instance.
        """
        self._registered_prompt_callback = None
        self._rule_added_hook = None

    # -- Phase 2.0 live-edit API (Module API Reference §11.8.2) --------

    async def get_rule(self, rule_id: str) -> Rule:
        """Look up one rule by stable id; raises if absent."""
        from errors import AuthzRuleNotFound  # noqa: PLC0415

        for compiled in self._rules:
            if compiled.raw.id == rule_id:
                return compiled.raw
        raise AuthzRuleNotFound(
            rule_id=rule_id,
            available_ids=[c.raw.id for c in self._rules],
        )

    def _insert_rule_before(self, rule: Rule, *, anchor_id: str) -> Rule:
        """Insert ``rule`` immediately before the rule with ``anchor_id``.

        Synchronous — used by the prompt-callback path in
        :meth:`_resolve_prompt` where the rule must be live by the
        time the next ``allow`` call evaluates. Returns the rule
        with its id populated.
        """
        rule = self._ensure_id(rule)
        compiled = self._compile_rule_record(rule)
        for index, existing in enumerate(self._rules):
            if existing.raw.id == anchor_id:
                self._rules.insert(index, compiled)
                return compiled.raw
        # Anchor not found — append before the default-deny tail if
        # present, else at the end.
        self._rules.insert(self._tail_index(), compiled)
        return compiled.raw

    async def add_rule(
        self,
        rule: Rule,
        *,
        before: str | None = None,
        after: str | None = None,
        first: bool = False,
    ) -> Rule:
        """Append/insert a rule into the in-memory rule list.

        Exactly one of ``before`` / ``after`` / ``first`` may be set;
        default is "before the default-deny tail" (the most useful
        position for an additive allow/deny). The disk write of
        ``platform.yaml`` is the caller's responsibility — this
        method updates only the in-memory list. The CLI command
        ``kolsch permissions add`` and the in-chat callback path
        both call this, then rewrite the YAML atomically.
        """
        from errors import AuthzRuleInvalid  # noqa: PLC0415

        position_specs = sum(1 for spec in (before, after, first) if spec)
        if position_specs > 1:
            raise AuthzRuleInvalid(
                position=-1,
                reason=("at most one of `before`, `after`, `first` may be specified"),
            )

        rule = self._ensure_id(rule)
        compiled = self._compile_rule_record(rule)

        if first:
            self._rules.insert(0, compiled)
            return compiled.raw
        if before is not None:
            for index, existing in enumerate(self._rules):
                if existing.raw.id == before:
                    self._rules.insert(index, compiled)
                    return compiled.raw
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`before` rule id {before!r} not found",
            )
        if after is not None:
            for index, existing in enumerate(self._rules):
                if existing.raw.id == after:
                    self._rules.insert(index + 1, compiled)
                    return compiled.raw
            raise AuthzRuleInvalid(
                position=-1,
                reason=f"`after` rule id {after!r} not found",
            )
        # Default — before the default-deny tail.
        self._rules.insert(self._tail_index(), compiled)
        return compiled.raw

    async def remove_rule(self, rule_id: str) -> None:
        """Remove a rule by id.

        Refuses to remove the protected :data:`DEFAULT_DENY_RULE_ID`
        tail (TOOL-R10 hard rule). Raises ``authz.rule_not_found`` for
        unknown ids.
        """
        from errors import AuthzProtectedRule, AuthzRuleNotFound  # noqa: PLC0415

        if rule_id == DEFAULT_DENY_RULE_ID:
            raise AuthzProtectedRule(
                rule_id=rule_id,
                reason=(
                    "the default-deny tail is removal-protected; "
                    "add permissive rules before it instead"
                ),
            )
        for index, compiled in enumerate(self._rules):
            if compiled.raw.id == rule_id:
                del self._rules[index]
                return
        raise AuthzRuleNotFound(
            rule_id=rule_id,
            available_ids=[c.raw.id for c in self._rules],
        )

    # -- Helpers --------------------------------------------------------

    def _tail_index(self) -> int:
        """Return the insertion index that lands *before* the
        default-deny tail (if present) or at the end of the list."""
        for index, compiled in enumerate(self._rules):
            if compiled.raw.id == DEFAULT_DENY_RULE_ID:
                return index
        return len(self._rules)

    @staticmethod
    def _ensure_id(rule: Rule) -> Rule:
        if rule.id:
            return rule
        derived = derive_rule_id(rule.who, rule.what, rule.effect)
        return rule.model_copy(update={"id": derived})

    @staticmethod
    def _compile_rule_record(rule: Rule) -> _CompiledRule:
        try:
            who_re = re.compile(rule.who)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=-1,
                field="who",
                regex=rule.who,
                error=str(exc),
            ) from exc
        try:
            what_re = re.compile(rule.what)
        except re.error as exc:
            raise AuthzRegexInvalid(
                rule_index=-1,
                field="what",
                regex=rule.what,
                error=str(exc),
            ) from exc
        return _CompiledRule(
            who_re=who_re,
            what_re=what_re,
            effect=rule.effect,
            raw=rule,
        )


# ---------------------------------------------------------------------------
# allow_all_test variant — test builds only
# ---------------------------------------------------------------------------


class AllowAllTestAuthorizationProvider:
    """Returns ``ALLOW`` for everything. Test builds only.

    Registered ONLY when :data:`KOLSCH_TEST_BUILD_ENV_VAR` is set to
    ``"1"``. Production builds refuse to load it (TOOL-R10 hard
    rule). The factory raises ``ConfigInvalid`` if the env var is
    unset at construction time.
    """

    def __init__(self, *, config: dict[str, Any]) -> None:
        _ = config
        if os.environ.get(KOLSCH_TEST_BUILD_ENV_VAR) != "1":
            raise ConfigInvalid(
                validation_errors=[
                    {
                        "layer": 2,
                        "source": "providers.authz",
                        "path": "/authorization/variant",
                        "code": "test_only_variant",
                        "message": (
                            "the 'allow_all_test' variant is for tests only "
                            f"and requires {KOLSCH_TEST_BUILD_ENV_VAR}=1 "
                            "in the environment; production builds refuse "
                            "to load it (AGENTS.md hard rule per TOOL-R10)"
                        ),
                    }
                ],
                where="providers.authz.allow_all_test.init",
            )

    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        callback: PromptCallback | None = None,
        agent_permissions: list[Rule] | None = None,
    ) -> AuthorizationResult:
        _ = principal, action, target, callback, agent_permissions
        return AuthorizationResult(
            allowed=True,
            effect=AuthzEffect.ALLOW,
            matched_rule=None,
            reason="allow_all_test variant — test builds only",
        )

    async def list_rules(self) -> list[Rule]:
        return []


# ---------------------------------------------------------------------------
# Registry seam
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Callable[..., AuthorizationProvider]] = {}


def register_authorization_variant(
    name: str,
    factory: Callable[..., AuthorizationProvider],
) -> None:
    _REGISTRY[name] = factory


def make_authorization_provider(
    *,
    config: dict[str, Any],
) -> AuthorizationProvider:
    """Dispatch by ``config['variant']`` per platform.yaml §authorization."""
    variant = config.get("variant")
    body = config.get("config", {}) or {}
    factory = _REGISTRY.get(variant or "")
    if factory is None:
        raise ConfigInvalid(
            validation_errors=[
                {
                    "layer": 2,
                    "source": "providers.authz",
                    "path": "/authorization/variant",
                    "code": "unknown_variant",
                    "message": (
                        f"unknown authorization variant {variant!r}; "
                        f"registered: {sorted(_REGISTRY)}"
                    ),
                }
            ],
            where="providers.authz.make_authorization_provider",
        )
    return factory(config=body)


register_authorization_variant("rule_based", RuleBasedAuthorizationProvider)


# Test-only variant. Registered unconditionally — the
# construction-time env-var check in
# :class:`AllowAllTestAuthorizationProvider.__init__` is what enforces
# the production refusal. Registering unconditionally lets
# `make_authorization_provider(variant="allow_all_test")` resolve
# the factory; the factory then validates the env state. Tests set
# ``KOLSCH_TEST_BUILD=1`` in conftest.py.
register_authorization_variant("allow_all_test", AllowAllTestAuthorizationProvider)
