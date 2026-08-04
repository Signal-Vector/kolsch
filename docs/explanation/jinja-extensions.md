# Jinja extensions in kolsch — contribution pattern (for maintainers)

**Draft 0.1**

> How new Jinja filters get added to kolsch. This document is for kolsch
> maintainers and PR contributors. Bundle authors do not add filters —
> the filter set is core-only by design (see Round 7 CV1 lock).
>
> If you are writing a bundle and want a missing filter, file an issue on
> the kolsch repo describing the use case. Do not vendor a filter into
> your bundle; bundles cannot register filters.

---

## Part 1 — Core-only policy

Bundles, plugins, and pip-installable packages **cannot** register custom
Jinja filters in kolsch. The filter set is kolsch's responsibility — kept
small, audited, and sandbox-compatible. To request a new filter, file an
issue on the kolsch repo describing the use case and the workflow shape
that motivates it.

Why core-only:

- Jinja runs in every workflow's evaluation context with elevated
  privilege (even sandboxed). Expanding the filter surface to community
  packages widens the attack and maintenance surface.
- Same rationale as Ansible's standard filters versus the
  `community.general` collection, which is opt-in trust.
- Aligns with kolsch's overall trust model (§1.4 of the workflow spec):
  bundles are trusted code via authored `@node`, but the Jinja
  interpreter stays narrow.

---

## Part 2 — v1 filter set

- All stock Jinja2 filters that work in the sandbox (`length`, `default`,
  `selectattr`, `sort`, `groupby`, etc.).
- One non-stock kolsch filter: `from_json` (added per Round 7 CV1).

That is the entire v1 surface. Additions follow Part 3.

---

## Part 3 — How to add a new filter (for maintainers)

### Step 1 — Write a pure function

Add it in `src/workflows/jinja_env.py`. The function must be:

- **Deterministic.** Same input, same output.
- **Side-effect-free.** No I/O, no global state mutation, no clock or RNG.
- **Returns native Python types.** No objects with a `__class__` that
  could leak the sandbox.
- **Single-arg + optional kwargs.** Idiomatic Jinja filter shape.

### Step 2 — Register the filter

Register it in the module-level `_register_builtin_filters(env)`
function in `src/workflows/jinja_env.py`. Do not register filters from
outside this function; it is the single audit point.

### Step 3 — Write tests

- **Unit test:** filter returns expected values for valid inputs and
  raises on invalid inputs (a JSON parse error propagates as
  `json.JSONDecodeError`).
- **Sandbox test:** confirm the filter cannot be combined with attribute
  access to escape the sandbox.
- **Fixture test:** at least one fixture YAML using the filter
  end-to-end.

### Step 4 — Update docs

- Add to the §2.7.3 filter list in `process/phase3.0-workflow-spec.md`.
- Add a usage example in `docs/architecture/workflows.md` if non-obvious.

### Step 5 — Submit PR

- Reference the issue justifying the filter.
- Include the canonical "could this filter be used to escape the
  sandbox?" review in the PR description, with the reviewer's answer.

---

## Part 4 — Example: `from_json`

Sample implementation in `src/workflows/jinja_env.py`:

```python
import json
from typing import Any

def _from_json_filter(value: str) -> Any:
    """The `from_json` Jinja filter.

    Wraps `json.loads`; a parse error propagates the underlying
    `json.JSONDecodeError` up to the engine boundary.

    Example:
        {{ nodes.fetch.stdout | from_json | attr('first_name') }}
    """
    return json.loads(value)


def _register_builtin_filters(env: WorkflowJinjaEnvironment) -> None:
    """Register the kolsch-maintained core Jinja filters. To add a
    new filter, write a pure function above and register it here.
    See docs/explanation/jinja-extensions.md."""
    env.filters["from_json"] = _from_json_filter
```

Keep the audit trail discoverable from the code — reference this file
from the registration docstring, as the sample above does.

---

## Part 5 — Future direction

Pip-installable filter packages via entry-points are explicitly **out of
scope at all phases**. There is no phase where third-party Jinja filters
become loadable.

Bundle authors who want custom data processing should use authored
`@node` Python, which is the documented trusted-code extension surface
(per S3 trust model). A `@node` runs in the bundle's trust envelope, has
the full Python language available, and integrates with the workflow
contract — it is the right tool for everything Jinja filters are the
wrong tool for.
