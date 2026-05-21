"""Tests for :func:`hpc_agent_pro.planning.checkpoint_detect.detect_checkpointing`.

The detector gates auto-daisy-chain. A false positive silently wastes
compute (chained job whose stage-1 doesn't checkpoint dies on
preemption and stage-2 starts from scratch); a false negative just
makes the user opt in manually. So every test pins the conservative-
fail-closed behaviour: missing dirs, malformed sidecars, no past runs
all yield ``False``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent_pro.planning.checkpoint_detect import detect_checkpointing

if TYPE_CHECKING:
    from pathlib import Path


def _write_sidecar(
    exp: Path,
    *,
    run_id: str,
    profile: str,
    cluster: str,
    result_dir_template: str,
) -> None:
    """Write a minimal v2 sidecar JSON for *run_id*."""
    runs = exp / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "sidecar_schema_version": 2,
                "run_id": run_id,
                "cmd_sha": "f" * 64,
                "hpc_agent_version": "0.0.1",
                "submitted_at": "2026-01-01T00:00:00+00:00",
                "executor": "exec.py",
                "result_dir_template": result_dir_template,
                "task_count": 1,
                "tasks_py_sha": "0" * 64,
                "profile": profile,
                "cluster": cluster,
            }
        )
    )


class TestDetectCheckpointing:
    def test_no_runs_dir_returns_false(self, tmp_path):
        # No .hpc/runs/ at all — nothing we could possibly know.
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_empty_runs_dir_returns_false(self, tmp_path):
        (tmp_path / ".hpc" / "runs").mkdir(parents=True)
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_past_run_with_checkpoint_pt_returns_true(self, tmp_path):
        run_root = tmp_path / "scratch" / "run_1"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "checkpoint.pt").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r1",
            profile="p",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is True

    def test_past_run_with_model_joblib_returns_true(self, tmp_path):
        run_root = tmp_path / "scratch" / "run_2"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "model.joblib").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r2",
            profile="p",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is True

    def test_past_run_with_no_matching_files_returns_false(self, tmp_path):
        run_root = tmp_path / "scratch" / "run_3"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "metrics.json").write_text("{}")
        (run_root / "task_0" / "log.txt").write_text("hi")
        _write_sidecar(
            tmp_path,
            run_id="r3",
            profile="p",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_errored_sidecar_does_not_crash(self, tmp_path):
        runs = tmp_path / ".hpc" / "runs"
        runs.mkdir(parents=True)
        (runs / "bad.json").write_text("{not json")
        # Errored sidecar is silently skipped; result is False (no other
        # signal). The detector must NOT raise.
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_multiple_past_runs_one_with_checkpoints(self, tmp_path):
        # Run 1: no checkpoints. Run 2: has checkpoints. Either order
        # should return True — first match short-circuits.
        run1 = tmp_path / "scratch" / "run_a"
        (run1 / "task_0").mkdir(parents=True)
        (run1 / "task_0" / "log.txt").write_text("nothing")
        _write_sidecar(
            tmp_path,
            run_id="r_a",
            profile="p",
            cluster="c",
            result_dir_template=str(run1 / "task_{task_id}"),
        )
        run2 = tmp_path / "scratch" / "run_b"
        (run2 / "task_0").mkdir(parents=True)
        (run2 / "task_0" / "epoch_5.pt").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r_b",
            profile="p",
            cluster="c",
            result_dir_template=str(run2 / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is True

    def test_other_profile_runs_ignored(self, tmp_path):
        # Past runs of a different profile shouldn't leak signal into
        # the queried (profile, cluster).
        run_root = tmp_path / "scratch" / "run_other"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "checkpoint.pt").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r_other",
            profile="OTHER",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_other_cluster_runs_ignored(self, tmp_path):
        run_root = tmp_path / "scratch" / "run_other_c"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "checkpoint.pt").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r_other_c",
            profile="p",
            cluster="OTHER",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is False

    def test_case_insensitive_match(self, tmp_path):
        # CHECKPOINT.PT (uppercase) still counts.
        run_root = tmp_path / "scratch" / "run_case"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "CHECKPOINT.PT").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r_case",
            profile="p",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is True

    def test_state_pkl_match(self, tmp_path):
        run_root = tmp_path / "scratch" / "run_state"
        (run_root / "task_0").mkdir(parents=True)
        (run_root / "task_0" / "state_optimizer.pkl").write_bytes(b"fake")
        _write_sidecar(
            tmp_path,
            run_id="r_state",
            profile="p",
            cluster="c",
            result_dir_template=str(run_root / "task_{task_id}"),
        )
        assert detect_checkpointing(tmp_path, profile="p", cluster="c") is True
