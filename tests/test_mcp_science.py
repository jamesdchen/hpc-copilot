"""The ``science`` MCP catalog — the Claude Science PRODUCER seam.

design: docs/design/claude-science-integration.md ("Mechanism — both, with the
boundary in exactly one place"). The ``science`` catalog is the producer subset a
coordinating agent (Claude Science) is handed: EXACTLY ``queue-run`` +
``queue-status`` + ``queue-advance``, and NOTHING that crosses a gate. The
disjointness from every gate-crossing verb IS the whole safety property — the
negative tests below pin it hard, in both mutation-flag states.

Mirrors the pattern in ``tests/test_mcp_curated.py`` (the existing catalog test):
a stub runner, membership read off ``list_tools`` and the invocation gate.
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent._kernel.registry.primitive import get_registry

# The producer subset, spelled out here rather than imported so the test pins the
# EXACT wire names an outside reader (and Claude Science) sees, not whatever the
# module constant happens to hold.
_SCIENCE = {"queue-run", "queue-status", "queue-advance"}

# The gate-crossing verbs the science catalog must be DISJOINT from. Representative,
# not exhaustive: the ACTOR that submits (queue-dispatch — verb=workflow, derives
# into curated via its next_block), the loop driver and the greenlight commit
# (block-drive / append-decision), and a submit-* block. If ANY of these were
# reachable under `science`, an autonomous agent could cross a gate — the exact
# thing this fork forbids.
_GATED = {"queue-dispatch", "block-drive", "append-decision", "submit-s2"}


def _server(*, allow_mutations: bool) -> M.McpServer:
    return M.McpServer(
        registry=get_registry(),
        allow_mutations=allow_mutations,
        catalog="science",
        runner=lambda _argv: (0, "{}", ""),
    )


def _science_names(*, allow_mutations: bool) -> set[str]:
    return {t["name"] for t in _server(allow_mutations=allow_mutations).list_tools()}


# ─── POSITIVE: the catalog advertises exactly the producer subset ────────────


def test_science_catalog_advertises_exactly_the_three_producer_verbs() -> None:
    """With mutations ON (the invocation Claude Science registers), the science
    catalog advertises EXACTLY queue-run + queue-status + queue-advance."""
    names = _science_names(allow_mutations=True)
    assert names == _SCIENCE
    # Every advertised verb is a live registry primitive (never a phantom).
    reg = get_registry()
    assert set(reg) >= _SCIENCE


def test_science_queue_run_needs_allow_mutations() -> None:
    """queue-run is a ``mutate``; the science catalog INTERSECTS with the read/act
    policy (unlike curated). So without --allow-mutations only the two pure-read
    queries are advertised, and queue-run is gated off — the reason the documented
    invocation carries --allow-mutations. The two queries are always reachable."""
    ro = _science_names(allow_mutations=False)
    assert ro == {"queue-status", "queue-advance"}
    assert "queue-run" not in ro
    # Turning mutations on only ADDS queue-run — never a fourth verb.
    rw = _science_names(allow_mutations=True)
    assert rw - ro == {"queue-run"}


# ─── NEGATIVE: the disjointness safety property (the load-bearing one) ───────


def test_science_catalog_is_disjoint_from_every_gated_verb() -> None:
    """The whole point: no gate-crossing verb is reachable under `science`, in
    EITHER mutation-flag state. Pinned against both the advertised listing and the
    invocation gate, so a client can neither SEE nor CALL a gated verb here."""
    for allow_mutations in (False, True):
        server = _server(allow_mutations=allow_mutations)
        listed = {t["name"] for t in server.list_tools()}
        invocable = set(server._invocable())
        # Disjoint from the representative gated set...
        assert listed.isdisjoint(_GATED), (allow_mutations, listed & _GATED)
        assert invocable.isdisjoint(_GATED), (allow_mutations, invocable & _GATED)
        # ...and, more strongly, a SUBSET of the three producer verbs — the fixed
        # allowlist can only ever shrink (queue-run gated), never grow.
        assert listed <= _SCIENCE
        assert invocable <= _SCIENCE


def test_science_dispatch_is_not_invocable() -> None:
    """queue-dispatch — the ACTOR that submits — is refused at the invocation gate
    under `science` (it is not in the catalog), even with mutations ON. This is the
    catalog acting as the boundary: the refusal is a client contract error."""
    import pytest

    server = _server(allow_mutations=True)
    with pytest.raises(M._Invalid):
        server.call_tool("queue-dispatch", {"spec": {}})


def test_science_run_is_invocable_with_mutations() -> None:
    """The positive counterpart: queue-run IS invocable under `science` once
    mutations are on — it routes through the CLI runner (here a stub) rather than
    being refused at the catalog gate. Proves the boundary admits the producer
    verb, not just rejects the gated ones."""
    server = _server(allow_mutations=True)
    result = server.call_tool("queue-run", {"spec": {}, "experiment_dir": "."})
    # The stub runner returns an empty envelope; the point is it was NOT refused
    # by the catalog gate (which would have raised _Invalid before any runner ran).
    assert "structuredContent" in result


def test_science_module_constant_matches_the_wire_names() -> None:
    """The module's _SCIENCE_VERBS constant is exactly the three producer verbs —
    a guard that an edit to the constant cannot silently widen the catalog."""
    assert set(M._SCIENCE_VERBS) == _SCIENCE


# ─── catalog plumbing ────────────────────────────────────────────────────────


def test_science_is_an_accepted_catalog() -> None:
    """`science` is a valid catalog value (the __init__ validation accepts it)."""
    server = M.McpServer(
        registry=get_registry(), catalog="science", runner=lambda _a: (0, "{}", "")
    )
    assert server._catalog == "science"


def test_initialize_names_the_producer_surface() -> None:
    """The initialize instructions describe the producer surface so an MCP client
    learns its role (enqueue + observe; the human approves dispatch)."""
    server = _server(allow_mutations=True)
    resp: dict[str, Any] = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    instructions = resp["result"]["instructions"].lower()
    assert "queue-run" in instructions
    assert "producer" in instructions
