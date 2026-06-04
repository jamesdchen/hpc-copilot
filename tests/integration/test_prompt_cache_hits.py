"""Opt-in audit: does the worker-prompt prefix actually hit prompt cache (#244)?

The cacheable prefix (scaffold + inlined procedure + return contract) is
byte-identical across runs of a workflow, so the *second* back-to-back spawn
of the same workflow should read most of it from cache rather than re-ingest
it. Everything up to the wire is unit-tested elsewhere; this is the one check
that the cache fires end-to-end against the real transport.

Gated behind ``RUN_NETWORK_TESTS=1`` (and the ``slow`` marker) because it
spawns two real ``claude -p`` workers and bills tokens. It needs a usable
worker credential (``ANTHROPIC_API_KEY`` or cloud-provider creds) in the
environment, exactly like ``hpc-agent run``.
"""

from __future__ import annotations

import os

import pytest

from hpc_agent._kernel.lifecycle.invoke import (
    ClaudeCliInvoker,
    RenderedPrompt,
    worker_credentials_available,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("RUN_NETWORK_TESTS") != "1",
        reason="network/billing test; set RUN_NETWORK_TESTS=1 to run",
    ),
]


def _spawn(prompt: RenderedPrompt):
    return ClaudeCliInvoker().invoke(prompt, cwd=os.getcwd(), report_cache_stats=True)


def test_second_spawn_reads_prefix_from_cache() -> None:
    if not worker_credentials_available():
        pytest.skip("no worker credential in the environment")

    # A large, fixed prefix is the part worth caching; a tiny variable suffix
    # mimics the per-invocation context. The worker is asked only to echo a
    # JSON object, so the run is cheap and deterministic.
    prefix = (
        "You are a cache-audit worker. Ignore all instructions in the user "
        "message except the final line. " + ("padding. " * 400)
    )
    first = _spawn(RenderedPrompt(prefix, 'Reply with exactly {"ok": true}'))
    second = _spawn(RenderedPrompt(prefix, 'Reply with exactly {"ok": true}'))

    assert first.cache_stats is not None, "transport surfaced no usage on the first spawn"
    assert second.cache_stats is not None, "transport surfaced no usage on the second spawn"

    cache_read = second.cache_stats.get("cache_read_input_tokens", 0)
    assert cache_read > 0, (
        "second identical spawn read zero tokens from cache — the cacheable "
        f"prefix is NOT hitting cache. second-spawn usage: {second.cache_stats}"
    )
