"""Integration test — kolsch → crewai adapter → wire receives OpenAI envelope.

Pins the ``_tool_to_openai_spec`` normalisation from commit ``aaeba38``
against the ``bootstrap_runtime`` → real-crewai path. Every unit and
component test for the adapter used ``_StubCrewaiLLM``, which accepted
whatever kolsch passed without a wire-format contract; the 2026-07-02
Kimi K2 bug shipped because no test drove the full composition.

Failure mode caught by this test: an author refactors
``_crewai_adapter.py``, ``FakeFramework``, or any upstream tool-shape
handler and reintroduces kolsch's internal ``{name, description,
schema}`` shape at the crewai boundary. Kimi K2 (and any Moonshot-style
provider that echoes empty ``function.arguments`` when the
advertisement lacks proper ``parameters``) then silently emits
``arguments: {}`` for every tool call; smart models mask the bug.

The test does NOT touch the real HTTP wire — ``crewai.LLM.call`` is
patched at the class level so the actual upstream provider is never
contacted. The intercept sits at the exact boundary where kolsch's
code stops (per litellm's ``crewai.LLM.call`` entry).

See:
- ``process/qol-integration-test-real-llm-tool-shape.md`` — this spec.
- ``process/feat-clarify-framework-selection.md`` — the FakeFramework
  rename (co-conspirator in why the underlying bug shipped).
- ``process/bug-observer-records-pre-normalisation-tool-shape.md`` —
  the log-shape lie discovered in the same retrospective.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from crewai.llms.providers.openai_compatible.completion import (
    OpenAICompatibleCompletion,
)

from engine import ExecutionContext
from platform_runtime import bootstrap_runtime
from schemas import EventType

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Workspace setup — reuse the CLI's setup path so provider config is real
# ---------------------------------------------------------------------------


def _setup_workspace(root: Path) -> None:
    """Bootstrap a workspace via ``kolsch setup --non-interactive``.

    Copies the CLI-visible on-disk layout: ``platform.yaml``,
    ``bundles/``, ``.env`` — which is important because
    ``bootstrap_runtime`` reads them all.
    """
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(root)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            f"setup failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)


def _configure_opencode_provider(root: Path) -> None:
    """Overwrite the starter ``platform.yaml`` with an opencode provider
    entry so ``bootstrap_runtime`` wires the real crewai adapter factory
    (via ``make_build_llm_factory``) instead of falling through to
    ``EchoLLMDriver``.

    The dummy API key never leaves the test — ``crewai.LLM.call`` is
    patched below so the actual HTTP call never fires.
    """
    import yaml

    plat_path = root / "platform.yaml"
    plat = yaml.safe_load(plat_path.read_text(encoding="utf-8")) or {}
    plat["defaults"] = {"model": {"provider": "opencode", "model": "kimi-k2.6"}}
    plat["llm_providers"] = {
        "opencode": {
            "type": "opencode",
            "api_key_secret": "OPENCODE_API_KEY",
            "base_url": "https://opencode.example.invalid/v1",
            "models": [{"id": "kimi-k2.6"}],
        },
    }
    plat_path.write_text(
        yaml.safe_dump(plat, sort_keys=False), encoding="utf-8"
    )
    env_path = root / ".env"
    env_path.write_text("OPENCODE_API_KEY=sk-test\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# TC-REAL-LLM-TOOL-SHAPE — end-to-end wire-shape assertion
# ---------------------------------------------------------------------------


async def test_bootstrap_runtime_sends_openai_envelope_tools_to_wire(
    tmp_path: Path,
) -> None:
    """Kolsch → crewai adapter → wire MUST receive OpenAI envelope tools.

    Drives the full composition:

    1. Real workspace via ``kolsch setup``.
    2. Real ``bootstrap_runtime`` wiring (opencode provider configured,
       ``crewai.LLM`` used as the LLM adapter — no ``llm=`` override).
    3. Real ``runtime.build_resolved_config`` for the starter helper
       agent (which declares four tools: ``read_file``, ``write_file``,
       ``list_files``, ``bash``).
    4. Real ``engine.execute`` under the observer + fallback +
       ``_crewai_adapter.make_llm_callable`` chain.
    5. ``crewai.LLM.call`` patched at the CLASS level so no HTTP goes
       out; the intercept captures the ``tools`` kwarg.

    Asserts on the intercept's captured ``tools``:

    - It's a non-empty list.
    - Every entry has the OpenAI envelope keys (``type`` + ``function``).
    - ``entry["function"]`` carries ``name``, ``description``,
      ``parameters`` — the three fields Kimi K2 needs to populate
      ``function.arguments``.
    - ``entry["function"]["parameters"]`` is a JSON schema
      (``type: "object"`` + populated ``properties``).

    Regression signal: temporarily comment out the
    ``_tool_to_openai_spec`` call at ``_crewai_adapter.py::call``. The
    assertions below fail immediately with a clear message pointing at
    the wrong shape.
    """
    root = tmp_path / "ws"
    root.mkdir()
    _setup_workspace(root)
    _configure_opencode_provider(root)

    captured: dict[str, Any] = {"tools": None, "n_calls": 0}

    def _intercept(
        self: Any,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> str:
        # Capture on the FIRST call so we don't overwrite with a
        # follow-up turn. The helper agent may loop through tool calls
        # (though our intercept returns text-only, terminating the
        # loop) — pin the initial advertisement.
        if captured["n_calls"] == 0:
            captured["tools"] = tools
        captured["n_calls"] += 1
        # Return a plain-text reply so the exchange completes cleanly.
        return "I've noted your request."

    # Patch the actual class `crewai.LLM(...)` constructs for
    # `hosted_vllm/kimi-k2.6` — `crewai.LLM.__new__` is a factory that
    # returns `OpenAICompatibleCompletion` for opencode's provider, so
    # patching `crewai.LLM.call` alone would miss the seam.
    with patch.object(OpenAICompatibleCompletion, "call", _intercept):
        runtime = await bootstrap_runtime(root=root)
        config = await runtime.build_resolved_config("kolsch/agents/helper")
        session = await runtime.store.create_session(
            str(config.agent_id), name="real-llm-tool-shape-test"
        )
        threads = await runtime.store.list_threads(session.session_id)
        ctx = ExecutionContext(
            thread_id=threads[0].thread_id,
            trigger={
                "kind": "user_message",
                "content": "list the files in the workspace",
            },
        )
        events = [e async for e in runtime.engine.execute_stream(config, ctx)]

    # Sanity — the exchange completed without an immediate failure.
    failures = [e for e in events if e.type == EventType.THREAD_EXCHANGE_FAILED]
    assert not failures, (
        f"exchange failed; expected clean completion. "
        f"failures={[f.data for f in failures]}"
    )

    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) >= 1, (
        f"crewai.LLM.call received no tools; kolsch's declared list "
        f"never reached the wire. captured={captured!r}"
    )

    # Every entry is the OpenAI envelope, not kolsch's internal
    # {name, schema} shape. Failure mode: pre-`aaeba38` code paths
    # returning kolsch shape would break the first assertion loudly.
    for i, entry in enumerate(tools):
        assert isinstance(entry, dict), f"tools[{i}] not a dict: {entry!r}"
        assert "type" in entry and entry["type"] == "function", (
            f"tools[{i}] missing OpenAI envelope 'type: function'; "
            f"got keys={list(entry.keys())}. Kolsch's internal "
            f"{{name, schema}} shape is reaching the wire — the "
            f"_tool_to_openai_spec normalisation at "
            f"src/engine/_crewai_adapter.py may have regressed."
        )
        assert "function" in entry, (
            f"tools[{i}] missing 'function' envelope; got keys="
            f"{list(entry.keys())}"
        )
        function = entry["function"]
        assert isinstance(function, dict), (
            f"tools[{i}].function not a dict: {function!r}"
        )
        assert function.get("name"), (
            f"tools[{i}].function.name missing or empty; "
            f"got={function!r}"
        )
        assert "description" in function, (
            f"tools[{i}].function.description missing"
        )
        assert "parameters" in function, (
            f"tools[{i}].function.parameters missing — Kimi K2 "
            f"cannot populate function.arguments without it"
        )
        parameters = function["parameters"]
        assert isinstance(parameters, dict), (
            f"tools[{i}].function.parameters not a dict: {parameters!r}"
        )
        assert parameters.get("type") == "object", (
            f"tools[{i}].function.parameters.type must be 'object'; "
            f"got {parameters.get('type')!r}"
        )
        properties = parameters.get("properties")
        assert isinstance(properties, dict) and properties, (
            f"tools[{i}].function.parameters.properties missing or "
            f"empty — the LLM has no signal about what argument "
            f"names to send. This is the Kimi K2 failure mode."
        )
