"""Behavior-pinning mutation coverage for the REPRODUCTION-ACT machinery.

The reproduction act is the thesis's terminal link: the verbs a STRANGER runs to
prove the table. ``reproduce-run`` mints the pinned-identity reproduction;
``verify-reproduction`` verdicts the pair. A silent bug in either is a FALSE
"reproduced" (a divergence laundered into a match) or a wrongly-rejected genuine
reproduction. The disclosure batteries (env-lock / hw-facts / data-identity) and
the end-to-end comparator/receipt/claim-check flows are pinned in the sibling
files; this file covers the VERDICT CORE those tests skip past:

* the classify() comparator BOUNDARIES — the ``<=`` inclusive operators, the
  abs-OR-rel semantics, the all-absent-per-key exact fallthrough, the
  denom-zero rel_diff;
* the anti-laundering split — what verify ASSERTS (the receipt-kind lock) vs
  what it merely REPORTS, and the fingerprint-sample admission rules
  (no-cluster → no sample; the verdict map);
* needs_decision derived PURELY from the metric stage (per branch);
* the reproduce-side mint helpers — cmd_sha prefix tolerance, first-differing
  task, the disjoint-remote-path guard firing, the env/hw mint disclosures.

Each test names the MUTATION it kills. Fixtures use the REAL sidecar writer +
hand-written aggregates, mirroring test_verify_reproduction.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.determinism import DeterminismSampleRecord
from hpc_agent._wire.queries.verify_reproduction import (
    KeyTolerance,
    ReproTolerance,
    VerifyReproductionSpec,
)
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.reproduce_run import (
    _cmd_sha_matches,
    _env_lock_disclosure,
    _first_differing_task,
    _hw_facts_disclosure,
    _repro_remote_path,
)
from hpc_agent.ops.verify_reproduction import (
    _SAMPLE_VERDICT_MAP,
    _compare_metrics,
    _is_nan,
    _is_number,
    _resolve_key_tol,
    verify_reproduction,
)
from hpc_agent.state.determinism import PerKeyDiff, build_sample_record
from hpc_agent.state.env_lock import STATUS_CAPTURED
from hpc_agent.state.fingerprint_store import fingerprint_path
from hpc_agent.state.hw_facts import hw_sha
from hpc_agent.state.runs import (
    stamp_run_sidecar_env_lock,
    stamp_run_sidecar_hw_facts,
    write_run_sidecar,
)

ORIG = "orig-run"
REPRO = "repro-run"
CMD_SHA = "a" * 64
TASKS_PY_SHA = "b" * 64
EXECUTOR = "python train.py"


# --------------------------------------------------------------------------- #
# fixtures / helpers (self-contained, mirroring test_verify_reproduction.py)
# --------------------------------------------------------------------------- #
def _write_sidecar(exp: Path, run_id: str, *, reproduces: str | None = None, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": CMD_SHA,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": EXECUTOR,
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": TASKS_PY_SHA,
    }
    kwargs.update(over)
    write_run_sidecar(exp, reproduces=reproduces, **kwargs)


def _write_aggregate(exp: Path, run_id: str, aggregated_metrics: dict[str, Any]) -> None:
    agg = exp / "_aggregated" / run_id
    agg.mkdir(parents=True, exist_ok=True)
    (agg / "metrics_aggregate.json").write_text(
        json.dumps({"run_id": run_id, "aggregated_metrics": aggregated_metrics}), encoding="utf-8"
    )


def _pair(
    exp: Path,
    orig: dict[str, Any] | None,
    repro: dict[str, Any] | None,
    *,
    cluster: str | None = None,
) -> None:
    """Both sidecars (+ optional cluster) + both aggregates under grid-point ``gp``."""
    over = {"cluster": cluster} if cluster is not None else {}
    _write_sidecar(exp, ORIG, **over)
    _write_sidecar(exp, REPRO, reproduces=ORIG, **over)
    if orig is not None:
        _write_aggregate(exp, ORIG, {"gp": orig})
    if repro is not None:
        _write_aggregate(exp, REPRO, {"gp": repro})


def _run(exp: Path, tolerance: ReproTolerance | None = None):
    spec = VerifyReproductionSpec(original_run_id=ORIG, repro_run_id=REPRO, tolerance=tolerance)
    return verify_reproduction(exp, spec=spec)


def _pi_diff(a: float, b: float) -> PerKeyDiff:
    abs_diff = abs(a - b)
    denom = max(abs(a), abs(b))
    return PerKeyDiff("gp.pi", a, b, abs_diff, abs_diff / denom if denom else 0.0, "float")


def _plant_sample(exp: Path, *, per_key: list[PerKeyDiff], verdict: str = "auto_cleared") -> None:
    """One admitted main-scale hoffman2 fingerprint sample on gp.pi."""
    record = build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha="d" * 64,
        identity={"cmd_sha": CMD_SHA, "tasks_py_sha": TASKS_PY_SHA, "executor": EXECUTOR},
        source="verify-reproduction",
        run_ids=["o-prior", "r-prior"],
        cluster="hoffman2",
        scale="main",
        verdict=verdict,
        per_key=per_key,
        same_submission=False,
        partial=False,
        task_indices=None,
    )
    append_jsonl_line(fingerprint_path(exp, CMD_SHA), record)


def _well_evidenced_pi(exp: Path, *, lo: float = 3.13, hi: float = 3.16) -> None:
    for _ in range(3):
        _plant_sample(exp, per_key=[_pi_diff(lo, hi)])


def _pair_pi(exp: Path, orig: float, repro: float) -> None:
    _pair(exp, {"pi": orig}, {"pi": repro}, cluster="hoffman2")


# =========================================================================== #
# 1. classify() comparator BOUNDARIES — the exact operators (SENSITIVITY-CHECKED)
# =========================================================================== #
def test_abs_tolerance_boundary_is_inclusive() -> None:
    # abs_diff EXACTLY == abs_tol is a MATCH (``<=`` not ``<``).
    # MUTATION KILLED: ``abs_diff <= abs_tol`` -> ``abs_diff < abs_tol``.
    verdicts = _compare_metrics({"k": 1.0}, {"k": 1.5}, ReproTolerance(default_abs_tol=0.5))
    assert verdicts[0]["abs_diff"] == 0.5
    assert verdicts[0]["verdict"] == "match"


def test_rel_tolerance_boundary_is_inclusive() -> None:
    # rel_diff EXACTLY == rel_tol is a MATCH. orig=1,repro=2 -> rel_diff = 1/2 = 0.5.
    # MUTATION KILLED: ``rel_diff <= rel_tol`` -> ``rel_diff < rel_tol``.
    verdicts = _compare_metrics({"k": 1.0}, {"k": 2.0}, ReproTolerance(default_rel_tol=0.5))
    assert verdicts[0]["rel_diff"] == 0.5
    assert verdicts[0]["verdict"] == "match"


def test_abs_or_rel_is_disjunction_not_conjunction() -> None:
    # BOTH bounds supplied: abs FAILS (1.0 > 0.5) but rel PASSES (~0.0099 <= 0.02)
    # -> match. A stranger's tolerance is satisfied if EITHER bound holds.
    # MUTATION KILLED: the ``or`` between the abs/rel clauses -> ``and``.
    tol = ReproTolerance(default_abs_tol=0.5, default_rel_tol=0.02)
    verdicts = _compare_metrics({"k": 100.0}, {"k": 101.0}, tol)
    assert verdicts[0]["abs_diff"] == 1.0  # abs clause fails on its own
    assert verdicts[0]["verdict"] == "match"  # rel clause carries it


def test_all_absent_per_key_override_forces_exact() -> None:
    # A per_key entry with NEITHER bound FULLY replaces a lenient default -> that
    # key is compared EXACTLY, so a real diff MISMATCHES (it does not inherit the
    # forgiving default). This is the anti-laundering edge: an empty override must
    # not silently widen to the default.
    # MUTATION KILLED: dropping the ``override is not None`` branch (falling
    # through to the default bounds when a per_key entry exists but is all-absent).
    tol = ReproTolerance(default_abs_tol=100.0, per_key={"k": KeyTolerance()})
    verdicts = _compare_metrics({"k": 1.0}, {"k": 2.0}, tol)
    assert verdicts[0]["verdict"] == "mismatch"
    assert verdicts[0]["tolerance_applied"] is None  # supplied=False -> exact


def test_rel_diff_denominator_zero_is_zero_not_a_raise() -> None:
    # Both sides 0.0 -> denom 0; rel_diff is 0.0 (not a ZeroDivisionError), so a
    # rel tolerance MATCHES two exact zeros.
    # MUTATION KILLED: ``abs_diff / denom if denom else 0.0`` -> ``abs_diff / denom``.
    verdicts = _compare_metrics({"k": 0.0}, {"k": 0.0}, ReproTolerance(default_rel_tol=0.1))
    assert verdicts[0]["rel_diff"] == 0.0
    assert verdicts[0]["abs_diff"] == 0.0
    assert verdicts[0]["verdict"] == "match"


def test_resolve_key_tol_override_semantics() -> None:
    # The tolerance-resolution table, pinned directly.
    assert _resolve_key_tol(None, "k") == (None, None, False)  # no tolerance -> exact
    default = ReproTolerance(default_abs_tol=0.5)
    assert _resolve_key_tol(default, "k") == (0.5, None, True)  # default applies
    # An all-absent per_key override wins over a lenient default AND reads as
    # exact (supplied False) — the mutation-critical case.
    override = ReproTolerance(default_abs_tol=99.0, per_key={"k": KeyTolerance()})
    assert _resolve_key_tol(override, "k") == (None, None, False)
    # A per_key entry with a bound fully replaces the default for that key.
    keyed = ReproTolerance(default_abs_tol=99.0, per_key={"k": KeyTolerance(rel_tol=0.1)})
    assert _resolve_key_tol(keyed, "k") == (None, 0.1, True)


def test_is_number_excludes_bool_but_not_int_or_float() -> None:
    # bool rides the equality path, never the numeric-tolerance path — a metric
    # value of True must never be compared as the number 1.
    # MUTATION KILLED: dropping the ``not isinstance(value, bool)`` carve-out.
    assert _is_number(1) is True
    assert _is_number(1.5) is True
    assert _is_number(True) is False
    assert _is_number(False) is False
    assert _is_number("1") is False
    assert _is_nan(math.nan) is True
    assert _is_nan(1.0) is False
    assert _is_nan(1) is False  # never raises for a non-float


# =========================================================================== #
# 2. anti-laundering split — fingerprint-sample ADMISSION rules
# =========================================================================== #
def test_no_cluster_mints_no_sample_but_still_writes_receipt(tmp_path: Path) -> None:
    # A comparison with no known measuring cluster mints NO fingerprint sample
    # (best-effort append), yet the durable RECEIPT is still written — the
    # receipt is the durable record, the sample is the accreting evidence.
    # MUTATION KILLED: dropping ``if not cluster: return None`` in
    # _append_fingerprint_sample (would launder an unclustered sample into the
    # ledger with a fabricated/blank cluster).
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})  # no cluster on either sidecar
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.appended_sample is None  # no cluster -> no sample
    assert not fingerprint_path(tmp_path, CMD_SHA).exists()  # ledger untouched
    # The receipt is still the durable record.
    assert Path(res.receipt_path).exists()
    assert res.receipt["overall"] == "match"


def test_sample_verdict_map_is_the_pinned_projection() -> None:
    # The overall verdict AT APPEND -> the sample's verdict vocabulary. A passing
    # match/auto_cleared is admitted by construction; an incomparable routes to
    # the human (needs_verdict), NEVER silently admitted.
    # MUTATION KILLED: remapping any entry (e.g. incomparable -> auto_cleared,
    # which would launder an incomparable pair into admissible evidence).
    assert _SAMPLE_VERDICT_MAP == {
        "match": "auto_cleared",
        "auto_cleared": "auto_cleared",
        "needs_verdict": "needs_verdict",
        "mismatch": "mismatch",
        "incomparable": "needs_verdict",
    }


def test_incomparable_stage_mints_needs_verdict_sample(tmp_path: Path) -> None:
    # LIVE exercise of the incomparable -> needs_verdict map entry: both metrics
    # present (cluster known, so a sample IS minted) but a mixed match + one-sided
    # key folds the overall to ``incomparable`` -> the minted sample is inadmissible.
    _pair(tmp_path, {"a": 1.0, "b": 1.0}, {"a": 1.0, "c": 1.0}, cluster="hoffman2")
    res = _run(tmp_path)
    assert res.stage_reached == "incomparable"
    assert res.appended_sample is not None  # cluster known -> sample minted
    assert res.appended_sample.verdict == "needs_verdict"


# =========================================================================== #
# 3. needs_decision derived PURELY from the metric stage (per branch)
# =========================================================================== #
def test_needs_decision_is_pure_function_of_metric_stage(tmp_path: Path) -> None:
    # The terminal contract: needs_decision == (stage not in {match, auto_cleared}).
    # A false-negative here is a false "reproduced"; a false-positive wrongly
    # rejects a genuine reproduction. Pinned per reachable branch, in one place.
    # MUTATION KILLED: adding needs_verdict/incomparable to the exempt set, or
    # dropping auto_cleared from it.
    cases: list[tuple[Path, ReproTolerance | None, str]] = []

    d_match = tmp_path / "match"
    d_match.mkdir()
    _pair(d_match, {"pi": 3.14}, {"pi": 3.14})  # empty ledger, exact
    cases.append((d_match, None, "match"))

    d_mis = tmp_path / "mismatch"
    d_mis.mkdir()
    _pair(d_mis, {"pi": 3.14}, {"pi": 9.99})
    cases.append((d_mis, None, "mismatch"))

    d_inc = tmp_path / "incomparable"
    d_inc.mkdir()
    _pair(d_inc, {"a": 1.0}, {"b": 1.0})  # disjoint keys
    cases.append((d_inc, None, "incomparable"))

    d_auto = tmp_path / "auto"
    d_auto.mkdir()
    _well_evidenced_pi(d_auto)
    _pair_pi(d_auto, 3.14, 3.155)  # inside a well-evidenced envelope
    cases.append((d_auto, None, "auto_cleared"))

    d_thin = tmp_path / "thin"
    d_thin.mkdir()
    _plant_sample(d_thin, per_key=[_pi_diff(3.13, 3.16)])
    _plant_sample(d_thin, per_key=[_pi_diff(3.13, 3.16)])
    _pair_pi(d_thin, 3.145, 3.155)  # n=2 thin -> routes to human
    cases.append((d_thin, None, "needs_verdict"))

    for exp, tol, expected_stage in cases:
        res = _run(exp, tol)
        assert res.stage_reached == expected_stage, expected_stage
        expected_needs = expected_stage not in ("match", "auto_cleared")
        assert res.needs_decision == expected_needs, expected_stage


def test_dimension_drift_never_gates_a_matching_metric_verdict(tmp_path: Path) -> None:
    # PURITY: env drift AND hw drift both present, but the metrics MATCH -> the
    # verdict stays ``match`` / needs_decision False. The disclosure dimensions are
    # REPORTED, never folded into the verdict (a drifted box/env is a legitimate
    # reproduction; only the numbers gate).
    # MUTATION KILLED: folding env_known/hw_known/env-status/hw-status into the
    # needs_decision derivation.
    _pair(tmp_path, {"widget": 3.14}, {"widget": 3.14}, cluster="widgetcluster")
    stamp_run_sidecar_env_lock(
        tmp_path, ORIG, env_lock_sha="1" * 64, env_lock_status=STATUS_CAPTURED
    )
    stamp_run_sidecar_env_lock(
        tmp_path, REPRO, env_lock_sha="2" * 64, env_lock_status=STATUS_CAPTURED
    )
    facts_a = {"node": "n-01", "cpu_model": "Widget Xeon", "partition": "gpu"}
    facts_b = {"node": "n-99", "cpu_model": "Widget Xeon", "partition": "gpu"}
    stamp_run_sidecar_hw_facts(
        tmp_path, ORIG, hw_facts=facts_a, hw_sha=hw_sha(facts_a), hw_status=STATUS_CAPTURED
    )
    stamp_run_sidecar_hw_facts(
        tmp_path, REPRO, hw_facts=facts_b, hw_sha=hw_sha(facts_b), hw_status=STATUS_CAPTURED
    )
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.needs_decision is False  # neither dimension gated the verdict
    assert res.receipt["env_identity"]["status"] == "drifted"
    assert res.receipt["hw_identity"]["status"] == "drifted"


# =========================================================================== #
# 4. receipt v1/v2 schema selection — the OR of dimensions (negative pins)
# =========================================================================== #
def test_untiered_undimensioned_mismatch_stays_v1(tmp_path: Path) -> None:
    # An empty-ledger exact mismatch with NO forcing dimension keeps schema_version
    # 1 (byte-identical to a pre-fingerprint receipt) — a mismatch alone must not
    # force v2.
    # MUTATION KILLED: OR-ing a spurious term (e.g. ``stage == 'mismatch'``) into
    # the v2 selection.
    _pair(tmp_path, {"pi": 3.14}, {"pi": 9.99})
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"
    assert res.receipt["schema_version"] == 1
    assert "partial" not in res.receipt  # no v2 partiality block
    assert "envelope_applied" not in res.receipt["per_key"][0]


def test_caller_tolerance_alone_does_not_force_v2(tmp_path: Path) -> None:
    # A caller tolerance on an empty ledger stays v1 (untiered) — tolerance is NOT
    # one of the v2-forcing dimensions.
    # MUTATION KILLED: OR-ing ``spec.tolerance is not None`` into the selection.
    _pair(tmp_path, {"pi": 3.14000}, {"pi": 3.14100})
    res = _run(tmp_path, ReproTolerance(default_abs_tol=0.01))
    assert res.stage_reached == "match"
    assert res.receipt["schema_version"] == 1
    assert "tier_reason" not in res.receipt["per_key"][0]


# =========================================================================== #
# 5. reproduce-side mint helpers (the MINT half's identity guards + brief)
# =========================================================================== #
def test_cmd_sha_matches_is_prefix_tolerant_both_directions() -> None:
    # A legacy 8-char recorded prefix and a full 64-char current sha are the SAME
    # identity (either may be the prefix) — but two genuinely different shas, or an
    # empty side, never match (an empty recorded must not vacuously "reproduce").
    # MUTATION KILLED: dropping either ``startswith`` clause, or the empty guard.
    assert _cmd_sha_matches("a" * 8, "a" * 64) is True  # recorded prefix of current
    assert _cmd_sha_matches("a" * 64, "a" * 8) is True  # current prefix of recorded
    assert _cmd_sha_matches("a" * 64, "a" * 64) is True
    assert _cmd_sha_matches("a" * 64, "b" * 64) is False
    assert _cmd_sha_matches("", "a" * 64) is False
    assert _cmd_sha_matches("a" * 64, "") is False


def test_first_differing_task_boundaries() -> None:
    # The param-drift evidence: the first task index whose params moved, or None.
    # MUTATION KILLED: off-by-one in the loop, or mishandling the length boundary /
    # the no-pre-image (None) legacy case.
    assert _first_differing_task(None, None) is None  # no pre-image
    assert _first_differing_task([{"s": 0}], [{"s": 0}]) is None  # equal
    assert _first_differing_task([{"s": 0}, {"s": 1}], [{"s": 0}, {"s": 99}]) == 1
    # length boundary: current has an extra task -> the boundary index is returned.
    assert _first_differing_task([{"s": 0}], [{"s": 0}, {"s": 1}]) == 1
    assert _first_differing_task([], [{"s": 0}]) == 0


def test_repro_remote_path_refuses_empty_original() -> None:
    # The disjointness guard CAN fire: no original remote_path to derive a disjoint
    # sibling from is refused (v1 never reconstructs a phantom path).
    # MUTATION KILLED: dropping the empty-original refusal.
    assert _repro_remote_path("/scratch/exp") == "/scratch/exp-repro"
    # A trailing slash is stripped before the suffix (sibling, not nested).
    assert _repro_remote_path("/scratch/exp/") == "/scratch/exp-repro"
    with pytest.raises(errors.SpecInvalid):
        _repro_remote_path("")


def test_env_lock_mint_disclosure_captured_vs_not() -> None:
    # The reproduce-side mint brief's env disclosure (U-ENV1): captured names the
    # recorded env_lock so the human knows an env comparison is PENDING at verify;
    # a null / could-not-capture original discloses not_captured.
    # MUTATION KILLED: inverting the captured/not_captured branch, or dropping the
    # recorded sha off the brief.
    assert _env_lock_disclosure({"env_lock_sha": "e" * 64}) == {
        "status": "captured",
        "original_env_lock": "e" * 64,
    }
    assert _env_lock_disclosure({}) == {"status": "not_captured", "original_env_lock": None}
    assert _env_lock_disclosure({"env_lock_sha": None}) == {
        "status": "not_captured",
        "original_env_lock": None,
    }


def test_hw_facts_mint_disclosure_captured_vs_not() -> None:
    # The reproduce-side mint brief's hardware disclosure (U-HW1): captured names
    # the recorded hw_sha; a null hw discloses not_captured.
    # MUTATION KILLED: inverting the branch, or dropping the recorded sha.
    assert _hw_facts_disclosure({"hw_sha": "h" * 64}) == {
        "status": "captured",
        "original_hw_sha": "h" * 64,
    }
    assert _hw_facts_disclosure({}) == {"status": "not_captured", "original_hw_sha": None}
    assert _hw_facts_disclosure({"hw_sha": None}) == {
        "status": "not_captured",
        "original_hw_sha": None,
    }


# --------------------------------------------------------------------------- #
# receipt-echo shape: the appended sample echoes only when wire-valid
# --------------------------------------------------------------------------- #
def test_appended_sample_echo_is_wire_valid(tmp_path: Path) -> None:
    # The echoed sample, when present, validates against the wire model — the
    # result surfaces the evidence just recorded.
    _pair_pi(tmp_path, 3.14, 3.14)  # exact, cluster known -> sample minted
    res = _run(tmp_path)
    assert res.appended_sample is not None
    DeterminismSampleRecord.model_validate(
        res.appended_sample.model_dump(mode="json")
    )  # round-trips
    assert res.appended_sample.verdict == "auto_cleared"  # match -> passing
