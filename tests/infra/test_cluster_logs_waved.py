"""fetch_task_logs on waved (over-cap) runs — job-local log indices.

A waved submission on an index-bounded backend submits each batch as a
LOCAL ``1-<size>`` array plus a ``TASK_OFFSET`` (``submit_plan``,
``uses_global_array_index=False``), so the scheduler names each job's
log files with the job-LOCAL array index. ``fetch_task_logs`` therefore
takes *job_task_spans* and must probe each job only for the tasks it
covers, with the local index — a global-index probe against the wrong
wave's job silently returns ANOTHER task's stderr (task 5's probe
against wave 1's job reads global task ``offset + 5``'s log).

The probe is a single server-side ``ssh_run`` that frames one
sentinel-delimited section per task (latency-elimination F5); the
:func:`tests._log_fakes.fused_remote_fs` fake evaluates that one script
against a ``path -> content`` map and records the paths it probed.
"""

from __future__ import annotations

from unittest.mock import patch

from hpc_agent.infra.cluster_logs import fetch_task_logs
from tests._log_fakes import fused_remote_fs, severed_remote_fs

# Two waves of 1000 on SLURM: job 100 covers global tasks 0-999, job 200
# covers 1000-1999 (TASK_OFFSET=1000). Both jobs name their logs with the
# LOCAL index, so `ml_100_6.err` is global task 5 and `ml_200_6.err` is
# global task 1005.
_SPANS = {"100": (0, 999), "200": (1000, 1999)}
_FILES = {
    "/exp/logs/ml_100_6.err": "stderr of global task 5",
    "/exp/logs/ml_200_6.err": "stderr of global task 1005",
}


def test_wave0_task_reads_its_own_log_not_the_collision_file():
    fake, probed = fused_remote_fs(_FILES)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200"],
            scheduler="slurm",
            task_ids=[5],
            job_task_spans=_SPANS,
        )
    assert logs == [
        {
            "task_id": 5,
            "path": "/exp/logs/ml_100_6.err",
            "job_id": "100",
            "content": "stderr of global task 5",
        }
    ]
    # Newest-first job 200 was SKIPPED (its span doesn't cover task 5) —
    # probing it with index 6 would have matched global task 1005's log.
    assert probed == ["/exp/logs/ml_100_6.err"]


def test_fold_probes_all_tasks_in_one_ssh_exec():
    """The F×J per-candidate fan-out is now ONE ssh exec regardless of how many
    tasks/job_ids are in play (latency-elimination F5 acceptance)."""
    fake, _probed = fused_remote_fs(_FILES)
    calls: list[str] = []

    def counting(cmd, *, ssh_target, **kw):
        calls.append(cmd)
        return fake(cmd, ssh_target=ssh_target, **kw)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=counting):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200"],
            scheduler="slurm",
            task_ids=[5, 1005],
            job_task_spans=_SPANS,
        )
    # Two tasks, two covering jobs each in scope: still exactly ONE round-trip.
    assert len(calls) == 1
    assert logs[0]["content"] == "stderr of global task 5"
    assert logs[1]["content"] == "stderr of global task 1005"


def test_wave1_task_probes_covering_job_with_local_index():
    fake, probed = fused_remote_fs(_FILES)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200"],
            scheduler="slurm",
            task_ids=[1005],
            job_task_spans=_SPANS,
        )
    # Local id 1005 - 1000 = 5 → ArrayIndex 6 under job 200; job 100 skipped.
    assert logs[0]["path"] == "/exp/logs/ml_200_6.err"
    assert logs[0]["job_id"] == "200"
    assert logs[0]["content"] == "stderr of global task 1005"
    assert probed == ["/exp/logs/ml_200_6.err"]


def test_sge_waved_path_uses_local_index_too():
    files = {"/exp/logs/ml.o200.6": "sge merged stream of task 1005"}
    fake, probed = fused_remote_fs(files)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200"],
            scheduler="sge",
            task_ids=[1005],
            job_task_spans=_SPANS,
        )
    assert logs[0]["path"] == "/exp/logs/ml.o200.6"
    assert probed == ["/exp/logs/ml.o200.6"]


def test_job_without_span_falls_back_to_global_index():
    # Resubmit arrays replay failed ids as GLOBAL array expressions (no
    # TASK_OFFSET), so a job absent from the span map keeps the global
    # probe — newest-first it wins over the original wave's log.
    files = dict(_FILES)
    files["/exp/logs/ml_300_6.err"] = "resubmit attempt of task 5"
    fake, probed = fused_remote_fs(files)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200", "300"],
            scheduler="slurm",
            task_ids=[5],
            job_task_spans=_SPANS,  # no entry for the resubmit job 300
        )
    assert logs[0]["job_id"] == "300"
    assert logs[0]["content"] == "resubmit attempt of task 5"
    assert probed[0] == "/exp/logs/ml_300_6.err"


def test_task_covered_by_no_job_is_missing_without_any_probe():
    fake, probed = fused_remote_fs(_FILES)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100", "200"],
            scheduler="slurm",
            task_ids=[5000],
            job_task_spans=_SPANS,
        )
    # Genuinely missing (no covering job) — NOT an ssh_error envelope, and no
    # ssh at all (nothing to probe anywhere).
    assert logs == [{"task_id": 5000, "missing": True}]
    assert probed == []


def test_no_spans_keeps_prior_global_probe_behavior():
    # Single ≤cap array: local == global, no span map needed.
    files = {"/exp/logs/ml_100_6.err": "task 5"}
    fake, probed = fused_remote_fs(files)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100"],
            scheduler="slurm",
            task_ids=[5],
        )
    assert logs[0]["path"] == "/exp/logs/ml_100_6.err"
    assert probed == ["/exp/logs/ml_100_6.err"]


def test_truncated_stream_reads_severed_per_missing_file():
    """Severed-frame honesty (engineering-principles enforcement-map row 3): a
    channel that clips the stream after task k leaves tasks k+1.. read SEVERED
    (an ``ssh_error`` envelope), never a settled "no log / empty log"."""
    files = {
        "/exp/logs/ml_100_1.err": "stderr of task 0",
        "/exp/logs/ml_100_2.err": "stderr of task 1",
        "/exp/logs/ml_100_3.err": "stderr of task 2",
    }
    # Only the first task's section is framed intact; the stream truncates
    # before tasks 1 and 2 (and before the closing ack).
    fake, _probed = severed_remote_fs(files, intact_tasks=1)
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100"],
            scheduler="slurm",
            task_ids=[0, 1, 2],
        )
    by_tid = {entry["task_id"]: entry for entry in logs}
    # Task 0's intact section is trusted.
    assert by_tid[0]["content"] == "stderr of task 0"
    assert "ssh_error" not in by_tid[0]
    # Tasks 1 and 2 were clipped off the stream → SEVERED, not "missing log".
    for tid in (1, 2):
        assert by_tid[tid]["missing"] is True
        assert by_tid[tid]["ssh_error"], by_tid[tid]
        assert "severed" in by_tid[tid]["ssh_error"]


def test_ssh_transport_failure_marks_every_task_severed():
    """A hard transport failure (rc != 0) on the single fused exec severs every
    task — an unreachable cluster must never masquerade as merely-missing logs."""
    import subprocess

    def boom(cmd, *, ssh_target, **kw):
        return subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="ssh: connect to host h port 22: refused"
        )

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=boom):
        logs = fetch_task_logs(
            ssh_target="u@h",
            remote_path="/exp",
            job_name="ml",
            job_ids=["100"],
            scheduler="slurm",
            task_ids=[0, 1],
        )
    for entry in logs:
        assert entry["missing"] is True
        assert "refused" in entry["ssh_error"]
