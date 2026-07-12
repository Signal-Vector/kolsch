"""Setup-time model-wiring regression — E-BUG-MODELLINE-001 (a-prime).

Verifies the post-fix shape:

- helper.yaml has NO ``model:`` field (the ``{{MODEL_LINE}}`` placeholder
  is gone; nothing replaced it).
- ``platform.yaml``'s ``defaults.model`` carries the picked model.
- The kernel's defaults composition surfaces that value in the
  resolved agent config — the agent runs against the picked model.

Two scenarios per acceptance §3 + §6:

- Default selection: no provider connected → mock LLM fallback lands in
  ``defaults.model`` and the resolved agent picks it up.
- Non-default selection: a connected provider's first model lands in
  ``defaults.model`` and the resolved agent picks it up.
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING, Any

import yaml

from assets import AssetManagement
from cli._setup import NonInteractivePrompter, _SetupIO, run_setup
from kernel import ValidationResult
from tests.component.test_setup import (
    _build_test_connect_io_factory,
    _noop_run_handler,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _accept_anything(_body: dict[str, Any]) -> ValidationResult:
    """Stand-in Layer-3 validator for the standard tools subsystem so
    resolution succeeds without needing the real Tools subsystem."""
    return ValidationResult(ok=True)


def _make_io(root: Path) -> _SetupIO:
    captured = io.StringIO()
    return _SetupIO(
        root=root,
        out=captured,
        err=io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=_build_test_connect_io_factory(captured),
        run_handler=_noop_run_handler,
    )


# ===========================================================================
# Shape — no MODEL_LINE / no model: in helper.yaml
# ===========================================================================


def _files_containing(root: Path, needle: str) -> list[Path]:
    """List workspace files (excluding .platform/) whose text contains
    ``needle``. Kept sync so callers in async tests don't trip
    ASYNC240."""
    hits: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".platform" in path.parts:
            continue
        if needle in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(path)
    return hits


async def test_setup_writes_no_model_line_into_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After setup, the workspace contains zero ``MODEL_LINE`` tokens
    and the helper agent has no ``model:`` field."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_io(tmp_path),
    )
    assert rc == 0

    leftover = _files_containing(tmp_path, "MODEL_LINE")
    assert leftover == [], f"files still contain MODEL_LINE: {leftover}"

    helper_payload = yaml.safe_load(
        (tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml").read_text()
    )
    assert "model" not in helper_payload


# ===========================================================================
# Default selection — mock LLM lands in defaults.model and propagates
# ===========================================================================


async def test_default_selection_writes_mock_to_defaults_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider connected → ``_pick_starter_model`` falls back to
    mock/mock-model; setup writes it to ``defaults.model``; the
    resolved agent config picks it up via defaults composition."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_io(tmp_path),
    )

    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "mock",
        "model": "mock-model",
    }

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    cfg = await am.build_resolved_config(
        "kolsch/agents/helper",
        subsystem_validators={"tools": {"standard": _accept_anything}},
    )
    assert cfg.model.provider == "mock"
    assert cfg.model.model == "mock-model"


# ===========================================================================
# Non-default selection — picked model lands in defaults.model
# ===========================================================================


async def test_non_default_selection_writes_picked_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected provider's first model is picked; setup writes it
    to ``defaults.model``; the resolved agent config picks it up."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    from cli._setup import _setup_platform_payload

    # Use _setup_platform_payload (NOT default_platform_payload) — the
    # former wipes `defaults` to {} so non-`model` defaults entries
    # don't leak into the agent's resolved config. This matches what
    # Step B writes on a fresh setup; Step D then fills in
    # `defaults.model`.
    seeded = {
        **_setup_platform_payload(),
        "llm_providers": {
            "openrouter": {
                "type": "openrouter",
                "api_key_secret": "OPENROUTER_API_KEY",
                "serves_as_metadata_source": True,
                "models": [{"id": "anthropic/claude-opus-4-7"}],
            },
        },
    }
    # Step B short-circuits when platform.yaml exists, so write the
    # full skeleton up front and then run setup to execute Steps C+D.
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(seeded, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_io(tmp_path),
    )

    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4-7",
    }

    am = AssetManagement(root=tmp_path)
    await am.initialize()
    cfg = await am.build_resolved_config(
        "kolsch/agents/helper",
        subsystem_validators={"tools": {"standard": _accept_anything}},
    )
    assert cfg.model.provider == "openrouter"
    assert cfg.model.model == "anthropic/claude-opus-4-7"
