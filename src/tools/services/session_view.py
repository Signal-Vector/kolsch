"""Session-view service — read-only access to session identity + context.

Two shapes:

- [`SessionView`][tools.services.session_view.SessionView] is the
  author-facing Protocol tools annotate against.
- [`AbstractSessionView`][tools.services.session_view.AbstractSessionView]
  is a plain ABC whose attributes + method raise `NotImplementedError`;
  tests subclass it, and the spec-006 concrete impl overrides them.

The ABC does NOT inherit from the Protocol. `@runtime_checkable` is
deliberately NOT set (default `False`).

Read-only by design: nothing on this Protocol mutates the session.
Session mutation goes through the engine, not through tool code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class SessionView(Protocol):
    """Tools' author-facing view of the current session."""

    @property
    def session_id(self) -> str:
        """The session id."""
        ...

    @property
    def thread_id(self) -> str:
        """The active thread id."""
        ...

    @property
    def agent_name(self) -> str:
        """The active agent's name."""
        ...

    @property
    def agent_ref(self) -> str:
        """The catalog ref of the active agent."""
        ...

    def read_context_file(self, path: str) -> str:
        """Read a bundle-scoped context file. Path-policy-checked."""
        ...


class AbstractSessionView(ABC):
    """Platform + test base for `SessionView` impls. Members raise by default."""

    @property
    @abstractmethod
    def session_id(self) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    @property
    @abstractmethod
    def thread_id(self) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    @property
    @abstractmethod
    def agent_ref(self) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    def read_context_file(self, path: str) -> str:
        """Default stub — subclasses override. Raises `NotImplementedError`."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Called from ChatApp.on_unmount. Cancels drain tasks, snapshot timers, cancel tokens. Idempotent. Post-aclose, subsequent mutation raises RuntimeError.

        The ABC default is a no-op — test mocks and the placeholder
        `ChatServiceRegistry` fields (all `None` today) don't need real
        teardown. Concrete impls in spec-006 override with real
        teardown logic.
        """
        return
