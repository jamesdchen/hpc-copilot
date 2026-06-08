"""The OpenAI-compatible ChatModel adapter — strict decode + env config.

The HTTP layer is always mocked (``urllib.request.urlopen``); these tests
never touch the network. They cover the three response-format modes, the
``_to_strict_schema`` transform, env resolution, the keyless-localhost
rule, registry wiring, and transport-error propagation through the floor.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pydantic
import pytest

import hpc_agent._kernel.lifecycle.structured as structured_mod
from hpc_agent import errors
from hpc_agent._kernel.lifecycle.chat_models import openai_compat
from hpc_agent._kernel.lifecycle.chat_models.openai_compat import (
    OpenAICompatModel,
    _to_strict_schema,
)
from hpc_agent._kernel.lifecycle.structured import ChatMessage, get_model, structured

# ── HTTP mocking scaffold ──────────────────────────────────────────────────


class _FakeResponse:
    """Minimal context-manager standing in for an ``http.client`` response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _envelope(content: str) -> dict[str, Any]:
    """A canned OpenAI ``/chat/completions`` envelope carrying *content*."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> list[Any]:
    """Patch ``urlopen`` to return canned *responses* and record requests.

    Each item in *responses* is either a payload dict (wrapped in a
    ``_FakeResponse``) or an exception instance (raised). Returns the list the
    captured :class:`urllib.request.Request` objects are appended to.
    """
    captured: list[Any] = []
    queue = list(responses)

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        captured.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", fake_urlopen)
    return captured


def _body(request: Any) -> dict[str, Any]:
    """Decode a captured request's JSON body."""
    return json.loads(request.data.decode("utf-8"))


class _Nested(pydantic.BaseModel):
    """A nested target shape so the strict transform has objects to recurse."""

    inner_label: str
    inner_count: int = 0  # defaulted → Pydantic omits it from `required`


class _Answer(pydantic.BaseModel):
    label: str
    count: int = 0  # defaulted → not in Pydantic's `required`
    nested: _Nested
    tags: list[_Nested] = []


def _model(**kwargs: Any) -> OpenAICompatModel:
    base = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model": "test-model",
    }
    base.update(kwargs)
    return OpenAICompatModel(**base)  # type: ignore[arg-type]


# ── strict json_schema accelerator ─────────────────────────────────────────


def test_json_schema_mode_sends_strict_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope('{"ok": true}')])
    model = _model(response_format_mode="json_schema")
    schema = _Answer.model_json_schema()
    model.complete([ChatMessage(role="user", content="hi")], schema=schema)

    body = _body(captured[0])
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"]  # a non-empty schema name
    sent = rf["json_schema"]["schema"]
    # Top-level object is strictified.
    assert sent["additionalProperties"] is False
    assert set(sent["required"]) == set(sent["properties"].keys())
    # `count` (defaulted, absent from Pydantic's required) is now required.
    assert "count" in sent["required"]
    # Nested $defs object is strictified too.
    defs = sent["$defs"]["_Nested"]
    assert defs["additionalProperties"] is False
    assert "inner_count" in defs["required"]


def test_json_schema_mode_without_schema_sends_no_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    _model(response_format_mode="json_schema").complete(
        [ChatMessage(role="user", content="hi")], schema=None
    )
    assert "response_format" not in _body(captured[0])


# ── _to_strict_schema unit ─────────────────────────────────────────────────


def test_to_strict_schema_recurses_all_object_nodes() -> None:
    schema = {
        "type": "object",
        "title": "Outer",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},  # defaulted in source → not in `required`
            "child": {"$ref": "#/$defs/Child"},
            "items_field": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            },
        },
        "required": ["a"],
        "$defs": {
            "Child": {
                "type": "object",
                "properties": {"y": {"type": "string"}},
            }
        },
    }
    strict = _to_strict_schema(schema)

    # Top-level object: all properties required, no extras.
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"a", "b", "child", "items_field"}
    # Object inside `items`.
    items_obj = strict["properties"]["items_field"]["items"]
    assert items_obj["additionalProperties"] is False
    assert items_obj["required"] == ["x"]
    # Object inside `$defs`.
    child = strict["$defs"]["Child"]
    assert child["additionalProperties"] is False
    assert child["required"] == ["y"]
    # $ref is preserved, not inlined.
    assert strict["properties"]["child"] == {"$ref": "#/$defs/Child"}


def test_to_strict_schema_does_not_mutate_input() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    _to_strict_schema(schema)
    assert schema["required"] == []  # original untouched
    assert "additionalProperties" not in schema


def test_to_strict_schema_walks_anyof_branches() -> None:
    schema = {
        "type": "object",
        "properties": {
            "u": {
                "anyOf": [
                    {"type": "object", "properties": {"k": {"type": "string"}}},
                    {"type": "null"},
                ]
            }
        },
    }
    strict = _to_strict_schema(schema)
    branch = strict["properties"]["u"]["anyOf"][0]
    assert branch["additionalProperties"] is False
    assert branch["required"] == ["k"]


def test_to_strict_schema_drops_unsupported_format() -> None:
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string", "format": "email"}},
    }
    strict = _to_strict_schema(schema)
    assert "format" not in strict["properties"]["email"]


def test_to_strict_schema_promotes_rootmodel_ref_root() -> None:
    # A RootModel wrapping a model emits a bare {"$ref": ...} root (no object),
    # which strict mode rejects. _to_strict_schema must inline it to an object.
    class Inner(pydantic.BaseModel):
        a: int
        b: str = "x"

    class Wrap(pydantic.RootModel[Inner]):
        pass

    schema = Wrap.model_json_schema()
    assert "$ref" in schema and "properties" not in schema  # precondition: bare-ref root
    strict = _to_strict_schema(schema)
    assert strict["type"] == "object"
    assert strict["additionalProperties"] is False
    assert sorted(strict["required"]) == ["a", "b"]  # all properties forced required
    assert "$defs" in strict  # carried forward so inner refs still resolve


def test_to_strict_schema_promotes_allof_ref_root() -> None:
    # Some shapes wrap the root ref in a single-element allOf; inline that too.
    schema = {
        "$defs": {"X": {"type": "object", "properties": {"a": {"type": "integer"}}}},
        "allOf": [{"$ref": "#/$defs/X"}],
        "title": "Wrapper",
    }
    strict = _to_strict_schema(schema)
    assert strict["type"] == "object"
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["a"]


def test_to_strict_schema_rejects_non_object_root() -> None:
    # A RootModel over a non-object (list / scalar) cannot be a strict object
    # root — fail fast with guidance instead of POSTing a payload the API 400s.
    class Items(pydantic.RootModel[list[int]]):
        pass

    with pytest.raises(errors.SpecInvalid, match="object-rooted"):
        _to_strict_schema(Items.model_json_schema())


# ── json_object mode ───────────────────────────────────────────────────────


def test_json_object_mode_sets_type_and_injects_json_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    model = _model(response_format_mode="json_object")
    schema = _Answer.model_json_schema()
    model.complete([ChatMessage(role="user", content="hi")], schema=schema)

    body = _body(captured[0])
    assert body["response_format"] == {"type": "json_object"}
    # A system hint that mentions "json" is prepended.
    first = body["messages"][0]
    assert first["role"] == "system"
    assert "json" in first["content"].lower()
    # The original user turn survives after the hint.
    assert body["messages"][-1] == {"role": "user", "content": "hi"}


# ── none mode / no schema ──────────────────────────────────────────────────


def test_none_mode_sends_no_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    model = _model(response_format_mode="none")
    model.complete([ChatMessage(role="user", content="hi")], schema=_Answer.model_json_schema())
    body = _body(captured[0])
    assert "response_format" not in body


# ── auth header ────────────────────────────────────────────────────────────


def test_auth_header_present_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    _model(api_key="sk-secret").complete([ChatMessage(role="user", content="hi")])
    # urllib title-cases header keys.
    assert captured[0].get_header("Authorization") == "Bearer sk-secret"


def test_auth_header_omitted_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    _model(api_key=None, base_url="http://localhost:8000/v1").complete(
        [ChatMessage(role="user", content="hi")]
    )
    assert captured[0].get_header("Authorization") is None


# ── happy path ─────────────────────────────────────────────────────────────


def test_complete_returns_inner_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_urlopen(monkeypatch, [_envelope('{"label": "hi", "count": 2}')])
    out = _model().complete([ChatMessage(role="user", content="hi")])
    assert out == '{"label": "hi", "count": 2}'


def test_request_targets_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_urlopen(monkeypatch, [_envelope("{}")])
    _model(base_url="https://api.example.com/v1/").complete(
        [ChatMessage(role="user", content="hi")]
    )
    # Trailing slash on base_url is normalized (no doubled slash).
    assert captured[0].full_url == "https://api.example.com/v1/chat/completions"


# ── end-to-end through structured() (adapter + floor compose) ──────────────


def test_structured_repair_loop_recovers_through_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First completion is valid JSON but the WRONG shape (missing required
    # fields); the floor feeds the error back and the second corrects it.
    bad = json.dumps({"label": "x"})  # missing `nested`
    good = json.dumps({"label": "x", "count": 1, "nested": {"inner_label": "n"}, "tags": []})
    _capture_urlopen(monkeypatch, [_envelope(bad), _envelope(good)])
    model = _model(response_format_mode="none")  # floor carries shape
    result = structured(model, _Answer, [ChatMessage(role="user", content="go")])
    assert result.nested.inner_label == "n"


# ── env config resolution ──────────────────────────────────────────────────


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HPC_AGENT_MODEL_BASE_URL",
        "HPC_AGENT_MODEL_API_KEY",
        "HPC_AGENT_MODEL_NAME",
        "HPC_AGENT_MODEL_RESPONSE_FORMAT",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "HPC_AGENT_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_from_env_resolves_full_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_API_KEY", "sk-env")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "deepseek-chat")
    monkeypatch.setenv("HPC_AGENT_MODEL_RESPONSE_FORMAT", "json_object")
    model = OpenAICompatModel.from_env()
    assert model.name == "openai-compat"
    assert model._model == "deepseek-chat"
    assert model._response_format_mode == "json_object"
    assert model._api_key == "sk-env"


def test_from_env_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "gpt-4o")
    with pytest.raises(errors.SpecInvalid, match="HPC_AGENT_MODEL_BASE_URL"):
        OpenAICompatModel.from_env()


def test_from_env_missing_model_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_API_KEY", "sk-x")
    with pytest.raises(errors.SpecInvalid, match="HPC_AGENT_MODEL_NAME"):
        OpenAICompatModel.from_env()


def test_from_env_remote_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "gpt-4o")
    with pytest.raises(errors.SpecInvalid, match="HPC_AGENT_MODEL_API_KEY"):
        OpenAICompatModel.from_env()


def test_from_env_localhost_keyless_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "Qwen/Qwen2.5-7B")
    model = OpenAICompatModel.from_env()  # no key — allowed for loopback
    assert model._api_key is None


def test_from_env_api_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
    model = OpenAICompatModel.from_env()
    assert model._api_key == "sk-fallback"


def test_from_env_bad_response_format_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "m")
    monkeypatch.setenv("HPC_AGENT_MODEL_RESPONSE_FORMAT", "bogus")
    with pytest.raises(errors.SpecInvalid, match="HPC_AGENT_MODEL_RESPONSE_FORMAT"):
        OpenAICompatModel.from_env()


# ── registry wiring ────────────────────────────────────────────────────────


def test_get_model_resolves_openai_compat_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "m")
    model = get_model("openai-compat")
    assert model.name == "openai-compat"


def test_get_model_resolves_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("HPC_AGENT_MODEL", "openai-compat")
    monkeypatch.setenv("HPC_AGENT_MODEL_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("HPC_AGENT_MODEL_NAME", "m")
    assert get_model().name == "openai-compat"


def test_registration_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registering the builtin must NOT auto-select it: with nothing chosen,
    # get_model still raises the Phase-1 "no chat model selected" error.
    _clear_model_env(monkeypatch)
    with pytest.raises(errors.SpecInvalid, match="no chat model selected"):
        get_model()


def test_builtin_appears_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    # Trigger lazy registration, then assert the factory is registered.
    with pytest.raises(errors.SpecInvalid):
        get_model("does-not-exist")
    assert "openai-compat" in structured_mod._MODELS


# ── transport errors propagate through the floor ───────────────────────────


def test_http_error_raises_typed_hpc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    err = urllib.error.HTTPError(
        url="https://api.example.com/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "bad key"}'),
    )
    _capture_urlopen(monkeypatch, [err])
    with pytest.raises(errors.HpcError) as excinfo:
        _model().complete([ChatMessage(role="user", content="hi")])
    assert excinfo.value.error_code == "model_endpoint_error"
    assert "401" in str(excinfo.value)


def test_url_error_propagates_through_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The floor only catches ValidationError/ValueError; a transport error
    # must propagate OUT of structured() uncaught (not be repaired).
    _capture_urlopen(monkeypatch, [urllib.error.URLError("connection refused")])
    model = _model(response_format_mode="none")
    with pytest.raises(errors.HpcError, match="unreachable"):
        structured(model, _Answer, [ChatMessage(role="user", content="go")])


def test_missing_choices_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_urlopen(monkeypatch, [{"id": "x", "choices": []}])
    with pytest.raises(errors.HpcError, match="no choices"):
        _model().complete([ChatMessage(role="user", content="hi")])
