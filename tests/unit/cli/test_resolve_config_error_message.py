"""pub-003 — `_format_resolve_config_error` renders readable stderr text.

Before this, `_cmd_run`/`_cmd_chat` let any `PlatformError` from
`build_resolved_config`/`build_session` escape uncaught — a raw
traceback on, e.g., a workspace left mid-`kolsch setup` (no
`defaults.model` yet). These tests lock in the three render branches:
`ConfigInvalid` (lists each validation finding + a `kolsch setup`
hint), `AssetsNotFound` (a typo'd ref — `kolsch setup` would NOT fix
this, so it gets its own hint), and the generic `PlatformError`
fallback.
"""

from __future__ import annotations

from cli.main import _format_resolve_config_error
from errors import AssetsNotFound, ConfigInvalid, PlatformError


def test_config_invalid_lists_findings_and_suggests_setup() -> None:
    exc = ConfigInvalid(
        validation_errors=[
            {"path": "/model", "code": "missing", "message": "Field required"},
        ],
    )
    out = _format_resolve_config_error(exc, agent_ref="kolsch/agents/helper")
    assert "kolsch/agents/helper" in out
    assert "/model: Field required" in out
    assert "kolsch setup" in out


def test_config_invalid_lists_every_finding() -> None:
    exc = ConfigInvalid(
        validation_errors=[
            {"path": "/model", "code": "missing", "message": "Field required"},
            {"path": "/io", "code": "missing", "message": "Field required"},
        ],
    )
    out = _format_resolve_config_error(exc, agent_ref="kolsch/agents/helper")
    assert "/model: Field required" in out
    assert "/io: Field required" in out


def test_assets_not_found_does_not_suggest_setup() -> None:
    """A typo'd ref isn't fixed by re-running setup — the hint must differ."""
    exc = AssetsNotFound(ref="kolsch/agents/typo", asset_type="agent")
    out = _format_resolve_config_error(exc, agent_ref="kolsch/agents/typo")
    assert "kolsch setup" not in out
    assert "bundles" in out


def test_generic_platform_error_gets_doctor_hint() -> None:
    exc = PlatformError(message="something else went wrong", where="test.fixture")
    out = _format_resolve_config_error(exc, agent_ref="kolsch/agents/helper")
    assert "something else went wrong" in out
    assert "kolsch doctor" in out
