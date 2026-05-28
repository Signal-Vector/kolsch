"""Authorization provider — spec Module 3 + Module API Reference §8.

The ``rule_based`` variant is a real ACL implementation from day one
(spec §3 — not a placeholder). Rules are ``{who, what, effect}``;
regexes are compiled at init with ``re.fullmatch`` semantics;
evaluation is first-match-wins with default-deny.

Conformance invariants:
- A-I1 — ``re.fullmatch`` ONLY (not ``match``/``search``).
- A-I2 — first-match-wins; no "most-specific-match" heuristic.
- A-I3 — default deny when no rule matches.
- A-I4 — pure decision; no I/O.
- A-I5 — result carries the matched rule (or ``None`` for default
  deny) for explainability.
- A-I6 — all methods are ``async``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from errors import AuthzMalformedRule, AuthzRegexInvalid, ConfigInvalid

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Types — Module API Reference §8.2
# ---------------------------------------------------------------------------


class Rule(BaseModel):
    """One ACL rule. ``effect`` is exactly ``allow`` or ``deny``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    who: str
    what: str
    effect: Literal["allow", "deny"]


class AuthorizationResult(BaseModel):
    """The structured outcome of one ``allow`` call.

    Carries the matched rule (or ``None`` for default-deny) so
    callers can explain the decision — Module API Reference §8.1
    + spec A-I5.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    matched_rule: Rule | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AuthorizationProvider(Protocol):
    """Three-method contract per spec §3.3."""

    async def allow(self, principal: str, action: str) -> AuthorizationResult: ...

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
        effect: Literal["allow", "deny"],
        raw: Rule,
    ) -> None:
        self.who_re = who_re
        self.what_re = what_re
        self.effect = effect
        self.raw = raw


class RuleBasedAuthorizationProvider:
    """Spec §3.4 — the v1 default authz variant."""

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
        if effect not in ("allow", "deny"):
            raise AuthzMalformedRule(
                rule_index=index,
                error=(f"effect must be 'allow' or 'deny'; got {effect!r}"),
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
        return _CompiledRule(
            who_re=who_re,
            what_re=what_re,
            effect=effect,
            raw=Rule(who=who, what=what, effect=effect),
        )

    async def allow(self, principal: str, action: str) -> AuthorizationResult:
        # A-I1 fullmatch + A-I2 first-match-wins.
        for compiled in self._rules:
            if compiled.who_re.fullmatch(principal) and compiled.what_re.fullmatch(
                action
            ):
                return AuthorizationResult(
                    allowed=compiled.effect == "allow",
                    matched_rule=compiled.raw,
                    reason=(
                        f"matched rule: who={compiled.raw.who!r} "
                        f"what={compiled.raw.what!r} "
                        f"effect={compiled.effect!r}"
                    ),
                )
        # A-I3 default deny.
        return AuthorizationResult(
            allowed=False,
            matched_rule=None,
            reason="no rule matched; default deny",
        )

    async def list_rules(self) -> list[Rule]:
        return [c.raw for c in self._rules]


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
