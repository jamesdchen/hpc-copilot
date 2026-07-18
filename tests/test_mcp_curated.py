"""Curated MCP catalog + warm in-process runner (Unit P, design §3).

* ``curated`` advertises exactly the DERIVED block verbs (a block = a verb whose
  Result model declares a ``next_block`` field) unioned with the fixed recovery /
  opt-in extras (doctor / kill / net-triage / submit-speculate), intersected with the
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
    expected = derived | {
        "doctor",
        "kill",
        "net-triage",
        "submit-speculate",
        "block-drive",
        "append-decision",
        "scope-lock",
        "verify-reproduction",
        # The notebook-audit loop's agent_facing verbs (Amendment 2, run #10):
        # human-sequenced (no next_block), so all unioned explicitly.
        "notebook-lint",
        "notebook-audit-view",
        "notebook-status",
        "notebook-auto-clear",
        "notebook-draft",
        "notebook-draft-context",
        "notebook-record-receipt",
        "notebook-scaffold-template",
        "notebook-record-config",
        # notebook-dry-run — the sampled preview run (drafting prelude);
        # human-sequenced like the loop, unioned explicitly (2026-07-18).
        "notebook-dry-run",
        # alerts-ack — the watchdog-backlog acknowledgment mutate (recovery
        # class beside doctor; 2026-07-18).
        "alerts-ack",
        # The audit prelude's non-notebook-prefixed companions (run #11: the
        # skill calls both MCP-first; human-sequenced, no next_block, so
        # unioned explicitly like the loop verbs).
        "audit-preflight",
        "evidence-brief",
        # The read-loop QUERY verbs the skills name MCP-direct ("go DIRECT
        # through MCP") — none declares next_block, so each is an explicit
        # union (the run-#8 unreachable-verb lesson; enforced by
        # scripts/lint_skill_mcp_reachability.py). revise-resolved is the
        # SKILL-tagged (MCP-direct) mutate that VERIFIABLY declares no
        # next_block, so it does not derive despite the directive.
        "read-decisions",
        "verify-relay",
        "attention-queue",
        "revise-resolved",
    }
    # poll-detached (architect memo §2) is a curated extra built by a SIBLING
    # unit (m-poll). Until it lands it is ABSENT from the registry and filtered
    # out of the curated set, so the pin guards on registry presence rather than
    # asserting a verb that does not exist yet.
    if "poll-detached" in get_registry():
        expected.add("poll-detached")

    assert names == expected
    # Sanity anchors: block verbs are in; the loop driver + commit are in; a
    # non-block read verb is out.
    assert {"submit-s2", "submit-s3", "submit-s4"} <= names
    assert {"doctor", "kill", "net-triage", "submit-speculate"} <= names
    assert {"block-drive", "append-decision"} <= names
    # scope-lock is a curated human-amplification mutate (an MCP-unreachable verb
    # gets hand-rolled, run #8); scope-status stays a pure read, OUT of curated.
    assert "scope-lock" in names
    assert "scope-status" not in names
    # verify-reproduction is a curated READ (no next_block): the sanctioned
    # post-repro receipt query reproduce-run's brief directs to — same run-#8
    # hand-rolled-if-unreachable lesson, unioned in explicitly like scope-lock.
    assert "verify-reproduction" in names
    # retarget-run AND reproduce-run derive in via their next_block hand-off field
    # (run #8: the agent, unable to reach retarget over MCP, hand-ran
    # kill→confirm→revise against a throttled cluster) — a next_block-declaring
    # verb must never silently fall back out of the catalog.
    assert {"retarget-run", "reproduce-run"} <= names
    # The notebook-audit loop's verbs are curated extras (Amendment 2, run #10:
    # their MCP absence priced as hand-authored spec JSONs + schema fumbles).
    # None declare next_block (the loop is human-sequenced — the driver was
    # rejected), so this is the explicit union, not derivation.
    assert {
        "notebook-lint",
        "notebook-audit-view",
        "notebook-status",
        "notebook-auto-clear",
        "notebook-draft",
        "notebook-draft-context",
        "notebook-record-receipt",
        "notebook-scaffold-template",
        "notebook-record-config",
    } <= names
    # The prelude verbs the notebook-audit skill calls MCP-first before any
    # drafting (run #11): the GO/NO-GO preflight brief + the evidence point
    # digest — unreachable, each is the next hand-derived check / store walk.
    assert {"audit-preflight", "evidence-brief"} <= names
    assert "clusters" not in names


def test_curated_covers_every_agent_facing_notebook_verb() -> None:
    """DERIVED drift guard for the Amendment-2 ruling: every agent_facing
    ``notebook-*`` verb in the registry must be reachable from the curated
    catalog (the audit loop's verbs never derive in — no ``next_block`` — so a
    new one added without a ``_CURATED_EXTRA_VERBS`` entry silently drops to
    the run-#10 hand-authored-spec fallback; this fails instead)."""
    names = _curated_names(allow_mutations=True)
    notebook_verbs = {
        n
        for n, meta in get_registry().items()
        if n.startswith("notebook-") and getattr(meta, "agent_facing", False)
    }
    assert notebook_verbs  # the substrate exists — an empty set is a probe bug
    assert notebook_verbs <= names


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
