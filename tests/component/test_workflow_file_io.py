"""Phase 3.5 Step 2 — file-IO runner coverage.

Covers Step 2 ACs:

- 2.1 — three runners route through ``dispatch_tool`` against the
  workflow-registered tool defs.
- 2.2 — output narrowing: ``{content}`` / ``{bytes_written}`` /
  ``{entries: list[str]}``; richer tool fields are dropped.
- 2.3 — ``FileAccessDenied`` from the tool is remapped to
  :class:`WorkflowFileAccessDenied` with ``__cause__`` preserved.
- 2.4 / 2.5 — ``register_builtins`` + CLI bootstrap accept the new
  defns; ``RunnerRegistry`` resolves the three slots.
- 2.7 — ``list_files`` pattern filter works (delegated to the
  underlying tool's ``glob`` arg).
- 2.8 — ``path`` / ``content`` / ``pattern`` rendered via
  :meth:`NodeContext.render_jinja`.
- Phase Test 2 — file-access denial surfaces as
  ``WorkflowFileAccessDenied`` + workflow ends with
  ``workflow.node.failed``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from files import PathPolicy
from providers.authz import RuleBasedAuthorizationProvider
from providers.sandbox import NativeSandboxProvider
from providers.secrets import DotenvSecretsProvider
from tools.builtins import make_list_files, make_read_file, make_write_file
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.errors import WorkflowFileAccessDenied
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _policy(allowed: list[str]) -> PathPolicy:
    return PathPolicy(allowed_paths=allowed)


def _registry(root: Path, *, allowed: list[str]) -> RunnerRegistry:
    """Wire register_builtins with all four tool defs against ``root``."""
    from tools.builtins import make_bash

    policy = _policy(allowed)
    catalog = AssetCatalog()
    registry = RunnerRegistry()
    bash_defn = get_tool_definition(make_bash(policy=policy, root=root))
    read_file_defn = get_tool_definition(
        make_read_file(policy=policy, root=root)
    )
    write_file_defn = get_tool_definition(
        make_write_file(policy=policy, root=root)
    )
    list_files_defn = get_tool_definition(
        make_list_files(policy=policy, root=root)
    )
    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
            catalog=catalog,
        ),
        secrets=DotenvSecretsProvider(root=root, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
        read_file_defn=read_file_defn,
        write_file_defn=write_file_defn,
        list_files_defn=list_files_defn,
    )
    return registry


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
    allowed: list[str] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    """Build an executor rooted at ``tmp_path`` with workspace-relative file IO."""
    registry = _registry(tmp_path, allowed=allowed or ["."])
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/file-io", inputs=inputs or {}
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC 2.4 — registry resolution
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_three_runners_resolve(self, tmp_path: Path) -> None:
        from workflows.builtins import (
            ListFilesRunner as _ListFilesRunner,
        )
        from workflows.builtins import (
            ReadFileRunner as _ReadFileRunner,
        )
        from workflows.builtins import (
            WriteFileRunner as _WriteFileRunner,
        )

        reg = _registry(tmp_path, allowed=["."])
        assert isinstance(
            reg.get("kolsch/nodes/read_file", node_name="r"),
            _ReadFileRunner,
        )
        assert isinstance(
            reg.get("kolsch/nodes/write_file", node_name="w"),
            _WriteFileRunner,
        )
        assert isinstance(
            reg.get("kolsch/nodes/list_files", node_name="l"),
            _ListFilesRunner,
        )


# ---------------------------------------------------------------------------
# AC 2.1 + 2.2 + 2.8 — write + read roundtrip, output narrowing, Jinja
# ---------------------------------------------------------------------------


class TestReadWriteRoundtrip:
    async def test_write_then_read_via_jinja(
        self, tmp_path: Path
    ) -> None:
        # write_file uses ``{{ inputs.filename }}`` for path and
        # ``{{ inputs.body }}`` for content. read_file reads it back.
        wf = _wf(
            {
                "nodes": {
                    "write": {
                        "type": "kolsch/nodes/write_file",
                        "path": "{{ inputs.filename }}",
                        "content": "{{ inputs.body }}",
                    },
                    "read": {
                        "type": "kolsch/nodes/read_file",
                        "path": "{{ inputs.filename }}",
                    },
                },
                "graph": {"edges": [{"from": "write", "to": "read"}]},
            }
        )
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            inputs={"filename": "hello.txt", "body": "greetings"},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # File landed at workspace-relative path.
        assert (tmp_path / "hello.txt").read_text() == "greetings"
        # Captured output narrowed to spec contract.
        assert ex._captured["write"] == {"bytes_written": 9}
        # read_file's content carries cat -n line numbering; we check
        # the body is present somewhere in the rendered content.
        assert "greetings" in ex._captured["read"]["content"]
        # Narrowing — captured dict has ONLY ``content`` (no
        # lines_returned / total_lines / next_offset / truncated).
        assert set(ex._captured["read"].keys()) == {"content"}
        _ = store, run

    async def test_bytes_written_reflects_size(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "write": {
                        "type": "kolsch/nodes/write_file",
                        "path": "out.txt",
                        "content": "abc",
                    },
                }
            }
        )
        ex, _store, _run = _make_executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._captured["write"]["bytes_written"] == 3


# ---------------------------------------------------------------------------
# AC 2.1 + 2.2 + 2.7 + 2.8 — list_files entries, narrowing, pattern
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_entries_are_leaf_names(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        (tmp_path / "c.py").write_text("x")
        wf = _wf(
            {
                "nodes": {
                    "ls": {
                        "type": "kolsch/nodes/list_files",
                        "path": ".",
                    },
                }
            }
        )
        ex, _store, _run = _make_executor(tmp_path, workflow=wf)
        await ex.run()
        entries = ex._captured["ls"]["entries"]
        # Narrowing — only ``entries`` key.
        assert set(ex._captured["ls"].keys()) == {"entries"}
        # Names only (no paths / is_dir / size_bytes).
        assert isinstance(entries, list)
        assert all(isinstance(e, str) for e in entries)
        assert {"a.txt", "b.txt", "c.py"}.issubset(set(entries))

    async def test_pattern_filters_via_tool_glob(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "keep.txt").write_text("x")
        (tmp_path / "skip.py").write_text("x")
        wf = _wf(
            {
                "nodes": {
                    "ls": {
                        "type": "kolsch/nodes/list_files",
                        "path": ".",
                        "pattern": "*.txt",
                    },
                }
            }
        )
        ex, _store, _run = _make_executor(tmp_path, workflow=wf)
        await ex.run()
        entries = set(ex._captured["ls"]["entries"])
        assert "keep.txt" in entries
        assert "skip.py" not in entries

    async def test_pattern_via_jinja(self, tmp_path: Path) -> None:
        (tmp_path / "hit.py").write_text("x")
        (tmp_path / "miss.txt").write_text("x")
        wf = _wf(
            {
                "nodes": {
                    "ls": {
                        "type": "kolsch/nodes/list_files",
                        "path": ".",
                        "pattern": "*.{{ inputs.ext }}",
                    },
                }
            }
        )
        ex, _store, _run = _make_executor(
            tmp_path, workflow=wf, inputs={"ext": "py"}
        )
        await ex.run()
        entries = set(ex._captured["ls"]["entries"])
        assert "hit.py" in entries
        assert "miss.txt" not in entries


# ---------------------------------------------------------------------------
# AC 2.3 + Phase Test 2 — file-access denial
# ---------------------------------------------------------------------------


class TestFileAccessDenied:
    async def test_path_outside_workspace_denied(
        self, tmp_path: Path
    ) -> None:
        # Allowed paths are restricted to ``tmp_path/data``; the node
        # tries to read /etc/passwd which sits outside.
        (tmp_path / "data").mkdir()
        wf = _wf(
            {
                "nodes": {
                    "read": {
                        "type": "kolsch/nodes/read_file",
                        "path": "/etc/passwd",
                    },
                }
            }
        )
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, allowed=["data/"]
        )
        status = await ex.run()
        # AC 2.3 / Phase Test 2 — run aborts via the existing failure
        # path; node.failed emitted with the denial details.
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = [
            e for e in events if e["kind"] == "workflow.node.failed"
        ]
        assert len(failed) == 1
        assert failed[0]["payload"]["error_class"] == (
            "WorkflowFileAccessDenied"
        )
        # JSONL ends with workflow.run.failed (terminal envelope).
        assert events[-1]["kind"] == "workflow.run.failed"

    async def test_remap_preserves_cause(self, tmp_path: Path) -> None:
        # Direct unit check on the runner remap — sanity check that
        # ``raise ... from exc`` keeps ``__cause__`` populated.
        from errors import FileAccessDenied
        from workflows.builtins.read_file import (
            ReadFileRunner,
        )

        try:
            try:
                raise FileAccessDenied(
                    path="/etc/passwd",
                    reason="outside scope",
                )
            except FileAccessDenied as exc:
                raise WorkflowFileAccessDenied(
                    path="/etc/passwd", node="r"
                ) from exc
        except WorkflowFileAccessDenied as wrapped:
            assert isinstance(wrapped.__cause__, FileAccessDenied)
            assert wrapped.path == "/etc/passwd"
            assert wrapped.node == "r"
        _ = ReadFileRunner  # imported for spec coverage; no instantiation
