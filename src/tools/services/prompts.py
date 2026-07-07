"""Prompts service — inline choice/text prompts with slot arbitration.

Two shapes:

- [`Prompts`][tools.services.prompts.Prompts] is the author-facing
  Protocol tools annotate against.
- [`AbstractPrompts`][tools.services.prompts.AbstractPrompts] is a
  plain ABC whose methods raise `NotImplementedError`; tests subclass
  it, and the spec-004 concrete impl overrides the stubs.

The ABC does NOT inherit from the Protocol. `@runtime_checkable` is
deliberately NOT set (default `False`).

Also exports:

- [`PromptSourceKind`][tools.services.prompts.PromptSourceKind] —
  closed set of sources that may claim a prompt slot.
- [`PromptSource`][tools.services.prompts.PromptSource] — the
  identity dict recorded with the arbitration state.

No `run_chain` field on `PromptSource` yet; that arrives in spec-017
when nested workflows land.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Literal, Protocol, TypedDict

PromptSourceKind = Literal["butler", "workflow", "tool"]
"""Closed set of sources that may claim the single prompt slot."""


class PromptSource(TypedDict):
    """Identity of a prompt's owner used by slot arbitration."""

    kind: PromptSourceKind
    id: str


class Prompts(Protocol):
    """Tools' author-facing view of the prompts service."""

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
    ) -> str:
        """Ask a multiple-choice prompt; return the user's selection."""
        ...

    async def text(
        self,
        prompt: str,
        *,
        source: PromptSource,
    ) -> str:
        """Ask a free-text prompt; return the user's response."""
        ...

    async def wait_for_slot(
        self,
        *,
        source: PromptSource,
    ) -> Any:  # noqa: ANN401
        """Block until the prompt slot is free for this source."""
        ...


class AbstractPrompts(ABC):
    """Platform + test base for `Prompts` impls. Methods raise by default."""

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
    ) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def text(
        self,
        prompt: str,
        *,
        source: PromptSource,
    ) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def wait_for_slot(
        self,
        *,
        source: PromptSource,
    ) -> Any:  # noqa: ANN401
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError
