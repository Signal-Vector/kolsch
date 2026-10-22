"""bug-153 — the starter bundle is the only copy of the platform's tools.

The platform tools live in ``src/cli/_starter_bundle/kolsch/tools/``
(``kolsch setup`` copies them into ``<workspace>/bundles/kolsch/``).
Until bug-153 a repo-root ``bundles/kolsch/`` tree held stale forks of
four of them, and the tool tests loaded the forks — so they tested code
no user ran. These guards keep the trees from forking again:

- (a) no repo-root ``bundles/`` tree is tracked or shipped: the
  ``Containerfile`` doesn't copy one, git tracks no file under it, and
  ``.gitignore`` ignores it. An untracked repo-root ``bundles/`` is
  fine — it's a workspace artefact (``kolsch setup`` in the dev
  container, where ``KOLSCH_WORKSPACE`` is the repo root), or an
  ignored ``__pycache__/`` left behind by the pre-bug-153 tree;
- (b) no example bundle under ``examples/bundles/`` reuses a starter
  tool filename, and none is named ``kolsch``;
- (c) no test addresses a repo-root ``bundles/`` path. Tests resolve
  bundle directories through ``tests/fixtures/bundle_paths.py``.

Tree reads plus one read-only ``git ls-files``; no platform I/O.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR

_REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
_EXAMPLES_ROOT = _REPO_ROOT_DIR / "examples"
_EXAMPLES_DIR = _EXAMPLES_ROOT / "bundles"
_TESTS_DIR = _REPO_ROOT_DIR / "tests"

_ROOT_BUNDLES_LINE_RE = re.compile(
    r'(parents\[[0-9]+\]|\b_?REPO[A-Z_]*)\)*\s*[/,]\s*"bundles"',
)
"""``"bundles"`` joined directly onto a repo-root expression on one line.

Catches ``parents[N] / "bundles"``, ``Path(_REPO) / "bundles"`` and
``os.path.join(REPO_ROOT, "bundles")``. It deliberately ignores
``parents[N] / "examples" / "bundles"``. The AST walk in
[`_root_bundles_joins`][] covers the multi-line ``/`` form.
"""


def _tool_filenames(tools_dir: Path) -> set[str]:
    """Return the ``*.py`` / ``*.yaml`` filenames directly under ``tools_dir``."""
    return {
        p.name
        for p in tools_dir.iterdir()
        if p.is_file() and p.suffix in {".py", ".yaml"} and p.name != "__init__.py"
    }


def _example_bundle_dirs() -> list[Path]:
    """Return every bundle directory under ``examples/bundles/``."""
    return sorted(p for p in _EXAMPLES_DIR.iterdir() if p.is_dir())


def _is_repo_root_name(name: str) -> bool:
    """True for ``_REPO``, ``_REPO_ROOT``, ``REPO_ROOT`` style identifiers."""
    return name.lstrip("_").startswith("REPO")


def _is_repo_root_expr(node: ast.expr) -> bool:
    """True when ``node`` plausibly evaluates to the repo root.

    Matches ``<x>.parents[N]``, a ``_REPO*`` / ``REPO_ROOT`` name or
    attribute, and those wrapped in ``Path(...)`` or ``.resolve()``.
    """
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
    if isinstance(node, ast.Name):
        return _is_repo_root_name(node.id)
    if isinstance(node, ast.Attribute):
        return _is_repo_root_name(node.attr)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Path" and node.args:
            return _is_repo_root_expr(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr == "resolve":
            return _is_repo_root_expr(func.value)
    return False


def _root_bundles_joins(source: str) -> list[int]:
    """Return line numbers of ``<repo-root expr> / "bundles"`` joins.

    Walks ``/`` BinOps, so it catches the multi-line form
    (``Path(__file__).resolve().parents[N]`` on one line,
    ``/ "bundles"`` on the next) that a single-line regex misses.
    """
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and node.right.value == "bundles"
            and _is_repo_root_expr(node.left)
        ):
            hits.append(node.lineno)
    return sorted(hits)


def test_containerfile_does_not_copy_repo_root_bundles() -> None:
    """(a1) The image never ships a repo-root ``bundles/`` tree.

    Skips inside the gate image, which doesn't copy the ``Containerfile``
    into itself; every checkout has it.
    """
    containerfile = _REPO_ROOT_DIR / "Containerfile"
    if not containerfile.is_file():
        pytest.skip(f"{containerfile} not in this tree (e.g. the gate image)")
    offending = [
        f"Containerfile:{lineno}: {line.strip()}"
        for lineno, line in enumerate(
            containerfile.read_text(encoding="utf-8").splitlines(), start=1
        )
        if re.match(r"\s*COPY\s+(\./)?bundles\b", line)
    ]
    assert not offending, (
        "Containerfile copies a repo-root bundles/ tree. Shipped tools live "
        f"only in {STARTER_TOOLS_DIR}; tutorial examples under {_EXAMPLES_DIR}:\n"
        + "\n".join(offending)
    )


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only git command at the repo root; ``None`` if git is absent."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None


def test_git_tracks_no_repo_root_bundles() -> None:
    """(a2) No git-tracked file lives under repo-root ``bundles/``.

    Skips where git can't see this tree as a work tree: the gate image
    has no ``.git``, and a container mounting a ``git worktree`` can't
    resolve its ``.git`` file. (a1) and (a3) still run there.
    """
    toplevel = _git("rev-parse", "--show-toplevel")
    if toplevel is None or toplevel.returncode != 0:
        pytest.skip("not inside a git work tree (no .git, or an unresolvable worktree)")
    if Path(toplevel.stdout.strip()).resolve() != _REPO_ROOT_DIR:
        pytest.skip(f"git work tree {toplevel.stdout.strip()} is not this repo root")
    tracked = _git("ls-files", "--", "bundles")
    assert tracked is not None
    assert tracked.returncode == 0, tracked.stderr
    files = tracked.stdout.split()
    assert not files, (
        "git tracks files under repo-root bundles/. Shipped tools live only in "
        f"{STARTER_TOOLS_DIR}; tutorial examples under {_EXAMPLES_DIR}:\n"
        + "\n".join(files)
    )


def test_gitignore_ignores_repo_root_bundles() -> None:
    """(a3) A workspace bundle at the repo root can't be committed by accident.

    Skips inside the gate image, which doesn't copy ``.gitignore``;
    every checkout has it.
    """
    gitignore = _REPO_ROOT_DIR / ".gitignore"
    if not gitignore.is_file():
        pytest.skip(f"{gitignore} not in this tree (e.g. the gate image)")
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert "/bundles/" in (line.strip() for line in lines), (
        ".gitignore must ignore the repo-root workspace artefact with a "
        "'/bundles/' entry (anchored, so tests/bundles/ stays tracked)."
    )


def test_example_bundles_do_not_overlap_starter_tools() -> None:
    """(b) Example bundles never shadow or fork a shipped tool."""
    starter = _tool_filenames(STARTER_TOOLS_DIR)
    assert starter, f"no tool files found under {STARTER_TOOLS_DIR}"
    bundles = _example_bundle_dirs()
    assert bundles, f"no example bundles found under {_EXAMPLES_DIR}"

    problems: list[str] = []
    for bundle in bundles:
        manifest = yaml.safe_load((bundle / "bundle.yaml").read_text(encoding="utf-8"))
        if bundle.name == "kolsch" or manifest.get("name") == "kolsch":
            problems.append(f"{bundle}: example bundle must not be named 'kolsch'")
        tools_dir = bundle / "tools"
        if tools_dir.is_dir():
            problems.extend(
                f"{tools_dir / name}: duplicates starter tool {STARTER_TOOLS_DIR / name}"
                for name in sorted(_tool_filenames(tools_dir) & starter)
            )
    assert not problems, "\n".join(problems)


def test_no_test_addresses_repo_root_bundles() -> None:
    """(c) Tests load bundle paths from ``tests/fixtures/bundle_paths.py``."""
    this_file = Path(__file__).resolve()
    problems: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if path.resolve() == this_file:
            continue
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO_ROOT_DIR)
        problems.extend(
            f"{rel}:{lineno}: repo-root bundles path"
            for lineno, line in enumerate(source.splitlines(), start=1)
            if _ROOT_BUNDLES_LINE_RE.search(line)
        )
        problems.extend(
            f"{rel}:{lineno}: repo-root / 'bundles' join"
            for lineno in _root_bundles_joins(source)
        )
    assert not problems, (
        "Tests must not address repo-root bundles/; use "
        "tests/fixtures/bundle_paths.py:\n" + "\n".join(sorted(set(problems)))
    )


def test_ast_check_catches_multi_line_form() -> None:
    """The AST walk catches the split-line join a regex can't see."""
    source = (
        "from pathlib import Path\n"
        "p = (\n"
        "    Path(__file__).resolve().parents[3]\n"
        '    / "bundles"\n'
        '    / "kolsch"\n'
        ")\n"
        'q = Path(_REPO) / "bundles"\n'
        'ok = tmp_path / "bundles"\n'
        'ok2 = Path(__file__).resolve().parents[2] / "examples" / "bundles"\n'
    )
    assert _root_bundles_joins(source) == [3, 7]


def test_line_regex_flags_direct_joins_only() -> None:
    """The single-line regex flags only ``"bundles"`` joined onto a root expr."""
    flagged = [
        'X = Path(__file__).parents[2] / "bundles"',
        'q = Path(_REPO) / "bundles" / "kolsch"',
        'r = os.path.join(REPO_ROOT, "bundles")',
    ]
    allowed = [
        'ok = Path(__file__).resolve().parents[2] / "examples" / "bundles"',
        'ok2 = tmp_path / "bundles"',
        'ok3 = _REPO_ROOT / "tests" / "bundles" / "kolsch"',
    ]
    assert [line for line in flagged if not _ROOT_BUNDLES_LINE_RE.search(line)] == []
    assert [line for line in allowed if _ROOT_BUNDLES_LINE_RE.search(line)] == []
