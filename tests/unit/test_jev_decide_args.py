"""Unit tests for the ``jev_decide`` tool's argument schema + wire body.

spec-136 Test 1 (AC 1-3). The tool file at
``src/cli/_starter_bundle/kolsch/tools/jev_decide.py`` is a
filesystem template (no ``__init__.py`` up the tree) and is loaded
here via ``importlib.util``, the same pattern ``test_fetch_url.py``
uses. No network: every rejection here happens before the body runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from types import ModuleType

    from providers import AuthorizationProvider, SandboxProvider, SecretsProvider

from errors import ToolInvalidArguments
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool


def _load_jev_decide_module() -> ModuleType:
    """Load the starter-bundle template as an importable module.

    Shares the module-name key with ``tests/component/test_jev_decide.py``
    so both files exercise the same module object in a full run.
    """
    module_name = "_test_kolsch_starter_bundle_jev_decide"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    repo_root = Path(__file__).resolve().parents[2]
    tool_path = (
        repo_root
        / "src"
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "jev_decide.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_jev_decide_module()
JevDecideArgs = _MODULE.JevDecideArgs
_DEFN: ToolDefinition = _MODULE.jev_decide._tool_definition


def _choice(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": "Which team owns this?",
        "criteria": {f"opt_{i}": f"option {i}" for i in range(n)},
        **extra,
    }


def _score(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "score",
        "instructions": "How urgent is this?",
        "criteria": [f"level {i}" for i in range(n)],
        **extra,
    }


def _noul(**extra: Any) -> dict[str, Any]:
    return {"type": "noul", "instructions": "Is this about billing?", **extra}


def _args(questions: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"state": "I was charged twice.", "questions": questions, **extra}


# ---------------------------------------------------------------------------
# Accepted shapes
# ---------------------------------------------------------------------------


def test_valid_choice_score_noul_accepted() -> None:
    args = JevDecideArgs.model_validate(
        _args(
            {
                "team": _choice(3, fail_below=0.6),
                "urgency": _score(10, fail_below=1.5),
                "billing": _noul(fail_below=0.5),
            }
        )
    )
    assert set(args.questions) == {"team", "urgency", "billing"}
    assert args.questions["team"].type == "choice"
    assert args.questions["urgency"].type == "score"
    assert args.questions["billing"].type == "noul"


def test_state_accepts_object_and_array() -> None:
    for state in ({"subject": "Refund", "body": "charged twice"}, ["a", {"b": 1}]):
        args = JevDecideArgs.model_validate(
            {"state": state, "questions": {"q": _noul()}}
        )
        assert args.state == state


def test_choice_option_description_may_be_null() -> None:
    args = JevDecideArgs.model_validate(
        _args(
            {
                "c": {
                    "type": "choice",
                    "instructions": "Pick",
                    "criteria": {"a": None, "b": "B"},
                }
            }
        )
    )
    assert args.questions["c"].criteria == {"a": None, "b": "B"}


def test_choice_and_score_bounds_inclusive() -> None:
    JevDecideArgs.model_validate(_args({"c1": _choice(1), "c255": _choice(255)}))
    JevDecideArgs.model_validate(_args({"s1": _score(1), "s10": _score(10)}))


def test_default_model_is_pinned_jev_1_14() -> None:
    args = JevDecideArgs.model_validate(_args({"q": _noul()}))
    assert args.model == "typesafe/jev-1.14"


# ---------------------------------------------------------------------------
# Rejected shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        (
            "box type",
            _args({"q": {"type": "box", "instructions": "Locate the cat"}}),
        ),
        ("unknown type", _args({"q": {"type": "maybe", "instructions": "?"}})),
        ("empty questions", _args({})),
        ("zero choice options", _args({"q": _choice(0)})),
        ("256 choice options", _args({"q": _choice(256)})),
        ("zero score levels", _args({"q": _score(0)})),
        ("11 score levels", _args({"q": _score(11)})),
        ("choice fail_below > 1", _args({"q": _choice(2, fail_below=1.01)})),
        ("choice fail_below < 0", _args({"q": _choice(2, fail_below=-0.1)})),
        ("noul fail_below > 1", _args({"q": _noul(fail_below=1.5)})),
        ("noul fail_below < 0", _args({"q": _noul(fail_below=-0.01)})),
        ("score fail_below < 0", _args({"q": _score(3, fail_below=-1)})),
        (
            "noul with choice-style criteria",
            _args({"q": _noul(criteria={"yes": "y", "no": "n"})}),
        ),
        ("unknown question field", _args({"q": _noul(extra_field=1)})),
        ("unknown top-level field", _args({"q": _noul()}, temperature=0.2)),
        ("missing state", {"questions": {"q": _noul()}}),
    ],
)
def test_invalid_args_rejected(label: str, payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JevDecideArgs.model_validate(payload)


class _NeverCalled:
    """Provider stand-in that fails the test if dispatch reaches it."""

    def __getattr__(self, name: str) -> Any:
        msg = f"dispatch reached provider.{name} — validation should fail first"
        raise AssertionError(msg)


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.ag_test",
        secrets={},
    )


@pytest.mark.parametrize(
    "payload",
    [
        _args({"q": {"type": "box", "instructions": "Locate the cat"}}),
        _args({}),
        _args({"q": _choice(256)}),
        _args({"q": _score(11)}),
        _args({"q": _noul(fail_below=2)}),
    ],
    ids=["box", "empty", "256-options", "11-levels", "fail_below-range"],
)
async def test_dispatch_rejects_invalid_args_as_tool_invalid_arguments(
    payload: dict[str, Any],
) -> None:
    """Rejections surface as ``tool.invalid_arguments`` before authz/secrets/network."""
    never = _NeverCalled()
    with pytest.raises(ToolInvalidArguments) as exc_info:
        await dispatch_tool(
            defn=_DEFN,
            args=payload,
            context=_ctx(),
            authz=cast("AuthorizationProvider", never),
            secrets=cast("SecretsProvider", never),
            sandbox=cast("SandboxProvider", never),
        )
    assert exc_info.value.code == "tool.invalid_arguments"
    assert exc_info.value.detail["tool_name"] == "jev_decide"
    # The envelope must serialise: it lands in a `tool.failed` event.
    # (A plain ValueError from a validator would put an exception
    # object in the error ctx — found in the M1 live check.)
    json.dumps(exc_info.value.to_dict())


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_advertised_schema_is_llm_provider_friendly() -> None:
    """The args JSON Schema avoids keywords OpenAI-backed callers reject.

    Found in the M1 live check: OpenAI 400s a tool whose parameters use
    ``minProperties`` / ``maxProperties`` or an array whose ``items``
    has no ``type``.
    """
    schema = JevDecideArgs.model_json_schema()
    for node in _walk(schema):
        assert "minProperties" not in node
        assert "maxProperties" not in node
        if node.get("type") == "array":
            items = node.get("items")
            assert items, node
            assert "type" in items or "anyOf" in items, node
            for branch in items.get("anyOf", []):
                assert "type" in branch, node


# ---------------------------------------------------------------------------
# Wire body + profiles
# ---------------------------------------------------------------------------


def test_wire_body_strips_fail_below_and_is_exactly_model_state_questions() -> None:
    args = JevDecideArgs.model_validate(
        _args(
            {
                "team": _choice(2, fail_below=0.6),
                "urgency": _score(3, fail_below=1.0),
                "billing": _noul(fail_below=0.5),
            }
        )
    )
    body = _MODULE._wire_body(args, _MODULE._PROFILES["openrouter"])
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "typesafe/jev-1.14"
    assert body["state"] == "I was charged twice."
    assert "fail_below" not in json.dumps(body)
    assert body["questions"]["billing"] == {
        "type": "noul",
        "instructions": "Is this about billing?",
    }
    assert body["questions"]["team"]["criteria"] == {
        "opt_0": "option 0",
        "opt_1": "option 1",
    }


def test_wire_body_preserves_null_option_and_noul_criteria_aliases() -> None:
    args = JevDecideArgs.model_validate(
        _args(
            {
                "c": {
                    "type": "choice",
                    "instructions": "Pick",
                    "criteria": {"a": None},
                },
                "n": _noul(criteria={"true": "spam", "false": "legit"}),
            }
        )
    )
    body = _MODULE._wire_body(args, _MODULE._PROFILES["openrouter"])
    assert body["questions"]["c"]["criteria"] == {"a": None}
    assert body["questions"]["n"]["criteria"] == {"true": "spam", "false": "legit"}


def test_wire_body_drops_explicit_null_noul_criteria() -> None:
    args = JevDecideArgs.model_validate(_args({"q": _noul(criteria=None)}))
    body = _MODULE._wire_body(args, _MODULE._PROFILES["openrouter"])
    assert body["questions"]["q"] == {
        "type": "noul",
        "instructions": "Is this about billing?",
    }


def test_explicit_model_is_sent_verbatim() -> None:
    args = JevDecideArgs.model_validate(
        _args({"q": _noul()}, model="typesafe/jev-1.13")
    )
    body = _MODULE._wire_body(args, _MODULE._PROFILES["openrouter"])
    assert body["model"] == "typesafe/jev-1.13"


def test_profiles_build_identical_bodies_apart_from_model() -> None:
    """AC 3: openrouter vs typesafe bodies are byte-identical except ``model``."""
    args = JevDecideArgs.model_validate(
        _args(
            {
                "team": _choice(3, fail_below=0.6),
                "urgency": _score(4),
                "billing": _noul(criteria={"true": "about money"}),
            }
        )
    )
    openrouter = _MODULE._wire_body(args, _MODULE._PROFILES["openrouter"])
    typesafe = _MODULE._wire_body(args, _MODULE._PROFILES["typesafe"])
    assert openrouter["model"] == "typesafe/jev-1.14"
    assert typesafe["model"] == "jev-1.14.0"
    strip = {k: v for k, v in openrouter.items() if k != "model"}
    strip_ts = {k: v for k, v in typesafe.items() if k != "model"}
    assert (
        json.dumps(strip, sort_keys=False).encode()
        == json.dumps(strip_ts, sort_keys=False).encode()
    )


def test_profile_seam_shape() -> None:
    """AC 3: one selectable profile (openrouter); typesafe is data only."""
    profiles = _MODULE._PROFILES
    assert set(profiles) == {"openrouter", "typesafe"}
    assert profiles["openrouter"].base_url == "https://openrouter.ai/api"
    assert profiles["openrouter"].secret_name == "OPENROUTER_API_KEY"
    assert profiles["openrouter"].selectable is True
    assert profiles["typesafe"].base_url == "https://api.typesafe.ai"
    assert profiles["typesafe"].secret_name == "TYPESAFE_API_KEY"
    assert profiles["typesafe"].selectable is False
    assert _MODULE._ACTIVE_PROFILE == "openrouter"
    assert [p.name for p in profiles.values() if p.selectable] == ["openrouter"]


def test_tool_definition_declares_openrouter_secret() -> None:
    assert _DEFN.name == "jev_decide"
    assert _DEFN.secrets_needed == ["OPENROUTER_API_KEY"]
    assert _DEFN.args_schema is JevDecideArgs
