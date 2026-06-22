"""Raw model-call → validated object — the structured-output seam.

Every other model-facing path in this codebase spawns an *agent* (a
``claude -p --bare`` tool loop) through a :class:`WorkerInvoker`. This
module introduces the first *raw model-call* seam: one completion turned
into a validated Pydantic instance, with no tool loop. It is the
model-call sibling of :func:`hpc_agent._kernel.lifecycle.run.run_workflow`,
and it mirrors that file's funnel — render the request, hand it to a
transport, parse the reply — with the agent loop collapsed to a single
``complete()``.

The transport boundary (:class:`ChatModel`) is provider-agnostic by
construction. A model is OFFERED the target JSON Schema on every call: a
future native-strict adapter uses it as a decode constraint (an
accelerator — the model can only emit conforming tokens), while a plain
model ignores it and the parse-validate-repair *floor* carries
correctness. This is the same "offer the schema, the accelerator may use
it" contract as the spawned worker's ``_worker_output_schema()`` →
``--json-schema`` path in
:mod:`hpc_agent._kernel.lifecycle.invoke`: the schema is durability, not
a hard dependency.

Selection precedence mirrors :func:`get_invoker`: an explicit name > the
``HPC_AGENT_MODEL`` environment variable. Phase 2 (#304) registers the
first real adapter — the OpenAI-compatible
:class:`~hpc_agent._kernel.lifecycle.chat_models.openai_compat.OpenAICompatModel`
— lazily inside :func:`get_model` (so importing this module stays free of
provider code). Registration is default-OFF: it never auto-selects, so
:func:`get_model` still raises a clear
:class:`~hpc_agent.errors.SpecInvalid` (the same shape as an unknown
invoker) until ``HPC_AGENT_MODEL`` or an explicit name picks one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import pydantic

from hpc_agent import errors
from hpc_agent._kernel.contract.json_extract import last_json_object

__all__ = [
    "ChatMessage",
    "ChatModel",
    "ScriptedModel",
    "get_model",
    "register_model",
    "structured",
]


@dataclass(frozen=True)
class ChatMessage:
    """One turn in a chat completion request.

    Deliberately minimal — a ``role`` and its ``content`` text. Roles are
    the three a completion request actually needs: ``system`` for the
    instruction frame, ``user`` for the request (and the synthesized
    repair turns this module appends), ``assistant`` for the model's own
    prior replies fed back during repair. Frozen so a message list can be
    extended for a repair attempt without mutating the caller's turns.
    """

    role: str
    content: str


class ChatModel(Protocol):
    """A provider-agnostic single-completion transport.

    Implementations know nothing about workflows or the structured-output
    funnel — only how to turn a list of :class:`ChatMessage` turns into
    one reply string. ``schema`` is OFFERED, not required: a native-strict
    adapter may use it as a decode constraint (an accelerator that
    guarantees conforming output); a plain adapter ignores it and the
    floor in :func:`structured` carries correctness. The two are
    complementary, never substitutes — exactly as ``--json-schema`` and
    :func:`parse_worker_report` are for the spawned worker.
    """

    name: str

    def complete(
        self, messages: list[ChatMessage], *, schema: dict[str, Any] | None = None
    ) -> str: ...


def structured(
    model: ChatModel,
    schema_model: type[pydantic.BaseModel],
    messages: list[ChatMessage],
    *,
    max_repairs: int = 2,
    post_validate: Callable[[Any], None] | None = None,
) -> Any:
    """Drive *model* to a validated instance of *schema_model*.

    The raw-completion counterpart to :func:`run_workflow`'s render →
    invoke → parse funnel, with the agent loop collapsed to one
    ``complete()`` per attempt:

    * The target JSON Schema is derived from *schema_model* via
      ``model_json_schema()`` — the schema is generated from the Pydantic
      model, the single source of truth, never hand-maintained (the same
      discipline as ``scripts/build_schemas.py``). It is passed to
      ``model.complete(..., schema=...)`` so a native-strict adapter can
      use it as a decode constraint.
    * Each attempt: ``complete`` → :func:`last_json_object` → the model's
      ``model_validate`` → optional *post_validate*. On any failure (no
      JSON found, a :class:`pydantic.ValidationError`, or a *post_validate*
      rejection) the bad assistant turn and a synthesized user "repair"
      turn carrying the error text are appended, and the loop retries.
    * Up to ``max_repairs + 1`` attempts run. When the budget is
      exhausted, :class:`~hpc_agent.errors.StructuredOutputError` is raised
      carrying the last error plus a tail of the last raw output.

    *post_validate* is the seam for semantic/referential checks that a
    JSON Schema cannot express — the spawned worker's ``DECISION_POINTS``
    membership and non-empty-``why``-at-judgement-points rules
    (:func:`parse_worker_report`) are exactly this shape. Keeping those in
    code rather than folding them into the schema is the issue's chosen
    default: it keeps the generic boundary free of workflow specifics. A
    *post_validate* that raises is treated like a validation failure and
    its message is fed back as a repair turn.
    """
    schema_json = schema_model.model_json_schema()
    # Work on a private copy: repair turns must not leak back into the
    # caller's list, and the frozen ChatMessage makes each turn safe to
    # carry forward unchanged.
    turns = list(messages)
    last_error = "model produced no output"
    last_raw = ""
    for _ in range(max_repairs + 1):
        last_raw = model.complete(turns, schema=schema_json)
        obj = last_json_object(last_raw)
        if obj is None:
            last_error = "no JSON object found in model output"
            turns.append(ChatMessage(role="assistant", content=last_raw))
            turns.append(_repair_turn(last_error))
            continue
        try:
            instance = schema_model.model_validate(obj)
            if post_validate is not None:
                post_validate(instance)
        except (pydantic.ValidationError, ValueError) as exc:
            last_error = str(exc)
            turns.append(ChatMessage(role="assistant", content=last_raw))
            turns.append(_repair_turn(last_error))
            continue
        return instance
    raise errors.StructuredOutputError(
        f"model did not return a valid {schema_model.__name__} after "
        f"{max_repairs + 1} attempt(s): {last_error}\n"
        f"last raw output (tail): {_tail(last_raw)}"
    )


def _repair_turn(error: str) -> ChatMessage:
    """A user turn that hands the model its own validation failure to fix.

    The format is plain and self-contained: it names that the previous
    reply did not validate, includes the verbatim error, and restates the
    single-object contract — enough for the model to correct without the
    funnel having to understand the target schema.
    """
    return ChatMessage(
        role="user",
        content=(
            "Your previous reply did not validate against the required schema:\n"
            f"{error}\n\n"
            "Return ONLY a single corrected JSON object that satisfies the schema. "
            "No prose before or after it."
        ),
    )


def _tail(text: str, *, cap: int = 2000) -> str:
    """The last *cap* chars of *text*, ellipsis-prefixed when truncated."""
    stripped = text.strip()
    return ("…" + stripped[-cap:]) if len(stripped) > cap else stripped


class ScriptedModel:
    """A test-double :class:`ChatModel` that replays canned responses.

    Exercises the :func:`structured` floor and repair loop with zero
    credentials. ``responses`` are returned in order from successive
    ``complete()`` calls; a call past the end raises (the test asked for
    more attempts than it scripted).

    When ``schema_response`` is set, the model returns *it* on the first
    call **iff** a ``schema`` is offered — standing in for a native-strict
    adapter that honours the decode constraint and conforms first try (the
    accelerator path, no repair). Without an offered schema it falls
    through to the ``responses`` queue, modelling a plain adapter the floor
    must carry.
    """

    name = "scripted"

    def __init__(
        self, responses: list[str] | None = None, *, schema_response: str | None = None
    ) -> None:
        self._responses = list(responses or [])
        self._schema_response = schema_response
        self._index = 0
        #: Schemas seen per call, so a test can assert the offer was made.
        self.schemas_seen: list[dict[str, Any] | None] = []

    def complete(self, messages: list[ChatMessage], *, schema: dict[str, Any] | None = None) -> str:
        self.schemas_seen.append(schema)
        if self._schema_response is not None and schema is not None and self._index == 0:
            self._index += 1
            return self._schema_response
        if self._index >= len(self._responses):
            raise AssertionError(
                "ScriptedModel exhausted: complete() called more times than responses were scripted"
            )
        response = self._responses[self._index]
        self._index += 1
        return response


_MODELS: dict[str, Callable[..., ChatModel]] = {}


def _register_builtins() -> None:
    """Lazily register the built-in adapters on first :func:`get_model` call.

    Kept out of import: the OpenAI-compatible adapter (and its
    ``urllib``-backed transport) loads only when a model is actually
    requested, so importing this boundary module stays provider-free.
    Idempotent and default-OFF — it only *registers* the factory under
    ``"openai-compat"``; it never selects it, so ``get_model`` with nothing
    chosen still raises (the Phase-1 contract holds).

    The "already done?" check reads the registry itself, not a separate
    ``_BUILTINS_REGISTERED`` flag. A parallel bool could desync from
    ``_MODELS``: a test that swaps ``_MODELS`` for a fresh dict
    (``monkeypatch.setattr``), triggers registration into *that* dict, then
    restores the original empty dict would leave the flag set but the registry
    empty — so registration never re-ran and ``get_model`` reported
    ``registered: []`` (a ``pytest -n auto`` ordering flake). Guarding on
    membership self-heals under any such swap; the import stays once-only on the
    hot path because it's skipped as soon as the key is present.
    """
    if "openai-compat" in _MODELS:
        return
    from hpc_agent._kernel.lifecycle.chat_models.openai_compat import OpenAICompatModel

    _MODELS["openai-compat"] = OpenAICompatModel.from_env


def register_model(name: str, factory: Callable[..., ChatModel]) -> None:
    """Register a :class:`ChatModel` factory under *name*.

    A new provider adapter (a native-strict Messages-API model, say) is
    one call to this plus its class — no :func:`structured` change. Mirror
    of :func:`hpc_agent._kernel.lifecycle.invoke.register_invoker`.
    """
    _MODELS[name] = factory


def get_model(name: str | None = None) -> ChatModel:
    """Resolve a :class:`ChatModel`: explicit name > ``HPC_AGENT_MODEL``.

    The built-in adapters are registered lazily here (:func:`_register_builtins`)
    so this module imports provider-free. Registration is default-OFF: with
    nothing selected this still raises
    :class:`~hpc_agent.errors.SpecInvalid` — the same shape as
    :func:`get_invoker`'s unknown-invoker error.
    """
    _register_builtins()
    chosen = name or os.environ.get("HPC_AGENT_MODEL")
    if chosen is None:
        raise errors.SpecInvalid(
            "no chat model selected; set HPC_AGENT_MODEL or pass a name. "
            f"registered: {sorted(_MODELS)}"
        )
    factory = _MODELS.get(chosen)
    if factory is None:
        raise errors.SpecInvalid(f"unknown chat model {chosen!r}; registered: {sorted(_MODELS)}")
    return factory()
