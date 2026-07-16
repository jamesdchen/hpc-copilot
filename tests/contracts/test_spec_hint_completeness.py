"""Contract: every block-chain successor hint validates the way the driver uses it.

Run #11 evidence (``docs/design/notebook-audit.md`` Addendum 8, item 13): the
demo hand-authored a ``submit-s3`` spec, bounced on a missing required
``monitor`` property, then burned describe|grep round-trips reverse-engineering
``MonitorFlowSpec``. The deeper contract: **a driver hint must never bounce off
its own successor's validator** — that is a driver bug, not an agent task.

The successor table (:data:`hpc_agent.infra.block_chain.SUCCESSORS`) has two
kinds of edge, and the driver treats their ``spec_hint`` differently — so the
contract this file pins is scoped to how the hint is *actually consumed*:

* **UNGATED successor** — the driver chains it IN CODE, passing the
  predecessor's ``spec_hint`` VERBATIM as the successor block's input spec
  (``_kernel/lifecycle/block_drive._chain``: ``spec = _next_spec_hint(result)``).
  A hint that omits a required nested sub-block the successor schema mandates is
  a ``SpecInvalid`` the instant the driver crosses the boundary — an
  unrecoverable stall on an unattended tick. So an ungated hint MUST fully
  validate against the successor's spec model. This is the load-bearing half.

* **GATED successor** (:func:`block_chain.is_gated`) — the driver PARKS for the
  human greenlight and, on resume, runs the block under the human-committed
  ``resolved`` spec, NOT the predecessor's skeleton hint
  (``block_drive.run_tick`` §3, the ``resume_action`` branch: ``first_spec`` is
  built from ``committed_resolved``, never from ``next_spec_hint``). The gated
  hint is the run-identity skeleton the greenlight brief carries; its full
  completion is the human's committed spec. Requiring it to fully validate would
  be a guard that can never correctly fire (engineering-principles, "verify a
  guard can actually fire"): the successor's caller-owned sub-specs
  (``SubmitS2Spec.submit`` / ``SubmitS3Spec.submit`` + ``invocation_argv``) have
  no schema defaults and are not present at the terminator — fabricating them
  would be exactly the ``ops/submit/field_partition`` anti-pattern. So a gated
  hint is asserted to be a run-identity skeleton, not a runnable spec.

The validator reused here is the SAME one the ``--spec`` seam applies
(``cli/_dispatch._load_and_model_validate_spec`` → ``shape.spec_model``),
resolved through the primitive registry via ``VERB_MODULE_MAP`` — the same
resolution ``block_drive._spec_model_field_names`` uses — so the test can never
drift from what the block actually accepts.
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent.infra import block_chain

pytestmark = pytest.mark.contract

_RUN_ID = "ml_run_abcd1234"
_CANARY_RUN_ID = "ml_run_abcd1234_canary"
_CAMPAIGN_ID = "camp_abcd1234"


# The representative ``**spec_hint`` kwargs each terminator passes to
# ``_next_block`` at its live call site (``ops/submit_blocks.py``,
# ``ops/status_blocks.py``, ``ops/aggregate_blocks.py``,
# ``meta/campaign/blocks.py``) — minimal realistic fields, keyed by the
# ``(current_verb, stage_reached)`` edge. The test builds each successor spec
# the way the block does (through ``block_chain.next_block_hint``, the shared
# composition point) so it exercises the REAL composition, never a strawman.
_REPRESENTATIVE_HINT_KWARGS: dict[tuple[str, str], dict[str, Any]] = {
    # submit family
    ("submit-s1", "resolved"): {"run_id": _RUN_ID},
    ("submit-s2", "canary_verified"): {
        "run_id": _RUN_ID,
        "canary_run_id": _CANARY_RUN_ID,
        "canary_job_ids": ["12344"],
    },
    ("submit-s3", "watching_terminal"): {"run_id": _RUN_ID},
    ("submit-s3", "watching_timeout"): {"run_id": _RUN_ID},
    # status family
    ("status-snapshot", "snapshot_clean"): {"monitor": {"run_id": _RUN_ID}},
    ("status-watch", "watch_terminal"): {"run_id": _RUN_ID},
    ("status-watch", "watch_timeout"): {"run_id": _RUN_ID},
    # aggregate family
    ("aggregate-check", "ready"): {"run_id": _RUN_ID},
    # campaign family
    ("campaign-greenlight", "greenlit"): {"campaign_id": _CAMPAIGN_ID},
    ("campaign-greenlight", "already_greenlit"): {"campaign_id": _CAMPAIGN_ID},
    ("campaign-watch", "watching_complete"): {"campaign_id": _CAMPAIGN_ID},
    ("campaign-watch", "watching_refill"): {"campaign_id": _CAMPAIGN_ID},
}


def _successor_spec_model(verb: str) -> Any:
    """The successor verb's input spec model — the ``--spec`` seam's validator.

    Resolves ``CliShape.spec_model`` through the primitive registry the same way
    ``cli/_dispatch`` (and ``block_drive._spec_model_field_names``) do, so the
    validation this test runs is byte-for-byte what a real ``--spec`` (or MCP
    typed-tool) invocation of the successor would run.
    """
    from hpc_agent._kernel.registry.primitive import get_meta, register_single_module
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

    entry = VERB_MODULE_MAP.get(verb)
    assert entry is not None, f"{verb!r} missing from VERB_MODULE_MAP"
    primitive_name, module_name = entry
    register_single_module(module_name)
    model = getattr(get_meta(primitive_name).cli, "spec_model", None)
    assert model is not None, f"{verb!r} has no spec_model to validate against"
    return model


def _composed_spec_hint(current_verb: str, stage_reached: str) -> tuple[str, dict[str, Any]]:
    """Compose ``(successor_verb, spec_hint)`` the way the block/driver does."""
    kwargs = _REPRESENTATIVE_HINT_KWARGS[(current_verb, stage_reached)]
    hint = block_chain.next_block_hint(current_verb, stage_reached, why="contract", **kwargs)
    assert hint is not None, f"({current_verb}, {stage_reached}) unexpectedly has no successor"
    return hint["verb"], hint["spec_hint"]


# Every edge in the table that HAS a deterministic successor, split by how the
# driver consumes the hint. Deriving from SUCCESSORS (not a hand list) means a
# newly-added edge is force-covered: its missing representative kwargs raise a
# KeyError in ``_composed_spec_hint`` rather than silently skipping the contract.
_NON_NONE_EDGES = [
    (current, stage, successor)
    for (current, stage), successor in sorted(block_chain.SUCCESSORS.items())
    if successor is not None
]
_UNGATED_EDGES = [e for e in _NON_NONE_EDGES if not block_chain.is_gated(e[2])]
_GATED_EDGES = [e for e in _NON_NONE_EDGES if block_chain.is_gated(e[2])]


def _edge_id(edge: tuple[str, str, str]) -> str:
    current, stage, successor = edge
    return f"{current}[{stage}]->{successor}"


def test_representative_hints_cover_every_edge() -> None:
    """Anti-drift: a representative hint exists for EVERY non-None successor edge.

    Guards against a new edge landing in SUCCESSORS without a completeness probe —
    the parametrized cases below only test what is enumerated, so this pins that
    the enumeration IS the whole table.
    """
    covered = set(_REPRESENTATIVE_HINT_KWARGS)
    table = {(current, stage) for (current, stage, _succ) in _NON_NONE_EDGES}
    assert covered == table, {
        "uncovered_edges": sorted(table - covered),
        "stale_representatives": sorted(covered - table),
    }


@pytest.mark.parametrize("edge", _UNGATED_EDGES, ids=[_edge_id(e) for e in _UNGATED_EDGES])
def test_ungated_successor_hint_validates(edge: tuple[str, str, str]) -> None:
    """An UNGATED hint is passed VERBATIM by the driver → it MUST fully validate.

    This is the run-#11 class: a hint missing a required nested sub-block bounces
    off the successor's own ``--spec`` validator mid-chain. Before the fix, the
    two flat-``run_id`` edges to ``status-watch`` — ``(submit-s3,
    watching_timeout)`` and ``(status-watch, watch_timeout)`` — failed here
    (``StatusWatchSpec`` requires a nested ``monitor``).
    """
    current, stage, successor = edge
    verb, spec_hint = _composed_spec_hint(current, stage)
    assert verb == successor
    model = _successor_spec_model(successor)
    # model_validate applies the successor schema's own defaults, so a hint that
    # carries the required non-default substructure validates cleanly. A raise
    # here is a driver bug: the tick would stall on SpecInvalid at this hop.
    model.model_validate(spec_hint)


@pytest.mark.parametrize("edge", _GATED_EDGES, ids=[_edge_id(e) for e in _GATED_EDGES])
def test_gated_successor_hint_is_run_identity_skeleton(edge: tuple[str, str, str]) -> None:
    """A GATED hint is a run-identity skeleton — the driver never runs it verbatim.

    The driver PARKS for the greenlight and, on resume, runs the gated block
    under the human-committed ``resolved`` spec (``block_drive.run_tick`` §3),
    not this hint. So the contract for a gated edge is only that the hint names
    the run the greenlight will authorize; the runnable spec is the human's
    committed one (see ``test_block_drive_specs.test_resume_advance_passes_
    committed_resolved_verbatim``). Full-validating it here would fail on the
    successor's caller-owned sub-specs (``submit`` / ``invocation_argv``), which
    have no schema defaults and must not be fabricated.
    """
    current, stage, successor = edge
    verb, spec_hint = _composed_spec_hint(current, stage)
    assert verb == successor
    assert block_chain.is_gated(successor)
    # Every gated block is per-run: the skeleton must identify the run so the
    # greenlight brief and the successor's gate can name the same target.
    assert isinstance(spec_hint, dict)
    assert spec_hint.get("run_id") == _RUN_ID


# ── run-14 finding #4: the driver COMPLETES a gated hint by REUSE, never fabrication ─
#
# Unit 3.1's materialization leg: a gated hint is a skeleton at the terminator
# (above), but the DRIVER composes the COMPLETE successor spec at park by REUSING
# the predecessor's own products (its input spec + result brief) —
# ``block_chain.compose_successor_spec``. The composed spec MUST validate against
# the successor's own ``--spec`` model (the run-#11 bounce class, now closed for
# gated successors too), and the composer must NEVER fabricate a caller-owned field
# (Row 14). This is the half the old skeleton-only invariant left to the human.


def _valid_submit_flow() -> dict[str, Any]:
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    ).model_dump(mode="json")


def _s2_predecessor_spec() -> dict[str, Any]:
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    flow = SubmitFlowSpec.model_validate(_valid_submit_flow())
    return SubmitS2Spec(
        submit=SubmitAndVerifySpec(submit=flow, poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    ).model_dump(mode="json")


# (successor, spec_hint, predecessor_spec, result_brief) that the driver composes
# from at each GATED boundary — the REAL sources (predecessor input spec + brief),
# not a strawman.
_GATED_COMPOSITION_CASES: dict[str, dict[str, Any]] = {
    # S2→S3: reuse S2's ``submit`` sub-spec + derive ``monitor`` from run_id.
    "submit-s3": {
        "spec_hint": {
            "run_id": _RUN_ID,
            "canary_run_id": _CANARY_RUN_ID,
            "canary_job_ids": ["12344"],
        },
        "predecessor_spec": None,  # filled by _s2_predecessor_spec() below
        "result_brief": {},
    },
    # S1→S2: reuse the SubmitFlowSpec S1's resolve leg BUILT (rides the brief).
    "submit-s2": {
        "spec_hint": {"run_id": _RUN_ID},
        "predecessor_spec": {},
        "result_brief": {"resolve": {"submit_spec": None}},  # filled below
    },
    # S3→S4: derive ``aggregate`` from the run identity.
    "submit-s4": {
        "spec_hint": {"run_id": _RUN_ID},
        "predecessor_spec": {},
        "result_brief": {},
    },
    # aggregate-check→aggregate-run: the check already emits the nested aggregate hint.
    "aggregate-run": {
        "spec_hint": {"aggregate": {"run_id": _RUN_ID}},
        "predecessor_spec": {},
        "result_brief": {},
    },
}


def _composition_case(successor: str) -> dict[str, Any]:
    case = {
        k: (dict(v) if isinstance(v, dict) else v)
        for k, v in _GATED_COMPOSITION_CASES[successor].items()
    }
    if successor == "submit-s3":
        case["predecessor_spec"] = _s2_predecessor_spec()
    if successor == "submit-s2":
        case["result_brief"] = {"resolve": {"submit_spec": _valid_submit_flow()}}
    return case


@pytest.mark.parametrize("successor", sorted(_GATED_COMPOSITION_CASES))
def test_gated_successor_composes_a_validating_spec(successor: str) -> None:
    """The driver's code-composed gated spec fully validates against the successor model.

    Run-14 #4: the composer REUSES the predecessor's own ``submit`` sub-spec (never
    re-authoring it) and derives identity sub-shapes (``monitor`` from run_id), so a
    plain ``y`` / overnight auto-advance runs the successor WITHOUT any agent-authored
    JSON. The load-bearing assertion: the composed spec round-trips the successor's
    ``extra="forbid"`` ``--spec`` validator.
    """
    assert block_chain.is_gated(successor)
    case = _composition_case(successor)
    composed = block_chain.compose_successor_spec(
        successor,
        spec_hint=case["spec_hint"],
        predecessor_spec=case["predecessor_spec"],
        result_brief=case["result_brief"],
    )
    model = _successor_spec_model(successor)
    model.model_validate(composed)  # must not raise — the run-#11 bounce class, closed


def test_composer_never_fabricates_a_required_caller_field() -> None:
    """Row 14: a boundary that cannot SOURCE a caller-owned sub-spec REFUSES, never fabricates.

    A field-less S2 predecessor (no ``submit``) means the composer has nothing to
    reuse for S3's required ``submit`` — it must raise :class:`SuccessorSpecIncomplete`
    (the caller then materializes nothing), never invent a ``submit`` with fabricated
    ``goal`` / ``task_generator`` (the ``field_partition`` anti-pattern). Mirrors
    ``overnight.py``'s "cmd_sha is NEVER composed".
    """
    with pytest.raises(block_chain.SuccessorSpecIncomplete) as ei:
        block_chain.compose_successor_spec(
            "submit-s3", spec_hint={"run_id": _RUN_ID}, predecessor_spec={}, result_brief={}
        )
    assert ei.value.missing == "submit"
    # S1→S2 with a resolve leg that has not run (no submit_spec on the brief) refuses too.
    with pytest.raises(block_chain.SuccessorSpecIncomplete):
        block_chain.compose_successor_spec(
            "submit-s2", spec_hint={"run_id": _RUN_ID}, predecessor_spec={}, result_brief={}
        )


def test_composed_spec_sha_is_byte_stable() -> None:
    """Row 16 foundation: same inputs → same bytes → same sha (the R3 drift-pin base).

    The park-time sha is computed over the SAME sorted-keys serialization
    ``atomic_write_json`` writes, so a consumer that recomputes over the file's bytes
    gets an identical stamp — and any edit-after-park moves it (drift is detectable).
    """
    case = _composition_case("submit-s3")
    composed = block_chain.compose_successor_spec(
        "submit-s3",
        spec_hint=case["spec_hint"],
        predecessor_spec=case["predecessor_spec"],
        result_brief=case["result_brief"],
    )
    sha_a = block_chain.successor_spec_sha(composed)
    # Recompose from scratch — a fresh dict with the same content — must match.
    composed_again = block_chain.compose_successor_spec(
        "submit-s3",
        spec_hint=_composition_case("submit-s3")["spec_hint"],
        predecessor_spec=_composition_case("submit-s3")["predecessor_spec"],
        result_brief=_composition_case("submit-s3")["result_brief"],
    )
    assert block_chain.successor_spec_sha(composed_again) == sha_a
    # An edit moves the sha — a post-park tamper is detectable at consumption (R3).
    tampered = {**composed, "monitor": {"run_id": "ml_run_tampered0"}}
    assert block_chain.successor_spec_sha(tampered) != sha_a
