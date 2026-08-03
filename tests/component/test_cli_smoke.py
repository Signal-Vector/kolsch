"""TC-CLI-SMOKE-* — CLI surface smoke tests.

These are NOT happy-path feature tests (those live in
``tests/component/test_cli.py`` as TC-CLI-*). Smoke tests prove
"the parser-to-dispatcher wiring works for every reachable entry
point" — they catch the class of bug fixed in
``docs/process/bug-sessions-namespace-attr-error.md``
(``kolsch sessions`` crashing with ``AttributeError`` because the
dispatcher fell through to a handler reading attributes from a
sibling subparser).

Per the ticket's acceptance criterion 4: each smoke test invokes
the CLI in a fresh subprocess via
``subprocess.run([sys.executable, "-m", "cli", ...])``. In-process
invocation would miss argparse-to-dispatcher wiring bugs, sys.exit
handling, and stderr formatting — the exact class of regression
smoke tests are meant to catch.

Per acceptance criterion 5, the suite is fast (<10s) when run
with **pytest-xdist's** ``-n auto`` flag — each subprocess-spawning
test runs in parallel across workers, dropping the wall-clock
from ~36s (serial) to ~6s on a multi-core laptop:

    uv run pytest tests/component/test_cli_smoke.py -n auto

A session-scoped ``initialized_workspace`` fixture runs
``kolsch setup --non-interactive`` once per worker (xdist's
session scope is per-worker) and is re-used across every
workspace-needing smoke test. Each worker gets its own
``tmp_path_factory.mktemp(...)`` so workers don't share state;
every covered command is read-only or no-op-write against the
workspace, so parallel readers never contend on the SQLite
entity DB or the JSONL log.

**Policy (catalog Part 9.X + AGENTS.md hard rule):** every new CLI
command or subcommand added in any future phase ships with a
smoke test here. No exceptions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Constants + helpers
# ---------------------------------------------------------------------------


# Subprocess timeout for every smoke test — generous enough for setup
# but tight enough that a hung test surfaces quickly.
_TIMEOUT_SECONDS = 30


def _run(
    *cli_args: str,
    workspace: Path | None = None,
    timeout: int = _TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m cli <args>`` in a fresh subprocess.

    When ``workspace`` is supplied, ``KOLSCH_WORKSPACE`` is set to
    its path so the CLI resolves to that workspace instead of the
    user's ``~/Documents/kolsch``.
    """
    env = dict(os.environ)
    env.pop("KOLSCH_PROVIDER", None)
    env.pop("KOLSCH_PROVIDER_KEY", None)
    # spec-120: an ambient bridge env (a live `kolsch mcp bridge`
    # session in the invoking shell) must not leak into smoke runs —
    # TC-CLI-SMOKE-16 asserts the env-absent path.
    env.pop("KOLSCH_MCP_SOCKET", None)
    env.pop("KOLSCH_MCP_TOKEN", None)
    if workspace is not None:
        env["KOLSCH_WORKSPACE"] = str(workspace)
    else:
        env.pop("KOLSCH_WORKSPACE", None)
    return subprocess.run(
        [sys.executable, "-m", "cli", *cli_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    """A smoke test that finds a Python traceback in stderr always fails.

    Used by every smoke test — the smoke layer's whole point is "no
    crashes from the parser-to-dispatcher path." Tracebacks are
    crashes per acceptance criterion 6.
    """
    assert "Traceback" not in result.stderr, (
        f"unexpected Python traceback in stderr:\n{result.stderr}"
    )
    assert "AttributeError" not in result.stderr, (
        f"AttributeError leaked through to stderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Session-scoped fixture — an initialized workspace
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def initialized_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace pre-initialized via ``kolsch setup --non-interactive``.

    Reused across every workspace-needing smoke test — running setup
    once amortizes the cost of the fixture (it's the most expensive
    part of the suite per acceptance criterion 5).

    Setup runs with no ``KOLSCH_PROVIDER`` / ``KOLSCH_PROVIDER_KEY``
    env vars, so the starter agent ships with the built-in mock LLM
    and no network is touched anywhere in the smoke suite (criterion 4).
    """
    root = tmp_path_factory.mktemp("smoke_workspace")
    result = _run("setup", "--non-interactive", workspace=root, timeout=60)
    if result.returncode != 0:
        msg = (
            f"setup --non-interactive failed (rc={result.returncode}); "
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    return root


# ===========================================================================
# TC-CLI-SMOKE-01..14 — `--help` for every command (no workspace needed)
# ===========================================================================


# Every top-level subcommand exposes ``--help`` without any fixture
# setup. The list pins what's wired today; new commands must be added
# here per the AGENTS.md hard rule.
_TOP_LEVEL_SUBCOMMANDS: tuple[str, ...] = (
    "run",
    "sessions",
    "reconstruct",
    "server",
    "setup",
    "connect",
    "disconnect",
    "providers",
    "models",
    "refresh",
    "permissions",
    "skills",
    "chat",
    "workflows",
    "nodes",
    "doctor",
    "mcp",
)


def test_tc_cli_smoke_01_top_level_help_exits_zero() -> None:
    """TC-CLI-SMOKE-01: ``kolsch --help`` exits 0 with non-empty stdout."""
    result = _run("--help")
    assert result.returncode == 0
    assert "kolsch" in result.stdout.lower()
    _assert_no_traceback(result)


@pytest.mark.parametrize("subcommand", _TOP_LEVEL_SUBCOMMANDS)
def test_tc_cli_smoke_02_subcommand_help_exits_zero(subcommand: str) -> None:
    """TC-CLI-SMOKE-02 (parametrized over every top-level subcommand):
    ``kolsch <subcommand> --help`` exits 0 with non-empty stdout."""
    result = _run(subcommand, "--help")
    assert result.returncode == 0, (
        f"`kolsch {subcommand} --help` failed (rc={result.returncode}); "
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"`kolsch {subcommand} --help` stdout empty"
    _assert_no_traceback(result)


def test_run_help_documents_model_override() -> None:
    """qol-122: ``kolsch run --help`` advertises the ``--model`` override."""
    result = _run("run", "--help")
    assert result.returncode == 0
    assert "--model" in result.stdout
    assert "override" in result.stdout.lower()
    _assert_no_traceback(result)


# ===========================================================================
# TC-CLI-SMOKE-03 — top-level no-args (no fixture)
# ===========================================================================


def test_tc_cli_smoke_03_no_args_prints_help_and_exits_one() -> None:
    """TC-CLI-SMOKE-03: ``kolsch`` (no args) prints help and exits 1
    rather than crashing with a parser error or AttributeError."""
    result = _run()
    assert result.returncode == 1
    _assert_no_traceback(result)
    # Help text on stdout — main.py:55-57 prints help when command is None.
    assert "usage:" in result.stdout.lower() or "kolsch" in result.stdout.lower()


def test_tc_cli_smoke_04_unknown_command_exits_nonzero() -> None:
    """TC-CLI-SMOKE-04: ``kolsch <bogus-cmd>`` exits non-zero without
    a Python traceback (argparse handles this; smoke pins it)."""
    result = _run("not-a-real-command")
    assert result.returncode != 0
    _assert_no_traceback(result)


# ===========================================================================
# TC-CLI-SMOKE-05 — bug-sessions-namespace-attr-error.md regression
# ===========================================================================


def test_tc_cli_smoke_05_sessions_no_op_does_not_crash(
    initialized_workspace: Path,
) -> None:
    """TC-CLI-SMOKE-05: ``kolsch sessions`` (no operation) prints the
    sub-parser help and exits non-zero. The original bug
    (bug-sessions-namespace-attr-error.md) was a crash with
    ``AttributeError: 'Namespace' object has no attribute 'starred'``
    because the dispatcher fell through to ``_cmd_sessions_list``.
    """
    result = _run("sessions", workspace=initialized_workspace)
    assert result.returncode != 0, "expected exit non-zero; bug regression if exit 0"
    _assert_no_traceback(result)
    # The help should list the available operations.
    combined = (result.stdout + result.stderr).lower()
    for op in ("list", "events", "update", "delete"):
        assert op in combined, (
            f"`kolsch sessions` help missing operation {op!r}; got:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def test_tc_cli_smoke_06_permissions_no_op_does_not_crash(
    initialized_workspace: Path,
) -> None:
    """TC-CLI-SMOKE-06: ``kolsch permissions`` (no operation) — same
    pattern as TC-CLI-SMOKE-05. Pre-fix, the handler returned 1 with
    an awkward ``"unknown permissions op None"`` message; the
    uniform fix prints subparser help instead."""
    result = _run("permissions", workspace=initialized_workspace)
    assert result.returncode != 0
    _assert_no_traceback(result)
    combined = (result.stdout + result.stderr).lower()
    for op in ("list", "add", "remove", "show"):
        assert op in combined, (
            f"`kolsch permissions` help missing operation {op!r}; got:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def test_tc_cli_smoke_06_mcp_no_op_does_not_crash(
    initialized_workspace: Path,
) -> None:
    """TC-CLI-SMOKE-06b (spec-105): ``kolsch mcp`` (no operation)
    exits non-zero with the subparser help on stderr — same pattern
    as SMOKE-05/-06. Guards against a future refactor accidentally
    letting bare ``kolsch mcp`` boot the serve loop and hang the
    smoke suite."""
    result = _run("mcp", workspace=initialized_workspace)
    assert result.returncode != 0
    _assert_no_traceback(result)
    combined = (result.stdout + result.stderr).lower()
    assert "serve" in combined, (
        f"`kolsch mcp` help missing operation 'serve'; got:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# ===========================================================================
# TC-CLI-SMOKE-07..14 — workspace-needing commands run cleanly
# ===========================================================================


def test_tc_cli_smoke_07_sessions_list(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-07: ``kolsch sessions list`` against an empty
    workspace exits 0 — no sessions, empty output, no crash."""
    result = _run("sessions", "list", workspace=initialized_workspace)
    assert result.returncode == 0
    _assert_no_traceback(result)


def test_tc_cli_smoke_08_sessions_list_with_flags(
    initialized_workspace: Path,
) -> None:
    """TC-CLI-SMOKE-08: ``kolsch sessions list --starred --archived
    --limit 5`` — pins that the flag combinations parse correctly
    and the handler dispatches without attribute errors."""
    result = _run(
        "sessions",
        "list",
        "--starred",
        "--archived",
        "--limit",
        "5",
        workspace=initialized_workspace,
    )
    assert result.returncode == 0
    _assert_no_traceback(result)


def test_tc_cli_smoke_09_providers(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-09: ``kolsch providers`` lists the empty connected
    set on a fresh workspace."""
    result = _run("providers", workspace=initialized_workspace)
    assert result.returncode == 0
    _assert_no_traceback(result)


def test_tc_cli_smoke_10_reconstruct(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-10: ``kolsch reconstruct`` against a fresh workspace
    completes cleanly."""
    result = _run("reconstruct", workspace=initialized_workspace)
    assert result.returncode == 0
    _assert_no_traceback(result)


def test_tc_cli_smoke_11_models(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-11: ``kolsch models`` with no providers connected."""
    result = _run("models", workspace=initialized_workspace)
    assert result.returncode == 0
    _assert_no_traceback(result)


def test_tc_cli_smoke_12_refresh(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-12: ``kolsch refresh`` with no providers connected.

    The handler legitimately exits 1 (``"no connected providers to
    refresh"``) when nothing is wired; that's not a crash. Smoke
    pins the no-traceback property only — exit code may be 0 or 1
    depending on workspace state."""
    result = _run("refresh", workspace=initialized_workspace)
    # 0 (no providers, no-op) or 1 (refusal message) both acceptable;
    # what matters is no Python-level crash.
    assert result.returncode in (0, 1), (
        f"unexpected exit {result.returncode}; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    _assert_no_traceback(result)


def test_tc_cli_smoke_13_permissions_list(initialized_workspace: Path) -> None:
    """TC-CLI-SMOKE-13: ``kolsch permissions list`` against the default
    workspace rule set (Phase 2.0 produces 6 rules including the
    protected default-deny tail)."""
    result = _run("permissions", "list", workspace=initialized_workspace)
    assert result.returncode == 0
    _assert_no_traceback(result)
    assert "rl_default_deny" in result.stdout


def test_tc_cli_smoke_14_missing_workspace_friendly_message(
    tmp_path: Path,
) -> None:
    """TC-CLI-SMOKE-14: a non-setup command against an uninitialized
    path emits the friendly Phase 1.2 message + exit 3 (the
    workspace-not-initialized exit code), no traceback."""
    result = _run("sessions", "list", workspace=tmp_path)
    assert result.returncode == 3, (
        f"expected exit 3 (workspace-not-initialized); got {result.returncode}"
    )
    _assert_no_traceback(result)
    assert "No Kolsch workspace at" in result.stderr
    assert "kolsch setup" in result.stderr


# ===========================================================================
# TC-CLI-SMOKE-15 — nested-help (sessions list / permissions list etc.)
# ===========================================================================


_NESTED_HELP_INVOCATIONS: tuple[tuple[str, ...], ...] = (
    ("sessions", "list", "--help"),
    ("sessions", "events", "--help"),
    ("sessions", "update", "--help"),
    ("sessions", "delete", "--help"),
    ("permissions", "list", "--help"),
    ("permissions", "add", "--help"),
    ("permissions", "remove", "--help"),
    ("permissions", "show", "--help"),
    # spec-105 — `kolsch mcp serve --help` must exit 0 without booting
    # the runtime or the stdio serve loop (argparse handles --help
    # before dispatch, so this is a proof of that guarantee).
    ("mcp", "serve", "--help"),
    # spec-120 — the tool-bridge stdio proxy, same guarantee.
    ("mcp", "bridge", "--help"),
)


@pytest.mark.parametrize("invocation", _NESTED_HELP_INVOCATIONS)
def test_tc_cli_smoke_15_nested_subcommand_help(
    invocation: tuple[str, ...],
) -> None:
    """TC-CLI-SMOKE-15 (parametrized): every nested op exposes
    ``--help`` cleanly. Pins that no nested op's argparse wiring is
    so broken that even ``--help`` crashes."""
    result = _run(*invocation)
    assert result.returncode == 0, (
        f"`kolsch {' '.join(invocation)}` failed (rc={result.returncode}); "
        f"stderr:\n{result.stderr}"
    )
    _assert_no_traceback(result)


# ===========================================================================
# TC-CLI-SMOKE-16 — spec-120: `mcp bridge` env contract
# ===========================================================================


def test_tc_cli_smoke_16_mcp_bridge_without_env_exits_cleanly() -> None:
    """TC-CLI-SMOKE-16: bare ``kolsch mcp bridge`` with no
    ``KOLSCH_MCP_SOCKET``/``KOLSCH_MCP_TOKEN`` exits non-zero without
    booting anything (no runtime, no serve loop, no workspace
    requirement — the proxy is spawnable from any cwd/env), with a
    clean actionable message and no traceback."""
    result = _run("mcp", "bridge")
    assert result.returncode == 2, (
        f"expected exit 2 (missing bridge env); got {result.returncode}; "
        f"stderr:\n{result.stderr}"
    )
    _assert_no_traceback(result)
    assert "KOLSCH_MCP_SOCKET" in result.stderr
    assert result.stdout == ""


# ===========================================================================
# Coverage-gate test — TOP-LEVEL command set is enumerated
# ===========================================================================


def test_smoke_coverage_enumerates_every_top_level_subcommand() -> None:
    """Belt-and-braces: every command in the dispatcher's ``_DISPATCH``
    table is also in this test module's ``_TOP_LEVEL_SUBCOMMANDS``
    tuple. A future contributor adding a new subcommand will fail
    this test until they add it here AND write its smoke test (per
    the AGENTS.md hard rule)."""
    from cli.main import _DISPATCH

    missing = set(_DISPATCH) - set(_TOP_LEVEL_SUBCOMMANDS)
    assert not missing, (
        f"these CLI subcommands are dispatched but missing from the "
        f"smoke-test coverage list: {sorted(missing)}. Per AGENTS.md "
        f"hard rule, every CLI subcommand requires a smoke test — "
        f"add an entry to _TOP_LEVEL_SUBCOMMANDS and a parametrized "
        f"`--help` test will cover it; add deeper tests below."
    )
