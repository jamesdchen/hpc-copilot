"""Pinning tests for the ``verify-reproduction`` comparator + receipt.

Fixtures use the REAL sidecar writer (``write_run_sidecar`` with ``reproduces``)
and hand-written ``metrics_aggregate.json`` files, so the artifact ladder is
exercised end-to-end. The comparator rules, the ladder fallback, the
mismatch-is-a-finding contract, the refusals, and the append-only receipt are
all pinned here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.verify_reproduction import (
    KeyTolerance,
    ReproTolerance,
    VerifyReproductionSpec,
)
from hpc_agent.ops.verify_reproduction import (
    _compare_metrics,
    _fold_overall,
    verify_reproduction,
)
from hpc_agent.state.runs import write_run_sidecar

ORIG = "orig-run"
REPRO = "repro-run"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _write_sidecar(exp: Path, run_id: str, *, reproduces: str | None = None, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python train.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": "b" * 64,
    }
    kwargs.update(over)
    write_run_sidecar(exp, reproduces=reproduces, **kwargs)


def _write_aggregate(exp: Path, run_id: str, aggregated_metrics: dict[str, Any]) -> Path:
    agg = exp / "_aggregated" / run_id
    agg.mkdir(parents=True, exist_ok=True)
    path = agg / "metrics_aggregate.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "aggregated_metrics": aggregated_metrics}, fh)
    return path


def _write_combiner_wave(exp: Path, run_id: str, wave: int, grid_points: dict[str, Any]) -> Path:
    cdir = exp / "_aggregated" / run_id / "_combiner"
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"wave_{wave}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"grid_points": grid_points, "errors": []}, fh)
    return path


def _pair(
    exp: Path,
    orig_metrics: dict[str, Any] | None,
    repro_metrics: dict[str, Any] | None,
    *,
    reproduces: str | None = ORIG,
) -> None:
    """Write both sidecars + (optionally) both metrics_aggregate.json files."""
    _write_sidecar(exp, ORIG)
    _write_sidecar(exp, REPRO, reproduces=reproduces)
    if orig_metrics is not None:
        _write_aggregate(exp, ORIG, {"gp": orig_metrics})
    if repro_metrics is not None:
        _write_aggregate(exp, REPRO, {"gp": repro_metrics})


def _run(exp: Path, tolerance: ReproTolerance | None = None):
    spec = VerifyReproductionSpec(original_run_id=ORIG, repro_run_id=REPRO, tolerance=tolerance)
    return verify_reproduction(exp, spec=spec)


# --------------------------------------------------------------------------- #
# exact + tolerance matching
# --------------------------------------------------------------------------- #
def test_exact_match_default(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14159, "n_samples": 1000}, {"pi": 3.14159, "n_samples": 1000})
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.needs_decision is False


def test_exact_mismatch_is_successful_finding(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14159}, {"pi": 3.20000})
    # A mismatch must NOT raise — it is a successful run with a finding.
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"
    assert res.needs_decision is True


def test_abs_tolerance_covers_small_diff(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14000}, {"pi": 3.14100})
    res = _run(tmp_path, ReproTolerance(default_abs_tol=0.01))
    assert res.stage_reached == "match"


def test_abs_tolerance_too_tight_mismatches(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14000}, {"pi": 3.30000})
    res = _run(tmp_path, ReproTolerance(default_abs_tol=0.01))
    assert res.stage_reached == "mismatch"


def test_rel_tolerance_covers_proportional_diff(tmp_path: Path) -> None:
    _pair(tmp_path, {"loss": 100.0}, {"loss": 101.0})
    res = _run(tmp_path, ReproTolerance(default_rel_tol=0.02))
    assert res.stage_reached == "match"


def test_per_key_tolerance_overrides_default(tmp_path: Path) -> None:
    # gp.a within its per-key abs tol; gp.b exact (no default) and equal.
    _pair(tmp_path, {"a": 1.0, "b": 5.0}, {"a": 1.5, "b": 5.0})
    tol = ReproTolerance(per_key={"gp.a": KeyTolerance(abs_tol=1.0)})
    res = _run(tmp_path, tol)
    assert res.stage_reached == "match"
    verdicts = {e["key"]: e["verdict"] for e in res.receipt["per_key"]}
    assert verdicts == {"gp.a": "match", "gp.b": "match"}


def test_n_samples_no_special_casing(tmp_path: Path) -> None:
    # n_samples compares like any other number; a big diff with no tol -> mismatch.
    _pair(tmp_path, {"n_samples": 1000}, {"n_samples": 2000})
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"


# --------------------------------------------------------------------------- #
# incomparable cases
# --------------------------------------------------------------------------- #
def test_incomparable_missing_repro_artifact(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, None)  # repro has no metrics_aggregate.json
    res = _run(tmp_path)
    assert res.stage_reached == "incomparable"
    assert res.needs_decision is True
    assert "repro" in res.reason
    assert res.receipt["per_key"] == []


def test_incomparable_disjoint_keys(tmp_path: Path) -> None:
    _pair(tmp_path, {"a": 1.0}, {"b": 1.0})
    res = _run(tmp_path)
    assert res.stage_reached == "incomparable"
    verdicts = {e["key"]: e["verdict"] for e in res.receipt["per_key"]}
    assert verdicts == {"gp.a": "incomparable", "gp.b": "incomparable"}


def test_incomparable_non_numeric_with_tolerance(tmp_path: Path) -> None:
    _pair(tmp_path, {"label": "cat"}, {"label": "cat"})
    res = _run(tmp_path, ReproTolerance(default_abs_tol=0.1))
    assert res.stage_reached == "incomparable"


def test_non_numeric_equality_match(tmp_path: Path) -> None:
    _pair(tmp_path, {"label": "cat"}, {"label": "cat"})
    res = _run(tmp_path)  # no tolerance -> equality
    assert res.stage_reached == "match"


def test_non_numeric_equality_mismatch(tmp_path: Path) -> None:
    _pair(tmp_path, {"label": "cat"}, {"label": "dog"})
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"


def test_nan_is_incomparable_not_a_raw_neq(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": float("nan")}, {"pi": 3.14})
    res = _run(tmp_path)
    assert res.stage_reached == "incomparable"
    entry = res.receipt["per_key"][0]
    assert entry["verdict"] == "incomparable"


# --------------------------------------------------------------------------- #
# artifact ladder fallback
# --------------------------------------------------------------------------- #
def test_combiner_wave_fallback_ladder(tmp_path: Path) -> None:
    # No metrics_aggregate.json anywhere; both runs only have _combiner waves.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _write_combiner_wave(tmp_path, ORIG, 0, {"gp": {"pi": 3.14, "n_samples": 1000}})
    _write_combiner_wave(tmp_path, REPRO, 0, {"gp": {"pi": 3.14, "n_samples": 1000}})
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.receipt["sources"]["original_artifact"].endswith("_combiner")
    assert res.receipt["sources"]["repro_artifact"].endswith("_combiner")


def test_metrics_aggregate_preferred_over_combiner(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    # A stale combiner wave that would DISAGREE must be ignored (rung 1 wins).
    _write_combiner_wave(tmp_path, REPRO, 0, {"gp": {"pi": 9.99, "n_samples": 1}})
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.receipt["sources"]["repro_artifact"].endswith("metrics_aggregate.json")


# --------------------------------------------------------------------------- #
# refusals (genuine reproduction pair only)
# --------------------------------------------------------------------------- #
def test_refuses_when_reproduces_is_none(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14}, reproduces=None)
    with pytest.raises(errors.SpecInvalid):
        _run(tmp_path)


def test_refuses_when_reproduces_names_wrong_original(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14}, reproduces="some-other-run")
    with pytest.raises(errors.SpecInvalid):
        _run(tmp_path)


def test_refuses_when_repro_sidecar_missing(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG)  # no repro sidecar
    with pytest.raises(errors.SpecInvalid):
        _run(tmp_path)


def test_refuses_when_original_sidecar_missing(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)  # no original sidecar
    with pytest.raises(errors.SpecInvalid):
        _run(tmp_path)


# --------------------------------------------------------------------------- #
# receipt: self-contained, verbatim identity, append-only
# --------------------------------------------------------------------------- #
def test_receipt_line_appended_and_self_contained(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14159}, {"pi": 3.14159})
    res = _run(tmp_path)
    ledger = Path(res.receipt_path)
    assert ledger.name == "reproduction_receipts.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == res.receipt  # returned receipt IS the persisted line
    for field in (
        "ts",
        "schema_version",
        "original",
        "repro",
        "tolerance_spec",
        "per_key",
        "overall",
        "sources",
    ):
        assert field in record
    assert record["schema_version"] == 1
    assert record["overall"] == "match"
    assert record["tolerance_spec"] is None  # exact -> null echo


def test_receipt_identity_lifted_verbatim_from_sidecar(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG, cmd_sha="c" * 64, env_hash="env-orig", cluster="hoffman2")
    _write_sidecar(
        tmp_path,
        REPRO,
        reproduces=ORIG,
        cmd_sha="c" * 64,
        env_hash="env-repro",
        cluster="discovery",
    )
    _write_aggregate(tmp_path, ORIG, {"gp": {"pi": 3.14}})
    _write_aggregate(tmp_path, REPRO, {"gp": {"pi": 3.14}})
    res = _run(tmp_path)
    assert res.receipt["original"]["cmd_sha"] == "c" * 64
    assert res.receipt["original"]["env_hash"] == "env-orig"
    assert res.receipt["original"]["cluster"] == "hoffman2"
    assert res.receipt["repro"]["env_hash"] == "env-repro"
    assert res.receipt["repro"]["cluster"] == "discovery"


def test_tolerance_spec_echoed_verbatim(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    tol = ReproTolerance(default_abs_tol=0.01, per_key={"gp.pi": KeyTolerance(rel_tol=0.5)})
    res = _run(tmp_path, tol)
    assert res.receipt["tolerance_spec"]["default_abs_tol"] == 0.01
    assert res.receipt["tolerance_spec"]["per_key"]["gp.pi"]["rel_tol"] == 0.5


def test_second_verify_appends_a_second_line(tmp_path: Path) -> None:
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    _run(tmp_path)
    res2 = _run(tmp_path)
    lines = Path(res2.receipt_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append-only, no dedup — each verify is its own event


# --------------------------------------------------------------------------- #
# overall-fold property
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["match", "match"], "match"),
        (["match", "mismatch"], "mismatch"),
        (["match", "incomparable"], "incomparable"),
        (["mismatch", "incomparable"], "mismatch"),
        (["incomparable", "incomparable"], "incomparable"),
        ([], "incomparable"),
    ],
)
def test_overall_fold_precedence(verdicts: list[str], expected: str) -> None:
    per_key = [{"verdict": v} for v in verdicts]
    assert _fold_overall(per_key) == expected


def test_compare_metrics_pure_shape() -> None:
    verdicts = _compare_metrics({"a": 1.0, "b": 2.0}, {"a": 1.0, "c": 3.0}, None)
    by_key = {e["key"]: e for e in verdicts}
    assert by_key["a"]["verdict"] == "match"
    assert by_key["b"]["verdict"] == "incomparable"  # present on one side
    assert by_key["c"]["verdict"] == "incomparable"
    # numeric match carries diffs; the schema is the 7 declared fields.
    assert set(by_key["a"]) == {
        "key",
        "original",
        "repro",
        "abs_diff",
        "rel_diff",
        "verdict",
        "tolerance_applied",
    }
    assert by_key["a"]["abs_diff"] == 0.0


def test_compare_metrics_nan_via_pure(tmp_path: Path) -> None:
    verdicts = _compare_metrics({"x": math.nan}, {"x": math.nan}, None)
    assert verdicts[0]["verdict"] == "incomparable"


# --------------------------------------------------------------------------- #
# determinism-fingerprint overlay (schema_version 2 — D-consume)
# --------------------------------------------------------------------------- #
from hpc_agent._wire.queries.verify_reproduction import ReproductionReceipt  # noqa: E402
from hpc_agent.infra.io import append_jsonl_line  # noqa: E402
from hpc_agent.ops.verify_reproduction import _validate_receipt_partiality  # noqa: E402
from hpc_agent.state.decision_journal import append_decision  # noqa: E402
from hpc_agent.state.determinism import PerKeyDiff, build_sample_record  # noqa: E402
from hpc_agent.state.fingerprint_store import fingerprint_path, read_samples  # noqa: E402

CMD_SHA = "a" * 64  # matches _write_sidecar's default cmd_sha
TASKS_PY_SHA = "b" * 64
EXECUTOR = "python train.py"


def _pi_diff(a: float, b: float) -> PerKeyDiff:
    """One ``gp.pi`` float observation for a hand-planted ledger sample."""
    abs_diff = abs(a - b)
    denom = max(abs(a), abs(b))
    return PerKeyDiff("gp.pi", a, b, abs_diff, abs_diff / denom if denom else 0.0, "float")


def _plant_sample(
    exp: Path,
    *,
    per_key: list[PerKeyDiff],
    verdict: str = "auto_cleared",
    scale: str = "main",
    cluster: str = "hoffman2",
    source: str = "verify-reproduction",
    partial: bool = False,
    task_indices: list[int] | None = None,
    same_submission: bool = False,
    content_sha: str = "d" * 64,
    run_ids: tuple[str, str] = ("o-prior", "r-prior"),
) -> dict[str, Any]:
    """Append one valid fingerprint sample line to the experiment ledger."""
    identity = {"cmd_sha": CMD_SHA, "tasks_py_sha": TASKS_PY_SHA, "executor": EXECUTOR}
    record = build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha=content_sha,
        identity=identity,
        source=source,
        run_ids=list(run_ids),
        cluster=cluster,
        scale=scale,
        verdict=verdict,
        per_key=per_key,
        same_submission=same_submission,
        partial=partial,
        task_indices=task_indices,
    )
    append_jsonl_line(fingerprint_path(exp, CMD_SHA), record)
    return record


def _well_evidenced_pi(exp: Path, *, lo: float = 3.13, hi: float = 3.16) -> None:
    """Plant 3 admitted main-scale hoffman2 samples spanning [lo, hi] on gp.pi."""
    for _ in range(3):
        _plant_sample(exp, per_key=[_pi_diff(lo, hi)])


def _pair_pi(exp: Path, orig: float, repro: float, *, cluster: str = "hoffman2") -> None:
    _write_sidecar(exp, ORIG, cluster=cluster)
    _write_sidecar(exp, REPRO, reproduces=ORIG, cluster=cluster)
    _write_aggregate(exp, ORIG, {"gp": {"pi": orig}})
    _write_aggregate(exp, REPRO, {"gp": {"pi": repro}})


def test_auto_cleared_inside_well_evidenced_envelope(tmp_path: Path) -> None:
    _well_evidenced_pi(tmp_path)
    _pair_pi(tmp_path, 3.14, 3.155)  # both inside [3.13, 3.16], nonzero deviation
    res = _run(tmp_path)
    assert res.stage_reached == "auto_cleared"
    assert res.needs_decision is False
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert entry["tier_reason"] == "within_evidenced_envelope"
    assert entry["envelope_applied"]["class"] == "stochastic"
    assert entry["envelope_applied"]["evidence"]["n"] == 3


def test_needs_verdict_thin_inside(tmp_path: Path) -> None:
    # n=2 thin envelope: a deviation INSIDE still routes to the human.
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _pair_pi(tmp_path, 3.145, 3.155)  # inside [3.13, 3.16], nonzero
    res = _run(tmp_path)
    assert res.stage_reached == "needs_verdict"
    assert res.needs_decision is True
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert entry["tier_reason"] == "within_thin_envelope"


def test_needs_verdict_thin_outside(tmp_path: Path) -> None:
    # n=2 thin envelope: a deviation OUTSIDE routes to the human, NOT a mismatch.
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _pair_pi(tmp_path, 3.10, 3.20)  # outside [3.13, 3.16]
    res = _run(tmp_path)
    assert res.stage_reached == "needs_verdict"
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert entry["tier_reason"] == "outside_thin_envelope"


def test_mismatch_outside_well_evidenced_envelope(tmp_path: Path) -> None:
    _well_evidenced_pi(tmp_path)
    _pair_pi(tmp_path, 3.14, 3.30)  # well outside [3.13, 3.16]
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"
    assert res.needs_decision is True
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert entry["tier_reason"] == "outside_evidenced_envelope"


def test_caller_override_labeling(tmp_path: Path) -> None:
    # A stochastic envelope makes the comparison tiered; a caller tolerance that
    # decides the key is labeled caller_override with NO envelope applied.
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _pair_pi(tmp_path, 3.14, 3.20)
    res = _run(tmp_path, ReproTolerance(default_abs_tol=0.1))
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert entry["tier_reason"] == "caller_override"
    assert entry["envelope_applied"] is None
    assert entry["verdict"] == "match"  # 0.06 <= 0.1


def test_receipt_v2_fields_present_and_parse(tmp_path: Path) -> None:
    _well_evidenced_pi(tmp_path)
    _pair_pi(tmp_path, 3.14, 3.155)
    res = _run(tmp_path)
    assert res.receipt["schema_version"] == 2
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.pi"]
    assert "envelope_applied" in entry
    assert "tier_reason" in entry
    for field in ("partial", "task_indices", "uncompared_keys", "uncompared_tasks"):
        assert field in res.receipt
    # The v2 receipt validates against the authoring wire model.
    ReproductionReceipt.model_validate(res.receipt)


def test_v1_receipt_still_parses(tmp_path: Path) -> None:
    # An empty-ledger exact match keeps the v1 (schema_version 1) posture.
    _pair(tmp_path, {"pi": 3.14159}, {"pi": 3.14159})
    res = _run(tmp_path)
    assert res.receipt["schema_version"] == 1
    assert "envelope_applied" not in res.receipt["per_key"][0]
    ReproductionReceipt.model_validate(res.receipt)  # v1 line parses under v2 model


# --------------------------------------------------------------------------- #
# append-back + judge-before-append (D-consume clause 1)
# --------------------------------------------------------------------------- #
def test_sample_appended_when_cluster_known(tmp_path: Path) -> None:
    _pair_pi(tmp_path, 3.14, 3.14)  # exact, empty ledger -> match, but sample minted
    res = _run(tmp_path)
    assert res.appended_sample is not None
    assert res.appended_sample.verdict == "auto_cleared"  # match -> passing verdict
    assert res.appended_sample.scale == "main"
    samples, _ = read_samples(tmp_path, CMD_SHA)
    assert len(samples) == 1


def test_judge_before_append_own_sample_does_not_flip(tmp_path: Path) -> None:
    # 2 prior samples => n=2 THIN. If this comparison's OWN sample were counted
    # pre-judgment, n would reach 3 (well-evidenced, same scale+cluster) and the
    # inside deviation would auto_clear. Judge-before-append => it stays thin.
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _plant_sample(tmp_path, per_key=[_pi_diff(3.13, 3.16)])
    _pair_pi(tmp_path, 3.145, 3.155)  # inside [3.13, 3.16]
    res = _run(tmp_path)
    assert res.stage_reached == "needs_verdict"  # NOT auto_cleared
    # The comparison was appended back (now 3 lines) but its own sample is
    # inadmissible (needs_verdict) so it never widens the envelope.
    samples, _ = read_samples(tmp_path, CMD_SHA)
    assert len(samples) == 3
    assert res.appended_sample is not None
    assert res.appended_sample.verdict == "needs_verdict"


# --------------------------------------------------------------------------- #
# partial (per-task) reproduction — design center 5
# --------------------------------------------------------------------------- #
def _write_per_task(exp: Path, run_id: str, idx: int, metrics: dict[str, Any]) -> None:
    d = exp / "_aggregated" / run_id / "_per_task" / str(idx)
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")


def test_partial_per_task_compare_and_accounting(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG, cluster="hoffman2")
    _write_sidecar(
        tmp_path, REPRO, reproduces=ORIG, cluster="hoffman2", extra={"task_sample": [0, 2]}
    )
    for run in (ORIG, REPRO):
        _write_per_task(tmp_path, run, 0, {"acc": 0.9})
        _write_per_task(tmp_path, run, 2, {"acc": 0.8})
    res = _run(tmp_path)
    assert res.receipt["schema_version"] == 2
    assert res.receipt["partial"] is True
    assert res.receipt["task_indices"] == [0, 2]
    assert res.receipt["uncompared_tasks"] == 0
    assert res.receipt["uncompared_keys"] == 0
    assert res.stage_reached == "match"  # per-task exact, empty ledger
    keys = {e["key"] for e in res.receipt["per_key"]}
    assert keys == {"task0.acc", "task2.acc"}


def test_partial_uncompared_task_accounted(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG, cluster="hoffman2")
    _write_sidecar(
        tmp_path, REPRO, reproduces=ORIG, cluster="hoffman2", extra={"task_sample": [0, 1, 2]}
    )
    # Task 1 is absent on BOTH sides -> uncompared.
    for run in (ORIG, REPRO):
        _write_per_task(tmp_path, run, 0, {"acc": 0.9})
        _write_per_task(tmp_path, run, 2, {"acc": 0.8})
    res = _run(tmp_path)
    assert res.receipt["task_indices"] == [0, 2]
    assert res.receipt["uncompared_tasks"] == 1


def test_partial_receipt_missing_field_refused() -> None:
    with pytest.raises(errors.SpecInvalid):
        _validate_receipt_partiality({"partial": True, "task_indices": None})
    with pytest.raises(errors.SpecInvalid):
        _validate_receipt_partiality(
            {"partial": True, "task_indices": [0], "uncompared_keys": None, "uncompared_tasks": 0}
        )
    # A full receipt is never refused.
    _validate_receipt_partiality({"partial": False})


# --------------------------------------------------------------------------- #
# the data-trace fingerprint interlock (docs/design/data-trace.md)
# --------------------------------------------------------------------------- #
from hpc_agent.state.data_trace import make_record, write_trace  # noqa: E402


def _toy_trace(
    exp: Path, run_id: str, digests: dict[str, str], *, rows: dict[str, int] | None = None
) -> None:
    """Write an ingested-store toy trace: one record per stage, seq = pipeline order."""
    rows = rows or {}
    records = [
        make_record(
            stage,
            seq,
            {
                "digest": sha,
                "row_count": {"rows": rows.get(stage, 100), "dropped": 0},
            },
            created_at="2026-01-01T00:00:00Z",
        )
        for seq, (stage, sha) in enumerate(digests.items())
    ]
    write_trace(exp, "run", run_id, 0, records)


def _clustered_pair(exp: Path, orig: dict[str, Any], repro: dict[str, Any]) -> None:
    """A pair with a known cluster, so the fingerprint sample is minted."""
    _write_sidecar(exp, ORIG, cluster="hoffman2")
    _write_sidecar(exp, REPRO, reproduces=ORIG, cluster="hoffman2")
    _write_aggregate(exp, ORIG, {"gp": orig})
    _write_aggregate(exp, REPRO, {"gp": repro})


def test_traced_pair_stage_keys_present_and_exact(tmp_path: Path) -> None:
    # Both runs traced, identical stage digests -> the namespaced stage keys ride
    # the comparison as exact-class entries and everything still matches.
    _clustered_pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    digests = {"load": "1" * 64, "scaling": "2" * 64, "fit": "3" * 64}
    _toy_trace(tmp_path, ORIG, digests)
    _toy_trace(tmp_path, REPRO, digests)
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.diverged_stage is None
    keys = {e["key"] for e in res.receipt["per_key"]}
    assert "stage:scaling.digest" in keys
    assert "stage:scaling.row_count" in keys
    by_key = {e["key"]: e for e in res.receipt["per_key"]}
    assert by_key["stage:scaling.digest"]["verdict"] == "match"
    # Exact semantics: a sha is a string, no tolerance ever applies.
    assert by_key["stage:scaling.digest"]["tolerance_applied"] is None
    # The interlock disclosure rides the (forced-v2) receipt.
    assert res.receipt["schema_version"] == 2
    interlock = res.receipt["stage_interlock"]
    assert interlock["compared"] is True
    assert interlock["original_trace_present"] is True
    assert interlock["repro_trace_present"] is True
    assert "stage:scaling.digest" in interlock["stage_keys"]
    assert res.receipt["diverged_stage"] is None
    ReproductionReceipt.model_validate(res.receipt)
    # Envelope accrual: the stage keys fold into the appended sample's per_key
    # (the SAME machinery, no new admission rule).
    assert res.appended_sample is not None
    sample_keys = {d.key for d in res.appended_sample.per_key}
    assert "stage:scaling.digest" in sample_keys


def test_planted_stage_divergence_localizes_to_named_stage(tmp_path: Path) -> None:
    # FIRES: a planted digest divergence at 'scaling' (and downstream 'fit')
    # localizes to 'scaling' — the FIRST diverging stage by pipeline order (seq).
    _clustered_pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    _toy_trace(tmp_path, ORIG, {"load": "1" * 64, "scaling": "2" * 64, "fit": "3" * 64})
    _toy_trace(tmp_path, REPRO, {"load": "1" * 64, "scaling": "9" * 64, "fit": "8" * 64})
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"  # a sha is exact-class; it moved
    assert res.needs_decision is True
    assert res.diverged_stage == "scaling"
    assert res.receipt["diverged_stage"] == "scaling"
    assert "diverges at stage 'scaling'" in res.reason
    by_key = {e["key"]: e for e in res.receipt["per_key"]}
    assert by_key["stage:scaling.digest"]["verdict"] == "mismatch"
    assert by_key["stage:load.digest"]["verdict"] == "match"
    ReproductionReceipt.model_validate(res.receipt)
    # The diverging stage key rides the appended sample (fingerprint-admissible).
    assert res.appended_sample is not None
    sample_by_key = {d.key: d for d in res.appended_sample.per_key}
    assert sample_by_key["stage:scaling.digest"].a != sample_by_key["stage:scaling.digest"].b


def test_row_count_divergence_localizes(tmp_path: Path) -> None:
    # A row_count move alone (digests identical) still localizes to its stage.
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    digests = {"load": "1" * 64, "pool": "2" * 64}
    _toy_trace(tmp_path, ORIG, digests, rows={"load": 246059, "pool": 218905})
    _toy_trace(tmp_path, REPRO, digests, rows={"load": 246059, "pool": 218900})
    res = _run(tmp_path)
    assert res.stage_reached == "mismatch"
    assert res.diverged_stage == "pool"


def test_one_side_traced_disclosed_not_fabricated(tmp_path: Path) -> None:
    # Only the original is traced: nothing folded (no stage keys, no divergence),
    # the presence/absence DISCLOSED on the receipt — never fabricated.
    _pair(tmp_path, {"pi": 3.14}, {"pi": 3.14})
    _toy_trace(tmp_path, ORIG, {"load": "1" * 64})
    res = _run(tmp_path)
    assert res.stage_reached == "match"
    assert res.diverged_stage is None
    keys = {e["key"] for e in res.receipt["per_key"]}
    assert not any(k.startswith("stage:") for k in keys)
    assert res.receipt["schema_version"] == 2
    interlock = res.receipt["stage_interlock"]
    assert interlock == {
        "original_trace_present": True,
        "repro_trace_present": False,
        "compared": False,
        "stage_keys": [],
    }
    assert res.receipt["diverged_stage"] is None
    ReproductionReceipt.model_validate(res.receipt)


def test_untraced_pair_receipt_byte_identical_to_pre_interlock(tmp_path: Path) -> None:
    # The no-trace regression pin: no traces on either side -> the receipt carries
    # NO interlock fields and keeps the v1 posture (byte-identical to today).
    _pair(tmp_path, {"pi": 3.14159}, {"pi": 3.14159})
    res = _run(tmp_path)
    assert res.receipt["schema_version"] == 1
    assert "stage_interlock" not in res.receipt
    assert "diverged_stage" not in res.receipt
    assert res.diverged_stage is None
    assert set(res.receipt) == {
        "ts",
        "schema_version",
        "original",
        "repro",
        "tolerance_spec",
        "per_key",
        "overall",
        "sources",
    }


def test_auto_cleared_verdict_never_names_a_stage(tmp_path: Path) -> None:
    # diverged_stage surfaces ONLY on a routed verdict; a matching traced pair
    # (even with an envelope in play) names no stage.
    _well_evidenced_pi(tmp_path)
    _pair_pi(tmp_path, 3.14, 3.155)  # auto_cleared inside the envelope
    digests = {"load": "1" * 64}
    _toy_trace(tmp_path, ORIG, digests)
    _toy_trace(tmp_path, REPRO, digests)
    res = _run(tmp_path)
    assert res.stage_reached == "auto_cleared"
    assert res.diverged_stage is None
    assert res.receipt["diverged_stage"] is None


def test_human_acceptance_admits_a_mismatch_sample(tmp_path: Path) -> None:
    # A planted mismatch sample is inadmissible until a reproduction-verdict
    # accept names its content_sha; once accepted it widens the envelope.
    sha = "e" * 64
    _well_evidenced_pi(tmp_path, lo=3.13, hi=3.16)
    _plant_sample(
        tmp_path,
        per_key=[_pi_diff(3.10, 3.40)],
        verdict="mismatch",
        content_sha=sha,
        run_ids=("o-x", "r-x"),
    )
    # Before acceptance: 3.30 is OUTSIDE [3.13, 3.16] -> mismatch.
    _pair_pi(tmp_path, 3.14, 3.30)
    assert _run(tmp_path).stage_reached == "mismatch"
    # Human accepts the planted mismatch sample on the repro run scope.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r-x",
        block="reproduction-verdict",
        response=f"accept {sha[:8]}",
        resolved={"accept": True, "content_sha": sha},
    )
    # Now the envelope spans [3.10, 3.40]; 3.30 is inside -> auto_cleared.
    _pair_pi(tmp_path, 3.14, 3.30)
    assert _run(tmp_path).stage_reached == "auto_cleared"


# --------------------------------------------------------------------------- #
# external-baseline (claim-check) mode — onboard-by-reproduction (Phase 6.5)
# --------------------------------------------------------------------------- #
from pydantic import ValidationError  # noqa: E402

from hpc_agent._wire.queries.verify_reproduction import (  # noqa: E402
    ClaimCheckReceipt,
    ExternalBaseline,
)
from hpc_agent.ops.verify_reproduction import (  # noqa: E402
    CLAIM_CONSISTENT_SENTENCE,
    _assert_receipt_kind_matches_baseline,
    _run_claim_check,
)


def _fresh_run(exp: Path, gp_metrics: dict[str, Any], **over: Any) -> None:
    """A fresh OBSERVED run (no `reproduces` link — the claim is the baseline)."""
    _write_sidecar(exp, REPRO, **over)
    _write_aggregate(exp, REPRO, {"gp": gp_metrics})


def _claim_spec(
    claimed_values: dict[str, Any],
    *,
    tolerance: ReproTolerance | None = None,
    claimed_data_sha: str | None = None,
) -> VerifyReproductionSpec:
    return VerifyReproductionSpec(
        repro_run_id=REPRO,
        external_baseline=ExternalBaseline(
            claimed_values=claimed_values,
            tolerance=tolerance,
            claimed_data_sha=claimed_data_sha,
        ),
    )


def test_claim_check_match_emits_consistency_sentence(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "match"
    assert res.needs_decision is False
    # The consistency sentence is CODE-emitted (a fixed module constant), not composed.
    assert res.reason == CLAIM_CONSISTENT_SENTENCE
    assert res.receipt["consistency"] == CLAIM_CONSISTENT_SENTENCE
    assert res.receipt["drift_disclosure"] is None


def test_claim_check_match_within_tolerance(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14100})
    res = verify_reproduction(
        tmp_path,
        spec=_claim_spec({"gp.pi": 3.14000}, tolerance=ReproTolerance(default_abs_tol=0.01)),
    )
    assert res.stage_reached == "match"
    assert res.reason == CLAIM_CONSISTENT_SENTENCE


def test_claim_check_mismatch_is_a_finding_never_blocking(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.30000})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    # A mismatch must NOT raise — exit-0, needs_decision finding.
    assert res.stage_reached == "mismatch"
    assert res.needs_decision is True
    assert res.receipt["consistency"] is None
    # No manifest at claim time -> the disclosed drift phrasing.
    assert "cannot distinguish result decay from data drift" in res.receipt["drift_disclosure"]
    assert "claim-check finding: mismatch" in res.reason


def test_claim_check_mismatch_with_manifest_names_data_dimension(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.30000}, data_sha="observed-data-sha-xyz")
    res = verify_reproduction(
        tmp_path, spec=_claim_spec({"gp.pi": 3.14159}, claimed_data_sha="claimed-data-sha-abc")
    )
    assert res.stage_reached == "mismatch"
    assert "the data changed since the claim" in res.receipt["drift_disclosure"]
    assert "claimed-data" in res.receipt["drift_disclosure"]


def test_claim_check_mismatch_manifest_unchanged_points_elsewhere(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.30000}, data_sha="same-data-sha")
    res = verify_reproduction(
        tmp_path, spec=_claim_spec({"gp.pi": 3.14159}, claimed_data_sha="same-data-sha")
    )
    assert res.stage_reached == "mismatch"
    assert "data is unchanged since the claim" in res.receipt["drift_disclosure"]
    assert "code/env or result decay" in res.receipt["drift_disclosure"]


def test_claim_check_embeds_claim_verbatim(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    tol = ReproTolerance(default_rel_tol=0.01)
    res = verify_reproduction(
        tmp_path,
        spec=_claim_spec({"gp.pi": 0.1203}, tolerance=tol, claimed_data_sha="d-sha"),
    )
    claim = res.receipt["claim"]
    assert claim["claimed_values"] == {"gp.pi": 0.1203}
    assert claim["tolerance"]["default_rel_tol"] == 0.01
    assert claim["claimed_data_sha"] == "d-sha"


def test_claim_check_receipt_kind_is_claim_check_never_reproduction(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.receipt["receipt_kind"] == "claim-check"
    # Lands in the claim-check ledger, NEVER the reproduction ledger.
    ledger = Path(res.receipt_path)
    assert ledger.name == "claim_check_receipts.jsonl"
    assert not (ledger.parent / "reproduction_receipts.jsonl").exists()
    # The receipt validates against its authoring wire model.
    ClaimCheckReceipt.model_validate(res.receipt)


def test_claim_check_appends_no_fingerprint_sample(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.appended_sample is None
    # The observed-runs-only lock: the fingerprint ledger is untouched.
    assert not fingerprint_path(tmp_path, "a" * 64).exists()


def test_claim_check_incomparable_missing_fresh_artifact(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, REPRO)  # sidecar but no metrics_aggregate.json
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "incomparable"
    assert res.needs_decision is True
    assert res.receipt["per_key"] == []


def test_claim_check_refuses_missing_fresh_sidecar(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))


def test_claim_check_receipt_appends_second_line(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    res2 = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    lines = Path(res2.receipt_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append-only


# --- mutual-exclusion (the two baseline-resolution modes) ------------------- #
def test_spec_recorded_mode_requires_original() -> None:
    with pytest.raises(ValidationError):
        VerifyReproductionSpec(repro_run_id=REPRO)  # no original, no external


def test_spec_external_and_original_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        VerifyReproductionSpec(
            original_run_id=ORIG,
            repro_run_id=REPRO,
            external_baseline=ExternalBaseline(claimed_values={"gp.pi": 3.14}),
        )


def test_spec_external_forbids_top_level_tolerance() -> None:
    with pytest.raises(ValidationError):
        VerifyReproductionSpec(
            repro_run_id=REPRO,
            tolerance=ReproTolerance(default_abs_tol=0.1),
            external_baseline=ExternalBaseline(claimed_values={"gp.pi": 3.14}),
        )


def test_spec_external_mode_valid_without_original() -> None:
    spec = VerifyReproductionSpec(
        repro_run_id=REPRO,
        external_baseline=ExternalBaseline(claimed_values={"gp.pi": 3.14}),
    )
    assert spec.original_run_id is None
    assert spec.external_baseline is not None


# --- the anti-laundering enforcement seam ----------------------------------- #
def test_reproduction_receipt_never_written_with_external_baseline() -> None:
    # FIRES: a reproduction-kind receipt with an external baseline is refused.
    with pytest.raises(errors.SpecInvalid):
        _assert_receipt_kind_matches_baseline(receipt_kind="reproduction", external_baseline=True)
    # A claim-check without a baseline is equally incoherent.
    with pytest.raises(errors.SpecInvalid):
        _assert_receipt_kind_matches_baseline(receipt_kind="claim-check", external_baseline=False)
    # PASSES: both honest pairings.
    _assert_receipt_kind_matches_baseline(receipt_kind="reproduction", external_baseline=False)
    _assert_receipt_kind_matches_baseline(receipt_kind="claim-check", external_baseline=True)


def test_recorded_reproduction_receipt_carries_reproduction_kind(tmp_path: Path) -> None:
    # v1/v2 compatibility: the recorded-original path still writes a reproduction
    # receipt, now stamped with the explicit kind discriminator.
    _pair(tmp_path, {"pi": 3.14159}, {"pi": 3.14159})
    res = _run(tmp_path)
    assert res.receipt["receipt_kind"] == "reproduction"


def test_run_claim_check_helper_direct(tmp_path: Path) -> None:
    _fresh_run(tmp_path, {"pi": 3.14159})
    res = _run_claim_check(
        tmp_path,
        repro_run_id=REPRO,
        baseline=ExternalBaseline(claimed_values={"gp.pi": 3.14159}),
    )
    assert res.stage_reached == "match"
