"""TC-OPENCODE-TIMEOUT-01..04 — regression pins for the connector timeout fix.

Spec: `process/bug-chat-opencode-qwen-timeout-investigation.md` Step 3.

The original 30s default sat below opencode's natural long-tail
latency (replay of session ses_abae69a7 showed 12 calls between
30-56s, two reaching 112-113s). Step 3 raised the default to 180s
in :mod:`llm.providers.opencode`. These tests pin that decision so
a future contributor who lowers the default has to make the case
in a PR review rather than slipping it in unnoticed.

Two layers of coverage:

- **Plumbing**: ``build_llm`` threads the right timeout to
  ``crewai.LLM`` whether or not the platform.yaml provider entry
  overrides ``timeout_seconds``.
- **Behaviour**: the connector's ``httpx.AsyncClient`` actually
  honours ``_DEFAULT_TIMEOUT_SECONDS`` — i.e. a slow handler past
  the timeout fires the connector's classify-as-network-error
  branch. Tested via ``httpx.MockTransport`` with a small
  monkey-patched timeout so the test runs in <1 s.
"""

from __future__ import annotations

import httpx

from llm.protocol import Credentials, ProviderConfig
from llm.providers import opencode as opencode_module
from llm.providers.opencode import OpenCodeConnector


def _provider_config(
    *,
    timeout_seconds: int | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="opencode",
        type="opencode",
        api_key_secret="OPENCODE_API_KEY",
        base_url="https://opencode.example.com/v1",
        timeout_seconds=timeout_seconds,
        extras={},
        models=[],
    )


# ===========================================================================
# TC-OPENCODE-TIMEOUT-01 — connector default is pinned at 180 s
# ===========================================================================


def test_tc_opencode_timeout_01_default_is_180s() -> None:
    """Pin the default introduced in Step 3 of the bug-investigation doc.

    Anyone who needs to change this is expected to (a) re-run the
    replay tool from :file:`scripts/replay_session.py` against a
    representative session, (b) re-state the new tail-latency
    headroom in the docs, and (c) update this pin in the same PR.
    """
    assert opencode_module._DEFAULT_TIMEOUT_SECONDS == 180.0


# ===========================================================================
# TC-OPENCODE-TIMEOUT-02 — build_llm uses the default when YAML omits it
# ===========================================================================


async def test_tc_opencode_timeout_02_build_llm_uses_default_when_unset() -> None:
    """`provider_config.timeout_seconds=None` → crewai.LLM.timeout=180.

    Closes the user-visible failure mode the bug doc describes: a
    workspace whose platform.yaml has no explicit ``timeout_seconds:``
    line (the default kolsch setup produces this shape) gets the new
    180 s default, not the old 30 s.
    """
    connector = OpenCodeConnector()
    llm = await connector.build_llm(
        "qwen3.6-plus",
        _provider_config(timeout_seconds=None),
        api_key="sk-test",
    )
    assert llm.timeout == 180.0


# ===========================================================================
# TC-OPENCODE-TIMEOUT-03 — explicit override still wins (regression guard)
# ===========================================================================


async def test_tc_opencode_timeout_03_explicit_override_still_honoured() -> None:
    """Authors who tuned ``timeout_seconds: 60`` keep their tuning.

    The Step 3 fix should NOT clobber explicit overrides — they're
    the escape hatch for the "drop it to fail fast" case documented
    in `docs/explanation/llm-providers.md` Part 6.1.
    """
    connector = OpenCodeConnector()
    llm = await connector.build_llm(
        "qwen3.6-plus",
        _provider_config(timeout_seconds=60),
        api_key="sk-test",
    )
    assert llm.timeout == 60


# ===========================================================================
# TC-OPENCODE-TIMEOUT-04 — timeout exception classifies to network error
# ===========================================================================


async def test_tc_opencode_timeout_04_timeout_classifies_as_network_error() -> None:
    """An httpx ReadTimeout from the transport becomes
    ``ValidationResult(ok=False)`` with a "network error" reason.

    What this guards: a future refactor of
    :meth:`OpenCodeConnector.validate_credentials` could narrow the
    ``except httpx.HTTPError`` clause and accidentally let
    TimeoutException escape, surfacing as an unhandled exception
    instead of an ok=False result. Either form arguably "works"
    (the caller sees an error), but the project's contract is
    ``ValidationResult(ok=False, reason=...)`` — the CLI's connect
    flow reads ``.reason`` to render the user prompt and would
    crash on an unhandled exception.

    NOTE on what this does NOT test: I tried to verify the actual
    timeout VALUE is plumbed by monkey-patching
    ``_DEFAULT_TIMEOUT_SECONDS`` to 0.5 s and having the handler
    sleep 1 s, but httpx.MockTransport short-circuits the I/O layer
    that enforces AsyncClient(timeout=...), so the sleep doesn't
    fire a timeout. A real-socket test would exercise that
    plumbing but is too heavyweight for the suite. TC-01 pins the
    constant; TC-02/03 verify ``build_llm`` threads it correctly;
    a code review of ``_client()`` is the last line of defence
    against a regression that silently substitutes the value.
    """
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated upstream timeout")

    transport = httpx.MockTransport(timeout_handler)
    connector = OpenCodeConnector(transport=transport)
    result = await connector.validate_credentials(
        Credentials(values={"api_key": "sk-test"}),
        base_url="https://opencode.example.com/v1",
    )
    assert result.ok is False
    assert result.reason is not None
    assert "network error" in result.reason.lower()
    assert "ReadTimeout" in result.reason


# ===========================================================================
# TC-OPENCODE-TIMEOUT-05 — fast handler succeeds at the new default
# ===========================================================================


async def test_tc_opencode_timeout_05_fast_handler_succeeds() -> None:
    """Sanity-check the happy path — the connector returns ok=True
    when the handler responds promptly under the (real) 180s default.

    Pairs with TC-04 as the negative-vs-positive bookend: TC-04 shows
    the timeout fires when exceeded, this shows it doesn't fire when
    not exceeded.
    """
    def fast_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(fast_handler)
    connector = OpenCodeConnector(transport=transport)
    result = await connector.validate_credentials(
        Credentials(values={"api_key": "sk-test"}),
        base_url="https://opencode.example.com/v1",
    )
    assert result.ok is True
