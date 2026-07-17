"""Tests for the ``cite-check`` query verb (the number → paper transcription audit).

Exercises the two-bucket disclosure (v1, Option B) over a tmp experiment with a
REAL sealed ``metrics_aggregate.json``: a manuscript citing sealed numbers → all
``matched``; a number with no sealed source → ``uncitable`` with the nearest sealed
value as CONTEXT; the false-positive battery (page / figure / table / equation refs,
citation years, ``[12]`` bibliography markers, path digits, run-ids — NONE extracted
as claims); the ``match_number`` tolerance boundary; and seed-resolution parity with
``extract-recipe``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._wire.queries.cite_check import CiteCheckInput
from hpc_agent.ops.cite_check import cite_check

if TYPE_CHECKING:
    from pathlib import Path


def _seal(experiment_dir: Path, run_id: str, metrics: dict[str, object]) -> None:
    """Write a sealed ``metrics_aggregate.json`` with the given per-run metric map."""
    agg = experiment_dir / "_aggregated" / run_id / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {run_id: metrics},
                "provenance": {"source": "local_reduce", "contributing_run_ids": []},
            }
        ),
        encoding="utf-8",
    )


def _check(experiment_dir: Path, text: str, run_id: str) -> dict:
    return cite_check(experiment_dir, spec=CiteCheckInput(manuscript_text=text, run_id=run_id))


def _kinds(result: dict) -> dict[str, str]:
    """claim → kind for every finding."""
    return {f["claim"]: f["kind"] for f in result["findings"]}


# ── the happy path: sealed numbers cited faithfully → all matched ──────────────


def test_manuscript_citing_sealed_numbers_is_all_matched(tmp_path: Path) -> None:
    _seal(tmp_path, "run-a", {"qlike": "0.9427", "n": "1536"})
    text = "Our model reached 0.9427 over 1,536 held-out samples."
    result = _check(tmp_path, text, "run-a")
    assert result["clean"] is True
    kinds = _kinds(result)
    assert kinds.get("0.9427") == "matched"
    # comma-grouped rendering of the sealed 1536 reconciles.
    assert kinds.get("1,536") == "matched"
    assert not any(f["kind"] == "uncitable" for f in result["findings"])


def test_number_with_no_sealed_source_is_uncitable_with_nearest(tmp_path: Path) -> None:
    _seal(tmp_path, "run-b", {"qlike": "0.9427"})
    # A fat-fingered transcription (0.9472 vs the sealed 0.9427).
    result = _check(tmp_path, "We report 0.9472 accuracy.", "run-b")
    assert result["clean"] is False
    finding = next(f for f in result["findings"] if f["claim"] == "0.9472")
    assert finding["kind"] == "uncitable"
    assert finding["nearest_chain_value"] == "0.9427"  # offered as CONTEXT


# ── the false-positive battery — the unit's soul ───────────────────────────────


def test_reference_labels_are_not_claims(tmp_path: Path) -> None:
    _seal(tmp_path, "run-c", {"qlike": "0.9427"})
    text = (
        "As shown in Table 3 and Figure 4 (see Section 3.2 and Eq. 5), "
        "the result on p. 12 and Algorithm 1 holds; the value 0.9427 is our headline."
    )
    result = _check(tmp_path, text, "run-c")
    claims = set(_kinds(result))
    # The ref numbers 3, 4, 3.2, 5, 12, 1 must NOT appear as claims.
    for ref in ("3", "4", "3.2", "5", "12", "1"):
        assert ref not in claims, f"reference number {ref!r} leaked as a claim: {claims}"
    # The genuine result IS checked.
    assert _kinds(result).get("0.9427") == "matched"


def test_citation_years_are_not_claims(tmp_path: Path) -> None:
    _seal(tmp_path, "run-d", {"qlike": "0.9427"})
    text = (
        "Prior work (Smith et al., 2024) and Jones (2021) — see also [Brown 1998] — "
        "motivates our 0.9427 result."
    )
    result = _check(tmp_path, text, "run-d")
    claims = set(_kinds(result))
    for year in ("2024", "2021", "1998"):
        assert year not in claims, f"citation year {year!r} leaked as a claim: {claims}"
    assert _kinds(result).get("0.9427") == "matched"


def test_bibliography_markers_are_not_claims(tmp_path: Path) -> None:
    _seal(tmp_path, "run-e", {"qlike": "0.9427"})
    text = "Following [12] and [13, 14] and the range [15-17], we obtain 0.9427."
    result = _check(tmp_path, text, "run-e")
    claims = set(_kinds(result))
    for marker in ("12", "13", "14", "15", "16", "17"):
        assert marker not in claims, f"bib marker {marker!r} leaked as a claim: {claims}"
    assert _kinds(result).get("0.9427") == "matched"


def test_path_digits_and_run_ids_are_not_claims(tmp_path: Path) -> None:
    _seal(tmp_path, "run-f", {"qlike": "0.9427"})
    text = (
        "Artifacts under results/2024/run.csv and /data/03/metrics.json for run-3 "
        "(pi-train-d363e2a3) give 0.9427."
    )
    result = _check(tmp_path, text, "run-f")
    claims = set(_kinds(result))
    # Path-embedded 2024 / 03 and the run-id digit must not be claims.
    for tok in ("2024", "03", "3", "363"):
        assert tok not in claims, f"path/run-id token {tok!r} leaked as a claim: {claims}"
    assert _kinds(result).get("0.9427") == "matched"


def test_bare_small_integers_are_low_signal_not_flooded(tmp_path: Path) -> None:
    _seal(tmp_path, "run-g", {"qlike": "0.9427"})
    # Hyperparameters in prose — none should flood the uncitable bucket.
    text = "We trained 300 epochs with batch size 64 and 5 seeds, reaching 0.9427."
    result = _check(tmp_path, text, "run-g")
    claims = set(_kinds(result))
    for hp in ("300", "64", "5"):
        assert hp not in claims, f"low-signal hyperparameter {hp!r} was flagged: {claims}"
    assert result["clean"] is True
    assert _kinds(result).get("0.9427") == "matched"


# ── the match_number tolerance boundary ────────────────────────────────────────


def test_tolerance_boundary_truncation_matches_rounding_flags(tmp_path: Path) -> None:
    _seal(tmp_path, "run-h", {"qlike": "3.1411"})
    # Pure truncation reconciles (3.14 is a prefix of 3.1411).
    trunc = _check(tmp_path, "We cite 3.14 here.", "run-h")
    assert _kinds(trunc).get("3.14") == "matched"
    # A rounding that changes a digit (3.15) is NOT a prefix → uncitable.
    rounded = _check(tmp_path, "We cite 3.15 here.", "run-h")
    assert _kinds(rounded).get("3.15") == "uncitable"


def test_display_rounding_matches(tmp_path: Path) -> None:
    _seal(tmp_path, "run-i", {"loss_val": "-15.4283"})
    # A standard 2dp render of the sealed -15.4283 reconciles (display tolerance).
    result = _check(tmp_path, "The loss was -15.43.", "run-i")
    assert _kinds(result).get("-15.43") == "matched"


# ── seed resolution parity + errors ────────────────────────────────────────────


def test_aggregate_path_seed_parity(tmp_path: Path) -> None:
    _seal(tmp_path, "run-j", {"qlike": "0.9427"})
    agg = tmp_path / "_aggregated" / "run-j" / "metrics_aggregate.json"
    result = cite_check(
        tmp_path,
        spec=CiteCheckInput(manuscript_text="Headline 0.9427.", aggregate_path=str(agg)),
    )
    assert result["seed_kind"] == "aggregate"
    assert _kinds(result).get("0.9427") == "matched"


def test_missing_manuscript_source_raises_spec_invalid(tmp_path: Path) -> None:
    try:
        cite_check(tmp_path, spec=CiteCheckInput(run_id="run-x"))
    except errors.SpecInvalid as exc:
        assert "manuscript" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SpecInvalid for a missing manuscript source")


def test_missing_seed_raises_spec_invalid(tmp_path: Path) -> None:
    try:
        cite_check(tmp_path, spec=CiteCheckInput(manuscript_text="0.9427"))
    except errors.SpecInvalid as exc:
        assert "seed" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("expected SpecInvalid for a missing seed")


def test_two_seeds_raises_spec_invalid(tmp_path: Path) -> None:
    try:
        cite_check(
            tmp_path,
            spec=CiteCheckInput(manuscript_text="0.9427", run_id="a", campaign_id="b"),
        )
    except errors.SpecInvalid:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected SpecInvalid for two seeds")


def test_opaque_pack_csv_seed_yields_empty_pool(tmp_path: Path) -> None:
    # A pack *.csv is an OPAQUE citation — content never parsed, so every high-signal
    # number is uncitable-against-it.
    csv = tmp_path / "_aggregated" / "run-k" / "metrics_table.csv"
    csv.parent.mkdir(parents=True, exist_ok=True)
    csv.write_text("metric,value\nqlike,0.9427\n", encoding="utf-8")
    result = cite_check(
        tmp_path,
        spec=CiteCheckInput(manuscript_text="Headline 0.9427.", aggregate_path=str(csv)),
    )
    assert result["sources_consulted"] == []
    assert _kinds(result).get("0.9427") == "uncitable"


def test_markdown_render_is_present_and_deterministic(tmp_path: Path) -> None:
    _seal(tmp_path, "run-l", {"qlike": "0.9427"})
    r1 = _check(tmp_path, "We report 0.9472 (typo).", "run-l")
    r2 = _check(tmp_path, "We report 0.9472 (typo).", "run-l")
    assert r1["markdown"] == r2["markdown"]
    assert "cite-check" in r1["markdown"]
    assert "0.9472" in r1["markdown"]
