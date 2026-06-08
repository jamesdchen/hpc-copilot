"""The raw model-call structured-output seam — floor, repair loop, registry."""

from __future__ import annotations

import pydantic
import pytest

import hpc_agent._kernel.lifecycle.structured as structured_mod
from hpc_agent import errors
from hpc_agent._kernel.contract.json_extract import last_json_object
from hpc_agent._kernel.lifecycle.structured import (
    ChatMessage,
    ScriptedModel,
    get_model,
    register_model,
    structured,
)


class _Answer(pydantic.BaseModel):
    """A tiny target shape: a label and a count."""

    label: str
    count: int


def _ask() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="give me an answer")]


# ── floor ────────────────────────────────────────────────────────────────


def test_floor_parses_a_clean_response_first_try() -> None:
    model = ScriptedModel(['{"label": "ok", "count": 3}'])
    result = structured(model, _Answer, _ask())
    assert result == _Answer(label="ok", count=3)


def test_floor_recovers_from_a_chatter_prefixed_response() -> None:
    # The model wraps the object in prose; last_json_object still extracts it.
    model = ScriptedModel(['Here you go:\n{"label": "ok", "count": 1}'])
    result = structured(model, _Answer, _ask())
    assert result.count == 1


# ── repair loop ────────────────────────────────────────────────────────────


def test_repair_loop_recovers_after_one_malformed_response() -> None:
    model = ScriptedModel(
        [
            "no json here at all",
            '{"label": "fixed", "count": 7}',
        ]
    )
    result = structured(model, _Answer, _ask())
    assert result == _Answer(label="fixed", count=7)


def test_repair_loop_recovers_after_two_validation_failures() -> None:
    model = ScriptedModel(
        [
            '{"label": "a"}',  # missing count
            '{"label": "b", "count": "notanint"}',  # bad type
            '{"label": "c", "count": 9}',  # valid
        ]
    )
    result = structured(model, _Answer, _ask(), max_repairs=2)
    assert result == _Answer(label="c", count=9)


def test_repair_turn_feeds_the_error_back_to_the_model() -> None:
    # The second complete() call must see the appended assistant reply +
    # a user repair turn carrying the validation error text.
    model = ScriptedModel(['{"label": "a"}', '{"label": "a", "count": 2}'])
    structured(model, _Answer, _ask())
    # complete() was called twice; capture the turns on the second call.
    captured: list[list[ChatMessage]] = []

    class _Recorder(ScriptedModel):
        def complete(self, messages, *, schema=None):  # type: ignore[override]
            captured.append(list(messages))
            return super().complete(messages, schema=schema)

    recorder = _Recorder(['{"label": "a"}', '{"label": "a", "count": 2}'])
    structured(recorder, _Answer, _ask())
    second_call = captured[1]
    assert second_call[-2].role == "assistant"
    assert second_call[-2].content == '{"label": "a"}'
    assert second_call[-1].role == "user"
    assert "did not validate" in second_call[-1].content
    assert "count" in second_call[-1].content  # the validation error mentions it


def test_caller_messages_are_not_mutated() -> None:
    messages = _ask()
    model = ScriptedModel(["nope", '{"label": "ok", "count": 0}'])
    structured(model, _Answer, messages)
    assert messages == [ChatMessage(role="user", content="give me an answer")]


# ── budget exhaustion ──────────────────────────────────────────────────────


def test_budget_exhaustion_raises_structured_output_error() -> None:
    model = ScriptedModel(
        ['{"bad": 1}', '{"bad": 2}', '{"bad": 3}'],  # max_repairs=2 → 3 attempts
    )
    with pytest.raises(errors.StructuredOutputError) as excinfo:
        structured(model, _Answer, _ask(), max_repairs=2)
    msg = str(excinfo.value)
    assert "_Answer" in msg
    assert "3 attempt" in msg
    assert "last raw output" in msg


def test_structured_output_error_is_internal_and_retry_safe() -> None:
    err = errors.StructuredOutputError("boom")
    assert err.error_code == "internal"
    assert err.retry_safe is True
    assert err.category == "internal"
    assert isinstance(err, errors.HpcError)


# ── accelerator path (schema honoured) ─────────────────────────────────────


def test_schema_offer_lets_an_adapter_conform_first_try() -> None:
    # schema_response stands in for a native-strict adapter: when a schema
    # is offered it returns conformant output on the first call, no repair.
    model = ScriptedModel(
        responses=[],  # nothing scripted: any repair would exhaust + AssertionError
        schema_response='{"label": "native", "count": 42}',
    )
    result = structured(model, _Answer, _ask())
    assert result == _Answer(label="native", count=42)
    # The funnel actually offered the Pydantic-derived schema.
    assert model.schemas_seen[0] == _Answer.model_json_schema()


# ── post_validate hook ─────────────────────────────────────────────────────


def _reject_negative(instance: _Answer) -> None:
    if instance.count < 0:
        raise ValueError(f"count must be non-negative, got {instance.count}")


def test_post_validate_rejection_feeds_back_and_recovers() -> None:
    model = ScriptedModel(
        [
            '{"label": "x", "count": -1}',  # schema-valid but post_validate rejects
            '{"label": "x", "count": 5}',  # passes both
        ]
    )
    result = structured(model, _Answer, _ask(), post_validate=_reject_negative)
    assert result.count == 5


def test_post_validate_rejection_can_exhaust_the_budget() -> None:
    model = ScriptedModel(
        [
            '{"label": "x", "count": -1}',
            '{"label": "x", "count": -2}',
        ]
    )
    with pytest.raises(errors.StructuredOutputError, match="non-negative"):
        structured(model, _Answer, _ask(), max_repairs=1, post_validate=_reject_negative)


# ── registry ───────────────────────────────────────────────────────────────


def test_get_model_with_nothing_selected_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_AGENT_MODEL", raising=False)
    with pytest.raises(errors.SpecInvalid, match="no chat model selected"):
        get_model()


def test_get_model_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_AGENT_MODEL", raising=False)
    with pytest.raises(errors.SpecInvalid, match="unknown chat model"):
        get_model("does-not-exist")


def test_register_and_get_model_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured_mod, "_MODELS", {}, raising=True)
    register_model("scripted", lambda: ScriptedModel(["{}"]))
    model = get_model("scripted")
    assert model.name == "scripted"


def test_get_model_env_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured_mod, "_MODELS", {}, raising=True)
    register_model("scripted", lambda: ScriptedModel(["{}"]))
    monkeypatch.setenv("HPC_AGENT_MODEL", "scripted")
    assert get_model().name == "scripted"
    # Explicit name beats the env var.
    monkeypatch.setenv("HPC_AGENT_MODEL", "bogus")
    with pytest.raises(errors.SpecInvalid, match="unknown chat model"):
        get_model()


# ── extractor regression after the move ────────────────────────────────────


def test_last_json_object_still_handles_chatter_prefix() -> None:
    # Regression for lifting _last_json_object → contract.json_extract.
    text = 'Here is my report:\n{"result": {}, "decisions": [], "anomalies": "x"}'
    obj = last_json_object(text)
    assert obj == {"result": {}, "decisions": [], "anomalies": "x"}


def test_last_json_object_returns_none_without_an_object() -> None:
    assert last_json_object("just prose, no object at all") is None
