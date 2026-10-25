"""Component tests for the CLI dispatcher gate + the `kolsch setup` flow.

Covers TC-UX-04..10 from docs/process/test-catalog.md Part 9.6.

Splits responsibilities:

- TC-UX-04 — CLI gate (missing-workspace message + exit 3 + no
  traceback). Exercised by calling :func:`cli.main.main` directly
  with an empty temp directory.
- TC-UX-05 — `setup --non-interactive` produces a runnable
  workspace (the `workspace_is_initialized` gate accepts it).
- TC-UX-06 — Re-running setup is idempotent. Verified by content
  hash; mtime is too noisy when steps re-create dirs.
- TC-UX-07 — `KOLSCH_PROVIDER` / `KOLSCH_PROVIDER_KEY` env-driven
  auto-connect. Uses `httpx.MockTransport` for both the OpenRouter
  `/auth/key` and `/api/v1/models` endpoints.
- TC-UX-08 — Starter agent file shape; mock fallback when no
  provider is connected.
- TC-UX-09 — Resumability after Ctrl-C during a prompt; second
  run picks up at step D.
- TC-UX-10 — End-to-end smoke: run setup, then run the helper
  agent against the produced workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml

from cli._setup import (
    _STARTER_BUNDLE_TEMPLATE_DIR,
    NonInteractivePrompter,
    SetupPrompter,
    _SetupIO,
    _TestRunResult,
    run_setup,
)
from cli.main import main
from kolsch._yamlio import load
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from cli.llm_commands import _CommandIO
    from llm.protocol import PromptField, ProviderConnector


# ===========================================================================
# Helpers
# ===========================================================================


_OPENROUTER_AUTH_RESPONSE = {
    "data": {"limit": 5.0, "usage": 0.0, "label": "test"},
}

_OPENROUTER_MODELS_RESPONSE = {
    "data": [
        {
            "id": "anthropic/claude-opus-4-7",
            "context_length": 200000,
            "supported_parameters": ["tools", "stream"],
            "pricing": {"prompt": "0.000015", "completion": "0.000075"},
        },
    ]
}

_OPENCODE_MODELS_RESPONSE = {
    "object": "list",
    "data": [{"id": "gpt-oss-120b"}],
}


def _openrouter_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/auth/key"):
        return httpx.Response(200, json=_OPENROUTER_AUTH_RESPONSE)
    if path.endswith("/models"):
        return httpx.Response(200, json=_OPENROUTER_MODELS_RESPONSE)
    return httpx.Response(404, json={"error": "unhandled"})


def _opencode_handler(request: httpx.Request) -> httpx.Response:
    _ = request
    return httpx.Response(200, json=_OPENCODE_MODELS_RESPONSE)


def _mock_connector_factory(
    provider_type: str,
) -> ProviderConnector:
    """Like the Phase 1.1 test factory — returns a connector with the
    matching HTTP MockTransport baked in."""
    if provider_type == "openrouter":
        return OpenRouterConnector(transport=httpx.MockTransport(_openrouter_handler))
    if provider_type == "opencode":
        return OpenCodeConnector(transport=httpx.MockTransport(_opencode_handler))
    from errors import LlmConnectorUnknown

    raise LlmConnectorUnknown(
        type_name=provider_type,
        registered_types=["opencode", "openrouter"],
    )


def _build_test_connect_io_factory(
    captured_out: io.StringIO,
) -> Callable[[Path, str, str | None], _CommandIO]:
    """Build a connect-IO factory that uses MockTransport-wired
    connectors and an env-key prompter (for the non-interactive
    auto-connect path)."""
    from cli.llm_commands import _CommandIO

    class _EnvKeyPrompter:
        def __init__(self, key: str) -> None:
            self._key = key

        def ask(self, field: PromptField) -> str:
            if field.name == "api_key":
                return self._key
            return field.default or ""

    def factory(root: Path, provider: str, api_key: str | None) -> _CommandIO:
        _ = provider
        prompter = _EnvKeyPrompter(api_key or "")
        return _CommandIO(
            root=root,
            out=captured_out,
            prompter=prompter,
            connector_factory=_mock_connector_factory,
        )

    return factory


async def _noop_run_handler(
    args: argparse.Namespace,
) -> _TestRunResult:
    """Stand-in for the run-once seam for tests that don't exercise
    step E end-to-end. Returns ``_TestRunResult(rc=0)`` to signal a
    "successful" test run without standing up the full runtime.

    bug-147: the run-handler returns a ``_TestRunResult`` (exit code,
    session id, durable error envelope) so Step E can route the error
    code into the classifier-driven hint."""
    _ = args
    return _TestRunResult(rc=0)


def _make_test_io(
    *,
    root: Path,
    out: io.StringIO | None = None,
    err: io.StringIO | None = None,
    prompter: SetupPrompter | None = None,
    run_handler: Callable[[argparse.Namespace], Awaitable[_TestRunResult]]
    | None = None,
) -> _SetupIO:
    captured_out = out if out is not None else io.StringIO()
    return _SetupIO(
        root=root,
        out=captured_out,
        err=err if err is not None else io.StringIO(),
        prompter=prompter if prompter is not None else NonInteractivePrompter(),
        connect_io_factory=_build_test_connect_io_factory(captured_out),
        run_handler=run_handler if run_handler is not None else _noop_run_handler,
    )


# ===========================================================================
# TC-UX-04 — friendly message + exit 3 + no traceback
# ===========================================================================


def test_tc_ux_04_cli_emits_friendly_message_on_missing_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a non-setup command against an empty path; expect the exact
    friendly message on stderr, exit 3, no traceback."""
    # Ensure no env var override leaks in.
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    rc = main(["--root", str(tmp_path), "providers"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "No Kolsch workspace at " in err
    assert str(tmp_path.resolve()) in err
    assert "Run `kolsch setup`" in err
    assert "KOLSCH_WORKSPACE" in err
    # No Python traceback markers.
    assert "Traceback" not in err
    assert "  File " not in err


# ===========================================================================
# TC-UX-05 — `setup --non-interactive` produces a runnable workspace
# ===========================================================================


async def test_tc_ux_05_non_interactive_setup_initializes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        io_,
    )
    assert rc == 0
    from platform_runtime.workspace import workspace_is_initialized

    assert workspace_is_initialized(tmp_path) is True
    # Skeleton present.
    for name in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        assert (tmp_path / name).is_dir()
    # Starter agent ships with the mock LLM since no provider connected.
    # Post-E-BUG-MODELLINE-001 option (a-prime): helper.yaml no longer
    # declares a `model:` field; the picked model lands in platform.yaml's
    # defaults.model and flows in through the kernel's defaults composition.
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert starter.is_file()
    helper_payload = yaml.safe_load(starter.read_text())
    assert "model" not in helper_payload
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "mock",
        "model": "mock-model",
    }


# ===========================================================================
# qol-workspace-root-listable — `.` belongs in file_access.allowed_paths
# ===========================================================================


async def test_fresh_setup_ships_workspace_root_in_allowed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qol-workspace-root-listable.md — a fresh setup ships `.`
    as the first allowed_paths entry so list_files(path=".")
    works in the workspace root."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            io_,
        )
        == 0
    )
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    allowed = platform_payload["file_access"]["allowed_paths"]
    assert allowed[0] == ".", (
        f"workspace root should be first in allowed_paths; got {allowed}"
    )


async def test_setup_rerun_adds_workspace_root_to_legacy_allowed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qol-workspace-root-listable.md — a workspace authored before
    the QoL (no `.` in allowed_paths) gains the entry on `kolsch setup`
    re-run. Mirrors the authz-rules-migration pattern."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    # First setup writes a fresh workspace.
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            io_,
        )
        == 0
    )
    # Simulate legacy state: strip `.` from allowed_paths.
    yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text())
    payload["file_access"]["allowed_paths"] = [
        p for p in payload["file_access"]["allowed_paths"] if p != "."
    ]
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    # Re-run; the migrator should add `.` back at the front.
    rerun_io = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            rerun_io,
        )
        == 0
    )
    final = yaml.safe_load(yaml_path.read_text())
    allowed = final["file_access"]["allowed_paths"]
    assert allowed[0] == ".", f"migration should put `.` first; got {allowed}"
    # Backup written under .platform/ (per _backup_platform_yaml at
    # src/cli/_setup.py:949+).
    backups = list((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    assert backups, "migration should have written a timestamped backup"


async def test_setup_rerun_adds_agent_execute_allow_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup ensures a broad `agent.execute.*` allow exists.

    Regression: a workspace hand-narrowed to a single agent-execute
    rule (e.g. `^agent\\.execute\\.ag_butler$`) used to fail Step E
    when the helper agent's pre-flight authz check found no matching
    allow. The setup step
    `_maybe_ensure_agent_execute_allow` prepends the broad
    `agent.execute.*` allow when no coverage is present, unblocking
    subsequent `kolsch run <agent>` invocations.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            io_,
        )
        == 0
    )
    yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text())
    # Simulate hand-narrowing: drop the default `agent.execute.*`
    # allow, keep only a butler-specific legacy rule.
    payload["authorization"]["config"]["rules"] = [
        {
            "id": "rl_butler_only",
            "who": ".*",
            "what": r"^agent\.execute\.ag_butler$",
            "effect": "allow",
        },
        # Retain the default-deny tail so the check is realistic.
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    # Re-run; the guard should prepend a broad agent.execute.* allow.
    rerun_io = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            rerun_io,
        )
        == 0
    )
    final = yaml.safe_load(yaml_path.read_text())
    rules = final["authorization"]["config"]["rules"]
    assert rules[0] == {
        "what": "agent.execute.*",
        "pattern": "*",
        "effect": "allow",
    }, f"expected broad agent.execute.* allow at position 0; got {rules[0]}"
    # Existing butler-specific rule survives at position 1 — auto-add
    # is additive, not destructive.
    assert rules[1]["what"] == r"^agent\.execute\.ag_butler$"


async def test_setup_rerun_no_op_when_agent_execute_allow_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent — a workspace already covering agent.execute.* is untouched."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            io_,
        )
        == 0
    )
    yaml_path = tmp_path / "platform.yaml"
    before = yaml.safe_load(yaml_path.read_text())
    before_rules = before["authorization"]["config"]["rules"]
    # Re-run.
    rerun_io = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            rerun_io,
        )
        == 0
    )
    after = yaml.safe_load(yaml_path.read_text())
    after_rules = after["authorization"]["config"]["rules"]
    assert after_rules == before_rules, (
        "setup re-run must be a no-op when agent.execute.* is already covered"
    )


async def test_setup_recognises_legacy_regex_agent_execute_catchall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy-shape `^agent\\.execute\\..*$` regex counts as coverage.

    Guards against redundantly prepending a new-shape allow when the
    workspace already has the legacy-form catchall (from a pre-QoL
    migration). Rule position 0 stays the legacy rule, no new-shape
    duplicate appears.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            io_,
        )
        == 0
    )
    yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text())
    payload["authorization"]["config"]["rules"] = [
        {
            "id": "rl_legacy_ae",
            "who": ".*",
            "what": r"^agent\.execute\..*$",
            "effect": "allow",
        },
        {"what": "*", "pattern": "*", "effect": "deny"},
    ]
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    rerun_io = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path),
            rerun_io,
        )
        == 0
    )
    final = yaml.safe_load(yaml_path.read_text())
    rules = final["authorization"]["config"]["rules"]
    # The pre-existing qol-schema migrator rewrites the legacy regex
    # to the new-shape catchall at position 0. Our guard sees THAT
    # as coverage and correctly does not append a duplicate.
    catchall_indices = [
        i for i, r in enumerate(rules) if r.get("what") == "agent.execute.*"
    ]
    assert len(catchall_indices) == 1, (
        "expected exactly one agent.execute.* allow after setup; "
        f"got {catchall_indices} across rules {rules}"
    )


# ===========================================================================
# TC-UX-06 — idempotency
# ===========================================================================


async def test_tc_ux_06_setup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    first_io = _make_test_io(root=tmp_path)
    assert await run_setup(args, first_io) == 0

    # qol-133 step 2: the very first re-run against a freshly-installed
    # workspace legitimately writes a one-time `.kolsch/hooks-notice-
    # shown` marker (the workspace has no `hooks:` key yet). That one
    # write is a deliberate, single, self-limiting side effect — not a
    # break in idempotency — so it happens here, before the snapshot
    # this test actually asserts on, rather than between the two
    # snapshots being compared.
    warmup_io = _make_test_io(root=tmp_path)
    assert await run_setup(args, warmup_io) == 0

    snapshot_before = _hash_workspace(tmp_path)

    second_io = _make_test_io(root=tmp_path)
    assert await run_setup(args, second_io) == 0

    snapshot_after = _hash_workspace(tmp_path)
    assert snapshot_before == snapshot_after


def _hash_workspace(root: Path) -> dict[str, str]:
    """Return path → sha256 for every regular file in the workspace
    (excluding `.platform/` since SQLite touches it on bootstrap)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".platform":
            continue
        out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ===========================================================================
# TC-UX-07 — env-driven auto-connect via mocked HTTP
# ===========================================================================


async def test_tc_ux_07_env_driven_auto_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOLSCH_PROVIDER", "openrouter")
    monkeypatch.setenv("KOLSCH_PROVIDER_KEY", "sk-test-or")
    io_ = _make_test_io(root=tmp_path)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    rc = await run_setup(args, io_)
    assert rc == 0

    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    providers = payload.get("llm_providers") or {}
    assert "openrouter" in providers
    assert providers["openrouter"]["api_key_secret"] == "OPENROUTER_API_KEY"

    env_text = (tmp_path / ".env").read_text()
    assert "OPENROUTER_API_KEY" in env_text
    assert "sk-test-or" in env_text
    # The literal secret value must not appear in platform.yaml.
    assert "sk-test-or" not in (tmp_path / "platform.yaml").read_text()


# ===========================================================================
# TC-UX-08 — starter agent model field reflects the connected provider
# ===========================================================================


async def test_tc_ux_08_starter_falls_back_to_mock_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (a): no provider → starter model is the mock LLM.

    Post-E-BUG-MODELLINE-001 (a-prime): the picked model lands in
    ``platform.yaml``'s ``defaults.model`` rather than the helper
    agent's ``model:`` field.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "mock",
        "model": "mock-model",
    }


async def test_tc_ux_08_starter_uses_opus_alias_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (b): a connected provider's first model has a matching
    alias → the starter agent's `model` is the alias string."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    # Seed a connected provider whose first model matches the
    # `opus` alias target. Step D's `_pick_starter_model` walks
    # the provider's models list FIRST, then substitutes a matching
    # alias for readability.
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "model_aliases": {
                    "opus": "openrouter/anthropic/claude-opus-4-7",
                    "smart": "openrouter/openai/gpt-4o",
                },
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "models": [
                            {"id": "anthropic/claude-opus-4-7"},
                            {"id": "openai/gpt-4o"},
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == "opus"


async def test_starter_prefers_openrouter_over_opencode_when_no_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both OpenCode and OpenRouter are connected with no
    ``model_aliases`` defined, the starter picks OpenRouter — per the
    documented provider-priority tie-breaker.

    *(Bug-setup-test-run-failure-handling.md: prior to the bug fix
    this test asserted the inverse — preferring the
    non-metadata-source provider — which surfaced the user-reported
    bug where setup chose a raw OpenCode model and tripped on a
    transient upstream 500. The new behaviour reflects acceptance
    criterion 3's tie-breaker order. The aliased-preferred path is
    covered separately by TC-UX-12.)*"""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-opus-4-7"}],
                    },
                    "opencode": {
                        "type": "opencode",
                        "api_key_secret": "OPENCODE_API_KEY",
                        "models": [
                            {"id": "gpt-oss-120b"},
                            {"id": "qwen2.5-coder-32b"},
                        ],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4-7",
    }


async def test_starter_uses_openrouter_when_sole_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If only an OpenRouter (metadata-source) provider is connected,
    the starter still uses it rather than falling back to the mock
    LLM — better real-LLM-than-echo."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-haiku-4-5"}],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert platform_payload["defaults"]["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
    }


async def test_starter_skips_provider_with_empty_models_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected provider whose discovery returned 0 models is
    skipped; the next provider in the priority pool is tried."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "opencode": {
                        "type": "opencode",
                        "api_key_secret": "OPENCODE_API_KEY",
                        "models": [],  # empty — should be skipped
                    },
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [{"id": "anthropic/claude-haiku-4-5"}],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    # OpenCode skipped (empty), OpenRouter picked even as fallback.
    assert platform_payload["defaults"]["model"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-haiku-4-5",
    }


def _default_yaml() -> str:
    from assets.platform_config import default_platform_payload

    return yaml.safe_dump(default_platform_payload(), sort_keys=False)


# ===========================================================================
# TC-UX-09 — resumability after KeyboardInterrupt during step D's prompt
# ===========================================================================


async def test_tc_ux_09_resumable_after_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run: a prompter that raises KeyboardInterrupt during step
    D's confirm prompt. Second run: a NonInteractivePrompter; assert
    step C is detected as done (skeleton + .env exist; no provider
    connected → step C still prompts but in non-interactive mode it
    cleanly skips) and step D actually writes the starter agent."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    class _InterruptingPrompter:
        def __init__(self) -> None:
            self.confirm_calls = 0

        def choose(self, q: str, opts: list[str], default: str) -> str:
            return default

        def text(self, q: str, default: str = "") -> str:
            return default

        def confirm(self, q: str, *, default: bool = True) -> bool:
            _ = q, default
            self.confirm_calls += 1
            raise KeyboardInterrupt

    first_prompter = _InterruptingPrompter()
    first_io = _make_test_io(root=tmp_path, prompter=first_prompter)
    rc_first = await run_setup(
        argparse.Namespace(non_interactive=False, root=tmp_path),
        first_io,
    )
    # The interrupt happens during step D's confirm prompt; setup
    # returns 4 (partial).
    assert rc_first == 4
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert not starter.exists(), (
        "starter agent should not exist after step D was interrupted"
    )

    # Second run: skeleton exists (B skipped), no provider (C skipped),
    # starter doesn't exist (D writes it), test run executes (E).
    rc_second = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_test_io(root=tmp_path),
    )
    assert rc_second == 0
    assert starter.exists()


# ===========================================================================
# TC-UX-10 — end-to-end: setup then `kolsch run`
# ===========================================================================


def test_tc_ux_10_end_to_end_run_against_setup_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Setup --non-interactive produces a workspace; `kolsch run` of
    the starter agent against the same workspace returns 0 and emits
    an `agent.message` event.

    Defined as a sync test on purpose — `cli.main.main` internally
    calls ``asyncio.run`` and would fail from inside an existing
    event loop. The CLI is the system-under-test, so we exercise it
    exactly as the console-script entry point does.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)

    # Setup via the real cli.main entry point — exercises the dispatch
    # gate + the setup orchestrator + the default IO wiring end to end.
    rc_setup = main(["--root", str(tmp_path), "setup", "--non-interactive"])
    assert rc_setup == 0

    # Then run the starter agent. Use the real CLI again so the
    # dispatcher + runtime bootstrap path is exercised.
    capsys.readouterr()  # drain setup's stdout.
    rc_run = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "kolsch/agents/helper",
            "--input",
            "content=hello",
        ]
    )
    assert rc_run == 0
    out = capsys.readouterr().out
    # `_cmd_run` writes the agent.message data as JSON to stdout on
    # success — confirm there's a non-empty JSON payload (the
    # EchoLLMDriver echoes the user message).
    assert "hello" in out or out.strip()


# ===========================================================================
# Extra coverage — interactive paths + production-factory + api.make_app
# ===========================================================================


class _ScriptedPrompter:
    """SetupPrompter stub that returns canned answers per call."""

    def __init__(
        self,
        choose: list[str] | None = None,
        text: list[str] | None = None,
        confirm: list[bool] | None = None,
    ) -> None:
        self._choose = list(choose or [])
        self._text = list(text or [])
        self._confirm = list(confirm or [])

    def choose(self, question: str, options: list[str], default: str) -> str:
        _ = question, options
        return self._choose.pop(0) if self._choose else default

    def text(self, question: str, default: str = "") -> str:
        _ = question
        return self._text.pop(0) if self._text else default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        _ = question
        return self._confirm.pop(0) if self._confirm else default


async def test_interactive_setup_user_skips_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive path: user picks `s` at step C (skip provider)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["s"],  # step C — skip provider
        confirm=[True, True],  # step D create, step E run
    )
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert starter.is_file()


async def test_interactive_user_declines_starter_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step D confirm returns False → starter agent not written."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["s"],
        confirm=[False],  # decline starter agent
    )
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    starter = tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml"
    assert not starter.exists()


async def test_step_e_skipped_when_starter_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If step D was declined, step E has no agent to run and skips."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    captured = io.StringIO()
    prompter = _ScriptedPrompter(choose=["s"], confirm=[False])
    io_ = _make_test_io(root=tmp_path, prompter=prompter, out=captured)
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    assert "no starter agent to test" in captured.getvalue()


async def test_make_default_setup_io_wires_production_defaults(
    tmp_path: Path,
) -> None:
    """The production IO factory wires StdinSetupPrompter + default
    connect-IO factory + the real run handler (over `_run_agent_once`).
    Constructing it
    is enough to exercise the wiring path."""
    from cli._setup import StdinSetupPrompter, make_default_setup_io

    io_ = make_default_setup_io(root=tmp_path)
    assert io_.root == tmp_path
    assert isinstance(io_.prompter, StdinSetupPrompter)
    # connect_io_factory + run_handler are callables wired to the
    # production paths; constructing them isn't safe to invoke
    # without stdin / a real runtime, but the references are pinned.
    assert callable(io_.connect_io_factory)
    assert callable(io_.run_handler)


async def test_api_make_app_raises_on_uninitialized_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`api.make_app` on a workspace with no platform.yaml raises
    :class:`WorkspaceNotInitialized` — operators get a typed error
    rather than a confusing FileNotFoundError later."""
    from api import make_app
    from errors import WorkspaceNotInitialized

    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    with pytest.raises(WorkspaceNotInitialized):
        await make_app(root=tmp_path)


async def test_api_make_app_returns_app_when_workspace_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace is initialized, `make_app` resolves +
    bootstraps + builds without error. Scaffolds the workspace
    directly (no in-process `main()` call — that would re-enter
    asyncio.run from inside the test loop)."""
    from api import make_app

    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)
    # Lay down the same skeleton kolsch setup --non-interactive does.
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    app = await make_app(root=tmp_path)
    paths = {route.path for route in app.routes}
    assert any("sessions" in p for p in paths)


def test_stdin_prompter_choose_accepts_default(tmp_path: Path) -> None:
    """`StdinSetupPrompter.choose` returns the (lowercased) default
    when stdin yields an empty line."""
    from cli._setup import StdinSetupPrompter

    sink = io.StringIO()
    prompter = StdinSetupPrompter(sink)

    import builtins

    seq = iter([""])  # one empty line
    real_input = builtins.input
    builtins.input = lambda *_, **__: next(seq)
    try:
        out = prompter.choose("pick:", ["[r] foo", "[s] skip"], default="s")
    finally:
        builtins.input = real_input
    assert out == "s"
    _ = tmp_path


def test_stdin_prompter_text_returns_default_on_empty() -> None:
    from cli._setup import StdinSetupPrompter

    prompter = StdinSetupPrompter(io.StringIO())
    import builtins

    real_input = builtins.input
    builtins.input = lambda *_, **__: ""
    try:
        assert prompter.text("name?", default="d") == "d"
        assert prompter.confirm("ok?", default=True) is True
    finally:
        builtins.input = real_input


def test_stdin_prompter_confirm_no_default() -> None:
    from cli._setup import StdinSetupPrompter

    prompter = StdinSetupPrompter(io.StringIO())
    import builtins

    real_input = builtins.input
    builtins.input = lambda *_, **__: "n"
    try:
        assert prompter.confirm("ok?", default=True) is False
    finally:
        builtins.input = real_input


async def test_interactive_step_c_picks_openrouter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive step C with the user picking 'r' (openrouter)
    routes through cmd_connect.

    Production interactive mode reads the API key from stdin via
    cmd_connect's StdinPrompter. For the test we override the
    connect-IO factory to inject a fixed-key prompter regardless of
    what the orchestrator passes — the substance we're verifying is
    that picking 'r' triggers the openrouter connect call (lines
    299-308 in src/cli/_setup.py)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    prompter = _ScriptedPrompter(
        choose=["r"],  # step C — pick openrouter
        confirm=[True, True],  # step D create, step E run
    )

    captured = io.StringIO()
    from cli.llm_commands import _CommandIO

    class _FixedKeyPrompter:
        def ask(self, field: PromptField) -> str:
            if field.name == "api_key":
                return "sk-fixed-test"
            return field.default or ""

    def fixed_factory(root: Path, provider: str, api_key: str | None) -> _CommandIO:
        _ = provider, api_key  # interactive path passes None here
        return _CommandIO(
            root=root,
            out=captured,
            prompter=_FixedKeyPrompter(),
            connector_factory=_mock_connector_factory,
        )

    io_ = _SetupIO(
        root=tmp_path,
        out=captured,
        err=io.StringIO(),
        prompter=prompter,
        connect_io_factory=fixed_factory,
        run_handler=_noop_run_handler,
    )
    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert rc == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert "openrouter" in (payload.get("llm_providers") or {})


async def test_step_e_test_run_failure_returns_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `run_handler` returns non-zero, setup returns 4 (partial)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    async def failing_run(
        args: argparse.Namespace,
    ) -> _TestRunResult:
        _ = args
        return _TestRunResult(rc=1)

    io_ = _make_test_io(root=tmp_path, run_handler=failing_run)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 4


async def test_non_interactive_with_partial_env_skips_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting `KOLSCH_PROVIDER` but not `KOLSCH_PROVIDER_KEY` (or vice
    versa) is treated as "no provider" — the env-driven connect path
    requires both."""
    monkeypatch.setenv("KOLSCH_PROVIDER", "openrouter")
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 0
    payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    assert not (payload.get("llm_providers") or {})


def test_default_connect_io_factory_without_api_key() -> None:
    """The production default connect-IO factory returns a normal
    `_CommandIO` with StdinPrompter when no env-key is supplied."""
    from pathlib import Path as _Path

    from cli._setup import _default_connect_io_factory
    from cli.llm_commands import StdinPrompter

    io_obj = _default_connect_io_factory(_Path("/var/ws/x"), "openrouter", None)
    assert isinstance(io_obj.prompter, StdinPrompter)


def test_default_connect_io_factory_with_api_key() -> None:
    """When an env-key is supplied, the factory wraps a prompter that
    returns that key for any ``api_key`` field."""
    from pathlib import Path as _Path

    from cli._setup import _default_connect_io_factory
    from llm.protocol import PromptField

    io_obj = _default_connect_io_factory(_Path("/var/ws/x"), "openrouter", "sk-x")
    field = PromptField(name="api_key", label="API key", kind="secret")
    assert io_obj.prompter.ask(field) == "sk-x"
    # Non-api-key fields fall through to default.
    field2 = PromptField(
        name="base_url",
        label="Base URL",
        kind="url",
        default="https://example.com",
    )
    assert io_obj.prompter.ask(field2) == "https://example.com"


# ===========================================================================
# TC-UX-11..13 — bug-setup-test-run-failure-handling.md regression tests
# ===========================================================================


async def test_tc_ux_11_failed_test_run_emits_partial_banner_and_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-UX-11: setup --non-interactive with a mock run handler that
    simulates a 5xx model failure. Assert:

    - the partial-completion banner appears in stdout
    - the "Kolsch is ready" string does NOT appear in stdout
    - exit code is 4
    - the diagnostic line mentions "5xx" or "server" (the
      classifier's hint for ``model.server_error``)
    - platform.yaml and starter agent exist (workspace IS set up;
      only the test run failed)

    This reproduces the user-reported bug
    (bug-setup-test-run-failure-handling.md Part 0) deterministically
    without depending on a flaky upstream.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    async def failing_run(
        args: argparse.Namespace,
    ) -> _TestRunResult:
        _ = args
        # The production handler in make_default_setup_io copies this
        # envelope from the failed event's data["error"]; the test
        # injects it directly so the classifier hint can be asserted.
        return _TestRunResult(rc=2, error={"type": "model.server_error"})

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    io_ = _make_test_io(
        root=tmp_path,
        out=captured_out,
        err=captured_err,
        run_handler=failing_run,
    )

    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)

    out = captured_out.getvalue()

    # Exit code 4 (partial completion) — Phase 1.2 spec §2.5.
    assert rc == 4

    # The "Kolsch is ready" string MUST NOT appear on this path.
    assert "Kolsch is ready" not in out, (
        f"the buggy success banner fired despite the test failing; "
        f"see bug-setup-test-run-failure-handling.md acceptance "
        f"criterion 1.\nfull stdout:\n{out}"
    )

    # The partial-completion banner appears with the workspace path +
    # the classifier-driven hint for model.server_error.
    assert "completed partially" in out
    assert "model.server_error" in out
    # The hint specifically mentions "5xx" per criterion 4.
    assert "5xx" in out

    # The workspace IS set up — only the test run failed.
    assert (tmp_path / "platform.yaml").is_file()
    assert (tmp_path / "bundles" / "kolsch" / "agents" / "helper.yaml").is_file()


async def test_tc_ux_12_starter_prefers_alias_over_raw_opencode_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-UX-12: two providers connected — OpenRouter has aliases
    (``opus``, ``smart``, ``gpt4o``), OpenCode has raw model ids.
    Assert that the starter agent's ``model:`` field is the
    ``opus`` alias string — NOT a raw ``opencode/<model>``
    object.

    This reproduces the user-reported model-picker bug
    (bug-setup-test-run-failure-handling.md Part 0 + H4): the user
    had aliases available but setup chose a raw OpenCode model
    instead, and that model returned a transient 500.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                **yaml.safe_load(_default_yaml()),
                "llm_providers": {
                    "openrouter": {
                        "type": "openrouter",
                        "api_key_secret": "OPENROUTER_API_KEY",
                        "serves_as_metadata_source": True,
                        "models": [
                            {"id": "anthropic/claude-opus-4-7"},
                            {"id": "openai/gpt-4o"},
                        ],
                    },
                    "opencode": {
                        "type": "opencode",
                        "api_key_secret": "OPENCODE_API_KEY",
                        "models": [
                            {"id": "minimax-m2.7"},
                            {"id": "qwen2.5-coder-32b"},
                        ],
                    },
                },
                "model_aliases": {
                    "opus": "openrouter/anthropic/claude-opus-4-7",
                    "smart": "openrouter/openai/gpt-4o",
                    "gpt4o": "openrouter/openai/gpt-4o",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    platform_payload = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    picked = platform_payload["defaults"]["model"]

    # The picker chose ``opus`` (the documented historic recommendation,
    # first in _ALIAS_PRIORITY). Post-E-BUG-MODELLINE-001 (a-prime) the
    # picked value lives in platform.yaml's defaults.model, not
    # helper.yaml.
    assert picked == "opus", (
        f"expected ``opus`` alias, got {picked!r}; this is "
        f"the user-reported model-picker bug — see "
        f"bug-setup-test-run-failure-handling.md acceptance "
        f"criterion 3 + H4."
    )

    # The raw OpenCode model that caused the upstream 500 in the
    # original report MUST NOT have been chosen.
    assert picked != {"provider": "opencode", "model": "minimax-m2.7"}


async def test_tc_ux_13_user_declining_test_run_still_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-UX-13: the user presses [n] on the test-run prompt. Setup
    treats this as full success (the user actively skipped, not a
    failure). The banner says "ready" with a "(test skipped)"
    suffix so the user knows nothing was exchanged but the workspace
    IS ready.

    Bug-setup-test-run-failure-handling.md acceptance criterion 2 —
    declining the test is exit 0, not exit 4.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)

    class _DeclineTestPrompter:
        """Mirrors NonInteractivePrompter for choose/text/default-confirm
        but returns False for the test-run confirm prompt specifically."""

        def __init__(self) -> None:
            self.confirm_count = 0

        def choose(self, q: str, opts: list[str], default: str) -> str:
            _ = q, opts
            return default.lower()

        def text(self, q: str, default: str = "") -> str:
            _ = q
            return default

        def confirm(self, q: str, *, default: bool = True) -> bool:
            self.confirm_count += 1
            # The Step E prompt asks "Run the helper agent once to
            # confirm everything works?". Step D's confirm also fires
            # ("Create starter agent at ..."). Return True for the
            # starter-agent confirm; False for the test-run confirm.
            if "Run the helper agent" in q:
                return False
            return default

    captured_out = io.StringIO()
    io_ = _make_test_io(
        root=tmp_path,
        out=captured_out,
        prompter=_DeclineTestPrompter(),
    )

    rc = await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)

    out = captured_out.getvalue()

    # Exit 0 — declined is success, not failure.
    assert rc == 0

    # The success banner appears with the "(test skipped)" suffix.
    assert "Kolsch is ready (test skipped)" in out, (
        f"expected the success banner with the (test skipped) suffix; got:\n{out}"
    )

    # The partial-completion banner MUST NOT appear.
    assert "completed partially" not in out


# ===========================================================================
# bug-147 — the failure banner reports what setup actually did
# ===========================================================================

_B147_SESSION = "ses_0cd8df9d-2c6e-4c21-bbff-dbb9d559de79"
_B147_ENVELOPE: dict[str, object] = {
    "type": "platform.internal_error",
    "message": "Internal platform error: openai.BadRequestError",
    "where": "engine.execute.boundary",
    "detail": {
        "original_type": "openai.BadRequestError",
        "args": [
            "Error code: 400 - {'type': 'error', 'error': {'type': "
            "'MissingSessionID', 'message': 'Request is missing "
            "x-opencode-session and cannot be routed efficiently.'}}"
        ],
    },
    "cause": None,
}


def _failing_handler(
    result: _TestRunResult,
) -> Callable[[argparse.Namespace], Awaitable[_TestRunResult]]:
    async def _handler(args: argparse.Namespace) -> _TestRunResult:
        _ = args
        return result

    return _handler


async def _run_failing_setup(tmp_path: Path, result: _TestRunResult) -> tuple[int, str]:
    captured = io.StringIO()
    io_ = _make_test_io(
        root=tmp_path, out=captured, run_handler=_failing_handler(result)
    )
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    return rc, captured.getvalue()


def _banner(out: str) -> str:
    marker = "Kolsch setup completed partially"
    assert marker in out, out
    return out[out.index(marker) :]


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)


async def test_b147_rerun_banner_reports_existing_state_and_durable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 3: a re-run against an existing workspace + helper whose test
    run fails says "already initialized" / "already present", names the
    session, and renders the durable envelope's code, message and
    original text."""
    _clear_provider_env(monkeypatch)
    first = await run_setup(
        argparse.Namespace(non_interactive=True, root=tmp_path),
        _make_test_io(root=tmp_path),
    )
    assert first == 0

    rc, out = await _run_failing_setup(
        tmp_path,
        _TestRunResult(rc=2, session_id=_B147_SESSION, error=dict(_B147_ENVELOPE)),
    )
    banner = _banner(out)

    assert rc == 4
    assert "Kolsch is ready" not in out
    assert f"Workspace: {tmp_path}  (already initialized)" in banner
    assert "Agent:     kolsch/agents/helper  (already present)" in banner
    assert "initialized successfully" not in banner
    assert "(created)" not in banner
    assert f"Session:   {_B147_SESSION}" in banner
    assert "Cause:     platform.internal_error — " in banner
    assert "Error:     Internal platform error: openai.BadRequestError" in banner
    detail_lines = [ln for ln in banner.splitlines() if ln.startswith("Detail:")]
    assert len(detail_lines) == 1
    assert "MissingSessionID" in detail_lines[0]
    # The first next step is the exact events command for that session.
    next_steps = banner.split("Next steps:\n", 1)[1].splitlines()
    assert next_steps[0].startswith(f"  kolsch sessions events {_B147_SESSION}")
    assert "kolsch sessions list" not in banner
    assert "<id>" not in banner
    assert "rc=2" not in banner


async def test_b147_fresh_workspace_without_code_says_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4a: a fresh workspace reports both as created; the no-code
    Cause line says "test exchange failed" once and no `<id>`."""
    _clear_provider_env(monkeypatch)
    rc, out = await _run_failing_setup(
        tmp_path, _TestRunResult(rc=2, session_id=_B147_SESSION, error=None)
    )
    banner = _banner(out)

    assert rc == 4
    assert f"Workspace: {tmp_path}  (created)" in banner
    assert "Agent:     kolsch/agents/helper  (created)" in banner
    assert out.count("test exchange failed") <= 1
    assert "<id>" not in out
    assert "Error:" not in banner
    assert "Detail:" not in banner
    assert f"kolsch sessions events {_B147_SESSION}" in banner


async def test_b147_no_session_falls_back_to_sessions_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4b: without a session id there is no Session line and the
    next step falls back to `kolsch sessions list`."""
    _clear_provider_env(monkeypatch)
    rc, out = await _run_failing_setup(tmp_path, _TestRunResult(rc=2))
    banner = _banner(out)

    assert rc == 4
    assert "Session:" not in banner
    assert "kolsch sessions events" not in banner
    next_steps = banner.split("Next steps:\n", 1)[1].splitlines()
    assert next_steps[0].startswith("  kolsch sessions list")
    assert "<id>" not in out


async def test_b147_detail_is_one_truncated_line_and_never_the_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4c + 4d: a long multi-line `detail.args[0]` renders as one
    `Detail:` line of at most 300 chars ending in `…`; `detail.traceback`
    never appears."""
    _clear_provider_env(monkeypatch)
    long_text = "\n".join(f"line {i} " + "x" * 40 for i in range(25))
    assert len(long_text) > 1000
    envelope = {
        "type": "platform.internal_error",
        "message": "Internal platform error: ValueError",
        "where": "engine.execute.boundary",
        "detail": {
            "original_type": "ValueError",
            "args": [long_text],
            "traceback": "Traceback (most recent call last):\n  SECRET_FRAME_MARKER",
        },
        "cause": None,
    }
    rc, out = await _run_failing_setup(
        tmp_path, _TestRunResult(rc=2, session_id=_B147_SESSION, error=envelope)
    )
    banner = _banner(out)

    assert rc == 4
    detail_lines = [ln for ln in banner.splitlines() if ln.startswith("Detail:")]
    assert len(detail_lines) == 1
    value = detail_lines[0].removeprefix("Detail:").strip()
    assert len(value) <= 300
    assert value.endswith("…")
    assert value.startswith("line 0 ")
    assert "line 1 " in value  # newline collapsed to a space, not cut
    assert "Traceback" not in out
    assert "SECRET_FRAME_MARKER" not in out


def test_b147_one_line_helper() -> None:
    from cli._setup import _one_line

    assert _one_line("a\n  b\tc") == "a b c"
    assert _one_line("x" * 300) == "x" * 300
    cut = _one_line("x" * 301)
    assert len(cut) == 300
    assert cut.endswith("…")


# ===========================================================================
# _classify_failure unit table (acceptance criterion 4 — five codes)
# ===========================================================================


def test_classify_failure_table_covers_documented_codes() -> None:
    """Each documented error code maps to a specific hint substring.

    Maintained as a single-source-of-truth table in ``_classify_failure``
    (bug-setup-test-run-failure-handling.md edit (c)). New
    ``model.*`` codes Phase 1.3 lands later extend this table; the
    default branch keeps unknown codes safe.
    """
    from cli._setup import _classify_failure

    cases = [
        ("model.server_error", "5xx"),
        ("model.authentication_failed", "credentials failed"),
        ("llm.provider_not_configured", "isn't connected"),
        ("model.timeout", "took too long"),
        ("model.rate_limited", "rate-limited"),
    ]
    for code, expected_substring in cases:
        hint = _classify_failure(code)
        assert expected_substring.lower() in hint.lower(), (
            f"_classify_failure({code!r}) missing expected substring "
            f"{expected_substring!r}; got: {hint!r}"
        )

    # Unknown code → default hint.
    default = _classify_failure("model.exotic_future_code")
    assert "test exchange failed" in default.lower()

    # None → default hint.
    none_hint = _classify_failure(None)
    assert "test exchange failed" in none_hint.lower()

    # bug-147: no hint carries a placeholder session id — the banner
    # prints the real `kolsch sessions events <session id>` command.
    all_codes: list[str | None] = [c for c, _ in cases]
    all_codes += ["model.context_overflow", "model.bad_request", None, "x.unknown"]
    for hint_code in all_codes:
        assert "<id>" not in _classify_failure(hint_code), hint_code


# ===========================================================================
# bug-chat-permissions-and-console-leak Step C — authz-rule migration
# ===========================================================================
#
# Workspaces created before commit deaf95f carry the Phase 1 allow-all
# sentinel. `kolsch setup` re-run against such a workspace must offer
# to migrate the rules to Phase 2.0 §3.1 defaults, preserving any
# user-added custom rules in their relative position. E-BUG-CHAT-001
# option (a) is the implementation; these tests pin the four
# user-facing flows ([m]/[k]/[d]/[s]) plus idempotency, custom-rule
# preservation, the timestamped backup, and the non-interactive
# "skip silently" safety default.


class _MigrationPrompter:
    """Test prompter for the migration flow that returns a fixed
    sequence of ``choose`` answers.

    Distinct from the file's existing ``_ScriptedPrompter`` (which
    uses separate keyword-script lists per method); the migration
    flow only invokes ``choose``, so the simpler queue model keeps
    the per-test setup minimal. Once the script is exhausted,
    ``choose`` returns the call's ``default``."""

    def __init__(self, choose_answers: list[str]) -> None:
        self._answers = list(choose_answers)
        self.choose_calls: list[tuple[str, tuple[str, ...]]] = []

    def choose(self, question: str, options: list[str], default: str) -> str:
        self.choose_calls.append((question, tuple(options)))
        if not self._answers:
            return default.lower()
        return self._answers.pop(0).lower()

    def text(self, question: str, default: str = "") -> str:
        _ = question
        return default

    def confirm(self, question: str, *, default: bool = True) -> bool:
        _ = question
        return default


def _seed_pre_phase_2_workspace(
    root: Path,
    *,
    extra_custom_rules: list[dict[str, str]] | None = None,
) -> Path:
    """Seed a workspace whose ``platform.yaml`` carries the Phase 1
    allow-all sentinel (optionally with user-added custom rules
    appended). Returns the path to ``platform.yaml``.

    This mirrors what `kolsch setup` produced before commit deaf95f
    landed the Phase 2.0 §3.1 defaults — the exact starting state
    the migration is designed to recover from.

    Also lays down a stub ``bundles/kolsch/agents/helper.yaml`` so
    Step D's "agent already at <path>" short-circuit fires on re-run.
    Without it, Step D would materialize the bundle and write to
    ``platform.yaml``'s ``defaults.model`` (E-BUG-MODELLINE-001
    option a-prime), which would defeat the byte-equality assertions
    these migration tests rely on.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / ".platform").mkdir(parents=True, exist_ok=True)
    (root / "bundles" / "kolsch" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "bundles" / "kolsch" / "agents" / "helper.yaml").write_text(
        "# stub — Step D short-circuits when this file exists\n",
        encoding="utf-8",
    )
    rules: list[dict[str, str]] = [
        {"who": ".*", "what": ".*", "effect": "allow"},
    ]
    if extra_custom_rules:
        rules.extend(extra_custom_rules)
    payload = {
        "schema_version": "0.1",
        "authorization": {
            "variant": "rule_based",
            "config": {"rules": rules},
        },
    }
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return yaml_path


def _rules_in(yaml_path: Path) -> list[dict[str, str]]:
    payload = yaml.safe_load(yaml_path.read_text())
    rules: list[dict[str, str]] = payload["authorization"]["config"]["rules"]
    return rules


_PHASE_2_0_DEFAULT_WHATS: tuple[str, ...] = (
    # qol-action-namespace-bundle-scoped.md Step 2 — defaults use
    # the canonical 3-seg catalog ref (`<bundle>/tools/<name>`).
    "agent.execute.*",
    "kolsch/tools/read_file",
    "kolsch/tools/list_files",
    "kolsch/tools/write_file",
    "kolsch/tools/bash",
    "kolsch/tools/fetch_url",
    "kolsch/tools/browse_url",
)


async def test_migration_replaces_allow_all_with_3_1_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[m]igrate against an allow-all-only workspace → rules become
    the §3.1 default set; backup file lands under ``.platform/``."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    yaml_path = _seed_pre_phase_2_workspace(tmp_path)
    prompter = _MigrationPrompter(choose_answers=["m"])
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    args = argparse.Namespace(non_interactive=False, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0

    rules = _rules_in(yaml_path)
    whats = [r["what"] for r in rules]
    # §3.1 defaults present in order…
    for default_what in _PHASE_2_0_DEFAULT_WHATS:
        assert default_what in whats, f"missing default rule: {default_what}"
    # …and the deny tail is last (first-match-wins floor).
    assert rules[-1] == {"what": "*", "pattern": "*", "effect": "deny"}

    # Backup file written into .platform/ with the timestamped name.
    backups = sorted((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    assert len(backups) == 1, f"expected one backup, found {backups}"
    backup_payload = yaml.safe_load(backups[0].read_text())
    backup_rules = backup_payload["authorization"]["config"]["rules"]
    assert backup_rules == [{"who": ".*", "what": ".*", "effect": "allow"}]


async def test_migration_preserves_user_custom_rules_in_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-added custom rules survive the migration in their relative
    position (LANDING before the §3.1 default-deny tail). Per
    E-BUG-CHAT-001 resolution: any rule whose ``(who, what, effect)``
    triple does NOT match the allow-all sentinel is preserved."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    customs = [
        {"who": "ag_helper", "what": r"tool\.bash", "effect": "allow"},
        {"who": ".*", "what": r"agent\.execute\.ag_admin", "effect": "deny"},
    ]
    yaml_path = _seed_pre_phase_2_workspace(tmp_path, extra_custom_rules=customs)
    prompter = _MigrationPrompter(choose_answers=["m"])
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    args = argparse.Namespace(non_interactive=False, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0

    migrated = _rules_in(yaml_path)
    # Both customs present, in original relative order, and BEFORE
    # the default-deny tail.
    tail = migrated[-1]
    assert tail == {"what": "*", "pattern": "*", "effect": "deny"}
    custom_positions = [migrated.index(c) for c in customs]
    assert custom_positions == sorted(custom_positions), (
        f"custom rules out of order: positions={custom_positions}"
    )
    assert all(pos < len(migrated) - 1 for pos in custom_positions), (
        "custom rule landed at or after the default-deny tail"
    )


async def test_migration_keep_mine_does_not_modify_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[k]eep mine → file content is byte-identical; no backup is
    written."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    yaml_path = _seed_pre_phase_2_workspace(tmp_path)
    before = yaml_path.read_bytes()
    prompter = _MigrationPrompter(choose_answers=["k"])
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    args = argparse.Namespace(non_interactive=False, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0

    assert yaml_path.read_bytes() == before
    assert not list((tmp_path / ".platform").glob("platform.yaml.bak.*"))


async def test_migration_skip_defers_decision_no_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[s]kip → file untouched, no backup; surface a "deferred"
    message so the user knows the prompt will return on next run."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    yaml_path = _seed_pre_phase_2_workspace(tmp_path)
    before = yaml_path.read_bytes()
    captured = io.StringIO()
    prompter = _MigrationPrompter(choose_answers=["s"])
    io_ = _make_test_io(root=tmp_path, out=captured, prompter=prompter)
    args = argparse.Namespace(non_interactive=False, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0
    assert yaml_path.read_bytes() == before
    assert not list((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    assert "deferred" in captured.getvalue()


async def test_migration_diff_then_migrate_shows_diff_and_migrates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[d]iff first → user sees a unified diff of current → migrated
    rules; the prompt re-fires; following with [m] performs the
    migration. Pins both the diff-display flow and the re-prompt
    loop's exit condition."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    yaml_path = _seed_pre_phase_2_workspace(tmp_path)
    captured = io.StringIO()
    prompter = _MigrationPrompter(choose_answers=["d", "m"])
    io_ = _make_test_io(root=tmp_path, out=captured, prompter=prompter)
    args = argparse.Namespace(non_interactive=False, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0

    transcript = captured.getvalue()
    # Diff markers — unified-diff format puts `---`/`+++` headers
    # then `-`/`+` lines for changes.
    assert "--- current" in transcript
    assert "+++ after migration" in transcript
    # Migration ran after diff.
    rules = _rules_in(yaml_path)
    assert rules[-1] == {"what": "*", "pattern": "*", "effect": "deny"}
    # Migration prompt fired twice (initial + after diff). The full
    # setup flow also fires Step C's provider prompt; filter to the
    # migration-specific calls by question text so the count is not
    # coupled to unrelated steps.
    migration_calls = [
        c for c in prompter.choose_calls if "allow-all" in c[0] or "Choose:" in c[0]
    ]
    assert len(migration_calls) == 2


async def test_migration_non_interactive_skips_silently_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactive re-runs default to skip (no silent mutation
    of the user's config in CI). A warning surfaces so the user
    knows the workspace is on the old defaults."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    yaml_path = _seed_pre_phase_2_workspace(tmp_path)
    before = yaml_path.read_bytes()
    captured = io.StringIO()
    io_ = _make_test_io(root=tmp_path, out=captured)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)

    rc = await run_setup(args, io_)
    assert rc == 0
    assert yaml_path.read_bytes() == before
    assert not list((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    transcript = captured.getvalue()
    assert "allow-all" in transcript
    assert "interactively" in transcript


async def test_migration_idempotent_on_already_migrated_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running setup against a workspace that ALREADY has the §3.1
    defaults must not prompt, not write a backup, not modify the
    file. Pins the sentinel-detection guard: anything other than
    the literal allow-all sentinel skips the migration path."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    # Fresh setup → §3.1 defaults.
    first_io = _make_test_io(root=tmp_path)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=True, root=tmp_path), first_io
        )
        == 0
    )
    yaml_path = tmp_path / "platform.yaml"
    before = yaml_path.read_bytes()
    # Re-run with a prompter that would fail loudly if invoked.
    failing_prompter = _MigrationPrompter(choose_answers=[])
    second_io = _make_test_io(root=tmp_path, prompter=failing_prompter)
    assert (
        await run_setup(
            argparse.Namespace(non_interactive=False, root=tmp_path), second_io
        )
        == 0
    )
    assert yaml_path.read_bytes() == before
    assert not list((tmp_path / ".platform").glob("platform.yaml.bak.*"))
    # No MIGRATION prompts fired; Step C's unrelated provider prompt
    # is acceptable (and orthogonal to the migration path).
    migration_calls = [
        c
        for c in failing_prompter.choose_calls
        if "allow-all" in c[0] or "Choose:" in c[0]
    ]
    assert migration_calls == []


# ===========================================================================
# qol-133 step 1 — fresh-workspace hooks example scaffold
# ===========================================================================


async def test_qol133_fresh_setup_scaffolds_commented_hooks_example(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 1: a fresh `kolsch setup` run writes a `platform.yaml`
    whose `load()`-parsed content has no `hooks` key, but whose raw
    text contains a fully-commented worked example (every line
    prefixed `#`) naming `event`, `matcher`, `command`, and pointing
    at docs/how-to/event-hooks.md."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
        == 0
    )

    yaml_path = tmp_path / "platform.yaml"
    raw_text = yaml_path.read_text(encoding="utf-8")

    # The parsed config has no live `hooks` key.
    parsed = load(raw_text)
    assert isinstance(parsed, dict)
    assert "hooks" not in parsed

    # The raw text contains a commented block referencing the
    # required fields + the docs pointer.
    assert "event" in raw_text
    assert "matcher" in raw_text
    assert "command" in raw_text
    assert "docs/how-to/event-hooks.md" in raw_text

    # Every line of the appended example block is commented — locate
    # the block via its known first line and verify each subsequent
    # non-blank line up to EOF starts with `#`.
    marker = "# Example: notify yourself when kolsch is about to ask for approval."
    assert marker in raw_text
    block_start = raw_text.index(marker)
    block_text = raw_text[block_start:]
    for line in block_text.splitlines():
        if not line.strip():
            continue
        assert line.startswith("#"), f"non-commented line in hooks example: {line!r}"


_HOOKS_SCAFFOLD_MARKER = (
    "# Example: notify yourself when kolsch is about to ask for approval."
)


def _assert_hooks_scaffold_intact(raw_text: str) -> None:
    """Shared assertion: the qol-133 scaffold is present, byte-for-byte,
    and every line of it is commented (no live `hooks:` key)."""
    assert _HOOKS_SCAFFOLD_MARKER in raw_text
    block_text = raw_text[raw_text.index(_HOOKS_SCAFFOLD_MARKER) :]
    for line in block_text.splitlines():
        if not line.strip():
            continue
        assert line.startswith("#"), f"non-commented line in hooks example: {line!r}"
    parsed = load(raw_text)
    assert isinstance(parsed, dict)
    assert "hooks" not in parsed


async def test_qol133_hooks_scaffold_survives_provider_connect_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer-found gap: Step C's `cmd_connect` write path
    (`llm_commands._write_platform_yaml_atomic`) does its own
    load-dict-dump round-trip on `platform.yaml`, independently of
    Step D's `_write_defaults_model`. A workspace where a provider
    actually gets connected (the `KOLSCH_PROVIDER`/`KOLSCH_PROVIDER_
    KEY` env-driven non-interactive path — mirrors
    `test_tc_ux_07_env_driven_auto_connect`) exercises Step B → Step C
    → Step D in sequence; the scaffold must survive ALL of them, not
    just B → D with C skipped (the gap the original Test 1 missed,
    since it explicitly deletes both env vars)."""
    monkeypatch.setenv("KOLSCH_PROVIDER", "openrouter")
    monkeypatch.setenv("KOLSCH_PROVIDER_KEY", "sk-test-or")
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    assert rc == 0

    # Sanity: Step C really did run and write a connected provider —
    # otherwise this test would silently degrade into a copy of Test 1.
    raw_text = (tmp_path / "platform.yaml").read_text(encoding="utf-8")
    parsed = load(raw_text)
    assert "openrouter" in (parsed.get("llm_providers") or {})

    _assert_hooks_scaffold_intact(raw_text)


async def test_qol133_hooks_scaffold_survives_standalone_cmd_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scaffold must also survive a LATER, standalone `kolsch
    connect` run against an already-set-up workspace that still has
    the untouched scaffold — not just writes that happen to fire
    inside the same `kolsch setup` invocation that created it."""
    from cli.llm_commands import _CommandIO, cmd_connect

    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=tmp_path)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
        == 0
    )
    before_connect = (tmp_path / "platform.yaml").read_text(encoding="utf-8")
    _assert_hooks_scaffold_intact(before_connect)

    captured = io.StringIO()

    class _EnvKeyPrompter:
        def ask(self, field: PromptField) -> str:
            if field.name == "api_key":
                return "sk-test-or-2"
            return field.default or ""

    connect_io = _CommandIO(
        root=tmp_path,
        out=captured,
        prompter=_EnvKeyPrompter(),
        connector_factory=_mock_connector_factory,
    )
    rc = await cmd_connect(
        argparse.Namespace(provider="openrouter"),
        connect_io,
    )
    assert rc == 0

    raw_text = (tmp_path / "platform.yaml").read_text(encoding="utf-8")
    parsed = load(raw_text)
    assert "openrouter" in (parsed.get("llm_providers") or {})
    _assert_hooks_scaffold_intact(raw_text)


# ===========================================================================
# qol-133 step 2 — one-time hooks-availability notice for existing workspaces
# ===========================================================================

_HOOKS_NOTICE_POINTER = "docs/how-to/event-hooks.md"


async def test_qol133_existing_workspace_without_hooks_gets_notice_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 2: a `kolsch setup` re-run against a workspace whose
    `platform.yaml` has no `hooks` key prints a notice pointing at
    docs/how-to/event-hooks.md, and does not modify `platform.yaml`.

    Uses the fresh-setup path (Test 1's fixture) as the base
    workspace: `test_tc_ux_06_setup_is_idempotent` already establishes
    that a second non-interactive re-run against a freshly-set-up
    workspace is a byte-for-byte no-op across every other `_maybe_*`
    helper, so any `platform.yaml` diff observed here is unambiguously
    attributable to this step.
    """
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    assert await run_setup(args, _make_test_io(root=tmp_path)) == 0

    yaml_path = tmp_path / "platform.yaml"
    before = yaml_path.read_bytes()
    parsed_before = load(before.decode("utf-8"))
    assert isinstance(parsed_before, dict)
    assert "hooks" not in parsed_before

    rerun_out = io.StringIO()
    rerun_io = _make_test_io(root=tmp_path, out=rerun_out)
    assert await run_setup(args, rerun_io) == 0

    captured = rerun_out.getvalue()
    assert _HOOKS_NOTICE_POINTER in captured

    after = yaml_path.read_bytes()
    assert after == before, "the hooks notice must never mutate platform.yaml"


async def test_qol133_hooks_notice_does_not_repeat_on_second_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 3: immediately after a re-run that showed the notice, a
    second re-run against the same still-hooks-less workspace does NOT
    show it again."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    assert await run_setup(args, _make_test_io(root=tmp_path)) == 0

    first_rerun_out = io.StringIO()
    assert await run_setup(args, _make_test_io(root=tmp_path, out=first_rerun_out)) == 0
    assert _HOOKS_NOTICE_POINTER in first_rerun_out.getvalue()

    second_rerun_out = io.StringIO()
    assert (
        await run_setup(args, _make_test_io(root=tmp_path, out=second_rerun_out)) == 0
    )
    assert _HOOKS_NOTICE_POINTER not in second_rerun_out.getvalue()


async def test_qol133_workspace_with_hooks_key_never_sees_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4: a workspace whose `platform.yaml` already has a `hooks`
    key (here, an empty list) never sees the notice, even on the very
    first re-run with no pre-existing marker file. No marker file
    should be written either, since the presence check alone is
    sufficient (Acceptance Criterion 4) — verified as a nice-to-have,
    not load-bearing."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    assert await run_setup(args, _make_test_io(root=tmp_path)) == 0

    yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(yaml_path.read_text())
    payload["hooks"] = []
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))

    marker_path = tmp_path / ".kolsch" / "hooks-notice-shown"
    assert not marker_path.exists()

    rerun_out = io.StringIO()
    rerun_io = _make_test_io(root=tmp_path, out=rerun_out)
    assert await run_setup(args, rerun_io) == 0

    assert _HOOKS_NOTICE_POINTER not in rerun_out.getvalue()
    assert not marker_path.exists(), (
        "a workspace with a `hooks` key already present should never "
        "need the one-time marker written"
    )


async def test_qol133_corrupt_marker_degrades_to_showing_notice_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted/unparseable marker file must be treated as "not yet
    shown" — fail toward showing the notice once more, never toward a
    crash or permanent silence (Acceptance Criterion 5)."""
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    args = argparse.Namespace(non_interactive=True, root=tmp_path)
    assert await run_setup(args, _make_test_io(root=tmp_path)) == 0

    first_rerun_out = io.StringIO()
    assert await run_setup(args, _make_test_io(root=tmp_path, out=first_rerun_out)) == 0
    assert _HOOKS_NOTICE_POINTER in first_rerun_out.getvalue()

    marker_path = tmp_path / ".kolsch" / "hooks-notice-shown"
    assert marker_path.is_file()
    marker_path.write_text("{not valid json", encoding="utf-8")

    second_rerun_out = io.StringIO()
    rc = await run_setup(args, _make_test_io(root=tmp_path, out=second_rerun_out))
    assert rc == 0, "a corrupt marker must never crash setup"
    assert _HOOKS_NOTICE_POINTER in second_rerun_out.getvalue(), (
        "a corrupt marker must degrade toward showing the notice again"
    )


# ===========================================================================
# bug-146 Test 6 — a setup re-run reports (and offers) the workspace upgrade
# ===========================================================================


async def _install_then_age(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh install, then the bug-146 Reproduce shape: an older workspace."""
    import shutil

    from cli.permissions import RemoveRule, mutate_platform_rules
    from providers.authz import Rule

    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _make_test_io(root=root)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=root), io_) == 0
    )
    bundle = root / "bundles" / "kolsch"
    (root / ".kolsch" / "bundle-baseline.json").unlink()
    (root / ".kolsch" / "default-rules.json").unlink()
    (bundle / "tools" / "jev_decide.py").unlink()
    shutil.rmtree(bundle / "workflows")
    helper = bundle / "agents" / "helper.yaml"
    helper.write_text("# my note\n" + helper.read_text(encoding="utf-8"), "utf-8")
    mutate_platform_rules(
        root / "platform.yaml",
        RemoveRule(
            match=Rule(what="kolsch/tools/jev_decide", pattern="*", effect="prompt")
        ),
        catalog=None,
    )


def _rerun_hash(root: Path) -> dict[str, str]:
    # qol-133's one-time `.kolsch/hooks-notice-shown` marker is written
    # by every first re-run (pre-existing); everything else is watched.
    return {
        k: v
        for k, v in _hash_workspace(root).items()
        if k.startswith(("bundles/kolsch/", "platform.yaml", ".kolsch/"))
        and k != ".kolsch/hooks-notice-shown"
    }


async def test_setup_rerun_reports_upgrade_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _install_then_age(tmp_path, monkeypatch)
    before = _rerun_hash(tmp_path)

    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    out = io_.out.getvalue()
    assert rc == 0, out
    workflows = len(list((_STARTER_BUNDLE_TEMPLATE_DIR / "workflows").iterdir()))
    assert (
        f"`--sync-bundle` would copy {workflows + 1} new file(s), ask about 1 "
        "file(s) that differ from this version" in out
    )
    assert "1 missing default permission rule: kolsch/tools/jev_decide → prompt" in out
    assert "Run `kolsch setup --sync-bundle` to review and apply" in out
    notice = out.split("✓ starter agent already at", 1)[1]
    assert notice.index("workspace bundle is behind") < notice.index("Running")
    assert _rerun_hash(tmp_path) == before


async def test_setup_rerun_on_synced_workspace_prints_no_upgrade_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    first = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), first)
    io_ = _make_test_io(root=tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    out = io_.out.getvalue()
    assert "behind kolsch" not in out
    assert "--sync-bundle" not in out
    assert "upgrade check skipped" not in out


class _UpgradeAnswers(NonInteractivePrompter):
    """Answer the D1 offer; keep everything else at its default."""

    def __init__(self, *, apply: bool) -> None:
        self.apply = apply
        self.asked: list[str] = []

    def confirm(self, question: str, *, default: bool = True) -> bool:
        self.asked.append(question)
        if "Review and apply now?" in question:
            return self.apply
        return default


async def test_setup_rerun_interactive_accept_runs_sync_before_step_e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 (b): interactive re-run offers the sync; yes runs it inline."""
    await _install_then_age(tmp_path, monkeypatch)
    prompter = _UpgradeAnswers(apply=True)
    io_ = _make_test_io(root=tmp_path, prompter=prompter)
    await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    out = io_.out.getvalue()
    assert any("Review and apply now?" in q for q in prompter.asked)
    assert out.index("Sync report") < out.index("Running")
    bundle = tmp_path / "bundles" / "kolsch"
    assert (bundle / "tools" / "jev_decide.py").is_file()
    assert (bundle / "workflows").is_dir()
    rules = yaml.safe_load((tmp_path / "platform.yaml").read_text())["authorization"][
        "config"
    ]["rules"]
    assert {
        "what": "kolsch/tools/jev_decide",
        "pattern": "*",
        "effect": "prompt",
    } in rules
    # The edited helper was kept (the scripted prompter keeps by default).
    assert (bundle / "agents" / "helper.yaml").read_text().startswith("# my note\n")


async def test_setup_rerun_interactive_decline_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _install_then_age(tmp_path, monkeypatch)
    before = _rerun_hash(tmp_path)
    io_ = _make_test_io(root=tmp_path, prompter=_UpgradeAnswers(apply=False))
    await run_setup(argparse.Namespace(non_interactive=False, root=tmp_path), io_)
    assert "Sync report" not in io_.out.getvalue()
    assert _rerun_hash(tmp_path) == before


async def test_setup_rerun_upgrade_check_failure_is_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli._bundle_sync as sync_mod

    await _install_then_age(tmp_path, monkeypatch)

    async def boom(_root: Path) -> object:
        msg = "planner exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(sync_mod, "plan_workspace_upgrade", boom)
    io_ = _make_test_io(root=tmp_path)
    rc = await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    out = io_.out.getvalue()
    assert rc == 0, out
    assert "  ⚠ upgrade check skipped: planner exploded\n" in out
    assert "Running" in out
