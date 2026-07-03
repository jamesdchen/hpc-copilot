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


def test_curated_ignores_allow_mutations() -> None:
    """Design §7: the ``--allow-mutations ∩ curated`` intersection was vestigial
    and is dropped. Curated is itself the allowlist (its block verbs are all
    inherently ``workflow``-typed), so its listing is identical whether or not
    mutations are opted in — the verb-level guards enforce at invocation. This
    pins that dropping the intersection does not re-hide the mutating block verbs
    (or the read-only ``workflow`` blocks the old intersection mis-classified)."""
    ro = _curated_names(allow_mutations=False)
    rw = _curated_names(allow_mutations=True)
    assert ro == rw
    # The block verbs (all `verb="workflow"`, i.e. mutating) and the mutating
    # extras are listed even without the opt-in.
    assert {"submit-s2", "submit-s3", "submit-s4"} <= ro
    assert {"kill", "submit-speculate"} <= ro


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


def test_in_process_and_subprocess_runners_have_envelope_parity(tmp_path: Any) -> None:
    """The warm in-process runner reproduces the isolated subprocess runner's
    envelope + exit code exactly — the contract that made subprocess delegation
    safe in the first place.

    Broadened past the read-only ``find`` (design §7) to also cover a MUTATING
    verb (``append-decision``, ``verb="mutate"``) and a WORKFLOW/block verb
    (``submit-s1``), so the parity oracle spans all three verb classes the
    server invokes. Kept hermetic: each mutating/workflow case is driven with an
    empty ``spec`` that fails schema validation deterministically (a
    ``spec_invalid`` envelope), so no cluster, scheduler, or nondeterministic
    state is touched — the two runners must still agree byte-for-byte."""
    reg = get_registry()
    ip = M.McpServer(
        registry=reg, allow_mutations=True, catalog="full", runner=M._in_process_cli_runner
    )
    sp = M.McpServer(
        registry=reg, allow_mutations=True, catalog="full", runner=M._subprocess_cli_runner
    )

    # (name, arguments, expected isError) — read-only, mutating, workflow/block.
    cases: list[tuple[str, dict[str, Any], bool]] = [
        ("find", {"query": "submit"}, False),
        ("append-decision", {"spec": {}, "experiment_dir": str(tmp_path)}, True),
        ("submit-s1", {"spec": {}, "experiment_dir": str(tmp_path)}, True),
    ]
    for name, arguments, expected_error in cases:
        a = ip.call_tool(name, arguments)
        b = sp.call_tool(name, arguments)
        assert a["isError"] is expected_error, name
        assert b["isError"] is expected_error, name
        # Identical envelope AND identical exit code across the two runners.
        assert a["structuredContent"] == b["structuredContent"], name
