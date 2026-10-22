"""Guard — every production `.allow(` call passes a real `caller_chain=` (bug-158).

`AuthorizationProvider.allow` defaults `caller_chain` to `()`, and an
empty chain never matches a `from:`-scoped rule. A call site that
forgets the keyword therefore makes every scoped rule silently inert
there — the exact bug-158 failure. This test AST-scans `src/` and
fails on any authz-shaped `.allow(` call that:

- omits `caller_chain=` (a `**kwargs` splat does not count — the
  keyword must be explicit), or
- passes a literal empty chain (`()`, `tuple()`, `[]`, `list()`) — the
  only empty chain is `CallerChain.unattributed().frames`, by name.

A call is authz-shaped when its `.allow` attribute call has ≥1
positional argument or an `action=` / `principal=` keyword.
`src/providers/authz.py` is exempt: it defines the method and its
internal delegations. Out of scope: calls through an alias
(`allow = authz.allow; allow(p, a)`) or `getattr(authz, "allow")(...)`;
production code calls the provider method directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
_EXEMPT = {_SRC / "providers" / "authz.py"}


def _is_literal_empty(value: ast.expr) -> bool:
    if isinstance(value, (ast.Tuple, ast.List)):
        return not value.elts
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"tuple", "list"}
        and not value.args
        and not value.keywords
    )


def _offenders(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "allow"):
            continue
        named = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        authz_shaped = bool(node.args) or "action" in named or "principal" in named
        if not authz_shaped:
            continue
        chain = named.get("caller_chain")
        if chain is None or _is_literal_empty(chain):
            found.append(f"{filename}:{node.lineno}")
    return found


def test_every_src_allow_call_passes_caller_chain() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _EXEMPT:
            continue
        offenders += _offenders(path.read_text(encoding="utf-8"), str(path))
    assert offenders == [], (
        "authz `.allow(` calls must pass a real `caller_chain=` explicitly "
        f"(see docs/how-to/permissions.md): {offenders}"
    )


def test_guard_catches_synthetic_offenders() -> None:
    bad = "\n".join(
        [
            "async def f(a, p, kw):",
            "    await a.allow(p, 'tool.x', layers=())",  # 2: no chain
            "    await a.allow(principal='p', action='a')",  # 3: keywords only
            "    await a.allow(p, **kw)",  # 4: splat hides the chain
            "    await a.allow(p, action='x')",  # 5: one positional
            "    await a.allow(p, 'x', caller_chain=())",  # 6: literal ()
            "    await a.allow(p, 'x', **kw)",  # 7: splat
            "    await a.allow(p, 'x', caller_chain=tuple())",  # 8: tuple()
            "    await a.allow(p, 'x', caller_chain=[])",  # 9: []
            "",
        ]
    )
    good = "\n".join(
        [
            "async def f(a, ctx, kw):",
            "    await a.allow(ctx.principal, 'tool.x',"
            " caller_chain=ctx.caller_chain.frames)",
            "    await a.allow(ctx.principal, 'x', caller_chain=c.frames, **kw)",
            "    await a.allow(p, 'x', caller_chain=CallerChain.unattributed().frames)",
            "    allowed = policy.allow()",
            "",
        ]
    )
    assert _offenders(bad, "bad.py") == [f"bad.py:{n}" for n in range(2, 10)]
    assert _offenders(good, "good.py") == []
