"""bug-146 — the shared bundle file walk and the per-file baseline merge.

`cli._bundle_baseline.is_shipped_bundle_file` / `iter_bundle_files`
are the one predicate the baseline writers, `--sync-bundle` and
`kolsch doctor` walk with; `update_baseline_entries` is how sync
records what it wrote, even on a partial run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cli._bundle_baseline import (
    baseline_path,
    is_shipped_bundle_file,
    iter_bundle_files,
    read_baseline,
    update_baseline_entries,
    write_baseline,
)

if TYPE_CHECKING:
    from pathlib import Path


def _bundle(root: Path) -> Path:
    bundle = root / "bundles" / "kolsch"
    (bundle / "tools" / "__pycache__").mkdir(parents=True)
    (bundle / "tools" / "bash.py").write_text("print('bash')\n", encoding="utf-8")
    (bundle / "tools" / "__pycache__" / "bash.cpython-313.pyc").write_bytes(b"x")
    (bundle / "agents").mkdir()
    (bundle / "agents" / "helper.yaml").write_text("name: helper\n", encoding="utf-8")
    (bundle / ".DS_Store").write_bytes(b"x")
    (bundle / "agents" / ".DS_Store").write_bytes(b"x")
    return bundle


def test_iter_bundle_files_skips_pycache_and_ds_store(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert iter_bundle_files(bundle) == {"tools/bash.py", "agents/helper.yaml"}
    assert not is_shipped_bundle_file(bundle / "tools", bundle)
    assert iter_bundle_files(tmp_path / "absent") == set()


def test_write_baseline_uses_the_shared_walk(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    baseline = write_baseline(tmp_path, bundle_dir=bundle)
    assert set(baseline.files) == {"tools/bash.py", "agents/helper.yaml"}


def test_update_creates_baseline_when_absent(tmp_path: Path) -> None:
    assert update_baseline_entries(tmp_path, {"tools/bash.py": "sha256:aa"})
    recorded = read_baseline(tmp_path)
    assert recorded is not None
    assert recorded.files == {"tools/bash.py": "sha256:aa"}


def test_update_merges_and_deletes_entries(tmp_path: Path) -> None:
    update_baseline_entries(
        tmp_path, {"tools/bash.py": "sha256:aa", "tools/old.py": "sha256:bb"}
    )
    assert update_baseline_entries(
        tmp_path,
        {"tools/old.py": None, "agents/helper.yaml": "sha256:cc", "gone.py": None},
    )
    recorded = read_baseline(tmp_path)
    assert recorded is not None
    assert recorded.files == {
        "tools/bash.py": "sha256:aa",
        "agents/helper.yaml": "sha256:cc",
    }


def test_update_without_change_does_not_rewrite(tmp_path: Path) -> None:
    update_baseline_entries(tmp_path, {"tools/bash.py": "sha256:aa"})
    before = baseline_path(tmp_path).read_bytes()
    assert not update_baseline_entries(tmp_path, {"tools/bash.py": "sha256:aa"})
    assert not update_baseline_entries(tmp_path, {})
    assert baseline_path(tmp_path).read_bytes() == before


def test_update_replaces_a_corrupt_baseline(tmp_path: Path) -> None:
    path = baseline_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert update_baseline_entries(tmp_path, {"tools/bash.py": "sha256:aa"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["files"] == {"tools/bash.py": "sha256:aa"}
