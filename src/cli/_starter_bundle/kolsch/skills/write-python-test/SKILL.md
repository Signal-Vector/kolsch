---
name: write-python-test
description: Write pytest-style tests for a Python module or function following the patterns Kolsch prefers — parametrize over subtests, mock at boundaries only, tmp_path over tempdir, freezegun for time.
allowed-tools:
  - read_file
  - write_file
  - list_files
  - bash
disable-model-invocation: false
---

# write-python-test

Use this skill when the user asks to write, add, or extend tests for a
Python file. The skill encodes the conventions this codebase uses so
the test you produce reads like it belongs alongside the existing
suite, not like a generic pytest example.

## When this skill applies

Trigger phrases the user might say:

- "write tests for `foo.py`"
- "add a test that covers the empty-input case"
- "the function I just wrote needs tests"
- "tests for the validator I added"

When the user is asking for a single one-off assertion in a REPL, or
for a doctest, that's a different shape — this skill is for files
under a `tests/` tree using pytest.

## Workflow

1. **Read the target source first.** Use the read tool on the file
   the user named. Identify the public surface — the functions or
   classes the test will exercise. Note types, error paths, and any
   imports that hint at where state lives (databases, filesystems,
   network).
2. **Find a neighbour test file.** Use the list/read tools to locate
   an existing test file for a similar module — same package, ideally
   same layer (tests/unit/, tests/component/, tests/integration/).
   Match its imports, its fixture style, and its naming convention.
   The neighbour answers more questions than this document.
3. **Decide what to verify.** For each public behaviour, name the
   specific assertion. Behaviours, not lines of code — a function
   that handles three input shapes gets three test names, not one
   test with three branches.
4. **Write the tests.** Apply the conventions below.
5. **Run pytest.** Use the bash tool: `uv run pytest <new test file>
   -v --no-cov`. If you wrote async tests and they fail with "the
   event loop is closed" or similar, check the `asyncio_mode`
   convention.
6. **Report.** Tell the user what you ran and the result. If any test
   failed, include the failure summary — don't claim "they pass" when
   they don't.

## Conventions

### Use pytest, not unittest

Plain `def test_...` functions. No `TestCase` subclasses. Plain
`assert`, no `assertEqual`. Pytest produces better diffs and the codebase
uses pytest throughout.

### Parametrize over subtests

When the same logic should hold for multiple inputs, use
`@pytest.mark.parametrize`, not a loop inside one test:

```python
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("alice", "Hello, alice!"),
        ("BOB", "Hello, BOB!"),
        ("", "Hello, !"),
    ],
)
def test_greet_returns_formatted_string(name, expected):
    assert greet(name) == expected
```

Each parameter row is a separate test in the report. A failure on
one case doesn't hide the others; the failure message names the row.

### Mock at boundaries only

If the function under test calls helper functions in the same module
or package, do NOT mock those — call them. The test exercises the
real wiring; that's the whole point. Mock only what crosses the unit's
boundary: network calls, time, subprocess invocations, filesystem
randomness. The codebase's component tests follow this rule: tests
under tests/component/ use real `SessionStore`, real providers,
real subsystems — only the LLM and the framework are stubbed.

### Prefer `tmp_path` over `tempfile.mkdtemp`

Pytest's `tmp_path` fixture gives a fresh `Path` per test and cleans
itself up. Don't write `tempfile.mkdtemp()` calls — the cleanup is
manual and easy to forget on a failure path.

```python
def test_writes_file(tmp_path):
    target = tmp_path / "out.txt"
    write_thing(target, "hello")
    assert target.read_text() == "hello"
```

### Freeze time with freezegun, never `time.sleep`

If the function depends on the current time, use the `freezegun`
package's `freeze_time` context manager. A `time.sleep` in a test is
a slow flake; a frozen clock is deterministic. Same goes for
`datetime.now()` — patch it via freezegun, not `monkeypatch.setattr`.

### Test names describe behaviour, not implementation

Bad: `test_resolve_returns_dict()`.
Good: `test_resolve_returns_empty_dict_when_partials_list_is_empty()`.

The name should tell a reader why this assertion is here, not just
what method it calls. Code generates the method names; the test name
adds the intent.

### Async: pytest-asyncio in auto mode

The codebase's `pyproject.toml` sets `asyncio_mode = "auto"`, so
`async def test_...` functions run without a decorator. Don't add
`@pytest.mark.asyncio` — it's redundant and reads as "this test
needed special wiring", which it didn't.

If your async test calls something that internally does
`asyncio.run(...)`, the test itself MUST be sync (`def`, not
`async def`) — Python's asyncio rejects nested event loops. The
common pattern is a sync test that drives the CLI via `main([...])`,
then a nested `asyncio.run(_check())` for the async verification
step. See the file at tests/component/cli/test_skills_new.py for an example.

### Error paths: assert the typed exception, not the message

The platform raises typed `PlatformError` subclasses (`SkillNotFound`,
`ToolUnauthorized`, etc.). Assert the type and any structured
`detail` fields. Don't assert against the string message — that's
prose that changes; the type and the detail are the contract.

```python
with pytest.raises(SkillNotFound) as exc_info:
    await skills.force_invoke("acme/skills/missing", config)
assert exc_info.value.detail["ref"] == "acme/skills/missing"
```

## Example A — pure function

Source under test:

```python
def normalise_ref(raw: str) -> str:
    raw = raw.strip().lower()
    if not raw:
        raise ValueError("ref cannot be empty")
    return raw.replace(" ", "-")
```

Test:

```python
import pytest
from mymod.refs import normalise_ref


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hello", "hello"),
        ("  spaced  ", "spaced"),
        ("two words", "two-words"),
    ],
)
def test_normalise_ref_returns_lowercase_dash_form(raw, expected):
    assert normalise_ref(raw) == expected


def test_normalise_ref_rejects_empty_input():
    with pytest.raises(ValueError, match="cannot be empty"):
        normalise_ref("   ")
```

Three rows for the happy path (parametrized); one separate test for
the error path because it's a different behaviour (raising) being
verified.

## Example B — function with filesystem dependency

Source under test:

```python
async def write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
```

Test:

```python
import yaml


async def test_write_manifest_creates_parents_and_writes_yaml(tmp_path):
    target = tmp_path / "nested" / "dir" / "manifest.yaml"
    await write_manifest(target, {"name": "test", "version": "1"})

    assert target.is_file()
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert payload == {"name": "test", "version": "1"}
```

`tmp_path` provides the fresh directory; `async def` works because
of `asyncio_mode = "auto"`; the assertion reads the file back to
confirm the round-trip (not just that "the call succeeded" — that's
not what was being tested).

## What this skill is NOT for

- **Performance / benchmark tests.** Different shape; pytest has
  `pytest-benchmark` for that and the conventions differ.
- **Property-based tests** (`hypothesis`). Useful but not the default
  in this codebase. If the user explicitly asks for property-based
  tests, follow their lead.
- **Integration tests that need a real LLM.** Those live behind a
  marker the user has to opt into; this skill is for unit/component
  tests against mocks.
