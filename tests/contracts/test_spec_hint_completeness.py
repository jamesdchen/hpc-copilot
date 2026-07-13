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
