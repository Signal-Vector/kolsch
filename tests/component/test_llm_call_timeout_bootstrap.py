"""bug-086 — bootstrap resolves ``workflows.llm_call_timeout_seconds``.

Covers the platform.yaml side of bug-086 (AC1, AC2, AC5):

- **AC1** — the LLM-call timeout is configurable via ``platform.yaml``
  under ``workflows.llm_call_timeout_seconds`` (not env-only).
- **AC2** — when the key is absent, the resolved default is
  ``>= max(configured provider timeout_seconds)`` — bootstrap
  derives ``max(...) + 30s`` so the cap never undercuts a purpose-
  tuned provider timeout. Falls back to 210s (opencode's tuned 180s
  + 30s margin) when no providers declare a timeout.
- **AC5** — no bootstrap regression; the resolver stays clean under
  the standard fixtures.

Resolution is asserted both on the ``PlatformRuntime`` field (for
observability + parity with the sibling ``workflow_default_node_
timeout_seconds`` field) and on the engine adapter's module-level
state (which is what actually gates the ``asyncio.wait_for`` wrap
per LLM call — see ``engine._crewai_adapter``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from assets.platform_config import default_platform_payload
from engine import _crewai_adapter
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path


def _write_platform_yaml(root: Path, overrides: dict[str, Any]) -> None:
    """Write a platform.yaml with the setup-time defaults + test overrides.

    ``bootstrap_runtime`` requires the mandatory sections (secrets /
    sandbox / authorization / storage / event_bus / file_access /
    defaults) — passing a bare ``{workflows: ...}`` mapping trips
    the section-lookup KeyError inside bootstrap. Starting from
    ``default_platform_payload`` and merging the override at the
    top level mirrors ``kolsch setup``'s output plus the caller's
    intent.
    """
    payload = default_platform_payload()
    payload.update(overrides)
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_configured_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate this file's writes to ``_configured_default_timeout``.

    Each bootstrap call writes process-global state via
    ``set_llm_call_timeout_default``. Resetting to the sentinel
    before every test keeps assertions here independent of test
    ordering (and of any earlier bootstrap-integration test).
    """
    monkeypatch.setattr(
        _crewai_adapter,
        "_configured_default_timeout",
        _crewai_adapter._UNSET_CONFIGURED_DEFAULT,
    )
    monkeypatch.delenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", raising=False)


@pytest.mark.asyncio
async def test_explicit_value_in_platform_yaml_is_installed(
    tmp_path: Path,
) -> None:
    """AC1 — an explicit ``workflows.llm_call_timeout_seconds: 300`` is
    threaded through bootstrap onto both the runtime field and the
    engine adapter's module-level state.
    """
    _write_platform_yaml(tmp_path, {"workflows": {"llm_call_timeout_seconds": 300}})

    runtime = await bootstrap_runtime(root=tmp_path)

    assert runtime.llm_call_timeout_seconds == pytest.approx(300.0)
    assert _crewai_adapter._configured_default_timeout == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_null_in_platform_yaml_disables_the_wrap(
    tmp_path: Path,
) -> None:
    """AC1 — explicit ``null`` opts out of the wrap (unbounded call).

    The engine adapter's ``_read_llm_call_timeout_seconds`` then
    returns ``None`` and ``make_llm_callable`` runs the underlying
    ``asyncio.to_thread`` without a ``wait_for`` bound.
    """
    _write_platform_yaml(tmp_path, {"workflows": {"llm_call_timeout_seconds": None}})

    runtime = await bootstrap_runtime(root=tmp_path)

    assert runtime.llm_call_timeout_seconds is None
    assert _crewai_adapter._configured_default_timeout is None


@pytest.mark.asyncio
async def test_absent_key_no_providers_falls_back_to_210_seconds(
    tmp_path: Path,
) -> None:
    """AC2 — with no ``llm_providers`` and no explicit key, bootstrap
    installs the 210s hardcoded fallback (= opencode's tuned 180s +
    30s margin) so a bare-install does not undercut the provider.
    """
    runtime = await bootstrap_runtime(root=tmp_path)

    assert runtime.llm_call_timeout_seconds == pytest.approx(210.0)
    assert _crewai_adapter._configured_default_timeout == pytest.approx(210.0)


@pytest.mark.asyncio
async def test_absent_key_derives_from_max_provider_timeout_plus_margin(
    tmp_path: Path,
) -> None:
    """AC2 — with providers configured but no explicit key, bootstrap
    picks ``max(provider.timeout_seconds) + 30s`` so the cap covers
    the slowest configured provider with a small margin.

    Two providers here: fast @ 60s, slow @ 300s → resolved default
    == 330s (300 + 30). The old fixed 120s constant would have
    fired inside every 300s provider call — the exact regression
    bug-086 fixes.
    """
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "fast": {"timeout_seconds": 60},
                "slow": {"timeout_seconds": 300},
            },
        },
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    assert runtime.llm_call_timeout_seconds == pytest.approx(330.0)
    assert _crewai_adapter._configured_default_timeout == pytest.approx(330.0)


@pytest.mark.asyncio
async def test_absent_key_ignores_providers_without_declared_timeout(
    tmp_path: Path,
) -> None:
    """AC2 — a provider with no ``timeout_seconds:`` doesn't participate
    in the derivation. Only declared timeouts feed ``max(...)``.

    Two providers here: one at 200s, one with the key omitted →
    resolved default == 230s (200 + 30). The undeclared provider
    is silent about its own HTTP cap, so we can't factor it in;
    bootstrap uses only the declared value.
    """
    _write_platform_yaml(
        tmp_path,
        {
            "llm_providers": {
                "declared": {"timeout_seconds": 200},
                "silent": {"base_url": "https://example.invalid"},
            },
        },
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    assert runtime.llm_call_timeout_seconds == pytest.approx(230.0)
    assert _crewai_adapter._configured_default_timeout == pytest.approx(230.0)
