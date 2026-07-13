"""fetch_task_logs on waved (over-cap) runs — job-local log indices.

A waved submission on an index-bounded backend submits each batch as a
LOCAL ``1-<size>`` array plus a ``TASK_OFFSET`` (``submit_plan``,
``uses_global_array_index=False``), so the scheduler names each job's
log files with the job-LOCAL array index. ``fetch_task_logs`` therefore
takes *job_task_spans* and must probe each job only for the tasks it
covers, with the local index — a global-index probe against the wrong
wave's job silently returns ANOTHER task's stderr (task 5's probe
against wave 1's job reads global task ``offset + 5``'s log).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from hpc_agent.infra.cluster_logs import fetch_task_logs


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _fake_remote_fs(files: dict[str, str]):
    """A fake ``remote.ssh_run`` that answers the ``[ -f <path> ]`` probe
    from a path→content map, mirroring the real script's FOUND/MISSING
    protocol. Records every probed path."""
    probed: list[str] = []

    def fake_ssh_run(cmd: str, *, ssh_target: str, **_kw):
        # The probe script quotes the path: extract it from `[ -f <q> ]`.
        path = cmd.split("[ -f ", 1)[1].split(" ]", 1)[0].strip("'\"")
        probed.append(path)
        if path in files:
            return _completed(stdout=f"FOUND\n{files[path]}\n")
        return _completed(stdout="MISSING\n")

    return fake_ssh_run, probed


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
    fake, probed = _fake_remote_fs(_FILES)
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
            "content": "stderr of global task 5\n",
        }
    ]
    # Newest-first job 200 was SKIPPED (its span doesn't cover task 5) —
    # probing it with index 6 would have matched global task 1005's log.
    assert probed == ["/exp/logs/ml_100_6.err"]


def test_wave1_task_probes_covering_job_with_local_index():
    fake, probed = _fake_remote_fs(_FILES)
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
    assert logs[0]["content"] == "stderr of global task 1005\n"
    assert probed == ["/exp/logs/ml_200_6.err"]


def test_sge_waved_path_uses_local_index_too():
    files = {"/exp/logs/ml.o200.6": "sge merged stream of task 1005"}
    fake, probed = _fake_remote_fs(files)
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
    fake, probed = _fake_remote_fs(files)
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
    assert logs[0]["content"] == "resubmit attempt of task 5\n"
    assert probed[0] == "/exp/logs/ml_300_6.err"


def test_task_covered_by_no_job_is_missing_without_any_probe():
    fake, probed = _fake_remote_fs(_FILES)
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
    # Genuinely missing (no covering job) — NOT an ssh_error envelope.
    assert logs == [{"task_id": 5000, "missing": True}]
    assert probed == []


def test_no_spans_keeps_prior_global_probe_behavior():
    # Single ≤cap array: local == global, no span map needed.
    files = {"/exp/logs/ml_100_6.err": "task 5"}
    fake, probed = _fake_remote_fs(files)
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
