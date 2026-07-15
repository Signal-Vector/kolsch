"""Interactive session picker for ``kolsch chat --resume``.

The picker is a thin pre-boot front door onto the existing
``--session`` resume path. It renders an arrow-key list of recent
sessions and returns the chosen :class:`~schemas.SessionId` (or
``None`` when the user cancels); ``_cmd_chat`` then stamps
``args.session`` and lets ``build_session`` take the resume branch.

**Package location.** Lives at ``src/cli/session_picker.py`` — not
under ``src/cli/chat/`` — because the interactive default uses
prompt_toolkit's :func:`radiolist_dialog`, whose ``.run_async()``
would trip TC-CHAT-18's AST guard requiring every ``.run_async``
call under ``src/cli/chat/`` to pass ``inline=True`` (an invariant
scoped to the Textual chat app). The picker is chat-adjacent, not
chat-owned.

The selection UI is isolated behind an injectable ``chooser`` seam
(mirroring how :mod:`cli._modal` isolates its ``input_fn``) so unit
tests can drive pick / cancel without a real TTY. The default
implementation lives in :func:`_default_chooser` and uses
prompt_toolkit's :func:`~prompt_toolkit.shortcuts.radiolist_dialog`
— arrow-key navigation, Enter selects, Esc cancels; already a
dependency, no extra scaffolding.

The rendered row shape (``PickerRow`` — session, last-activity
timestamp) is produced by
:func:`cli.main._recent_sessions_decorated` so the picker and
``kolsch sessions list`` never diverge on ordering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from schemas import SessionId

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas.entities import Session

    PickerRow = tuple[Session, datetime | None]
    Chooser = Callable[[list[PickerRow]], Awaitable[SessionId | None]]


def _format_row_label(session: Session, last_at: datetime | None) -> str:
    """Render a single picker row.

    Shape: ``<name>  •  <last-activity>  •  <session-id>``. Matches the
    disambiguation columns of ``kolsch sessions list`` (name, relative
    last-activity, id) but joined on `` • `` since the picker is a
    single-column list, not a table.
    """
    from cli.main import (
        _format_delta,
    )

    now = datetime.now(UTC)
    name = session.name or "(unnamed)"
    last = _format_delta(now - last_at) if last_at is not None else "—"
    return f"{name}  •  {last}  •  {session.session_id}"


async def _default_chooser(  # pragma: no cover — interactive TTY path
    rows: list[PickerRow],
) -> SessionId | None:
    """Interactive selector — arrow keys, Enter selects, Esc cancels.

    Uses prompt_toolkit's ``radiolist_dialog`` (already a dep). Kept
    in its own function so unit-test coverage can exclude it — the
    seam in :func:`pick_session` accepts a fake chooser for tests.
    """
    from prompt_toolkit.shortcuts import radiolist_dialog  # noqa: PLC0415

    values = [
        (str(session.session_id), _format_row_label(session, last_at))
        for session, last_at in rows
    ]
    dialog = radiolist_dialog(
        title="Resume session",
        text="Pick a session to resume (arrow keys, Enter to select, Esc to cancel):",
        values=values,
    )
    chosen = await dialog.run_async()
    if chosen is None:
        return None
    return SessionId(str(chosen))


async def pick_session(
    rows: list[PickerRow],
    *,
    chooser: Chooser | None = None,
) -> SessionId | None:
    """Return the chosen :class:`SessionId` or ``None`` when cancelled.

    Delegates the actual interactive selection to ``chooser`` so tests
    can inject a fake. When ``chooser`` is ``None`` the module-level
    :func:`_default_chooser` is resolved *at call time* so tests can
    monkeypatch the module attribute (the bound-at-definition-time
    default idiom would defeat that). Empty ``rows`` returns ``None``
    without invoking the chooser — the empty-list path is handled by
    the caller (``_cmd_chat`` prints "No sessions to resume." before
    reaching here).
    """
    if not rows:
        return None
    if chooser is None:
        chooser = _default_chooser
    return await chooser(rows)


__all__ = [
    "_default_chooser",
    "_format_row_label",
    "pick_session",
]
