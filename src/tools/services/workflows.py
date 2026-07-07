"""Workflows service — start / status / cancel a nested workflow run.

Two shapes:

- [`Workflows`][tools.services.workflows.Workflows] is the
  author-facing Protocol tools annotate against.
- [`AbstractWorkflows`][tools.services.workflows.AbstractWorkflows]
  is a plain ABC whose methods raise `NotImplementedError`; tests
  subclass it, and the spec-012 concrete impl overrides the stubs.

The ABC does NOT inherit from the Protocol. `@runtime_checkable` is
deliberately NOT set (default `False`).

`WorkflowRunId` is a `NewType` over `str`. When the shared
`WorkflowRunId` lands in `src/schemas/identifiers.py` (later spec),
this alias will re-export from there.
"""

from __future__ import annotations

from abc import ABC
from typing import Any, NewType, Protocol

WorkflowRunId = NewType("WorkflowRunId", str)
"""Identifier for a nested workflow run."""


class Workflows(Protocol):
    """Tools' author-facing view of the workflows service."""

    async def start(
        self,
        workflow_ref: str,
        inputs: dict[str, Any],
    ) -> WorkflowRunId:
        """Start a nested workflow run; return its id."""
        ...

    async def status(self, run_id: WorkflowRunId) -> str:
        """Return the current status string for a run."""
        ...

    async def cancel(self, run_id: WorkflowRunId) -> None:
        """Cancel a running workflow run."""
        ...


class AbstractWorkflows(ABC):
    """Platform + test base for `Workflows` impls. Methods raise by default."""

    async def start(
        self,
        workflow_ref: str,
        inputs: dict[str, Any],
    ) -> WorkflowRunId:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def status(self, run_id: WorkflowRunId) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def cancel(self, run_id: WorkflowRunId) -> None:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError
