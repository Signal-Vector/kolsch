"""Sandboxed Jinja environment singleton — Round 6 S1.

All Jinja evaluation in the workflows layer routes through the
module-level :data:`JINJA_ENV`. ALL means: ``binds:`` rendering,
config-string rendering, ``when:``/``condition:`` evaluation,
``compute.value``, ``map.over``, and the validator's AST walk. No
other module instantiates ``Environment`` / ``NativeEnvironment``
directly.

The class is a hybrid of two stock Jinja pieces:

- :class:`jinja2.sandbox.ImmutableSandboxedEnvironment` blocks the
  unsafe attribute / callable family that fuels the well-known
  Jinja-to-Python escape payloads (``__class__``, ``__mro__``,
  ``__subclasses__``, ``__globals__``, etc.).
- :class:`jinja2.nativetypes.NativeCodeGenerator` +
  :func:`jinja2.nativetypes.native_concat` preserve Python typed
  values for single-segment templates so ``"{{ 1 + 1 }}"`` returns
  ``2`` (int), ``"{{ items | length }}"`` returns an int, and
  ``"{{ '{}' | from_json }}"`` returns a Python dict — same
  ergonomics as :class:`jinja2.nativetypes.NativeEnvironment`,
  with the sandbox bolted on.

Master spec §2.7.2 lock pointer: "The workflows layer evaluates
Jinja inside ``src/workflows/jinja_env.py``'s
``ImmutableSandboxedEnvironment`` subclass with native-type returns
preserved. Author-supplied Jinja CANNOT escape into the Python
runtime via attribute traversal."

Round 7 CV1: a single ``_register_builtin_filters`` helper is the
contribution chokepoint for kolsch-maintained Jinja filters. Today
the registered set is ``from_json``; future additions land here in
~5 LoC each. Filters are CORE-ONLY — bundles, plugins, and
pip-installable packages cannot register custom filters (the
contract is documented at ``docs/explanation/jinja-extensions.md``).
"""

from __future__ import annotations

import json
from typing import Any

from jinja2.exceptions import SecurityError as JinjaSecurityError
from jinja2.nativetypes import NativeCodeGenerator, native_concat
from jinja2.sandbox import ImmutableSandboxedEnvironment

from workflows.errors import WorkflowJinjaSecurityError

__all__ = [
    "JINJA_ENV",
    "WorkflowJinjaEnvironment",
    "render_jinja",
]


class WorkflowJinjaEnvironment(ImmutableSandboxedEnvironment):
    """The sandboxed + native-types Jinja environment used throughout workflows.

    Subclasses :class:`ImmutableSandboxedEnvironment` (the sandbox)
    and overrides the ``concat`` + ``code_generator_class`` hooks
    that :class:`jinja2.nativetypes.NativeEnvironment` sets — same
    machinery, with the sandbox semantics retained.
    """

    code_generator_class = NativeCodeGenerator
    concat = staticmethod(native_concat)  # type: ignore[assignment]


def _from_json_filter(value: str) -> Any:  # noqa: ANN401
    """The v1 ``from_json`` Jinja filter (R7 CV1).

    Wraps :func:`json.loads` so a parse error surfaces as a
    :class:`workflows.errors.WorkflowError` family member (re-raised
    by callers as ``WorkflowJinjaSecurityError`` when relevant; for
    parse errors the underlying :class:`json.JSONDecodeError`
    propagates with the original message and bubbles up as a
    runtime error at the engine boundary).
    """
    return json.loads(value)


def _register_builtin_filters(env: WorkflowJinjaEnvironment) -> None:
    """The single chokepoint for kolsch-maintained core Jinja filters.

    Round 7 CV1 — filters are CORE-ONLY. Bundles, plugins, and
    packages CANNOT add filters via the asset catalog or any other
    discovery path. Contributions to the filter set live here in
    the kolsch repo and go through code review.
    """
    env.filters["from_json"] = _from_json_filter


JINJA_ENV: WorkflowJinjaEnvironment = WorkflowJinjaEnvironment()
"""The singleton sandboxed Jinja env shared across the workflows layer."""

_register_builtin_filters(JINJA_ENV)


def render_jinja(expression: str, *, location: str, **context: Any) -> Any:  # noqa: ANN401
    """Render a Jinja expression under the singleton sandbox.

    The convenience wrapper that the validator + executor call into.
    Translates :class:`jinja2.exceptions.SecurityError` (raised by
    the sandbox on unsafe attribute / callable access) into
    :class:`workflows.errors.WorkflowJinjaSecurityError` carrying
    the offending ``expression`` text plus a ``location`` hint
    (e.g., ``"workflows/triage.yaml:42 binds.url"``).

    Args:
        expression: the Jinja source string.
        location: a human-readable identifier for the offending
            site (file:line + path).
        **context: variables available to the template.

    Returns:
        The rendered native Python value (int, str, dict, list,
        bool, etc.).

    Raises:
        WorkflowJinjaSecurityError: when the sandbox blocked an
            unsafe access.
    """
    try:
        return JINJA_ENV.from_string(expression).render(**context)
    except JinjaSecurityError as exc:
        raise WorkflowJinjaSecurityError(
            location=location,
            expression=expression,
            cause=exc,
        ) from exc
