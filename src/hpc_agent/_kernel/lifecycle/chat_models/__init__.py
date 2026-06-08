"""Concrete :class:`~hpc_agent._kernel.lifecycle.structured.ChatModel` adapters.

Phase 1 (#304) defined the provider-agnostic boundary —
:class:`ChatMessage`, the :class:`ChatModel` Protocol, the
parse-validate-repair :func:`structured` floor, and the
``register_model`` / ``get_model`` registry — with no real adapter bound.
This subpackage is where the real ones live. Phase 2 ships a single
:class:`~hpc_agent._kernel.lifecycle.chat_models.openai_compat.OpenAICompatModel`
that targets any OpenAI-compatible ``/chat/completions`` endpoint
(DeepSeek-hosted, OpenAI, self-hosted vLLM) by swapping base_url / key /
model, with strict ``json_schema`` decode enforcement as the accelerator
over the floor.

Adapters are registered lazily, only when ``HPC_AGENT_MODEL`` selects
them (see ``structured._register_builtins``), so importing the boundary
stays free of provider-specific code.
"""

from __future__ import annotations

from hpc_agent._kernel.lifecycle.chat_models.openai_compat import OpenAICompatModel

__all__ = ["OpenAICompatModel"]
