"""Tests for claude_hpc.orchestrator.state.failure_signatures.classify."""

from __future__ import annotations

from claude_hpc.orchestrator.state.failure_signatures import CATALOG, classify


def test_catalog_size() -> None:
    """The catalog covers the 10 documented failure modes (segv was
    removed when the SEGV blacklist feature was deleted; preempted was
    added when dispatch.py learned to trap SIGTERM)."""
    assert len(CATALOG) == 10


def test_preempted_matches_exit_130() -> None:
    """Cluster-side dispatch.py exits 130 after trapping SIGTERM —
    classify() must surface this as ``preempted`` so the harness can
    resubmit cleanly without escalating to the user."""
    out = classify("[claude-hpc] SIGTERM received; cluster preemption imminent\n", 130)
    assert out["error_class"] == "preempted"
    assert out["suggested_fix"] == {"action": "resubmit-preempted"}


def test_preempted_matches_exit_130_alone() -> None:
    """Even without the dispatch.py stderr line (e.g. log clipped),
    exit code 130 alone is enough to classify as preempted because the
    catalog entry runs at priority>=90."""
    out = classify("", 130)
    assert out["error_class"] == "preempted"


def test_gpu_oom_matches_cuda_pattern() -> None:
    out = classify("CUDA out of memory: tried to allocate 2GB", None)
    assert out["error_class"] == "gpu_oom"
    assert out["suggested_fix"] == {"action": "increase-mem-per-gpu", "factor": 1.5}


def test_system_oom_matches() -> None:
    out = classify("oom-killer killed pid 1234", 137)
    assert out["error_class"] == "system_oom"
    assert out["suggested_fix"] == {"action": "increase-mem", "factor": 1.5}


def test_walltime_matches() -> None:
    out = classify("DUE TO TIME LIMIT", 271)
    assert out["error_class"] == "walltime"
    assert out["suggested_fix"] == {"action": "increase-walltime", "factor": 1.5}


def test_segv_falls_through() -> None:
    """SEGV entry was removed from the catalog when the SEGV blacklist
    feature was deleted. A bare 'Segmentation fault' line now falls
    through to python_traceback or unknown — classify() must not return
    the deleted 'segv' error_class."""
    out = classify("Segmentation fault (core dumped)", 139)
    assert out["error_class"] != "segv"


def test_node_failure_matches() -> None:
    out = classify("NODE_FAIL on node foo", None)
    assert out["error_class"] == "node_failure"


def test_file_not_found_matches() -> None:
    out = classify("FileNotFoundError: [Errno 2]", 2)
    assert out["error_class"] == "file_not_found"


def test_import_error_matches() -> None:
    out = classify("ModuleNotFoundError: numpy", 1)
    assert out["error_class"] == "import_error"


def test_permission_denied_matches() -> None:
    out = classify("PermissionError: [Errno 13]", 13)
    assert out["error_class"] == "permission_denied"


def test_disk_full_matches() -> None:
    out = classify("No space left on device", 28)
    assert out["error_class"] == "disk_full"


def test_python_traceback_fallback() -> None:
    out = classify("Traceback (most recent call last):\n  File 'foo.py', line 1, in <module>", 1)
    assert out["error_class"] == "python_traceback"


def test_unknown_fallback() -> None:
    out = classify("some unrecognized error", None)
    assert out["error_class"] == "unknown"
    assert out["suggested_fix"] == {"action": "user-debug"}


def test_priority_high_beats_low() -> None:
    """When stderr matches both a high- and low-priority pattern, the
    high-priority one wins (gpu_oom over python_traceback)."""
    stderr = (
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 1, in <module>\n"
        "RuntimeError: CUDA out of memory: tried to allocate 2GB"
    )
    out = classify(stderr, 1)
    assert out["error_class"] == "gpu_oom"


def test_exit_code_alone_walltime() -> None:
    """Exit 271 alone should still pick walltime (priority >= 90)."""
    out = classify("", 271)
    assert out["error_class"] == "walltime"


def test_exit_code_alone_low_priority_does_not_match() -> None:
    """Exit 1 alone should NOT pick python_traceback (priority < 90)."""
    out = classify("", 1)
    assert out["error_class"] == "unknown"


def test_classify_returns_a_fresh_dict() -> None:
    """suggested_fix must be a copy, not a reference to CATALOG state."""
    a = classify("CUDA out of memory", None)["suggested_fix"]
    a["action"] = "mutated"
    b = classify("CUDA out of memory", None)["suggested_fix"]
    assert b["action"] == "increase-mem-per-gpu"
