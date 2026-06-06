"""Tests for hpc_agent.ops.recover.failure_signatures.classify."""

from __future__ import annotations

from hpc_agent.ops.recover.failure_signatures import CATALOG, classify


def test_catalog_size() -> None:
    """The catalog covers the 15 documented failure modes (segv was
    removed when the SEGV blacklist feature was deleted; preempted was
    added when dispatch.py learned to trap SIGTERM; five empirical canary
    signatures — ``uv_not_on_path`` / ``conda_command_not_found`` /
    ``output_file_required`` / ``module_not_found_hpc_agent`` /
    ``undefined_var_expansion`` — were added so the verifier surfaces a
    structured remediation instead of a bare ``dispatcher_failed``)."""
    assert len(CATALOG) == 15


def test_preempted_matches_exit_130() -> None:
    """Cluster-side dispatch.py exits 130 after trapping SIGTERM —
    classify() must surface this as ``preempted`` so the harness can
    resubmit cleanly without escalating to the user."""
    out = classify("[hpc-agent] SIGTERM received; cluster preemption imminent\n", 130)
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


# ── empirical canary signatures (Fix 1) ─────────────────────────────────────


def test_uv_not_on_path_matches() -> None:
    """The cluster preamble error token when HPC_RUNTIME=uv but uv is missing.

    Matches the verbatim string the preamble emits, not the operator's
    paraphrase — the catalog row is a structural-failure remediation, not a
    spell-check on the user's stderr.
    """
    out = classify("[template] HPC_RUNTIME=uv but 'uv' not on PATH", 2)
    assert out["error_class"] == "uv_not_on_path"
    assert out["suggested_fix"]["action"] == "drop-runtime-uv-or-install"
    assert "drop" in out["suggested_fix"]["hint"].lower()


def test_conda_command_not_found_matches() -> None:
    """Conda activation failed because conda was never sourced — usually a
    bad ``conda_source`` in clusters.yaml."""
    out = classify("conda: command not found", None)
    assert out["error_class"] == "conda_command_not_found"
    assert out["suggested_fix"]["action"] == "fix-cluster-conda-source"
    out2 = classify("bash: conda: command not found", None)
    assert out2["error_class"] == "conda_command_not_found"


def test_output_file_required_matches() -> None:
    """Executor's argparse rejects its invocation because --output-file
    wasn't auto-injected. Points at a register_run / kind misconfig."""
    out = classify(
        "executor.py: error: the following arguments are required: --output-file",
        2,
    )
    assert out["error_class"] == "output_file_required"
    assert out["suggested_fix"]["action"] == "verify-register-run-on-disk"


def test_module_not_found_hpc_agent_matches() -> None:
    """Cluster-side python can't import hpc_agent — wrong env activation."""
    out = classify("ModuleNotFoundError: No module named 'hpc_agent'", 1)
    assert out["error_class"] == "module_not_found_hpc_agent"
    assert out["suggested_fix"]["action"] == "fix-cluster-env-activation"


def test_module_not_found_hpc_agent_beats_generic_import_error() -> None:
    """The hpc_agent-specific signature wins over the generic import_error
    catalog row — higher priority (85 > 80)."""
    out = classify(
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'hpc_agent'\n",
        1,
    )
    assert out["error_class"] == "module_not_found_hpc_agent"


def test_undefined_var_expansion_matches() -> None:
    """An executor flag expected a value but got an empty string from
    ``--samples $SAMPLES`` when SAMPLES was unexported."""
    out = classify("executor.py: error: argument --samples: expected one argument", 2)
    assert out["error_class"] == "undefined_var_expansion"
    assert out["suggested_fix"]["action"] == "fix-empty-env-var-in-executor"


def test_uv_not_on_path_beats_python_traceback() -> None:
    """When the stderr carries both a uv structural marker and an
    incidental Traceback, the structural marker wins (priority 95 > 10)."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "x.py", ...\n'
        "[template] HPC_RUNTIME=uv but 'uv' not on PATH"
    )
    out = classify(stderr, 2)
    assert out["error_class"] == "uv_not_on_path"
