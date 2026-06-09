"""An OpenAI-compatible :class:`ChatModel` with strict json_schema decode.

The first real adapter behind the Phase-1 :class:`ChatModel` boundary
(#304). One class targets every OpenAI-shaped ``/chat/completions``
endpoint — DeepSeek-hosted, OpenAI, a self-hosted vLLM server — by
swapping ``base_url`` / ``api_key`` / ``model``; the wire shape is the
same, only the endpoint differs.

The phase's core is the **accelerator**: in the default
``response_format_mode="json_schema"`` the offered JSON Schema is sent as
a ``response_format={"type":"json_schema", ..., "strict": True}`` decode
constraint, so a conforming server *cannot* emit non-conforming tokens.
This is real decode-time enforcement, not a prompt hint. It is the raw
model-call sibling of the spawned worker's ``--json-schema`` accelerator
(:mod:`hpc_agent._kernel.lifecycle.invoke`): the schema is durability, and
the parse-validate-repair floor in
:func:`hpc_agent._kernel.lifecycle.structured.structured` remains the
universal BACKSTOP — it still catches the semantic / ``post_validate``
errors a shape constraint can't express, and carries providers/schemas
where strict isn't honoured.

Strictness is achieved by transforming a *copy* of the schema at this
boundary (:func:`_to_strict_schema`), never the source Pydantic models —
the floor keeps validating against the original lenient model, so a
strict-decoded output is always a superset-constrained case of the
lenient validate; the two never conflict (see :func:`_to_strict_schema`).

Selection precedence and the env-driven config mirror
:func:`hpc_agent._kernel.lifecycle.invoke.get_invoker`: an explicit value
beats nothing here (this is the adapter, not the registry), and
:meth:`OpenAICompatModel.from_env` reads the ``HPC_AGENT_MODEL_*`` vars.
Zero new runtime dependencies — stdlib ``urllib.request`` + ``json``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.contract.strict_schema import to_strict_schema as _to_strict_schema
from hpc_agent._kernel.lifecycle.structured import ChatMessage

__all__ = ["OpenAICompatModel"]

# The operator's per-endpoint downgrade knob (HPC_AGENT_MODEL_RESPONSE_FORMAT):
#   json_schema  DEFAULT — strict decode-time shape enforcement (OpenAI, vLLM).
#   json_object  JSON-valid only (no shape); for providers without schema
#                support (DeepSeek-hosted). The schema is injected as a system
#                hint and the floor carries conformance.
#   none         no response_format at all; rely entirely on the floor.
_RESPONSE_FORMAT_MODES = ("json_schema", "json_object", "none")

_DEFAULT_TIMEOUT = 120.0

# Env vars (mirrors invoke.py's env-driven config). Documented in
# docs/reference/env-vars.md alongside the #269 --json-schema gate.
_ENV_BASE_URL = "HPC_AGENT_MODEL_BASE_URL"
_ENV_API_KEY = "HPC_AGENT_MODEL_API_KEY"
_ENV_NAME = "HPC_AGENT_MODEL_NAME"
_ENV_RESPONSE_FORMAT = "HPC_AGENT_MODEL_RESPONSE_FORMAT"

# Fallback api-key vars, tried after the primary HPC_AGENT_MODEL_API_KEY so an
# operator can reuse a key already exported for the OpenAI / DeepSeek SDK.
_API_KEY_FALLBACKS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY")


def _is_localhost(base_url: str) -> bool:
    """True when *base_url*'s host is loopback — a keyless vLLM is allowed there.

    A self-hosted vLLM on ``http://localhost:8000/v1`` typically needs no
    bearer token; only a remote endpoint must carry a key. Matches the host
    portion so ``http://127.0.0.1:8000/v1`` and ``http://localhost/v1`` both
    qualify, while ``https://localhost.example.com`` (a real remote) does not.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


class OpenAICompatModel:
    """A :class:`ChatModel` over an OpenAI-compatible ``/chat/completions`` API.

    See the module docstring for the accelerator contract. Construct
    directly with explicit values, or via :meth:`from_env` for the
    registered ``HPC_AGENT_MODEL=openai-compat`` path.
    """

    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        response_format_mode: str = "json_schema",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if response_format_mode not in _RESPONSE_FORMAT_MODES:
            raise errors.SpecInvalid(
                f"{_ENV_RESPONSE_FORMAT}={response_format_mode!r} is not one of "
                f"{list(_RESPONSE_FORMAT_MODES)}."
            )
        # Normalize so f"{base_url}/chat/completions" never doubles the slash.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._response_format_mode = response_format_mode
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> OpenAICompatModel:
        """Build from the ``HPC_AGENT_MODEL_*`` environment, à la invoke.py.

        Resolves ``HPC_AGENT_MODEL_BASE_URL`` (required),
        ``HPC_AGENT_MODEL_NAME`` (required), ``HPC_AGENT_MODEL_API_KEY``
        (required unless the base_url is loopback — a keyless vLLM — falling
        back to ``OPENAI_API_KEY`` / ``DEEPSEEK_API_KEY``), and
        ``HPC_AGENT_MODEL_RESPONSE_FORMAT`` (default ``json_schema``). A
        missing required value raises :class:`~hpc_agent.errors.SpecInvalid`
        naming the env var, in invoke.py's remediation tone.
        """
        base_url = (os.environ.get(_ENV_BASE_URL) or "").strip()
        if not base_url:
            raise errors.SpecInvalid(
                f"no chat-model endpoint configured: set {_ENV_BASE_URL} to an "
                "OpenAI-compatible base URL (e.g. https://api.deepseek.com/v1, "
                "https://api.openai.com/v1, or http://localhost:8000/v1) before "
                "selecting HPC_AGENT_MODEL=openai-compat."
            )
        model = (os.environ.get(_ENV_NAME) or "").strip()
        if not model:
            raise errors.SpecInvalid(
                f"no chat-model id configured: set {_ENV_NAME} to the model to "
                "call (e.g. deepseek-chat, gpt-4o) before selecting "
                "HPC_AGENT_MODEL=openai-compat."
            )
        api_key = (os.environ.get(_ENV_API_KEY) or "").strip()
        if not api_key:
            for fallback in _API_KEY_FALLBACKS:
                api_key = (os.environ.get(fallback) or "").strip()
                if api_key:
                    break
        if not api_key and not _is_localhost(base_url):
            raise errors.SpecInvalid(
                f"no chat-model credential: set {_ENV_API_KEY} (or "
                f"{' / '.join(_API_KEY_FALLBACKS)}) for a remote endpoint. A key "
                "is required for any non-loopback base_url; only a keyless "
                "localhost/127.0.0.1 vLLM may omit it."
            )
        mode = (os.environ.get(_ENV_RESPONSE_FORMAT) or "json_schema").strip().lower()
        if mode not in _RESPONSE_FORMAT_MODES:
            raise errors.SpecInvalid(
                f"{_ENV_RESPONSE_FORMAT}={mode!r} is not one of "
                f"{list(_RESPONSE_FORMAT_MODES)}; default json_schema is strict "
                "decode, json_object is JSON-valid-only (floor carries shape), "
                "none disables the constraint."
            )
        return cls(
            base_url=base_url,
            api_key=api_key or None,
            model=model,
            response_format_mode=mode,
        )

    def complete(self, messages: list[ChatMessage], *, schema: dict[str, Any] | None = None) -> str:
        """One chat completion. Honours the offered *schema* per the mode.

        Builds the request body, applies the accelerator
        (``response_format``) for the configured mode, POSTs to
        ``{base_url}/chat/completions``, and returns
        ``choices[0].message.content``. Transport / envelope failures raise a
        typed :class:`~hpc_agent.errors.HpcError` (see :meth:`_post`); those
        propagate OUT of :func:`structured` uncaught — its floor only catches
        validation failures, and a transport error is not one to repair.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        response_format, extra_messages = self._accelerator(schema)
        if response_format is not None:
            body["response_format"] = response_format
        if extra_messages:
            # json_object needs the word "json" somewhere in the conversation
            # and the schema as a shape hint; prepend so it frames the request.
            body["messages"] = extra_messages + body["messages"]
        envelope = self._post(body)
        return self._content(envelope)

    def _accelerator(
        self, schema: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
        """Resolve ``(response_format, extra_messages)`` for the offered schema.

        * ``json_schema`` + schema → strict decode constraint carrying the
          :func:`_to_strict_schema`-transformed copy. Real enforcement.
        * ``json_object`` + schema → ``{"type":"json_object"}`` (JSON-valid
          only) plus a system hint that names "json" and carries the schema;
          the hint + floor carry shape, which json_object alone does not.
        * ``none``, or no schema → no constraint; the floor carries everything.
        """
        if schema is None or self._response_format_mode == "none":
            return None, []
        if self._response_format_mode == "json_schema":
            name = schema.get("title") or "Result"
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "schema": _to_strict_schema(schema),
                    "strict": True,
                },
            }
            return response_format, []
        # json_object: guarantees valid JSON but not shape. Inject the schema
        # as a system hint (which also satisfies the "must mention json" rule
        # OpenAI enforces for json_object) and lean on the floor for shape.
        hint = {
            "role": "system",
            "content": (
                "Respond with a single json object that conforms to this JSON "
                "Schema. Output only the json object, no prose.\n"
                f"{json.dumps(schema, separators=(',', ':'))}"
            ),
        }
        return {"type": "json_object"}, [hint]

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST *body* to ``/chat/completions`` and return the parsed envelope.

        Network / HTTP failure or a non-JSON body raises a typed
        :class:`~hpc_agent.errors.ModelEndpointError` — the retry-safe,
        ``network``-category class for a raw model-call transport failure (a
        transient endpoint blip is the natural recovery, and re-running the
        funnel resamples). It is distinct from
        :class:`~hpc_agent.errors.StructuredOutputError` (a *valid* completion
        that failed the floor): this is a failure to obtain a completion at all.
        """
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - best-effort body for the message
                detail = ""
            raise errors.ModelEndpointError(
                f"chat-model endpoint returned HTTP {exc.code} for "
                f"{self._base_url}/chat/completions: {detail}",
                remediation=(
                    "Verify HPC_AGENT_MODEL_BASE_URL, the model id, and the API "
                    "key/credential for the endpoint; a 4xx is usually a bad key "
                    "or unsupported response_format, a 5xx a transient outage."
                ),
            ) from exc
        except urllib.error.URLError as exc:
            raise errors.ModelEndpointError(
                f"chat-model endpoint unreachable at "
                f"{self._base_url}/chat/completions: {exc.reason}",
                remediation=(
                    "Check HPC_AGENT_MODEL_BASE_URL is reachable from this host "
                    "(DNS, network, the vLLM server is up)."
                ),
            ) from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise errors.ModelEndpointError(
                "chat-model endpoint returned a non-JSON body from "
                f"{self._base_url}/chat/completions"
            ) from exc
        if not isinstance(envelope, dict):
            raise errors.ModelEndpointError(
                "chat-model endpoint returned a JSON value that is not an object "
                f"from {self._base_url}/chat/completions"
            )
        return envelope

    @staticmethod
    def _content(envelope: dict[str, Any]) -> str:
        """Lift ``choices[0].message.content`` out of the response envelope.

        A missing/empty ``choices`` array or a non-string content is a
        malformed envelope (the contract the floor's input depends on) and
        raises the same typed transport error as a network failure — there is
        no completion text to validate or repair.
        """
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise errors.ModelEndpointError(
                "chat-model response had no choices to read a completion from"
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise errors.ModelEndpointError(
                "chat-model response choice carried no string message content"
            )
        return content
