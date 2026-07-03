#!/usr/bin/env python3
"""Replay model.request events from a session JSONL against opencode.

Investigation tool for
``process/bug-chat-opencode-qwen-timeout-investigation.md`` Step 1.

Two modes:

- ``kolsch`` — re-issue each model.request payload through the same
  pipeline the chat surface uses: ``DotenvSecretsProvider`` +
  ``make_build_llm_factory`` → ``llm.build_llm`` → ``crewai.LLM`` →
  litellm → opencode. Measures end-to-end latency the user experiences.
- ``curl``   — bypass kolsch entirely. POST the model.request payload
  reshaped to OpenAI chat-completions form directly to the provider's
  ``/chat/completions`` endpoint via httpx. Measures raw provider
  latency, isolating "kolsch + crewai + litellm overhead" from
  upstream behaviour.

Invariants (per the bug doc Part 2.3):

- The original session JSONL is read-only — this script never opens
  the source path for write.
- Tool execution is NOT replayed; only the LLM call is. The model's
  tool_use response (if any) is recorded as bytes but never dispatched.
- Replays run sequentially; concurrent calls might mask rate limits.
- A 60-second per-call timeout caps each measurement (longer than the
  current 30s connector default, so timeouts are captured rather than
  masked).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make src/ importable so we can reuse the kolsch-side pipeline.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import httpx  # noqa: E402

DEFAULT_PER_CALL_TIMEOUT_SECONDS = 60.0
DEFAULT_WORKSPACE = Path.home() / "Documents" / "kolsch"

# Mutable per-run cap so probes can override the baseline 60s wrapper
# without touching the call sites. ``main`` sets this from ``--per-call-timeout``.
PER_CALL_TIMEOUT_SECONDS = DEFAULT_PER_CALL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------


@dataclass
class ModelRequestEvent:
    """One ``model.request`` event extracted from the session JSONL."""

    event_index: int  # 0-based line index in the source JSONL
    call_id: str
    model_provider: str
    model_id: str
    parameters: dict[str, Any]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]


def load_model_requests(session_path: Path) -> list[ModelRequestEvent]:
    """Read ``session_path`` and return the ordered ``model.request`` events.

    Reads-only; opens with mode ``"r"`` so an accidental write would
    raise. The session file is opened once and closed after the read
    so subsequent SHA-256 checks see no handle leak.
    """
    out: list[ModelRequestEvent] = []
    with session_path.open("r", encoding="utf-8") as f:
        for idx, raw in enumerate(f):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "model.request":
                continue
            data = event.get("data", {}) or {}
            model_blob = data.get("model", {}) or {}
            out.append(
                ModelRequestEvent(
                    event_index=idx,
                    call_id=data.get("call_id", ""),
                    model_provider=model_blob.get("provider", ""),
                    model_id=model_blob.get("model", ""),
                    parameters=dict(model_blob.get("parameters") or {}),
                    messages=list(data.get("messages") or []),
                    tools=list(data.get("tools") or []),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Target selection (--target all | first-N | last-N | a,b,c)
# ---------------------------------------------------------------------------


def select_targets(
    events: list[ModelRequestEvent],
    spec: str,
) -> list[ModelRequestEvent]:
    """Pick the subset of events to replay per ``--target``."""
    spec = spec.strip().lower()
    if spec in ("all", ""):
        return list(events)
    if spec.startswith("first-"):
        n = int(spec.removeprefix("first-"))
        return events[:n]
    if spec.startswith("last-"):
        n = int(spec.removeprefix("last-"))
        return events[-n:]
    # Comma-separated explicit indices into the model.request list.
    try:
        picks = [int(x.strip()) for x in spec.split(",") if x.strip()]
    except ValueError as exc:
        raise SystemExit(f"--target value not understood: {spec!r}") from exc
    return [events[i] for i in picks if 0 <= i < len(events)]


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    call_id: str
    event_index: int
    mode: str
    started_at: str
    latency_ms: int | None
    outcome: str  # "success" | "timeout" | "other_error"
    error_message: str | None
    response_kind: str | None  # "text" | "tool_calls" | None on failure


@dataclass
class RunResults:
    session_path: str
    session_sha256_before: str
    session_sha256_after: str
    mode: str
    target: str
    started_at: str
    finished_at: str
    per_call: list[CallResult] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "session_path": self.session_path,
            "session_sha256_before": self.session_sha256_before,
            "session_sha256_after": self.session_sha256_after,
            "mode": self.mode,
            "target": self.target,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self._summary(),
            "per_call": [r.__dict__ for r in self.per_call],
        }

    def _summary(self) -> dict[str, Any]:
        total = len(self.per_call)
        by_outcome: dict[str, int] = {}
        timeout_latencies: list[int] = []
        success_latencies: list[int] = []
        for r in self.per_call:
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            if r.outcome == "timeout" and r.latency_ms is not None:
                timeout_latencies.append(r.latency_ms)
            if r.outcome == "success" and r.latency_ms is not None:
                success_latencies.append(r.latency_ms)
        return {
            "total_calls": total,
            "by_outcome": by_outcome,
            "timeout_latency_ms": sorted(timeout_latencies),
            "success_latency_ms_p50": _pct(success_latencies, 0.5),
            "success_latency_ms_p90": _pct(success_latencies, 0.9),
            "success_latency_ms_max": (
                max(success_latencies) if success_latencies else None
            ),
        }


def _pct(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    sorted_v = sorted(values)
    k = max(0, min(len(sorted_v) - 1, round(pct * (len(sorted_v) - 1))))
    return sorted_v[k]


# ---------------------------------------------------------------------------
# kolsch-mode replay — go through the production build_llm pipeline
# ---------------------------------------------------------------------------


async def replay_kolsch(
    events: list[ModelRequestEvent],
    *,
    workspace: Path,
) -> list[CallResult]:
    """Re-issue each event through ``make_build_llm_factory`` → crewai.LLM.

    Loads the workspace's ``platform.yaml`` to build the same
    ProviderConfig the runtime would use; threads ``DotenvSecretsProvider``
    so the api_key comes from the workspace's .env.
    """
    # Lazy imports — pulled inside the function so plain ``--mode curl``
    # runs even if the kolsch source tree fails to import (it shouldn't,
    # but the principle stands).
    import yaml  # noqa: PLC0415

    # Register the opencode connector by importing llm.providers (side
    # effect: registry.register inside opencode.py runs).
    importlib.import_module("llm.providers")
    from engine._crewai_adapter import make_build_llm_factory  # noqa: PLC0415
    from engine.framework import LLMRequest, Message  # noqa: PLC0415
    from llm import ProviderConfig  # noqa: PLC0415
    from providers.secrets import DotenvSecretsProvider  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    platform_yaml = workspace / "platform.yaml"
    raw = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
    raw_secrets_cfg = (raw.get("secrets") or {}).get("config", {}) or {}
    secrets = DotenvSecretsProvider(root=workspace, config=raw_secrets_cfg)

    llm_providers_config: dict[str, ProviderConfig] = {}
    for name, entry in (raw.get("llm_providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        llm_providers_config[name] = ProviderConfig(
            name=name,
            type=entry.get("type", ""),
            api_key_secret=entry.get("api_key_secret"),
            base_url=entry.get("base_url"),
            timeout_seconds=entry.get("timeout_seconds"),
            extras={
                k: v
                for k, v in entry.items()
                if k
                not in {
                    "type",
                    "api_key_secret",
                    "base_url",
                    "timeout_seconds",
                    "models",
                }
            },
            models=list(entry.get("models") or []),
        )

    factory = make_build_llm_factory(
        llm_providers_config=llm_providers_config,
        secrets=secrets,
    )

    results: list[CallResult] = []
    for ev in events:
        spec = ModelSpec(
            provider=ev.model_provider,
            model=ev.model_id,
            parameters=ev.parameters,
        )
        try:
            call = await factory(spec)
        except Exception as exc:  # noqa: BLE001 — replay tool, capture verbatim
            results.append(
                CallResult(
                    call_id=ev.call_id,
                    event_index=ev.event_index,
                    mode="kolsch",
                    started_at=_now_iso(),
                    latency_ms=None,
                    outcome="other_error",
                    error_message=_truncate(f"{type(exc).__name__}: {exc}"),
                    response_kind=None,
                )
            )
            continue

        request = LLMRequest(
            messages=[Message(**m) for m in ev.messages],
            tools=ev.tools,
            model=ev.model_id,
            parameters=ev.parameters,
        )
        started = _now_iso()
        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                call(request),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            kind = "tool_calls" if response.tool_calls else "text"
            results.append(
                CallResult(
                    call_id=ev.call_id,
                    event_index=ev.event_index,
                    mode="kolsch",
                    started_at=started,
                    latency_ms=latency_ms,
                    outcome="success",
                    error_message=None,
                    response_kind=kind,
                )
            )
        except TimeoutError:
            latency_ms = int((time.monotonic() - t0) * 1000)
            results.append(
                CallResult(
                    call_id=ev.call_id,
                    event_index=ev.event_index,
                    mode="kolsch",
                    started_at=started,
                    latency_ms=latency_ms,
                    outcome="timeout",
                    error_message="asyncio.TimeoutError (per-call cap)",
                    response_kind=None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - t0) * 1000)
            msg = _truncate(f"{type(exc).__name__}: {exc}")
            outcome = (
                "timeout"
                if "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
                else "other_error"
            )
            results.append(
                CallResult(
                    call_id=ev.call_id,
                    event_index=ev.event_index,
                    mode="kolsch",
                    started_at=started,
                    latency_ms=latency_ms,
                    outcome=outcome,
                    error_message=msg,
                    response_kind=None,
                )
            )

        _log_progress("kolsch", ev, results[-1])
    return results


# ---------------------------------------------------------------------------
# curl-mode replay — direct httpx POST to {base_url}/chat/completions
# ---------------------------------------------------------------------------


async def replay_curl(
    events: list[ModelRequestEvent],
    *,
    workspace: Path,
) -> list[CallResult]:
    """POST OpenAI-shape chat-completions JSON directly to opencode."""
    import yaml  # noqa: PLC0415

    raw = (
        yaml.safe_load((workspace / "platform.yaml").read_text(encoding="utf-8")) or {}
    )
    opencode_cfg = (raw.get("llm_providers") or {}).get("opencode") or {}
    base_url = (opencode_cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise SystemExit("opencode.base_url missing from platform.yaml")
    api_key_secret = opencode_cfg.get("api_key_secret") or "OPENCODE_API_KEY"

    # Resolve api key — dotenv only (matches DotenvSecretsProvider behavior).
    dotenv_path = workspace / (
        (raw.get("secrets") or {}).get("config", {}).get("path", ".env")
    )
    api_key = _read_dotenv_value(dotenv_path, api_key_secret)
    if not api_key:
        raise SystemExit(f"secret {api_key_secret!r} not found in {dotenv_path}")

    results: list[CallResult] = []
    async with httpx.AsyncClient(timeout=PER_CALL_TIMEOUT_SECONDS) as client:
        for ev in events:
            body = _build_openai_body(ev)
            started = _now_iso()
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                if resp.status_code == 200:
                    payload = resp.json()
                    kind = _classify_openai_response(payload)
                    results.append(
                        CallResult(
                            call_id=ev.call_id,
                            event_index=ev.event_index,
                            mode="curl",
                            started_at=started,
                            latency_ms=latency_ms,
                            outcome="success",
                            error_message=None,
                            response_kind=kind,
                        )
                    )
                else:
                    snippet = _truncate(resp.text)
                    results.append(
                        CallResult(
                            call_id=ev.call_id,
                            event_index=ev.event_index,
                            mode="curl",
                            started_at=started,
                            latency_ms=latency_ms,
                            outcome="other_error",
                            error_message=f"HTTP {resp.status_code}: {snippet}",
                            response_kind=None,
                        )
                    )
            except (TimeoutError, httpx.TimeoutException) as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                results.append(
                    CallResult(
                        call_id=ev.call_id,
                        event_index=ev.event_index,
                        mode="curl",
                        started_at=started,
                        latency_ms=latency_ms,
                        outcome="timeout",
                        error_message=_truncate(f"{type(exc).__name__}: {exc}"),
                        response_kind=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = int((time.monotonic() - t0) * 1000)
                results.append(
                    CallResult(
                        call_id=ev.call_id,
                        event_index=ev.event_index,
                        mode="curl",
                        started_at=started,
                        latency_ms=latency_ms,
                        outcome="other_error",
                        error_message=_truncate(f"{type(exc).__name__}: {exc}"),
                        response_kind=None,
                    )
                )
            _log_progress("curl", ev, results[-1])
    return results


def _build_openai_body(ev: ModelRequestEvent) -> dict[str, Any]:
    """Reshape one model.request event into an OpenAI chat-completions body.

    Messages: drop ``None`` fields so the wire is clean (some providers
    reject ``tool_call_id: null``). Tools: wrap kolsch's
    ``{name, schema}`` into OpenAI's
    ``{"type": "function", "function": {name, parameters}}``.
    """
    messages: list[dict[str, Any]] = []
    for m in ev.messages:
        msg: dict[str, Any] = {"role": m.get("role"), "content": m.get("content") or ""}
        if m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
        if m.get("tool_calls"):
            # kolsch's session events store tool_calls in the engine's flat
            # shape {id, name, arguments(dict)} (see
            # engine/_crewai_adapter._engine_tool_call_to_openai). OpenAI
            # chat-completions expects {id, type:"function", function:{name,
            # arguments(JSON-string)}}. Without this reshape, opencode
            # returns HTTP 400 "Field required: ...tool_calls.0.function"
            # on every multi-turn tool-using exchange.
            msg["tool_calls"] = [_to_openai_tool_call(tc) for tc in m["tool_calls"]]
        if m.get("name"):
            msg["name"] = m["name"]
        messages.append(msg)

    tools: list[dict[str, Any]] = []
    for t in ev.tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "parameters": t.get("schema") or {},
                },
            }
        )

    body: dict[str, Any] = {
        "model": ev.model_id,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    return body


def _to_openai_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
    """Convert kolsch's flat tool_call shape into OpenAI's nested form."""
    # Already in spec shape — pass through with `type` filled in.
    if isinstance(tc, dict) and "function" in tc:
        out = dict(tc)
        out.setdefault("type", "function")
        return out
    args = tc.get("arguments") if isinstance(tc, dict) else None
    if isinstance(args, dict):
        args_str = json.dumps(args)
    elif isinstance(args, str):
        args_str = args
    else:
        args_str = "{}"
    return {
        "id": tc.get("id", ""),
        "type": "function",
        "function": {
            "name": tc.get("name", ""),
            "arguments": args_str,
        },
    }


def _classify_openai_response(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return "text"
    message = (choices[0] or {}).get("message", {}) or {}
    if message.get("tool_calls"):
        return "tool_calls"
    return "text"


def _read_dotenv_value(path: Path, name: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truncate(s: str, limit: int = 500) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _log_progress(mode: str, ev: ModelRequestEvent, r: CallResult) -> None:
    print(
        f"[{mode}] idx={ev.event_index:>3} call={ev.call_id[:14]} "
        f"outcome={r.outcome:<12} latency_ms={r.latency_ms}",
        file=sys.stderr,
        flush=True,
    )


def _sha256(path: Path) -> str:
    import hashlib  # noqa: PLC0415

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session",
        type=Path,
        required=True,
        help="Path to session JSONL (read-only).",
    )
    parser.add_argument(
        "--mode",
        choices=["kolsch", "curl"],
        required=True,
    )
    parser.add_argument(
        "--target",
        default="all",
        help="all | first-N | last-N | comma-separated indices (default: all)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write results JSON.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help=f"Workspace root containing platform.yaml + .env (default: {DEFAULT_WORKSPACE}).",
    )
    parser.add_argument(
        "--per-call-timeout",
        type=float,
        default=DEFAULT_PER_CALL_TIMEOUT_SECONDS,
        help=(
            "Outer per-call timeout in seconds. Default 60. Bump above the "
            "connector's timeout_seconds so probes can measure how long calls "
            "actually take when allowed to run."
        ),
    )
    args = parser.parse_args()

    global PER_CALL_TIMEOUT_SECONDS  # noqa: PLW0603 — single-process script
    PER_CALL_TIMEOUT_SECONDS = args.per_call_timeout

    if not args.session.is_file():
        parser.error(f"--session not found: {args.session}")

    sha_before = _sha256(args.session)
    events = load_model_requests(args.session)
    print(
        f"loaded {len(events)} model.request events from {args.session.name}",
        file=sys.stderr,
    )
    targets = select_targets(events, args.target)
    print(
        f"replaying {len(targets)} event(s) in mode={args.mode} "
        f"(target={args.target!r})",
        file=sys.stderr,
    )

    started = _now_iso()
    if args.mode == "kolsch":
        per_call = asyncio.run(replay_kolsch(targets, workspace=args.workspace))
    else:
        per_call = asyncio.run(replay_curl(targets, workspace=args.workspace))
    finished = _now_iso()

    sha_after = _sha256(args.session)
    if sha_before != sha_after:
        print("ERROR: session file mutated during replay!", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    run = RunResults(
        session_path=str(args.session),
        session_sha256_before=sha_before,
        session_sha256_after=sha_after,
        mode=args.mode,
        target=args.target,
        started_at=started,
        finished_at=finished,
        per_call=per_call,
    )
    args.output.write_text(json.dumps(run.to_jsonable(), indent=2), encoding="utf-8")
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Quiet down crewai/litellm/instructor noise during the run; their
    # default stderr chatter overwhelms the per-call progress lines.
    os.environ.setdefault("LITELLM_LOG", "WARNING")
    os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")
    raise SystemExit(main())
