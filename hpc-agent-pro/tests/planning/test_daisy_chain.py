"""Tests for the auto-daisy-chain helper and its planner integration.

The chain helper is pure; the integration test exercises plan_submit's
detection-driven default (kill switch / detect / always) so the
"don't silently waste compute" survival framing is enforced end-to-end.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from hpc_agent.infra import inspect as ins
from hpc_agent.infra.inspect import ClusterSnapshot, NodeSnapshot
from hpc_agent_pro.planning import planner
from hpc_agent_pro.planning.daisy_chain import (
    QUEUE_WAIT_BUFFER_SEC,
    compute_daisy_chain_plan,
    format_dependency_flag,
    should_daisy_chain,
)


@pytest.fixture(autouse=True)
def _clear_inspect_cache():
    ins._CACHE.clear()
    yield
    ins._CACHE.clear()


def _fake_snapshot():
    healthy = NodeSnapshot(
        name="d11-07",
        state="MIXED",
        real_mem_mb=192000,
        alloc_mem_mb=64000,
        alloc_mem_pct=0.33,
        cpu_tot=32,
        cpu_load=3.2,
        cpu_load_frac=0.10,
        gres="gpu:a100:2",
        gres_used="gpu:a100:0",
        active_features=["a100"],
        is_stressed=False,
        is_drained=False,
    )
    return ClusterSnapshot(
        cluster="discovery",
        scheduler_kind="slurm",
        now_iso="2026-01-01T00:00:00+00:00",
        nodes=[healthy],
    )


def _write_clusters(
    tmp_path: Path,
    *,
    scheduler: str = "slurm",
    max_walltime_sec: int = 86400,
    auto_daisy_chain: object = "absent",
) -> Path:
    """Write a clusters.yaml fragment for the test cluster."""
    p = tmp_path / "clusters.yaml"
    lines = [
        "discovery:",
        "  host: example.invalid",
        "  user: tester",
        f"  scheduler: {scheduler}",
        "  scratch: /tmp",
        "  gpu_types: [a100]",
        f"  max_walltime_sec: {max_walltime_sec}",
    ]
    if auto_daisy_chain != "absent":
        lines.append(f"  auto_daisy_chain: {str(auto_daisy_chain).lower()}")
    p.write_text("\n".join(lines) + "\n")
    return p


def _seed_checkpoint(tmp_path: Path, *, profile: str, cluster: str, run_id: str) -> None:
    """Write a sidecar referencing a result_dir that contains a checkpoint."""
    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    result_root = tmp_path / "scratch" / run_id
    (result_root / "task_0").mkdir(parents=True, exist_ok=True)
    (result_root / "task_0" / "checkpoint.pt").write_bytes(b"x")
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "sidecar_schema_version": 2,
                "run_id": run_id,
                "cmd_sha": "f" * 64,
                "hpc_agent_version": "0.0.1",
                "submitted_at": "2026-01-01T00:00:00+00:00",
                "executor": "exec.py",
                "result_dir_template": str(result_root / "task_{task_id}"),
                "task_count": 1,
                "tasks_py_sha": "0" * 64,
                "profile": profile,
                "cluster": cluster,
            }
        )
    )


# ─── pure helpers ───────────────────────────────────────────────────────────


class TestShouldDaisyChain:
    def test_below_threshold_returns_false(self):
        # 23h ask on a 24h cluster: 23h <= (24h - 1h), so no chain.
        assert should_daisy_chain(23 * 3600, max_walltime_sec=86400) is False

    def test_at_threshold_returns_false(self):
        # 23h is exactly at max - 1h; trigger is strictly greater.
        assert should_daisy_chain(86400 - QUEUE_WAIT_BUFFER_SEC, max_walltime_sec=86400) is False

    def test_above_threshold_returns_true(self):
        # 23h + 1s on a 24h cluster -> chain.
        assert should_daisy_chain(86400 - QUEUE_WAIT_BUFFER_SEC + 1, max_walltime_sec=86400) is True


class TestComputeDaisyChainPlan:
    def test_no_chain_needed_returns_one_segment(self):
        plan = compute_daisy_chain_plan(3600, max_walltime_sec=86400)
        assert plan.n_segments == 1
        assert plan.segment_walltime_sec == 3600
        assert plan.total_walltime_sec == 3600

    def test_two_day_task_on_24h_cluster(self):
        # 48h on 24h cluster: per-segment cap = 23h; ceil(48/23) = 3.
        # Rebalanced: each segment = ceil(48*3600 / 3) = 57600 sec = 16h
        # (not 23h, since 3 equal segments fit comfortably below the cap).
        plan = compute_daisy_chain_plan(48 * 3600, max_walltime_sec=86400)
        assert plan.n_segments == 3
        assert plan.segment_walltime_sec == 16 * 3600
        # Sanity: rebalanced segment <= per-segment cap.
        assert plan.segment_walltime_sec <= 86400 - QUEUE_WAIT_BUFFER_SEC
        assert plan.total_walltime_sec == 48 * 3600

    def test_seven_day_task_on_24h_cluster(self):
        # 7 days = 168h; per-segment cap = 23h; ceil(168/23) = 8.
        plan = compute_daisy_chain_plan(7 * 86400, max_walltime_sec=86400)
        assert plan.n_segments == 8

    def test_thirty_day_task_on_24h_cluster(self):
        # 30 days = 720h; per-segment cap = 23h; ceil(720/23) = 32.
        plan = compute_daisy_chain_plan(30 * 86400, max_walltime_sec=86400)
        assert plan.n_segments == 32

    def test_boundary_just_above_per_segment_rebalances(self):
        # Ask = per_segment_cap + 1: naive split would emit a 1-second tail.
        # Rebalanced: 2 segments of ceil((cap + 1) / 2) seconds — well above
        # the 60s sanity floor and the cluster's minimum-job duration.
        max_walltime = 86400
        per_segment_cap = max_walltime - QUEUE_WAIT_BUFFER_SEC
        plan = compute_daisy_chain_plan(per_segment_cap + 1, max_walltime_sec=max_walltime)
        assert plan.n_segments == 2
        # Every segment is >= 60s (no degenerate slivers).
        assert plan.segment_walltime_sec >= 60
        # And still under the per-segment cap.
        assert plan.segment_walltime_sec <= per_segment_cap
        # Exactly: ceil((per_segment_cap + 1) / 2).
        import math as _math

        assert plan.segment_walltime_sec == _math.ceil((per_segment_cap + 1) / 2)

    def test_invalid_max_walltime_raises(self):
        # max <= queue-wait buffer: chain can't make progress.
        with pytest.raises(ValueError, match="must exceed"):
            compute_daisy_chain_plan(86400, max_walltime_sec=QUEUE_WAIT_BUFFER_SEC)

    def test_invalid_ask_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            compute_daisy_chain_plan(0, max_walltime_sec=86400)


class TestFormatDependencyFlag:
    def test_slurm_uses_afterany_not_afterok(self):
        # afterany is critical: preempted segment N (exit 130) must
        # still trigger N+1; afterok would deadlock the chain.
        flag = format_dependency_flag("slurm", "12345")
        assert flag == "--dependency=afterany:12345"
        assert "afterok" not in flag

    def test_sge_uses_hold_jid(self):
        assert format_dependency_flag("sge", "67890") == "-hold_jid 67890"

    def test_case_insensitive(self):
        assert format_dependency_flag("SLURM", "1") == "--dependency=afterany:1"
        assert format_dependency_flag("Sge", "2") == "-hold_jid 2"

    def test_unknown_scheduler_raises(self):
        with pytest.raises(ValueError, match="not implemented"):
            format_dependency_flag("torque", "1")


# ─── plan_submit integration ───────────────────────────────────────────────


class TestPlanSubmitDaisyChain:
    """End-to-end: plan_submit threads the cluster yaml + checkpoint
    detection into a chain decision and surfaces segments in output.
    """

    def test_no_chain_when_ask_fits(self, tmp_path, monkeypatch):
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=3600,
            )
        assert out["daisy_chain_segments"] is None
        assert out["daisy_chain_dep_jobids"] is None

    def test_chain_with_checkpoint_detection_true(self, tmp_path, monkeypatch):
        # Past run produced a checkpoint -> detection True -> chain.
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        _seed_checkpoint(tmp_path, profile="p", cluster="discovery", run_id="r1")
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=48 * 3600,
            )
        # 48h on 24h cluster, per-segment cap 23h -> 3 segments.
        assert out["daisy_chain_segments"] == 3
        # Plan-time always emits null dep_jobids; submit_flow fills in.
        assert out["daisy_chain_dep_jobids"] is None

    def test_chain_blocked_when_no_checkpoint_signal(self, tmp_path, monkeypatch):
        # No past runs / no checkpoints -> detection False -> error.
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with (
            patch(
                "hpc_agent_pro.planning.planner.inspect_cluster",
                return_value=_fake_snapshot(),
            ),  # noqa: E501
            pytest.raises(ValueError, match="no checkpoint files detected"),
        ):
            planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=48 * 3600,
            )

    def test_kill_switch_never_chains(self, tmp_path, monkeypatch):
        # auto_daisy_chain: false explicit -> error even when checkpoints exist.
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400, auto_daisy_chain=False)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        _seed_checkpoint(tmp_path, profile="p", cluster="discovery", run_id="r1")
        with (
            patch(
                "hpc_agent_pro.planning.planner.inspect_cluster",
                return_value=_fake_snapshot(),
            ),  # noqa: E501
            pytest.raises(ValueError, match="exceeds cluster max"),
        ):
            planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=48 * 3600,
            )

    def test_always_chain_override_skips_detection(self, tmp_path, monkeypatch):
        # auto_daisy_chain: true -> chain even with no checkpoint signal.
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400, auto_daisy_chain=True)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
                walltime_user_ask_sec=48 * 3600,
            )
        assert out["daisy_chain_segments"] == 3

    def test_no_decision_when_no_user_ask(self, tmp_path, monkeypatch):
        # walltime_user_ask_sec=None -> never trigger, fields are null.
        cfg = _write_clusters(tmp_path, max_walltime_sec=86400)
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        with patch(
            "hpc_agent_pro.planning.planner.inspect_cluster",
            return_value=_fake_snapshot(),  # noqa: E501
        ):
            out = planner.plan_submit(
                tmp_path,
                profile="p",
                cluster="discovery",
                candidates=["a100"],
                adversarial=False,
            )
        assert out["daisy_chain_segments"] is None
        assert out["daisy_chain_dep_jobids"] is None
