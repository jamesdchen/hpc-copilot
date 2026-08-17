"""Pinning tests for the claim-check consistency-sentence VARIANT selection.

``verify-reproduction`` in external-baseline mode renders exactly one of two
code-rendered consistency sentences on a match:

* ``CLAIM_CONSISTENT_SENTENCE_ADOPTED`` when the compared run's sidecar carries
  adopt-run's ``extra.adopted`` marker (the run was never observed — the honest
  ceiling is consistency with the adopted run's records);
* ``CLAIM_CONSISTENT_SENTENCE`` (the fresh-observed variant) otherwise.

Fixtures mirror ``tests/ops/aggregate/test_verify_reproduction.py`` (the REAL
sidecar writer + a hand-written ``metrics_aggregate.json``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent._wire.queries.verify_reproduction import (
    ExternalBaseline,
    VerifyReproductionSpec,
)
from hpc_agent.ops.verify_reproduction import (
    CLAIM_CONSISTENT_SENTENCE,
    CLAIM_CONSISTENT_SENTENCE_ADOPTED,
    verify_reproduction,
)
from hpc_agent.state.runs import write_run_sidecar

RUN = "adopted-claim-run"


def _write_sidecar(exp: Path, *, extra: dict[str, Any] | None = None) -> None:
    write_run_sidecar(
        exp,
        run_id=RUN,
        cmd_sha="a" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python train.py",
        result_dir_template="results/{task_id}",
        task_count=1,
        tasks_py_sha="b" * 64,
        extra=extra,
    )


def _write_aggregate(exp: Path, aggregated_metrics: dict[str, Any]) -> None:
    agg = exp / "_aggregated" / RUN
    agg.mkdir(parents=True, exist_ok=True)
    with (agg / "metrics_aggregate.json").open("w", encoding="utf-8") as fh:
        json.dump({"run_id": RUN, "aggregated_metrics": aggregated_metrics}, fh)


def _claim_spec(claimed_values: dict[str, Any]) -> VerifyReproductionSpec:
    return VerifyReproductionSpec(
        repro_run_id=RUN,
        external_baseline=ExternalBaseline(claimed_values=claimed_values),
    )


def test_adopted_sidecar_renders_adopted_sentence(tmp_path: Path) -> None:
    # The sidecar carries adopt-run's marker (extra.adopted, as adopt-run stamps it).
    _write_sidecar(tmp_path, extra={"adopted": {"by": "adopt-run"}})
    _write_aggregate(tmp_path, {"gp": {"pi": 3.14159}})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "match"
    # The ADOPTED variant rides consistency, reason, and the persisted receipt.
    assert res.reason == CLAIM_CONSISTENT_SENTENCE_ADOPTED
    assert res.receipt["consistency"] == CLAIM_CONSISTENT_SENTENCE_ADOPTED
    persisted = json.loads(Path(res.receipt_path).read_text(encoding="utf-8").splitlines()[-1])
    assert persisted["consistency"] == CLAIM_CONSISTENT_SENTENCE_ADOPTED
    # Never the fresh-observed wording — an adopted run was never observed.
    assert res.reason != CLAIM_CONSISTENT_SENTENCE


def test_unmarked_sidecar_renders_fresh_observed_sentence(tmp_path: Path) -> None:
    # No extra.adopted marker: the fresh-observed variant is rendered.
    _write_sidecar(tmp_path, extra=None)
    _write_aggregate(tmp_path, {"gp": {"pi": 3.14159}})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "match"
    assert res.reason == CLAIM_CONSISTENT_SENTENCE
    assert res.receipt["consistency"] == CLAIM_CONSISTENT_SENTENCE
    persisted = json.loads(Path(res.receipt_path).read_text(encoding="utf-8").splitlines()[-1])
    assert persisted["consistency"] == CLAIM_CONSISTENT_SENTENCE
