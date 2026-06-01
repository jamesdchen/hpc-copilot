"""Behavioral eval harness — the pytest entry point.

Two tiers, the lara api / --no-api split (issue #204):

* OFFLINE tier (DEFAULT, no marker → runs in the normal ``pytest -q``):
    1. Unit-tests :func:`recursive_compare` against hand-written gold/candidate
       pairs — the grader must be obviously correct before it grades anything.
    2. Drives the genuinely DETERMINISTIC resolution path (``resolve_offline``:
       grid expansion + cluster lookup + ``plan-throughput`` + resource
       defaulting) for every corpus case and grades it against the case's
       hand-authored ``expect`` block AND against the committed gold snapshot.
    This tier needs NO network and NO API key — it is pure local computation
    over the fixture repos.

* LLM tier (marked ``slow`` + ``skipif`` no ``ANTHROPIC_API_KEY``):
    Drives the REAL decision skill (the inline ``WorkerInvoker`` / ``claude -p``
    worker) and grades the resulting envelope. Skipped — not failed — when no
    key is present, so the slow-tier CI (which has no key) stays green. The
    driver is intentionally unwired in this first slice (see
    ``resolve.resolve_via_llm``); the test is structured so the skip is on the
    KEY, and an explicit ``xfail`` marks the not-yet-wired driver so the harness
    advertises the gap honestly instead of silently passing.

Why the offline tier is more than a grader unit test: ``resolve_offline`` runs
the same cluster-config load, the same ``plan-throughput`` pure function, and
the same documented resource-defaulting rule the production decision path uses.
A regression in any of those (a planner change that mis-packs a 300-task grid,
a default that flips CPU↔GPU) fails here, offline, for free.
"""

from __future__ import annotations

import os

import pytest

from tests.eval._gold import read_gold, write_gold
from tests.eval.cases import CASES, EvalCase
from tests.eval.recursive_compare import Range, Tol, recursive_compare
from tests.eval.resolve import llm_available, resolve_offline, resolve_via_llm

# ``HPC_EVAL_REGEN=1`` turns the gold-snapshot assertion into a (re)write:
# regen + run in one command. Read once at import.
_REGEN = os.environ.get("HPC_EVAL_REGEN") == "1"


# ── 1. grader self-tests (offline, no fixtures, no hpc_agent import) ─────────


class TestRecursiveCompare:
    """The grader must pass obvious matches and catch the divergences that
    represent a WRONG agent decision (wrong cluster, off-by-one grid, a
    resource off by an order of magnitude) while tolerating the ones that are
    the SAME decision (a walltime within band, an int-vs-float)."""

    def test_exact_scalar_and_subset_dict_match(self) -> None:
        gold = {"cluster": "hoffman2", "grid_points": 6}
        candidate = {"cluster": "hoffman2", "grid_points": 6, "extra": "ignored"}
        assert recursive_compare(gold, candidate).ok

    def test_wrong_cluster_is_caught(self) -> None:
        result = recursive_compare({"cluster": "hoffman2"}, {"cluster": "discovery"})
        assert not result.ok
        assert "cluster" in result.report()

    def test_off_by_one_grid_is_caught(self) -> None:
        # grid_points must be EXACT: 6 vs 5 is a different decision. The default
        # 5% band on a value of 6 is ±0.3, so off-by-one exceeds it and fails.
        assert not recursive_compare({"grid_points": 6}, {"grid_points": 5}).ok

    def test_int_float_equivalence(self) -> None:
        # JSON may render 6 as 6.0; that is the same decision, not a mismatch.
        assert recursive_compare({"grid_points": 6}, {"grid_points": 6.0}).ok

    def test_list_length_mismatch_is_caught(self) -> None:
        # A grid of 6 axis values is not a grid of 5 — lists are length-checked.
        gold = {"horizon": [1, 5, 25]}
        assert not recursive_compare(gold, {"horizon": [1, 5]}).ok

    def test_list_elementwise_mismatch_is_caught(self) -> None:
        gold = {"horizon": [1, 5, 25]}
        assert not recursive_compare(gold, {"horizon": [1, 5, 99]}).ok

    def test_nested_axes_match(self) -> None:
        gold = {"axes": {"executor": ["ml_ridge", "ml_xgboost"], "horizon": [1, 5, 25]}}
        candidate = {"axes": {"executor": ["ml_ridge", "ml_xgboost"], "horizon": [1, 5, 25]}}
        assert recursive_compare(gold, candidate).ok

    def test_tolerant_float_within_band(self) -> None:
        # A resolved mem of 16384 MB vs a request for ~16000 is the same ask.
        gold = {"mem_mb": 16000}
        candidate = {"mem_mb": 16384}
        assert recursive_compare(gold, candidate, tolerant={"mem_mb": Tol(rel=0.10)}).ok

    def test_tolerant_float_outside_band_is_caught(self) -> None:
        # An order-of-magnitude miss (16G asked, 160G resolved) is a real bug.
        gold = {"mem_mb": 16000}
        candidate = {"mem_mb": 160000}
        result = recursive_compare(gold, candidate, tolerant={"mem_mb": Tol(rel=0.10)})
        assert not result.ok

    def test_range_bound_pass_and_fail(self) -> None:
        # walltime "somewhere in 1h..6h" — a band, not a point.
        tol = {"walltime_sec": Range(3600, 6 * 3600)}
        assert recursive_compare({"walltime_sec": 0}, {"walltime_sec": 4 * 3600}, tolerant=tol).ok
        assert not recursive_compare(
            {"walltime_sec": 0}, {"walltime_sec": 30 * 3600}, tolerant=tol
        ).ok

    def test_missing_key_is_caught(self) -> None:
        result = recursive_compare({"cluster": "hoffman2"}, {"backend": "sge"})
        assert not result.ok
        assert "missing key" in result.report()

    def test_bool_is_not_treated_as_numeric(self) -> None:
        # canary on/off is categorical — True must not float-match 1.
        assert not recursive_compare({"canary": True}, {"canary": 1}).ok
        assert recursive_compare({"canary": True}, {"canary": True}).ok

    def test_type_shape_mismatch_is_caught(self) -> None:
        # A dict where a list was expected (or vice versa) is a structural bug.
        assert not recursive_compare({"axes": [1, 2]}, {"axes": {"a": 1}}).ok
        assert not recursive_compare({"axes": {"a": 1}}, {"axes": [1, 2]}).ok


# ── 2. offline resolution + structural grading over the corpus ───────────────


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_offline_resolution_matches_expect(case: EvalCase) -> None:
    """Each case resolves (deterministically, offline) to a spec that
    STRUCTURALLY matches its hand-authored ``expect`` block.

    This is the behavioral assertion: given the request's parsed axes + the
    fixture cluster config, the agent surface's deterministic machinery
    (grid expansion, cluster→backend, plan-throughput waves, resource
    defaults) lands the right decision. Graded with ``recursive_compare`` —
    exact on ``cluster`` / ``grid_points`` / ``axes``, tolerant on resources.
    """
    resolved = resolve_offline(case)
    result = recursive_compare(case.expect, resolved, tolerant=case.tolerant)
    assert result.ok, f"[{case.id}] {result.report()}\nresolved={resolved}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_offline_resolution_matches_gold_snapshot(case: EvalCase) -> None:
    """The live offline resolution still equals the committed gold snapshot.

    The regression tripwire (see ``_gold.py``): an unintended change to the
    deterministic resolution shows up as a gold diff even when it stays inside
    the hand-authored ``expect`` tolerances. ``HPC_EVAL_REGEN=1`` rewrites the
    snapshot instead of asserting; otherwise a missing snapshot fails with an
    actionable "run regen" message rather than a confusing ``None``.
    """
    resolved = resolve_offline(case)
    if _REGEN:
        write_gold(case.gold_path, resolved)
        pytest.skip(f"regen: wrote {case.gold_path.name}")
    gold = read_gold(case.gold_path)
    assert gold is not None, (
        f"[{case.id}] no gold snapshot at {case.gold_path}. "
        f"Run: HPC_EVAL_REGEN=1 pytest -q tests/eval (or python -m tests.eval.regen)"
    )
    # Gold is a full snapshot → compare EXACTLY (the snapshot is the resolver's
    # own prior output, so no tolerance is wanted; any drift is meaningful).
    result = recursive_compare(gold, resolved)
    assert result.ok, f"[{case.id}] resolution drifted from gold:\n{result.report()}"


def test_corpus_is_nonempty_and_ids_unique() -> None:
    """Guardrail: the corpus exists and every case id is unique (ids name the
    gold files + parametrized tests — a dup would silently overwrite a gold)."""
    assert CASES, "eval corpus is empty"
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids)), f"duplicate case ids: {ids}"


# ── 3. LLM tier — opt-in, slow, key-gated (skips cleanly with no key) ────────


@pytest.mark.slow
@pytest.mark.skipif(
    not llm_available(),
    reason=(
        "LLM eval tier requires ANTHROPIC_API_KEY (drives a real claude -p / "
        "inline worker). Skipped so default + slow-tier CI stay free and offline."
    ),
)
@pytest.mark.xfail(
    reason=(
        "resolve_via_llm is intentionally unwired in this first slice (needs a "
        "fixture repo the worker can fully execute against + a reachable "
        "cluster). The grader + offline corpus are complete; this seam is "
        "key-gated so it can be filled in without touching them."
    ),
    raises=NotImplementedError,
    strict=False,
)
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
@pytest.mark.parametrize("register", ["eval", "user"])
def test_llm_resolution_matches_expect(case: EvalCase, register: str) -> None:
    """Drive the real decision skill for each request register and grade the
    envelope against the same ``expect`` block the offline tier uses.

    Both registers (precise + casual) must land the same decision — a prompt
    edit that over-fits one is the regression this tier exists to catch. The
    test is key-gated (skips without a key) and currently xfails on the
    unwired driver, so the slow-tier CI passes and the gap is advertised, not
    hidden. Filling in ``resolve_via_llm`` flips these from xfail to real.
    """
    resolved = resolve_via_llm(case, register=register)
    result = recursive_compare(case.expect, resolved, tolerant=case.tolerant)
    assert result.ok, f"[{case.id}/{register}] {result.report()}\nresolved={resolved}"
