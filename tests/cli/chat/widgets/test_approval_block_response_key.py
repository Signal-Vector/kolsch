"""bug-021 Test 4 — `ApprovalBlock.response_key_from` helper.

Covers AC #8: maps a widget-side ``PromptCallbackReturn`` back to the
choice-pane key string the ``WIRE_PROMPT_ANSWERED`` payload carries.

Round-trip with ``InlineApprovalCallback._translate_response`` is
asserted for the two effect-only paths (``y`` / ``n``). The tuple
paths (``a`` / ``d``) round-trip to allow-once / deny-once today —
the Rule is dropped on the widget → store bridge until bug-022 lands
a richer answered-payload shape.
"""

from __future__ import annotations

from cli.chat.approval import _translate_response
from cli.chat.widgets.approval_block import response_key_from
from providers.authz import AuthzEffect, Rule


def test_response_key_from_allow_effect() -> None:
    assert response_key_from(AuthzEffect.ALLOW) == "y"


def test_response_key_from_deny_effect() -> None:
    assert response_key_from(AuthzEffect.DENY) == "n"


def test_response_key_from_allow_tuple() -> None:
    rule = Rule(what="bash", pattern="ls*", effect="allow")
    assert response_key_from((AuthzEffect.ALLOW, rule)) == "a"


def test_response_key_from_deny_tuple() -> None:
    rule = Rule(what="bash", pattern="rm*", effect="deny")
    assert response_key_from((AuthzEffect.DENY, rule)) == "d"


# --------------------------------------------------------------------
# Round-trip with `_translate_response` — closes the widget→store→callback
# loop symbolically. The `a`/`d` legs drop the Rule; that's bug-022 scope.
# --------------------------------------------------------------------


def test_roundtrip_allow_effect_via_y() -> None:
    """`ALLOW → "y" → ALLOW` — round-trip clean for the fast path."""
    key = response_key_from(AuthzEffect.ALLOW)
    assert _translate_response(key) is AuthzEffect.ALLOW


def test_roundtrip_deny_effect_via_n() -> None:
    """`DENY → "n" → DENY` — round-trip clean for the fast path."""
    key = response_key_from(AuthzEffect.DENY)
    assert _translate_response(key) is AuthzEffect.DENY


def test_roundtrip_allow_tuple_collapses_to_allow_once() -> None:
    """`(ALLOW, Rule) → "a" → ALLOW` — the Rule drops (bug-022 scope)."""
    rule = Rule(what="bash", pattern="ls*", effect="allow")
    key = response_key_from((AuthzEffect.ALLOW, rule))
    # Documented today: `a` collapses to allow-once.
    assert _translate_response(key) is AuthzEffect.ALLOW


def test_roundtrip_deny_tuple_collapses_to_deny_once() -> None:
    """`(DENY, Rule) → "d" → DENY` — the Rule drops (bug-022 scope)."""
    rule = Rule(what="bash", pattern="rm*", effect="deny")
    key = response_key_from((AuthzEffect.DENY, rule))
    assert _translate_response(key) is AuthzEffect.DENY
