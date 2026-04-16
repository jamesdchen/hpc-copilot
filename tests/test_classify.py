"""Tests for hpc_mapreduce.reduce.classify.classify_failure."""

from __future__ import annotations

from hpc_mapreduce.reduce.classify import CATEGORIES, classify_failure


def test_gpu_oom_torch_message():
    log = (
        "Traceback (most recent call last):\n"
        "  File 'train.py', line 42, in <module>\n"
        "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
    )
    # GPU OOM must win over the generic Traceback → code_bug match.
    assert classify_failure(log) == "gpu_oom"


def test_system_oom_killed_process():
    log = "Out of memory: Killed process 12345 (python) total-vm:..."
    assert classify_failure(log) == "system_oom"


def test_walltime_slurm_time_limit():
    log = "slurmstepd: error: *** JOB 9 CANCELLED DUE TO TIME LIMIT ***"
    assert classify_failure(log) == "walltime"


def test_walltime_sge_keyword():
    log = "qacct: failed 37  : qmaster enforced h_rt, h_cpu, or h_vmem limit: walltime"
    assert classify_failure(log) == "walltime"


def test_node_failure_slurm():
    log = "slurmstepd: error: *** NODE FAILURE on node gpu-03 ***"
    assert classify_failure(log) == "node_failure"


def test_node_failure_sge_eqw():
    log = "job-ID  prior   name       user         state\n  42  0.55 train  alice       Eqw"
    assert classify_failure(log) == "node_failure"


def test_queue_stall():
    log = "detected queue_stall across 3 checks"
    assert classify_failure(log) == "queue_stall"


def test_code_bug_plain_traceback():
    log = (
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 1\n"
        "KeyError: 'missing'\n"
    )
    assert classify_failure(log) == "code_bug"


def test_unknown_empty_and_junk():
    assert classify_failure("") == "unknown"
    assert classify_failure("successfully wrote 3 rows\n") == "unknown"


def test_all_returned_labels_are_valid():
    samples = [
        "CUDA out of memory",
        "MemoryError: ",
        "DUE TO TIME LIMIT",
        "NODE_FAIL",
        "queue_stall",
        "Traceback (most recent call last):",
        "",
    ]
    for s in samples:
        assert classify_failure(s) in CATEGORIES
