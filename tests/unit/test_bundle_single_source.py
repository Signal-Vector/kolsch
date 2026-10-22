"""bug-153 — the starter bundle is the only copy of the platform's tools.

The platform tools live in ``src/cli/_starter_bundle/kolsch/tools/``
(``kolsch setup`` copies them into ``<workspace>/bundles/kolsch/``).
Until bug-153 a repo-root ``bundles/kolsch/`` tree held stale forks of
four of them, and the tool tests loaded the forks — so they tested code
no user ran. These guards keep the trees from forking again:

- (a) repo-root ``bundles/`` does not exist;
- (b) no example bundle under ``examples/bundles/`` reuses a starter
  tool filename, and none is named ``kolsch``;
- (c) no test addresses a repo-root ``bundles/`` path. Tests resolve
  bundle directories through ``tests/fixtures/bundle_paths.py``.

Pure tree reads; no platform I/O.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from tests.fixtures.bundle_paths import STARTER_TOOLS_DIR

_REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
_EXAMPLES_ROOT = _REPO_ROOT_DIR / "examples"
_EXAMPLES_DIR = _EXAMPLES_ROOT / "bundles"
_TESTS_DIR = _REPO_ROOT_DIR / "tests"

_ROOT_BUNDLES_LINE_RE = re.compile(
    r'(parents\[[0-9]\]|_REPO[A-Z_]*|REPO_ROOT)[^#\n]*"bundles"',
)
"""Single-line form: a repo-root expression and ``"bundles"`` on one line."""


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


def test_repo_root_bundles_dir_does_not_exist() -> None:
    """(a) The stale repo-root ``bundles/`` tree is gone for good."""
    root_bundles = _REPO_ROOT_DIR / "bundles"
    assert not root_bundles.exists(), (
        f"{root_bundles} exists. Shipped tools live only in "
        f"{STARTER_TOOLS_DIR}; tutorial examples live under {_EXAMPLES_DIR}."
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
