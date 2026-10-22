"""Guard — every production `.allow(` call passes `caller_chain=` (bug-158).

`AuthorizationProvider.allow` defaults `caller_chain` to `()`, and an
empty chain never matches a `from:`-scoped rule. A call site that
forgets the keyword therefore makes every scoped rule silently inert
there — the exact bug-158 failure. This test AST-scans `src/` and
fails on any authz-shaped `.allow(` call (≥2 positional args, or a
`principal=` keyword) that does not pass `caller_chain=` explicitly.
`src/providers/authz.py` is exempt: it defines the method and its
internal delegations.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
_EXEMPT = {_SRC / "providers" / "authz.py"}


def _offenders(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "allow"):
            continue
        keywords = {kw.arg for kw in node.keywords}
        authz_shaped = len(node.args) >= 2 or "principal" in keywords
        if authz_shaped and "caller_chain" not in keywords:
            found.append(f"{filename}:{node.lineno}")
    return found


def test_every_src_allow_call_passes_caller_chain() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _EXEMPT:
            continue
        offenders += _offenders(path.read_text(encoding="utf-8"), str(path))
    assert offenders == [], (
        "authz `.allow(` calls must pass `caller_chain=` explicitly "
        f"(see docs/how-to/permissions.md): {offenders}"
    )


def test_guard_catches_a_synthetic_offender() -> None:
    bad = (
        "async def f(authz, ctx):\n"
        "    await authz.allow(ctx.principal, 'tool.x', layers=())\n"
        "    await authz.allow(principal='p', action='a')\n"
    )
    good = (
        "async def f(authz, ctx):\n"
        "    await authz.allow(ctx.principal, 'tool.x',"
        " caller_chain=ctx.caller_chain.frames)\n"
        "    allowed = policy.allow('only-one-arg')\n"
    )
    assert _offenders(bad, "bad.py") == ["bad.py:2", "bad.py:3"]
    assert _offenders(good, "good.py") == []
