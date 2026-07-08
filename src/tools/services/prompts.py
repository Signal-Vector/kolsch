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

`run_chain` is defined here as an optional field (spec-004); it is
empty by default and populated only for nested-workflow approval
breadcrumbs in spec-017.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Literal, NotRequired, Protocol, TypedDict

PromptSourceKind = Literal["butler", "workflow", "tool"]
"""Closed set of sources that may claim the single prompt slot."""


class PromptSource(TypedDict):
    """Identity of a prompt's owner used by slot arbitration.

    `kind` + `id` form the baseline identity every source carries.
    The remaining keys are optional and shape-per-kind:

    - `tool_call_id` — set on `kind="tool"` so the crash-scan can
      correlate the request back to the parent `tool.called` event
      (see `EventAdapter._scan_dangling_prompts`).
    - `run_id` / `node_name` — set on `kind="workflow"` so the
      approval renderer can group prompts by workflow run and node.
    - `run_chain` — spec-004 stub for spec-017's nested-workflow
      breadcrumbs. Defaults to an empty list; today's readers ignore
      it.
    """

    kind: PromptSourceKind
    id: str
    tool_call_id: NotRequired[str]
    run_id: NotRequired[str]
    node_name: NotRequired[str]
    run_chain: NotRequired[list[dict[str, str]]]


class Prompts(Protocol):
    """Tools' author-facing view of the prompts service."""

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Ask a multiple-choice prompt; return the user's selection.

        bug-021: ``metadata`` (optional) is a free-form dict of
        top-level payload keys forwarded onto the emitted
        ``kolsch.prompt.requested`` event. Callers use this to
        supply widget-header context (``rule_id`` / ``action`` /
        ``target`` for the approval flow). Impls MUST merge these
        keys at the top level of the payload but MUST NOT let a
        metadata key clobber a reserved payload key
        (``prompt_id`` / ``kind`` / ``prompt`` / ``options`` /
        ``placeholder`` / ``source``).
        """
        ...

    async def text(
        self,
        prompt: str,
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Ask a free-text prompt; return the user's response.

        bug-021: ``metadata`` mirrors ``choice`` — see the docstring
        there for merge semantics.
        """
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
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`.

        bug-021 signature parity: accepts the optional ``metadata``
        kwarg so test doubles subclassing this ABC don't have to
        re-declare the shape.
        """
        raise NotImplementedError

    async def text(
        self,
        prompt: str,
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`.

        bug-021 signature parity: accepts the optional ``metadata``
        kwarg.
        """
        raise NotImplementedError

    async def wait_for_slot(
        self,
        *,
        source: PromptSource,
    ) -> Any:  # noqa: ANN401
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Called from ChatApp.on_unmount. Cancels drain tasks, snapshot timers, cancel tokens. Idempotent. Post-aclose, subsequent mutation raises RuntimeError.

        The ABC default is a no-op — test fakes and the placeholder
        `ChatServiceRegistry` fields (all `None` today) don't need real
        teardown. Concrete impls in spec-004 override with real
        teardown logic.
        """
        return
