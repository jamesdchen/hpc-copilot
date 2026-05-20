"""Tests for hpc_agent.mapreduce.reduce.classify.classify_failure."""

from __future__ import annotations

from hpc_agent.mapreduce.reduce.classify import CATEGORIES, classify_failure
from hpc_agent.runner.failure_signatures import classify as classify_signature


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
    log = "Traceback (most recent call last):\n  File 'x.py', line 1\nKeyError: 'missing'\n"
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


# ---------------------------------------------------------------------------
# Cross-validation invariant: classify_failure delegates the resource-error
# categories (gpu_oom, system_oom, walltime, node_failure) to
# failure_signatures.classify().  Any stderr that the catalog tags with one of
# those error_class values MUST surface as the same category here -- otherwise
# the two pattern tables have drifted apart and the dedup is leaking.
# ---------------------------------------------------------------------------


def test_dedup_invariant_resource_categories_agree() -> None:
    """For every failure_signatures error_class that maps directly to a
    classify_failure category, a representative stderr sample must produce
    the same label through both code paths.
    """
    cases = [
        ("CUDA out of memory: tried to allocate 2GB", "gpu_oom"),
        ("oom-killer killed pid 1234", "system_oom"),
        ("slurmstepd: error: *** JOB 9 CANCELLED DUE TO TIME LIMIT ***", "walltime"),
        ("NODE_FAIL on node foo", "node_failure"),
    ]
    for stderr, expected in cases:
        sig = classify_signature(stderr, None)
        assert sig["error_class"] == expected, (stderr, sig)
        assert classify_failure(stderr) == expected, (stderr, sig)


def test_dedup_invariant_python_traceback_remaps_to_code_bug() -> None:
    """The catalog's ``python_traceback`` error_class must surface as
    ``code_bug`` through classify_failure -- the older /status vocabulary."""
    stderr = "Traceback (most recent call last):\n  File 'x.py', line 1\nKeyError\n"
    assert classify_signature(stderr, None)["error_class"] == "python_traceback"
    assert classify_failure(stderr) == "code_bug"


def test_dedup_invariant_segv_stays_local() -> None:
    """SEGV is intentionally absent from the catalog (test_segv_falls_through
    pins this).  classify_failure keeps its own SEGV regex and must still
    surface ``segv`` for a bare segfault line."""
    stderr = "Segmentation fault (core dumped)"
    # Catalog does NOT classify this as segv (or any specific class).
    assert classify_signature(stderr, None)["error_class"] != "segv"
    # But classify_failure does, via the local _SEGV regex.
    assert classify_failure(stderr) == "segv"
