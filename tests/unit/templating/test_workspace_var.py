"""``WorkspaceVar`` unit tests — Phase 2.2 Step 5 (spec §4.4 + §7).

Pins the single-object-attribute contract Step 14's engine
pre-flight builds against:

- Four attributes (``path`` / ``summary`` / ``allowed_paths`` /
  ``denied_paths``).
- ``summary`` is ``None`` (not the empty string) when
  ``workspace_summary`` is unset.
- Default rendering includes the summary block ONLY when
  :attr:`summary` is set (no fallback prose).
- NO list-shaped methods (the reviewer's check 5 — ``workspace``
  is single-object).
- Typo'd attribute names raise :class:`AttributeError`
  (``slots=True`` discipline).
"""

from __future__ import annotations

import pytest

from templating.vars.workspace import WorkspaceVar


# ---------------------------------------------------------------------------
# Attribute access — four typed fields, no extras.
# ---------------------------------------------------------------------------


def test_four_documented_attributes_reachable() -> None:
    """Spec §4.4 names exactly four attributes; pin them all."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary="A test workspace.",
        allowed_paths=["data/", "bundles/"],
        denied_paths=[".env", ".platform/"],
    )
    assert ws.path == "/tmp/ws"
    assert ws.summary == "A test workspace."
    assert ws.allowed_paths == ["data/", "bundles/"]
    assert ws.denied_paths == [".env", ".platform/"]


def test_typoed_attribute_raises_attribute_error() -> None:
    """Reviewer check 4 — ``slots=True`` makes the dataclass
    reject unknown attribute access. ``workspace.allowd_paths``
    fails at template render under StrictUndefined; here we pin
    the underlying AttributeError so the harness can surface a
    clean ``config.template_render_failed``."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    with pytest.raises(AttributeError):
        _ = ws.allowd_paths  # noqa: SLF001 — deliberate typo


# ---------------------------------------------------------------------------
# .summary — None when unset (NOT empty string).
# ---------------------------------------------------------------------------


def test_summary_returns_text_when_set() -> None:
    """``workspace.summary`` is an ATTRIBUTE (not a method) per
    spec §4.4 — naming collides with the spec §4.2 list-shaped
    ``.summary()`` method on purpose."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary="Active development workspace.",
        allowed_paths=[],
        denied_paths=[],
    )
    assert ws.summary == "Active development workspace."


def test_summary_returns_none_when_unset() -> None:
    """The load-bearing check — when ``platform.yaml:
    workspace_summary`` is absent, ``summary`` is ``None``, not
    the empty string. Lets templates use
    ``{{ workspace.summary | default("…") }}`` cleanly."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    assert ws.summary is None


# ---------------------------------------------------------------------------
# Default rendering — spec §4.4 format.
# ---------------------------------------------------------------------------


def test_default_rendering_includes_summary_when_set() -> None:
    """Spec §4.4: default ``{{ workspace }}`` rendering shows the
    summary block when set."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary="Active development workspace.",
        allowed_paths=["data/", "bundles/"],
        denied_paths=[".env"],
    )
    rendered = str(ws)
    assert "## Active workspace" in rendered
    assert "Active development workspace." in rendered
    assert "Path: /tmp/ws" in rendered
    assert "You can read from: data/, bundles/" in rendered
    assert "You cannot access: .env" in rendered


def test_default_rendering_omits_summary_block_when_unset() -> None:
    """Spec §4.4: "If workspace.summary is unset, the summary
    block is omitted (no fallback prose)." Pin the no-fallback
    contract so a future change that adds prose breaks loudly."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=["data/"],
        denied_paths=[".env"],
    )
    rendered = str(ws)
    assert "## Active workspace" in rendered
    assert "Path: /tmp/ws" in rendered
    # No fallback prose like "Active Kolsch workspace." or similar.
    lines = rendered.split("\n")
    # The header is line 0; the next non-blank line should be
    # "Path:" (NOT a summary fallback).
    non_blank = [line for line in lines if line]
    assert non_blank[1].startswith("Path: ")


# ---------------------------------------------------------------------------
# No list-shaped methods (single-object discipline — reviewer check 5).
# ---------------------------------------------------------------------------


def test_workspace_has_no_list_method() -> None:
    """The reviewer's load-bearing check — ``WorkspaceVar`` is
    single-object. No ``.list()`` method exists (it would be
    nonsensical: workspace isn't a collection)."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    assert not hasattr(ws, "list")


def test_workspace_has_no_get_method() -> None:
    """No ``.get(...)`` method — there's nothing to look up by
    name on a single-object var."""
    ws = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    assert not hasattr(ws, "get")


def test_workspace_summary_is_attribute_not_method() -> None:
    """The naming-collision check — ``workspace.summary`` is the
    attribute (text or None), NOT the spec §4.2 list-shaped
    method. Calling it as a method raises ``TypeError`` (or
    similar) because ``None`` and ``str`` aren't callable."""
    ws_set = WorkspaceVar(
        path="/tmp/ws",
        summary="Hello.",
        allowed_paths=[],
        denied_paths=[],
    )
    ws_unset = WorkspaceVar(
        path="/tmp/ws",
        summary=None,
        allowed_paths=[],
        denied_paths=[],
    )
    # The values themselves aren't callable — confirms the
    # naming-collision design choice.
    assert not callable(ws_set.summary)
    assert ws_unset.summary is None
