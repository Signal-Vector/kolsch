"""TC-E2E-01..04 — end-to-end / overall-validation tests.

These are the "real LLM, real infra" cases from test-catalog Part 13.
All marked with ``@pytest.mark.e2e`` so the default `pytest` run
skips them (the marker is registered in pyproject.toml's
`markers` list under the existing `e2e:` definition).

Run via:

    pytest -m e2e

Each test skips when its required environment isn't present:

- TC-E2E-01 / TC-E2E-02: need ``E2E_LLM_API_KEY`` set + a real
  provider configured (Anthropic / OpenAI). Without it, the
  CrewAIFramework adapter has nothing real to talk to.
- TC-E2E-03 (soak): needs ``E2E_SOAK_MINUTES`` set + a real LLM.
- TC-E2E-04 (default install): runs by env-var opt-in
  (``E2E_DEFAULT_INSTALL=1``) — exercises the clean-machine first-run
  experience and would otherwise pollute the local tmp_path with a
  full bootstrap.

The scaffolds are deliberately thin in this commit: the E2E surface
is the human-judgment / external-infra tier (test plan §12). Step
14's job is to (a) reserve the slots in the test catalog and the
test tree so the run discipline + CI schedule exist, and (b) write
the executable scaffolds so adding the real assertions later is a
one-method-body change rather than a new file.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

pytestmark = pytest.mark.e2e

if TYPE_CHECKING:
    from pathlib import Path


_LLM_KEY_ENV = "E2E_LLM_API_KEY"
_LLM_PROVIDER_ENV = "E2E_LLM_PROVIDER"
_SOAK_MINUTES_ENV = "E2E_SOAK_MINUTES"
_DEFAULT_INSTALL_ENV = "E2E_DEFAULT_INSTALL"


def _llm_available() -> bool:
    return bool(os.environ.get(_LLM_KEY_ENV)) and bool(
        os.environ.get(_LLM_PROVIDER_ENV)
    )


# ===========================================================================
# TC-E2E-01 — Feasibility (GATING)
# ===========================================================================


@pytest.mark.skipif(
    not _llm_available(),
    reason=(
        "TC-E2E-01 (Feasibility) requires E2E_LLM_API_KEY + E2E_LLM_PROVIDER. "
        "Set both to run the real-LLM contract check (test plan Test 1). "
        "GATING — must pass before any production release."
    ),
)
async def test_tc_e2e_01_feasibility_crewai_with_real_llm(
    tmp_path: Path,
) -> None:
    """Real CrewAI + real LLM over 3 diverse tasks; assert the engine
    contract (I3, I5, I6) holds end-to-end.

    Three diverse tasks per the catalog:
      1. Text-only Q&A (no tools).
      2. Tool-using task (one tool call → final answer).
      3. Multi-turn conversational follow-up.

    Each: capture the engine's emitted events + the actual LLM input
    (via the §B.6 amendment's captured `model.request.data.messages`).
    Assert every required event landed and the captured messages
    match what CrewAI sent. Verifies the same invariants TC-CRW-02
    pins with a Fake LLM, now with a real provider in the loop.
    """
    pytest.skip(
        "Test plan §12 Test 1 — scaffolded; real-LLM body lands when "
        "an E2E run is scheduled. The CrewAIFramework + engine "
        "infrastructure to support it is already in place (see "
        "tests/integration/test_engine_crewai.py for the Fake-LLM "
        "form of the same shape).",
    )


# ===========================================================================
# TC-E2E-02 — Soundness (v1 vs baseline CrewAI on 5-10 tasks)
# ===========================================================================


@pytest.mark.skipif(
    not _llm_available(),
    reason=(
        "TC-E2E-02 (Soundness) requires E2E_LLM_API_KEY + "
        "E2E_LLM_PROVIDER. Set to run the human/judge comparison "
        "against baseline CrewAI."
    ),
)
async def test_tc_e2e_02_soundness_vs_baseline_crewai(tmp_path: Path) -> None:
    """v1 platform agents vs baseline CrewAI agents on 5-10 tasks.

    Per the catalog: identical inputs, human-or-LLM-judge comparison;
    expect v1 better on a majority (more grounded, better tool use,
    better recovery). Requires a curated task set + a judging
    methodology (deferred to the run that exercises it).
    """
    pytest.skip(
        "Test plan §12 Test 2 — scaffolded; task corpus + judging "
        "methodology authored when an E2E comparison run is scheduled. "
        "Result table goes in the run's commit message per the prompt.",
    )


# ===========================================================================
# TC-E2E-03 — Robustness (real-use soak)
# ===========================================================================


@pytest.mark.skipif(
    not os.environ.get(_SOAK_MINUTES_ENV),
    reason=(
        "TC-E2E-03 (Robustness) requires E2E_SOAK_MINUTES (e.g. 60). "
        "Set to run the multi-hour real-use soak; tracks crashes, "
        "corruption, resume correctness, memory leaks."
    ),
)
async def test_tc_e2e_03_robustness_soak(tmp_path: Path) -> None:
    """Long-running real-use soak. Tracks:
    - Zero data corruption (JSONL is parseable; entity DB matches).
    - Crashes recoverable (kill -9 mid-exchange; restart resumes).
    - Long sessions don't degrade (memory growth, latency
      regressions).
    """
    pytest.skip(
        "Test plan §12 Test 3 — scaffolded; soak driver authored "
        "when a robustness run is scheduled. Likely landing as a "
        "separate harness in tests/soak/ once requirements crystallize.",
    )


# ===========================================================================
# TC-E2E-04 — Default install needs no external services
# ===========================================================================


@pytest.mark.skipif(
    os.environ.get(_DEFAULT_INSTALL_ENV) != "1",
    reason=(
        "TC-E2E-04 (Default install) is opt-in via E2E_DEFAULT_INSTALL=1. "
        "Exercises the clean-machine first-run experience — installs a "
        "wheel into a throwaway venv, runs `pi-platform run`, verifies "
        "no external services were touched. Slow + writes to disk; "
        "opt-in to keep the default `pytest` run fast."
    ),
)
async def test_tc_e2e_04_default_install_no_external_services(
    tmp_path: Path,
) -> None:
    """Clean machine + only Python + uv. Install the wheel into a
    throwaway venv; run a simple agent; verify the default tier
    (SQLite + in-process bus + EchoLLM + FakeFramework) worked
    without touching Postgres / Redis / a real LLM provider.

    Phase 1 acceptance gate per v1-scope §2.5 "great first-run
    experience."
    """
    pytest.skip(
        "Test plan §12 Test 4 — scaffolded. Lands when the wheel-build "
        "+ throwaway-venv harness is wired (subprocess uv venv + uv "
        "pip install + pi-platform run; assert exit 0 + no socket "
        "syscalls via auditing or strace integration).",
    )
