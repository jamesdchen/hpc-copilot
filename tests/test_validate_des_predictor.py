"""Tests for scripts/validate_des_predictor (Phase 4d)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from claude_hpc.infra.inspect import (
    ClusterSnapshot,
    NodeSnapshot,
    persist_snapshot,
)
from claude_hpc.orchestrator import runtime_prior as rp


def _load_script():
    """Import scripts/validate_des_predictor.py as a module by path."""
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "validate_des_predictor.py"
    spec = importlib.util.spec_from_file_location("validate_des", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_des"] = mod
    spec.loader.exec_module(mod)
    return mod


PROFILE = "ml_ridge"
CLUSTER = "discovery"


def test_replay_with_no_data_returns_graceful_summary(tmp_path):
    mod = _load_script()
    summary = mod.replay(
        tmp_path,
        profile=PROFILE,
        cluster=CLUSTER,
        n_replications=2,
    )
    assert summary["n_samples"] == 0
    assert "message" in summary


def test_replay_with_observation_and_snapshot(tmp_path):
    mod = _load_script()
    # 1) Persist an idle snapshot at t=10:00.
    snap = ClusterSnapshot(
        cluster=CLUSTER,
        scheduler_kind="slurm",
        now_iso="2026-04-28T10:00:00+00:00",
        nodes=[
            NodeSnapshot(
                name="n0",
                state="IDLE",
                real_mem_mb=64_000,
                alloc_mem_mb=0,
                cpu_tot=8,
                cpu_alloc=0,
                gres="",
                gres_used="",
                co_tenants=[],
                is_drained=False,
            )
        ],
    )
    persist_snapshot(tmp_path, snap)
    # 2) Append a runtime-prior sample with submitted_at_iso AFTER the
    #    snapshot, observed wait of 60s.
    rp.append_sample(
        tmp_path,
        profile=PROFILE,
        cluster=CLUSTER,
        run_id="r1",
        task_id=0,
        gpu_type="a100",
        node="n0",
        elapsed_sec=120,
        submitted_at_iso="2026-04-28T10:30:00+00:00",
        queue_wait_sec=60,
    )
    summary = mod.replay(
        tmp_path,
        profile=PROFILE,
        cluster=CLUSTER,
        n_replications=2,
    )
    assert summary["n_samples"] == 1
    # Idle cluster + small candidate → DES predicts 0; observed 60.
    # MAE should be ~60.
    assert summary["mae_sec"] == 60.0
    assert summary["residual_p50_sec"] == -60.0
