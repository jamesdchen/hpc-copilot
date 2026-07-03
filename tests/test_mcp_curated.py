"""Curated MCP catalog + warm in-process runner (Unit P, design §3).

* ``curated`` advertises exactly the DERIVED block verbs (a block = a verb whose
  Result model declares a ``next_block`` field) unioned with the fixed recovery /
  opt-in extras (doctor / kill / submit-speculate), intersected with the
  mutation policy.
* the server DEFAULTS to the warm in-process runner (reuses the process's
  registry, no per-call cold start) whose envelope + exit code are parity-checked
  against the isolated subprocess runner.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent._kernel.registry.primitive import get_registry


def _curated_names(*, allow_mutations: bool) -> set[str]:
    server = M.McpServer(
        registry=get_registry(),
        allow_mutations=allow_mutations,
        catalog="curated",
        runner=lambda _argv: (0, "{}", ""),
    )
    return {t["name"] for t in server.list_tools()}


def test_curated_catalog_is_derived_blocks_union_extras() -> None:
    names = _curated_names(allow_mutations=True)

    # Recompute the DERIVED block set from the same predicate the server uses,
    # so the assertion tracks whatever set N marked — never a hardcoded list.
    allowed = M.allowed_primitives(get_registry(), allow_mutations=True)
    derived = {n for n, meta in allowed.items() if M._declares_next_block(meta)}
    expected = derived | {"doctor", "kill", "submit-speculate"}

    assert names == expected
    # Sanity anchors: block verbs are in; a non-block read verb is out.
    assert {"submit-s2", "submit-s3", "submit-s4"} <= names
    assert {"doctor", "kill", "submit-speculate"} <= names
    assert "clusters" not in names


def test_curated_extras_gated_by_allow_mutations() -> None:
    ro = _curated_names(allow_mutations=False)
    # kill (mutate) and submit-speculate (workflow) require the mutation opt-in.
    assert "kill" not in ro
    assert "submit-speculate" not in ro


def test_declares_next_block_derivation_flips_with_the_field() -> None:
    """Proves curated membership is DERIVED from the presence of a ``next_block``
    field on the Result model — add it and the verb is a block; remove it and it
    is not."""

    class _WithNextBlock(BaseModel):
        next_block: dict[str, Any] | None = None

    class _NoNextBlock(BaseModel):
        run_id: str = ""

    def _fn() -> None: ...

    _fn.__annotations__ = {"return": _WithNextBlock}
    assert M._declares_next_block(cast("Any", _Meta(_fn))) is True

    _fn.__annotations__ = {"return": _NoNextBlock}
    assert M._declares_next_block(cast("Any", _Meta(_fn))) is False


class _Meta:
    """Minimal stand-in carrying just the ``func`` the predicate inspects."""

    def __init__(self, func: Any) -> None:
        self.func = func


def test_build_server_defaults_to_in_process_runner() -> None:
    assert M.build_server()._runner is M._in_process_cli_runner


def test_in_process_and_subprocess_runners_have_envelope_parity() -> None:
    """The warm in-process runner reproduces the isolated subprocess runner's
    envelope + exit code exactly — the contract that made subprocess delegation
    safe in the first place."""
    ip = M.McpServer(registry=get_registry(), catalog="full", runner=M._in_process_cli_runner)
    sp = M.McpServer(registry=get_registry(), catalog="full", runner=M._default_cli_runner)

    a = ip.call_tool("find", {"query": "submit"})
    b = sp.call_tool("find", {"query": "submit"})

    assert a["isError"] is False
    assert b["isError"] is False
    assert a["structuredContent"] == b["structuredContent"]
