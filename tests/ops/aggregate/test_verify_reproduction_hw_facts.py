"""The hardware dimension at verify — U-HW1 (reproducibility gap #5).

verify-reproduction compares the reproduction's ``hw_sha`` against the original's
and DISCLOSES the result (match / drifted / unknown) — never a gate. The value
over a bare drift line: it frames the placement delta against whether the metrics
actually diverged. Covers:

* hw drift + MATCHING metrics → NOT gated (a matching reproduction still
  auto-clears), disclosed on the receipt with the moved-fact delta, and the
  reason notes the delta did NOT perturb the metrics here;
* hw drift + DIVERGING metrics → the delta is named a CANDIDATE attribution for
  the divergence (offered, not asserted); the verdict is the metric verdict, not
  the hardware's;
* hw match + DIVERGING metrics → placement EQUIVALENT, hardware ruled OUT
  (strengthens the signal);
* hw match + matching metrics → hw_identity=match on the receipt, no reason line;
* one side not captured → ``unknown`` disclosed;
* NEITHER side captured (pre-U-HW1 sidecars) → no hw_identity block, the receipt
  stays byte-identical to a pre-U-HW1 one.

Toy fixtures (widget metrics), mirroring test_verify_reproduction_env_lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent._wire.queries.verify_reproduction import (
    ReproductionReceipt,
    VerifyReproductionSpec,
)
from hpc_agent.ops.verify_reproduction import verify_reproduction
from hpc_agent.state.hw_facts import STATUS_CAPTURED, STATUS_COULD_NOT_CAPTURE, hw_sha
from hpc_agent.state.runs import stamp_run_sidecar_hw_facts, write_run_sidecar

ORIG = "orig-run"
REPRO = "repro-run"
FACTS_A = {"node": "gpu-a-01", "cpu_model": "Widget Xeon Gold 6248", "partition": "gpu"}
FACTS_B = {"node": "gpu-a-99", "cpu_model": "Widget Xeon Gold 6248", "partition": "gpu"}
HW_A = hw_sha(FACTS_A)
HW_B = hw_sha(FACTS_B)


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
        "cluster": "widgetcluster",
    }
    kwargs.update(over)
    write_run_sidecar(exp, reproduces=reproduces, **kwargs)


def _stamp_hw(
    exp: Path, run_id: str, facts: dict[str, Any] | None, sha: str | None, status: str
) -> None:
    stamp_run_sidecar_hw_facts(exp, run_id, hw_facts=facts, hw_sha=sha, hw_status=status)


def _write_aggregate(exp: Path, run_id: str, metrics: dict[str, Any]) -> None:
    agg = exp / "_aggregated" / run_id
    agg.mkdir(parents=True, exist_ok=True)
    (agg / "metrics_aggregate.json").write_text(
        json.dumps({"run_id": run_id, "aggregated_metrics": {"gp": metrics}}), encoding="utf-8"
    )


def _run(exp: Path):
    spec = VerifyReproductionSpec(original_run_id=ORIG, repro_run_id=REPRO)
    return verify_reproduction(exp, spec=spec)


def test_hw_drift_matching_metrics_disclosed_not_gated(tmp_path: Path) -> None:
    # Metrics MATCH exactly, but the run landed on a different NODE. The verdict
    # must NOT be gated (a matching reproduction still auto-clears), the drift is
    # DISCLOSED with the moved-fact delta, and the reason notes it did not perturb.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_hw(tmp_path, ORIG, FACTS_A, HW_A, STATUS_CAPTURED)
    _stamp_hw(tmp_path, REPRO, FACTS_B, HW_B, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    # NOT gated: metrics matched → match verdict, needs_decision stays False.
    assert res.stage_reached == "match"
    assert res.needs_decision is False
    disc = res.receipt["hw_identity"]
    assert disc["status"] == "drifted"
    assert disc["original"] == HW_A and disc["repro"] == HW_B
    assert disc["delta"] == ["node"]  # the attribution surface names the moved fact
    assert "hardware dimension: placement DRIFTED" in res.reason
    assert "did not perturb" in res.reason  # not diverged → no attribution claim
    assert res.receipt["schema_version"] == 2  # hw_known forced v2
    ReproductionReceipt.model_validate(res.receipt)  # the v2 shape parses


def test_hw_drift_diverging_metrics_named_candidate_attribution(tmp_path: Path) -> None:
    # Metrics DIVERGE and the hardware moved → the delta is a CANDIDATE
    # attribution for the divergence (offered, never asserted).
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_hw(tmp_path, ORIG, FACTS_A, HW_A, STATUS_CAPTURED)
    _stamp_hw(tmp_path, REPRO, FACTS_B, HW_B, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 9.99})  # diverged
    res = _run(tmp_path)
    assert res.needs_decision is True  # the METRIC verdict routes to the human
    assert "CANDIDATE attribution for the observed divergence" in res.reason
    assert res.receipt["hw_identity"]["status"] == "drifted"


def test_hw_match_diverging_metrics_rules_hardware_out(tmp_path: Path) -> None:
    # Metrics DIVERGE but the hardware was EQUIVALENT → hardware is ruled OUT as a
    # source, which strengthens the divergence signal.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_hw(tmp_path, ORIG, FACTS_A, HW_A, STATUS_CAPTURED)
    _stamp_hw(tmp_path, REPRO, FACTS_A, HW_A, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 9.99})
    res = _run(tmp_path)
    assert res.receipt["hw_identity"]["status"] == "match"
    assert "ruled OUT as a source of the divergence" in res.reason


def test_hw_match_matching_metrics_no_line(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_hw(tmp_path, ORIG, FACTS_A, HW_A, STATUS_CAPTURED)
    _stamp_hw(tmp_path, REPRO, FACTS_A, HW_A, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    # Equivalent hardware + matching metrics → hw_identity=match, NO reason line.
    assert res.receipt["hw_identity"]["status"] == "match"
    assert "hardware dimension" not in res.reason


def test_hw_unknown_when_one_side_not_captured(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_hw(tmp_path, ORIG, FACTS_A, HW_A, STATUS_CAPTURED)
    _stamp_hw(tmp_path, REPRO, None, None, STATUS_COULD_NOT_CAPTURE)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    disc = res.receipt["hw_identity"]
    assert disc["status"] == "unknown"
    assert disc["original"] == HW_A and disc["repro"] is None
    assert "hardware dimension: placement identity unknown" in res.reason


def test_no_hw_stays_byte_identical(tmp_path: Path) -> None:
    # Neither side captured hardware (old sidecars) → the hw leg says nothing: no
    # hw_identity block, no hw clause, no v2 forcing from the hw dimension.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    assert res.receipt.get("hw_identity") is None
    assert "hardware dimension" not in res.reason
    assert res.receipt["schema_version"] == 1  # hw-less + metric-match stays v1
