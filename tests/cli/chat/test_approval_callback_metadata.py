"""bug-021 Test 3 — `InlineApprovalCallback` forwards metadata to `Prompts.choice`.

Covers AC #4: the callback constructs
``metadata={"rule_id": rule_id, "action": action, "target": target}``
and passes it into ``self._prompts.choice(...)``. The composite
question string in the ``prompt`` field remains for the headless
fallback path.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.chat.approval import InlineApprovalCallback
from providers.authz import AuthzEffect
from tools.services.prompts import PromptSource


class _SpyPrompts:
    """Records `choice` invocations verbatim so the callback shape is asserted."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "options": list(options),
                "source": dict(source),
                "metadata": metadata,
            },
        )
        return "y"


class _MockApp:
    def __init__(self) -> None:
        self.cleared_calls = 0

    def clear_awaiting_approval(self) -> None:
        self.cleared_calls += 1


# --------------------------------------------------------------------
# AC #4 — metadata carries rule_id/action/target.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_callback_forwards_metadata() -> None:
    """`__call__` builds `metadata={rule_id, action, target}` and passes it."""
    prompts = _SpyPrompts()
    app = _MockApp()
    cb = InlineApprovalCallback(app, prompts)

    result = await cb(rule_id="rl_x", action="bash", target="ls -la")

    assert result is AuthzEffect.ALLOW  # `y` maps to ALLOW
    assert len(prompts.calls) == 1
    call = prompts.calls[0]
    assert call["metadata"] == {
        "rule_id": "rl_x",
        "action": "bash",
        "target": "ls -la",
    }
    # The composite question survives — headless callers still get it.
    assert "bash" in call["prompt"]
    assert "ls -la" in call["prompt"]
    assert "rl_x" in call["prompt"]


@pytest.mark.asyncio
async def test_callback_metadata_survives_null_rule_id() -> None:
    """`rule_id=None` still lands in metadata (as `None`); others populate."""
    prompts = _SpyPrompts()
    app = _MockApp()
    cb = InlineApprovalCallback(app, prompts)

    await cb(rule_id=None, action="read_file", target="/etc/passwd")

    call = prompts.calls[0]
    assert call["metadata"] == {
        "rule_id": None,
        "action": "read_file",
        "target": "/etc/passwd",
    }


@pytest.mark.asyncio
async def test_callback_metadata_survives_null_target() -> None:
    """`target=None` still lands (as `None`) so the adapter's reader keeps
    its `isinstance(target, str)` narrowing intact."""
    prompts = _SpyPrompts()
    app = _MockApp()
    cb = InlineApprovalCallback(app, prompts)

    await cb(rule_id="rl_x", action="net.request", target=None)

    call = prompts.calls[0]
    assert call["metadata"]["target"] is None
