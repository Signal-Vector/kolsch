"""Phase 3.0 Step 1 — Round 6 S1 sandbox-escape coverage.

The workflows layer evaluates Jinja inside
``src/workflows/jinja_env.py``'s
:class:`workflows.jinja_env.WorkflowJinjaEnvironment`, a hybrid of
:class:`jinja2.sandbox.ImmutableSandboxedEnvironment` (blocks
unsafe attribute / callable access) and
:func:`jinja2.nativetypes.native_concat` (preserves Python typed
returns).

This file enumerates the ten canonical Jinja-to-Python escape
payloads + asserts each one raises
:class:`workflows.errors.WorkflowJinjaSecurityError` when rendered
through :func:`workflows.jinja_env.render_jinja` (the production
entry point — direct ``JINJA_ENV.from_string(...).render()`` raises
the unwrapped :class:`jinja2.exceptions.SecurityError`).

Master spec §2.7.2 lock pointer: "Author-supplied Jinja CANNOT
escape into the Python runtime via attribute traversal."
"""

from __future__ import annotations

from typing import Any

import pytest

from workflows.errors import WorkflowJinjaSecurityError
from workflows.jinja_env import JINJA_ENV, render_jinja

# ---------------------------------------------------------------------------
# Native-type returns are preserved (S1's hybrid design)
# ---------------------------------------------------------------------------


def test_int_arithmetic_returns_int() -> None:
    # The hybrid env's native_concat preserves typed returns, so the
    # runtime value is an int even though Jinja's stub claims str.
    result: Any = JINJA_ENV.from_string("{{ 1 + 1 }}").render()
    assert isinstance(result, int)
    assert result == 2


def test_list_length_returns_int() -> None:
    result: Any = JINJA_ENV.from_string("{{ [1, 2, 3] | length }}").render(
        items=[1, 2, 3]
    )
    assert isinstance(result, int)
    assert result == 3


def test_dict_passthrough_preserves_type() -> None:
    result: Any = JINJA_ENV.from_string("{{ x }}").render(x={"a": 1})
    assert isinstance(result, dict)
    assert result == {"a": 1}


# ---------------------------------------------------------------------------
# R7 CV1 — from_json filter registered + returns native dict
# ---------------------------------------------------------------------------


def test_from_json_filter_returns_native_dict() -> None:
    result: Any = JINJA_ENV.from_string(
        '{{ \'{"name":"x","n":3}\' | from_json }}'
    ).render()
    assert isinstance(result, dict)
    assert result == {"name": "x", "n": 3}


def test_from_json_filter_registered_on_singleton() -> None:
    assert "from_json" in JINJA_ENV.filters


# ---------------------------------------------------------------------------
# 10 canonical sandbox-escape payloads — each raises WorkflowJinjaSecurityError
# ---------------------------------------------------------------------------


_ESCAPE_PAYLOADS = [
    ("mro_chain", "{{ ''.__class__.__mro__[1].__subclasses__() }}"),
    ("bases_chain", "{{ ().__class__.__bases__[0].__subclasses__() }}"),
    ("dict_rooted", "{{ {}.__class__.__mro__[1].__subclasses__() }}"),
    ("lipsum_globals", "{{ lipsum.__globals__['__builtins__'] }}"),
    ("cycler_globals", "{{ cycler.__init__.__globals__['os'] }}"),
    ("joiner_globals", "{{ joiner.__init__.__globals__['os'] }}"),
    ("namespace_globals", "{{ namespace.__init__.__globals__['os'] }}"),
    ("self_globals", "{{ self.__init__.__globals__['os'] }}"),
    ("config_class", "{{ config.__class__.__mro__[1].__subclasses__() }}"),
    (
        "rce_shaped_compound",
        "{{ ''.__class__.__mro__[2].__subclasses__()[0]('id', shell=True) }}",
    ),
]


@pytest.mark.parametrize(
    ("label", "payload"),
    _ESCAPE_PAYLOADS,
    ids=[p[0] for p in _ESCAPE_PAYLOADS],
)
def test_canonical_escape_payload_blocked(label: str, payload: str) -> None:
    with pytest.raises(WorkflowJinjaSecurityError) as exc_info:
        render_jinja(payload, location=f"test:{label}")
    # Single-line + carries the location + offending expression
    assert "\n" not in str(exc_info.value)
    assert label in str(exc_info.value)
