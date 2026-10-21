"""bug-145 Test 3 — the LLM call seam's classification wrapper.

`engine._crewai_adapter.make_llm_callable(crewai_llm, classify=...)`
is where a raw provider exception from `crewai.LLM.call` becomes the
building connector's typed `model.*` error, before `FallbackLLMHandle`
sees it. The wrapper must never mask, double-wrap or swallow: a
classifier miss or a classifier bug re-raises the original object, and
`PlatformError` / `CancelledError` pass through untouched.

`make_build_llm_factory` binds the classifier through
`llm.call_error_classifier` for every non-mock spec; the mock branch is
unchanged. Wire-level coverage (a real connector-built `crewai.LLM`
over `httpx.MockTransport`) lives in
`tests/integration/test_provider_http_error_classification.py`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest

from engine import _crewai_adapter
from engine._crewai_adapter import make_build_llm_factory, make_llm_callable
from engine.dev_llm import EchoLLMDriver
from engine.framework import LLMRequest, Message
from errors import ModelBadRequest, ModelServerError, ModelTimeout, PlatformError
from llm import ProviderConfig, registry
from schemas import ModelSpec

if TYPE_CHECKING:
    from collections.abc import Iterator

    from crewai import LLM


class _RaisingLLM:
    """Stand-in for `crewai.LLM` whose `.call` raises `exc`."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def call(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        raise self._exc


class _Classifier:
    """Records what it was asked and returns a scripted answer."""

    def __init__(
        self,
        result: PlatformError | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.result = result
        self.raises = raises
        self.seen: list[BaseException] = []

    def __call__(self, exc: BaseException) -> PlatformError | None:
        self.seen.append(exc)
        if self.raises is not None:
            raise self.raises
        return self.result


def _request() -> LLMRequest:
    return LLMRequest(model="m", messages=[Message(role="user", content="hi")])


def _bad_request() -> ModelBadRequest:
    return ModelBadRequest(provider="p", model="m", status_code=400, reason="nope")


@pytest.fixture(autouse=True)
def _unset_configured_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the process-global bootstrap timeout (see test_llm_call_timeout)."""
    monkeypatch.setattr(
        _crewai_adapter,
        "_configured_default_timeout",
        _crewai_adapter._UNSET_CONFIGURED_DEFAULT,
    )


@pytest.mark.parametrize("timeout_env", ["", "30"], ids=["unbounded", "wait_for"])
async def test_classifier_hit_raises_classified_from_original(
    monkeypatch: pytest.MonkeyPatch, timeout_env: str
) -> None:
    """Both `to_thread` branches (env opt-out and `wait_for`) classify."""
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", timeout_env)
    original = RuntimeError("provider said 400")
    classified = _bad_request()
    classifier = _Classifier(classified)
    fn = make_llm_callable(cast("LLM", _RaisingLLM(original)), classify=classifier)

    with pytest.raises(ModelBadRequest) as exc_info:
        await fn(_request())

    assert exc_info.value is classified
    assert exc_info.value.__cause__ is original
    assert classifier.seen == [original]


async def test_classifier_miss_reraises_original_object() -> None:
    original = RuntimeError("unknown")
    fn = make_llm_callable(cast("LLM", _RaisingLLM(original)), classify=_Classifier())

    with pytest.raises(RuntimeError) as exc_info:
        await fn(_request())

    assert exc_info.value is original


async def test_no_classifier_reraises_original_object() -> None:
    original = ValueError("no hook bound")
    fn = make_llm_callable(cast("LLM", _RaisingLLM(original)))

    with pytest.raises(ValueError, match="no hook bound") as exc_info:
        await fn(_request())

    assert exc_info.value is original


async def test_classifier_bug_reraises_original_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = RuntimeError("provider failure")
    classifier = _Classifier(raises=KeyError("classifier bug"))
    fn = make_llm_callable(cast("LLM", _RaisingLLM(original)), classify=classifier)

    with (
        caplog.at_level(logging.WARNING, logger="engine.crewai_adapter"),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await fn(_request())

    assert exc_info.value is original
    records = [r for r in caplog.records if r.name == "engine.crewai_adapter"]
    assert len(records) == 1
    assert "classify_call_error raised" in records[0].getMessage()
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], KeyError)


async def test_platform_error_passes_through_without_classifying() -> None:
    typed = ModelServerError(provider="p", model="m", status_code=502)
    classifier = _Classifier(_bad_request())
    fn = make_llm_callable(cast("LLM", _RaisingLLM(typed)), classify=classifier)

    with pytest.raises(ModelServerError) as exc_info:
        await fn(_request())

    assert exc_info.value is typed
    assert classifier.seen == []


async def test_cancelled_error_passes_through_without_classifying() -> None:
    classifier = _Classifier(_bad_request())
    fn = make_llm_callable(
        cast("LLM", _RaisingLLM(asyncio.CancelledError())), classify=classifier
    )

    with pytest.raises(asyncio.CancelledError):
        await fn(_request())

    assert classifier.seen == []


class _WedgedLLM:
    """`.call` blocks until released — drives the adapter's own timeout."""

    def __init__(self) -> None:
        self._release = threading.Event()

    def call(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        self._release.wait(timeout=5)
        return "late"

    def release(self) -> None:
        self._release.set()


async def test_adapter_timeout_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter's `TimeoutError` → `ModelTimeout` stays first and is
    a PlatformError, so the classifier never sees it."""
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", "0.05")
    wedged = _WedgedLLM()
    classifier = _Classifier(_bad_request())
    fn = make_llm_callable(cast("LLM", wedged), classify=classifier)
    try:
        with pytest.raises(ModelTimeout) as exc_info:
            await fn(_request())
    finally:
        wedged.release()

    assert exc_info.value.where == "engine._crewai_adapter.make_llm_callable"
    assert classifier.seen == []


# ===========================================================================
# make_build_llm_factory binding
# ===========================================================================


async def test_factory_mock_spec_is_unchanged_echo_driver() -> None:
    factory = make_build_llm_factory(llm_providers_config={}, secrets=object())

    fn = await factory(ModelSpec(provider="mock", model="echo"), context=None)

    assert getattr(fn, "__self__", None).__class__ is EchoLLMDriver


_TYPE = "seam_bug145"
_CLASSIFY_CALLS: list[dict[str, Any]] = []


class _SeamConnector:
    """Registry connector whose LLM raises and whose hook records its bind."""

    type_name = _TYPE

    def auth_prompt_spec(self) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    async def validate_credentials(
        self, credentials: Any, base_url: str | None
    ) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    async def discover_models(
        self, credentials: Any, base_url: str | None
    ) -> Any:  # pragma: no cover — unused
        raise NotImplementedError

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Any = None,
        *,
        context: Any = None,
    ) -> Any:
        return _RaisingLLM(RuntimeError(f"raw failure from {model_id}"))

    def classify_call_error(
        self, exc: BaseException, *, provider_config: ProviderConfig, model_id: str
    ) -> PlatformError | None:
        _CLASSIFY_CALLS.append(
            {"exc": exc, "provider": provider_config.name, "model": model_id}
        )
        return ModelBadRequest(
            provider=provider_config.name,
            model=model_id,
            status_code=400,
            reason=str(exc),
        )


@pytest.fixture
def _seam_connector() -> Iterator[None]:
    _CLASSIFY_CALLS.clear()
    registry.register(_TYPE, _SeamConnector)
    try:
        yield
    finally:
        registry._REGISTRY.pop(_TYPE, None)
        _CLASSIFY_CALLS.clear()


@pytest.mark.usefixtures("_seam_connector")
async def test_factory_binds_connector_classifier_for_real_specs() -> None:
    cfg = ProviderConfig(
        name="seam_entry",
        type=_TYPE,
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )
    factory = make_build_llm_factory(
        llm_providers_config={"seam_entry": cfg},
        secrets=object(),
    )
    fn = await factory(ModelSpec(provider="seam_entry", model="m-7"), context=None)

    with pytest.raises(ModelBadRequest) as exc_info:
        await fn(_request())

    assert exc_info.value.detail["provider"] == "seam_entry"
    assert exc_info.value.detail["model"] == "m-7"
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert [(c["provider"], c["model"]) for c in _CLASSIFY_CALLS] == [
        ("seam_entry", "m-7")
    ]
