"""bug-146 — every whole-document `platform.yaml` writer runs under the rules lock.

Each test races one writer against a simulated chat "always": a thread
takes `platform_yaml_lock`, reads the file, waits, then writes the file
back with one extra rule — as bug-162's writer does. A writer that reads
and writes without the lock lands its change during the wait and the
chat write then clobbers it. A writer that takes the lock and reads
inside it waits for the chat write and keeps both.
"""

from __future__ import annotations

import argparse
import io
import threading
import time
from typing import TYPE_CHECKING, Any

import yaml

from assets.platform_config import platform_yaml_lock
from cli._setup import (
    NonInteractivePrompter,
    _maybe_add_workspace_root_to_file_access,
    _SetupIO,
    _TestRunResult,
    _write_defaults_model,
    run_setup,
)
from cli.llm_commands import _write_platform_yaml_atomic

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

_CHAT_RULE = {"what": "kolsch/tools/bash", "pattern": "git status", "effect": "allow"}


async def _noop_run_handler(args: argparse.Namespace) -> _TestRunResult:
    _ = args
    return _TestRunResult(rc=0)


def _connect_io_factory(
    _root: object, _provider: str, _api_key: str | None = None
) -> object:
    raise NotImplementedError


def _io(root: Path) -> _SetupIO:
    return _SetupIO(
        root=root,
        out=io.StringIO(),
        err=io.StringIO(),
        prompter=NonInteractivePrompter(),
        connect_io_factory=_connect_io_factory,
        run_handler=_noop_run_handler,
    )


async def _install(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    io_ = _io(root)
    assert (
        await run_setup(argparse.Namespace(non_interactive=True, root=root), io_) == 0
    )
    return root / "platform.yaml"


def _load(yaml_path: Path) -> dict[str, Any]:
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _race_with_chat_always(root: Path, write: Callable[[], object]) -> None:
    """Run `write` while a chat "always" holds the lock mid read-modify-write."""
    holding = threading.Event()

    def chat() -> None:
        with platform_yaml_lock(root):
            data = _load(root / "platform.yaml")
            holding.set()
            time.sleep(0.4)
            rules = data["authorization"]["config"]["rules"]
            rules.insert(len(rules) - 1, dict(_CHAT_RULE))
            (root / "platform.yaml").write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
            )

    thread = threading.Thread(target=chat)
    thread.start()
    assert holding.wait(5)
    try:
        write()
    finally:
        thread.join(10)


def _has_chat_rule(yaml_path: Path) -> bool:
    return _CHAT_RULE in _load(yaml_path)["authorization"]["config"]["rules"]


async def test_defaults_model_write_keeps_concurrent_chat_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _install(tmp_path, monkeypatch)
    _race_with_chat_always(tmp_path, lambda: _write_defaults_model(yaml_path, "x/y"))
    assert _load(yaml_path)["defaults"]["model"] == "x/y"
    assert _has_chat_rule(yaml_path)


async def test_file_access_write_keeps_concurrent_chat_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _install(tmp_path, monkeypatch)
    data = _load(yaml_path)
    data["file_access"]["allowed_paths"] = [
        p for p in data["file_access"]["allowed_paths"] if p != "."
    ]
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    io_ = _io(tmp_path)

    _race_with_chat_always(
        tmp_path, lambda: _maybe_add_workspace_root_to_file_access(io_)
    )
    assert _load(yaml_path)["file_access"]["allowed_paths"][0] == "."
    assert _has_chat_rule(yaml_path)
    assert "added '.' to file_access.allowed_paths" in io_.out.getvalue()


async def test_llm_provider_write_keeps_concurrent_chat_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kolsch connect` / `disconnect` / `refresh` share this writer."""
    yaml_path = await _install(tmp_path, monkeypatch)

    def add_provider(payload: dict[str, Any]) -> bool:
        payload["llm_providers"] = {"mine": {"type": "openai_compat", "models": []}}
        return True

    _race_with_chat_always(
        tmp_path, lambda: _write_platform_yaml_atomic(tmp_path, add_provider)
    )
    assert "mine" in _load(yaml_path)["llm_providers"]
    assert _has_chat_rule(yaml_path)


async def test_llm_writer_no_change_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = await _install(tmp_path, monkeypatch)
    before = yaml_path.read_bytes()
    assert _write_platform_yaml_atomic(tmp_path, lambda _payload: False) is False
    assert yaml_path.read_bytes() == before


# ---------------------------------------------------------------------------
# N8 / N10 — vanished provider; lock timeout is a clean error line
# ---------------------------------------------------------------------------


class _StubPrompter:
    def ask(self, field: object) -> str:
        _ = field
        return ""


def _llm_io(root: Path) -> tuple[Any, io.StringIO]:
    from cli.llm_commands import _CommandIO

    buf = io.StringIO()

    def no_connector(name: str) -> Any:
        raise AssertionError(name)

    return _CommandIO(
        root=root,
        out=buf,
        prompter=_StubPrompter(),
        connector_factory=no_connector,
        err=io.StringIO(),
    ), buf


def _write_provider_workspace(root: Path) -> Path:
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {"llm_providers": {"aaa": {"type": "openai_compat", "models": []}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return yaml_path


async def test_disconnect_of_a_concurrently_removed_provider_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.llm_commands as llm_mod
    from cli.llm_commands import cmd_disconnect

    yaml_path = _write_provider_workspace(tmp_path)
    stale = _load(yaml_path)
    yaml_path.write_text(yaml.safe_dump({"llm_providers": {}}), encoding="utf-8")
    monkeypatch.setattr(llm_mod, "_load_platform_yaml", lambda _root: stale)

    io_, buf = _llm_io(tmp_path)
    assert await cmd_disconnect(argparse.Namespace(provider="aaa"), io_) == 0
    assert "provider 'aaa' was already removed from platform.yaml" in buf.getvalue()
    assert "✓ removed aaa" not in buf.getvalue()


def _lock_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import assets.platform_config as config_mod
    from errors import AuthzRulesPersistFailed

    class _TimedOut:
        def __init__(self, root: Path, *, timeout_s: float = 5.0) -> None:
            self.root = root
            _ = timeout_s

        def __enter__(self) -> None:
            raise AuthzRulesPersistFailed(
                path=str(self.root / "platform.yaml"),
                reason="lock_timeout",
                detail="rules lock still held after 5.0s",
                where="test",
            )

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(config_mod, "platform_yaml_lock", _TimedOut)


async def test_llm_writer_lock_timeout_is_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.llm_commands import cmd_disconnect

    yaml_path = _write_provider_workspace(tmp_path)
    before = yaml_path.read_bytes()
    _lock_times_out(monkeypatch)
    io_, buf = _llm_io(tmp_path)
    assert await cmd_disconnect(argparse.Namespace(provider="aaa"), io_) == 1
    text = io_.err.getvalue()
    assert buf.getvalue() == ""
    assert text.startswith("error: platform.yaml was not updated:"), text
    assert "lock" in text
    assert yaml_path.read_bytes() == before


async def test_setup_defaults_model_lock_timeout_is_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    _lock_times_out(monkeypatch)
    io_ = _io(tmp_path)
    await run_setup(argparse.Namespace(non_interactive=True, root=tmp_path), io_)
    out = io_.out.getvalue()
    assert "error: defaults.model not written to platform.yaml:" in out
    assert "Traceback" not in out + io_.err.getvalue()
    assert "materialized bundle" in out
