"""The environment dimension at verify — U-ENV1 (reproducibility program).

verify-reproduction compares the reproduction's resolved ``env_lock_sha`` against
the original's and DISCLOSES the result (match / drifted / unknown) — never a
gate. Covers the required cases:

* a drifted env → the disclosure rides the receipt + reason, and the verdict is
  NOT gated (a matching-metrics reproduction still auto-clears exit-0);
* a matching env → no drift line (receipt status ``match``, no reason clause);
* one side's env not captured → ``unknown`` disclosed;
* NEITHER side captured (pre-U-ENV1 sidecars) → no env_identity block, the receipt
  stays byte-identical to a pre-U-ENV1 one.

Toy fixtures (widget metrics), mirroring test_verify_reproduction_data_identity.
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
from hpc_agent.state.env_lock import STATUS_CAPTURED, STATUS_COULD_NOT_CAPTURE
from hpc_agent.state.runs import stamp_run_sidecar_env_lock, write_run_sidecar

ORIG = "orig-run"
REPRO = "repro-run"
ENV_A = "1" * 64
ENV_B = "2" * 64


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


def _stamp_env(exp: Path, run_id: str, sha: str | None, status: str) -> None:
    stamp_run_sidecar_env_lock(exp, run_id, env_lock_sha=sha, env_lock_status=status)


def _write_aggregate(exp: Path, run_id: str, metrics: dict[str, Any]) -> None:
    agg = exp / "_aggregated" / run_id
    agg.mkdir(parents=True, exist_ok=True)
    (agg / "metrics_aggregate.json").write_text(
        json.dumps({"run_id": run_id, "aggregated_metrics": {"gp": metrics}}), encoding="utf-8"
    )


def _run(exp: Path):
    spec = VerifyReproductionSpec(original_run_id=ORIG, repro_run_id=REPRO)
    return verify_reproduction(exp, spec=spec)


def test_env_drift_disclosed_not_gated(tmp_path: Path) -> None:
    # Metrics MATCH exactly, but the resolved env DRIFTED. The verdict must NOT be
    # gated by the env drift (a matching reproduction still exit-0 / auto-clears),
    # and the drift is DISCLOSED on the receipt + named in the reason.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_env(tmp_path, ORIG, ENV_A, STATUS_CAPTURED)
    _stamp_env(tmp_path, REPRO, ENV_B, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    # NOT gated: the metrics matched, so the comparison still resolves to a match
    # and needs_decision stays False — the env drift did not refuse or fail it.
    assert res.stage_reached == "match"
    assert res.needs_decision is False
    # DISCLOSED: the env dimension is named on the receipt + in the reason.
    disc = res.receipt["env_identity"]
    assert disc["status"] == "drifted"
    assert disc["original"] == ENV_A and disc["repro"] == ENV_B
    assert "environment dimension" in res.reason
    assert res.receipt["schema_version"] == 2  # env_known forced v2
    ReproductionReceipt.model_validate(res.receipt)  # the v2 shape parses


def test_env_match_no_drift_line(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_env(tmp_path, ORIG, ENV_A, STATUS_CAPTURED)
    _stamp_env(tmp_path, REPRO, ENV_A, STATUS_CAPTURED)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    assert res.receipt["env_identity"]["status"] == "match"
    # Matching env → NO drift line in the reason.
    assert "environment dimension" not in res.reason


def test_env_unknown_when_one_side_not_captured(tmp_path: Path) -> None:
    # The original captured its env; the reproduction's canary could-not-capture.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _stamp_env(tmp_path, ORIG, ENV_A, STATUS_CAPTURED)
    _stamp_env(tmp_path, REPRO, None, STATUS_COULD_NOT_CAPTURE)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    disc = res.receipt["env_identity"]
    assert disc["status"] == "unknown"
    assert disc["original"] == ENV_A and disc["repro"] is None
    assert "environment dimension: env_lock identity unknown" in res.reason


def test_no_env_lock_stays_byte_identical(tmp_path: Path) -> None:
    # Neither side captured an env (old sidecars) → the env leg says nothing: no
    # env_identity block, no env clause, no v2 forcing from the env dimension.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    assert res.receipt.get("env_identity") is None
    assert "environment dimension" not in res.reason
    assert res.receipt["schema_version"] == 1  # env-less + metric-match stays v1
