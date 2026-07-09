"""Unit tests for the K5 relay TRIPLES themselves (the K2 vector discipline).

The conformance module (``hpc_agent.conformance.test_capability_relay``) asserts
a harness's enforcement seam blocks/passes per triple. This file is the
triples' OWN test: it proves the vectors are generated-from / checked-against
the reference — each triple's pinned ``mismatch_kinds`` is exactly what OUR
``verify_relay`` / ``verify_notebook_relay`` returns for its seeded journal, and
the derived ``blocks`` agrees with the Stop hook's own ``_CONTRADICTION_KINDS``.
Without this, a triple could pin a verdict the reference never produces and the
conformance kit would certify against a fiction.

Also the guard-can-fire checks: every blocking kind is represented, every pass
kind is represented, and the fail-open triple genuinely degrades (its intact
counterpart contradicts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.relay_fixtures import (
    CONTRADICTION_KINDS,
    RelayTriple,
    blocking_kinds,
    load_triples,
    reference_result,
    seed_triple,
)

if TYPE_CHECKING:
    from pathlib import Path

_TRIPLES = load_triples()
_IDS = [t.name for t in _TRIPLES]


def test_triples_present() -> None:
    assert len(_TRIPLES) >= 12
    # names are unique (the parametrization ids and the by-name lookups rely on it)
    assert len(_IDS) == len(set(_IDS))


# --- the reference agrees with every pin -------------------------------------


@pytest.mark.parametrize("triple", _TRIPLES, ids=_IDS)
def test_reference_reproduces_pinned_kinds(triple: RelayTriple, tmp_path: Path) -> None:
    """OUR audit returns exactly the triple's pinned mismatch kinds.

    The vectors are checked against the reference implementation (the K2
    discipline): a triple that pinned a kind ``verify_relay`` never produces —
    or missed one it does — fails here before it can mislead the conformance
    kit.
    """
    seed_triple(tmp_path, triple)
    result = reference_result(tmp_path, triple)

    got = sorted({m.kind for m in result.mismatches})
    assert got == sorted(set(triple.mismatch_kinds)), (
        f"{triple.name}: reference kinds {got} != pinned "
        f"{sorted(set(triple.mismatch_kinds))} — {triple.doc}"
    )
    assert result.clean is (not triple.mismatch_kinds), triple.name


@pytest.mark.parametrize("triple", _TRIPLES, ids=_IDS)
def test_blocks_derives_from_the_hook_set(triple: RelayTriple) -> None:
    """``blocks`` is exactly the pinned kinds intersected with the blocking set.

    Derived from ``_CONTRADICTION_KINDS`` (the constant the hook filters on),
    never stored — so an additive change to the blocking set re-derives here
    rather than desyncing silently.
    """
    assert triple.contradiction_kinds == blocking_kinds(triple.mismatch_kinds)
    assert triple.blocks is bool(triple.contradiction_kinds)


# --- guard-can-fire: the inventory is complete -------------------------------


def test_every_blocking_kind_is_covered() -> None:
    """Each contradiction kind the hook blocks on appears in some triple —
    for BOTH the run relay and (where it applies) the notebook relay."""
    covered: set[str] = set()
    for t in _TRIPLES:
        covered |= set(t.contradiction_kinds)
    assert covered == set(CONTRADICTION_KINDS), (
        f"blocking kinds covered {sorted(covered)} != hook set {sorted(CONTRADICTION_KINDS)}"
    )

    # The notebook relay reuses `state` (status / passed) and `number` (sha).
    nb_kinds: set[str] = set()
    for t in _TRIPLES:
        if t.scope == "notebook":
            nb_kinds |= set(t.contradiction_kinds)
    assert {"state", "number"} <= nb_kinds


def test_pass_and_failopen_cases_present() -> None:
    """The non-blocking inventory: at least one faithful pass, one
    ``unverifiable`` drop, and the fail-open (torn-record) case."""
    passes = [t for t in _TRIPLES if not t.blocks]
    assert len(passes) >= 4
    kinds_across_passes: set[str] = set()
    for t in passes:
        kinds_across_passes |= set(t.mismatch_kinds)
    # an unverifiable claim rides a PASS (the seam drops it) — proving the
    # blocking filter, not merely the absence of any claim.
    assert "unverifiable" in kinds_across_passes
    assert any(t.name == "run_torn_record_failopen" for t in passes)


def test_failopen_torn_record_degrades_from_a_real_contradiction(tmp_path: Path) -> None:
    """The torn-record triple is graceful degradation, not a dead test: with the
    record intact the SAME 'running' claim is a live ``state`` contradiction."""
    torn = next(t for t in _TRIPLES if t.name == "run_torn_record_failopen")
    seed_triple(tmp_path, torn)
    torn_result = reference_result(tmp_path, torn)
    assert not any(m.kind in CONTRADICTION_KINDS for m in torn_result.mismatches)

    # Same seed WITHOUT the corruption -> the state claim contradicts the record.
    intact = RelayTriple(
        name="run_torn_record_intact",
        scope="run",
        target_id=torn.target_id,
        final_message=torn.final_message,
        seed={k: v for k, v in torn.seed.items() if k != "corrupt_record"},
        mismatch_kinds=["state"],
        doc="intact counterpart",
    )
    intact_dir = tmp_path / "intact"
    intact_dir.mkdir()
    seed_triple(intact_dir, intact)
    intact_result = reference_result(intact_dir, intact)
    assert any(m.kind == "state" for m in intact_result.mismatches)
