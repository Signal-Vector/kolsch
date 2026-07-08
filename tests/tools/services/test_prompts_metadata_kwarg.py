"""bug-021 Test 1 — `Prompts` / `AbstractPrompts` grow a `metadata` kwarg.

Covers AC #1 (`Prompts.choice` + `Prompts.text` accept
``metadata: dict[str, Any] | None = None``) and AC #2 (parity on the
``AbstractPrompts`` ABC). Backward-compat default (``None``) is
verified by the "old signature still works" test.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tools.services import AbstractPrompts, Prompts, PromptSource

# --------------------------------------------------------------------
# AC #1 — signature carries the kwarg.
# --------------------------------------------------------------------


def test_choice_accepts_metadata_kwarg() -> None:
    """The Protocol's `choice` grows a keyword-only ``metadata`` param."""
    sig = inspect.signature(Prompts.choice)
    assert "metadata" in sig.parameters
    param = sig.parameters["metadata"]
    # `metadata` MUST be keyword-only (matches `source=`).
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    # Default `None` preserves backward-compat with every existing call
    # site.
    assert param.default is None


def test_text_accepts_metadata_kwarg() -> None:
    """The Protocol's `text` grows the same ``metadata`` kwarg (parity)."""
    sig = inspect.signature(Prompts.text)
    assert "metadata" in sig.parameters
    param = sig.parameters["metadata"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


# --------------------------------------------------------------------
# AC #2 — the ABC mirrors the Protocol.
# --------------------------------------------------------------------


def test_abstract_prompts_choice_carries_metadata_kwarg() -> None:
    """The `AbstractPrompts` ABC's `choice` mirrors the Protocol."""
    sig = inspect.signature(AbstractPrompts.choice)
    assert "metadata" in sig.parameters
    param = sig.parameters["metadata"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_abstract_prompts_text_carries_metadata_kwarg() -> None:
    sig = inspect.signature(AbstractPrompts.text)
    assert "metadata" in sig.parameters
    param = sig.parameters["metadata"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


# --------------------------------------------------------------------
# Backward-compat — pre-bug-021 callers keep working.
# --------------------------------------------------------------------


class _PromptsStub(AbstractPrompts):
    """Minimal Protocol-conformant subclass used to assert call shape."""

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
                "options": options,
                "source": source,
                "metadata": metadata,
            },
        )
        return "y"

    async def text(
        self,
        prompt: str,
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "source": source,
                "metadata": metadata,
            },
        )
        return ""


@pytest.mark.asyncio
async def test_choice_metadata_none_backcompat() -> None:
    """Pre-bug-021 caller shape (no `metadata=`) still works."""
    stub = _PromptsStub()
    source: PromptSource = {"kind": "tool", "id": "t1"}
    result = await stub.choice("pick", ["y", "n"], source=source)
    assert result == "y"
    assert stub.calls[0]["metadata"] is None


@pytest.mark.asyncio
async def test_text_metadata_none_backcompat() -> None:
    """Pre-bug-021 caller shape for `text` (no `metadata=`) still works."""
    stub = _PromptsStub()
    source: PromptSource = {"kind": "tool", "id": "t1"}
    await stub.text("say", source=source)
    assert stub.calls[0]["metadata"] is None


@pytest.mark.asyncio
async def test_choice_accepts_explicit_metadata() -> None:
    """Passing `metadata={...}` explicitly threads it through to the impl."""
    stub = _PromptsStub()
    source: PromptSource = {"kind": "tool", "id": "t1"}
    md = {"rule_id": "rl_x", "action": "bash", "target": "ls"}
    await stub.choice("pick", ["y", "n"], source=source, metadata=md)
    assert stub.calls[0]["metadata"] == md
