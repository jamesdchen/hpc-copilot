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
    spec = VerifyReproductionSpec(
        original_run_id=ORIG, repro_run_id=REPRO, tolerance=tolerance
    )
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
    for field in ("ts", "schema_version", "original", "repro", "tolerance_spec",
                  "per_key", "overall", "sources"):
        assert field in record
    assert record["schema_version"] == 1
    assert record["overall"] == "match"
    assert record["tolerance_spec"] is None  # exact -> null echo


def test_receipt_identity_lifted_verbatim_from_sidecar(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG, cmd_sha="c" * 64, env_hash="env-orig", cluster="hoffman2")
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG, cmd_sha="c" * 64, env_hash="env-repro",
                   cluster="discovery")
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
        "key", "original", "repro", "abs_diff", "rel_diff", "verdict", "tolerance_applied",
    }
    assert by_key["a"]["abs_diff"] == 0.0


def test_compare_metrics_nan_via_pure(tmp_path: Path) -> None:
    verdicts = _compare_metrics({"x": math.nan}, {"x": math.nan}, None)
    assert verdicts[0]["verdict"] == "incomparable"
